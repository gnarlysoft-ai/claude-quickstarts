"""
FastAPI server that wraps the computer-use sampling loop, exposing it as a REST API.
"""

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
import uvicorn
from anthropic.types.beta import BetaContentBlockParam, BetaMessageParam
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from computer_use_demo.loop import APIProvider, sampling_loop
from computer_use_demo.tools import ToolResult, ToolVersion

logger = logging.getLogger("computer_use_api")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# ---------------------------------------------------------------------------
# Model configuration (mirrored from streamlit.py to avoid streamlit import)
# ---------------------------------------------------------------------------


@dataclass(kw_only=True, frozen=True)
class ModelConfig:
    tool_version: ToolVersion
    max_output_tokens: int
    default_output_tokens: int
    has_thinking: bool = False


_CLAUDE_4 = ModelConfig(
    tool_version="computer_use_20250429",
    max_output_tokens=64_000,
    default_output_tokens=1024 * 16,
    has_thinking=True,
)

_CLAUDE_4_5 = ModelConfig(
    tool_version="computer_use_20250124",
    max_output_tokens=128_000,
    default_output_tokens=1024 * 16,
    has_thinking=True,
)

_CLAUDE_4_WITH_ZOOMABLE_TOOL = ModelConfig(
    tool_version="computer_use_20251124",
    max_output_tokens=64_000,
    default_output_tokens=1024 * 16,
    has_thinking=True,
)

_HAIKU_4_5 = ModelConfig(
    tool_version="computer_use_20250124",
    max_output_tokens=1024 * 8,
    default_output_tokens=1024 * 4,
    has_thinking=False,
)

MODEL_TO_MODEL_CONF: dict[str, ModelConfig] = {
    "claude-opus-4-1-20250805": _CLAUDE_4,
    "claude-sonnet-4-20250514": _CLAUDE_4,
    "claude-opus-4-20250514": _CLAUDE_4,
    "claude-sonnet-4-5-20250929": _CLAUDE_4_5,
    "anthropic.claude-sonnet-4-5-20250929-v1:0": _CLAUDE_4_5,
    "claude-sonnet-4-5@20250929": _CLAUDE_4_5,
    "claude-haiku-4-5-20251001": _HAIKU_4_5,
    "anthropic.claude-haiku-4-5-20251001-v1:0": _HAIKU_4_5,
    "claude-haiku-4-5@20251001": _HAIKU_4_5,
    "claude-opus-4-5-20251101": _CLAUDE_4_WITH_ZOOMABLE_TOOL,
}

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "3600"))
MAX_SESSIONS = int(os.getenv("MAX_SESSIONS", "100"))
CLEANUP_INTERVAL_SECONDS = 60


class ServerConfig:
    """Server configuration read from environment variables."""

    def __init__(self) -> None:
        self.system_prompt_suffix: str = os.getenv("SYSTEM_PROMPT_SUFFIX", "")
        self.default_model: str = os.getenv(
            "DEFAULT_MODEL", "claude-sonnet-4-5-20250929"
        )
        self.api_provider: APIProvider = APIProvider(
            os.getenv("API_PROVIDER", "anthropic")
        )
        self.api_timeout: int = int(os.getenv("API_TIMEOUT", "300"))
        self.api_key: str = os.getenv("ANTHROPIC_API_KEY", "")


server_config = ServerConfig()

# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------


@dataclass
class Session:
    id: str
    messages: list[BetaMessageParam]
    config: dict[str, Any]
    created_at: str
    last_accessed_at: float = field(
        default_factory=lambda: datetime.now(timezone.utc).timestamp()
    )
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def touch(self) -> None:
        """Update last_accessed_at to current time."""
        self.last_accessed_at = datetime.now(timezone.utc).timestamp()


sessions: dict[str, Session] = {}

# ---------------------------------------------------------------------------
# Pydantic request / response schemas
# ---------------------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    system_prompt: str | None = None
    model: str | None = None
    provider: str | None = None
    tool_version: ToolVersion | None = None
    only_n_most_recent_images: int | None = None
    max_tokens: int | None = None
    thinking_budget: int | None = None
    token_efficient_tools_beta: bool | None = None


class SessionResponse(BaseModel):
    id: str
    config: dict[str, Any]
    created_at: str
    message_count: int


class SendMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=100_000)
    timeout: int | None = None


class MessageResponse(BaseModel):
    new_messages: list[dict[str, Any]]
    final_text: str | None = None


class ConfigUpdateRequest(BaseModel):
    system_prompt_suffix: str


class ConfigResponse(BaseModel):
    system_prompt_suffix: str
    default_model: str
    api_provider: str
    api_timeout: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_model_config(model: str) -> ModelConfig:
    """Return the ModelConfig for a given model string, falling back to _CLAUDE_4."""
    return MODEL_TO_MODEL_CONF.get(model, _CLAUDE_4)


def _get_session(session_id: str) -> Session:
    """Retrieve a session or raise 404."""
    session = sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


async def _acquire_session_lock(session: Session) -> None:
    """Try to acquire the session lock without waiting. Raises 409 if busy."""
    try:
        await asyncio.wait_for(session.lock.acquire(), timeout=0.01)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=409, detail="Session is busy processing another message"
        ) from None


def _build_loop_kwargs(session: Session) -> dict[str, Any]:
    """Build the keyword arguments for sampling_loop from a session's config."""
    cfg = session.config
    model: str = cfg["model"]
    model_conf = _resolve_model_config(model)

    tool_version: ToolVersion = cfg.get("tool_version") or model_conf.tool_version
    max_tokens: int = cfg.get("max_tokens") or model_conf.default_output_tokens
    thinking_budget: int | None = cfg.get("thinking_budget")
    if thinking_budget is None and model_conf.has_thinking:
        thinking_budget = int(model_conf.default_output_tokens / 2)

    return {
        "model": model,
        "provider": APIProvider(cfg.get("provider") or server_config.api_provider),
        "system_prompt_suffix": cfg.get("system_prompt")
        or server_config.system_prompt_suffix,
        "messages": session.messages,
        "api_key": cfg.get("api_key") or server_config.api_key,
        "only_n_most_recent_images": cfg.get("only_n_most_recent_images"),
        "max_tokens": max_tokens,
        "tool_version": tool_version,
        "thinking_budget": thinking_budget if model_conf.has_thinking else None,
        "token_efficient_tools_beta": cfg.get("token_efficient_tools_beta", False),
    }


def _strip_images_from_messages(
    messages: list[BetaMessageParam],
) -> list[dict[str, Any]]:
    """Return a JSON-safe copy of messages with base64 image data removed."""
    cleaned: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            new_blocks: list[Any] = []
            for block in content:
                if isinstance(block, dict):
                    block = dict(block)
                    if block.get("type") == "tool_result" and isinstance(
                        block.get("content"), list
                    ):
                        block["content"] = [
                            b
                            for b in block["content"]
                            if not (isinstance(b, dict) and b.get("type") == "image")
                        ]
                    new_blocks.append(block)
                else:
                    new_blocks.append(block)
            cleaned.append({"role": msg["role"], "content": new_blocks})
        else:
            cleaned.append(dict(msg))
    return cleaned


# ---------------------------------------------------------------------------
# Session cleanup background task
# ---------------------------------------------------------------------------


async def _session_cleanup_loop() -> None:
    """Periodically remove expired sessions that are not currently locked."""
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        now = datetime.now(timezone.utc).timestamp()
        expired_ids = [
            sid
            for sid, session in sessions.items()
            if (now - session.last_accessed_at) > SESSION_TTL_SECONDS
            and not session.lock.locked()
        ]
        for sid in expired_ids:
            sessions.pop(sid, None)


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_session_cleanup_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Computer Use API", version="0.1.0", lifespan=lifespan)


# -- Health ------------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# -- Config ------------------------------------------------------------------


@app.get("/config", response_model=ConfigResponse)
async def get_config() -> ConfigResponse:
    return ConfigResponse(
        system_prompt_suffix=server_config.system_prompt_suffix,
        default_model=server_config.default_model,
        api_provider=server_config.api_provider.value,
        api_timeout=server_config.api_timeout,
    )


@app.put("/config/system-prompt")
async def update_system_prompt(body: ConfigUpdateRequest) -> dict[str, str]:
    server_config.system_prompt_suffix = body.system_prompt_suffix
    return {"system_prompt_suffix": server_config.system_prompt_suffix}


# -- Sessions ----------------------------------------------------------------


@app.post("/sessions", response_model=SessionResponse, status_code=201)
async def create_session(body: CreateSessionRequest | None = None) -> SessionResponse:
    if len(sessions) >= MAX_SESSIONS:
        raise HTTPException(
            status_code=429, detail="Maximum number of sessions reached"
        )
    body = body or CreateSessionRequest()
    session_id = str(uuid.uuid4())
    config: dict[str, Any] = {
        "model": body.model or server_config.default_model,
        "provider": body.provider or server_config.api_provider.value,
        "system_prompt": body.system_prompt,
        "tool_version": body.tool_version,
        "only_n_most_recent_images": body.only_n_most_recent_images,
        "max_tokens": body.max_tokens,
        "thinking_budget": body.thinking_budget,
        "token_efficient_tools_beta": body.token_efficient_tools_beta or False,
    }
    session = Session(
        id=session_id,
        messages=[],
        config=config,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    sessions[session_id] = session
    logger.info("Session created: %s (model=%s)", session_id[:8], config["model"])
    return SessionResponse(
        id=session.id,
        config=session.config,
        created_at=session.created_at,
        message_count=0,
    )


@app.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_session(session_id: str) -> SessionResponse:
    session = _get_session(session_id)
    session.touch()
    return SessionResponse(
        id=session.id,
        config=session.config,
        created_at=session.created_at,
        message_count=len(session.messages),
    )


@app.delete("/sessions/{session_id}", status_code=204)
async def delete_session(session_id: str) -> None:
    session = _get_session(session_id)
    if session.lock.locked():
        raise HTTPException(
            status_code=409, detail="Session is busy processing a message"
        )
    del sessions[session_id]
    logger.info("Session deleted: %s", session_id[:8])


# -- Messages (sync) --------------------------------------------------------


@app.post("/sessions/{session_id}/messages", response_model=MessageResponse)
async def send_message(session_id: str, body: SendMessageRequest) -> MessageResponse:
    session = _get_session(session_id)
    await _acquire_session_lock(session)
    try:
        session.touch()
        logger.info("[%s] Prompt: %s", session_id[:8], body.message)

        # Append user message
        session.messages.append(
            {"role": "user", "content": [{"type": "text", "text": body.message}]}
        )
        snapshot_len = len(session.messages)

        timeout = body.timeout or server_config.api_timeout
        kwargs = _build_loop_kwargs(session)

        # Capture API errors instead of re-raising inside the callback
        api_errors: list[Exception] = []

        def output_callback(block: BetaContentBlockParam) -> None:
            if isinstance(block, dict):
                btype = block.get("type", "unknown")
                if btype == "text":
                    logger.info("[%s] Agent: %s", session_id[:8], block["text"][:200])
                elif btype == "thinking":
                    logger.info(
                        "[%s] Thinking: %s",
                        session_id[:8],
                        block.get("thinking", "")[:200],
                    )
                elif btype == "tool_use":
                    logger.info(
                        "[%s] Tool call: %s(%s)",
                        session_id[:8],
                        block.get("name"),
                        json.dumps(block.get("input", {}), default=str)[:200],
                    )

        def tool_output_callback(result: ToolResult, tool_id: str) -> None:
            if result.error:
                logger.warning("[%s] Tool error: %s", session_id[:8], result.error[:200])
            elif result.output:
                logger.info("[%s] Tool output: %s", session_id[:8], result.output[:200])
            elif result.base64_image:
                logger.info("[%s] Tool output: <screenshot>", session_id[:8])

        def api_response_callback(
            request: httpx.Request,
            response: httpx.Response | object | None,
            error: Exception | None,
        ) -> None:
            if error:
                logger.error("[%s] API error: %s", session_id[:8], error)
                api_errors.append(error)

        try:
            session.messages = await asyncio.wait_for(
                sampling_loop(
                    **kwargs,
                    output_callback=output_callback,
                    tool_output_callback=tool_output_callback,
                    api_response_callback=api_response_callback,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="Request timed out") from None
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e)) from e

        # If the loop returned normally but an API error was captured, report it
        if api_errors:
            raise HTTPException(status_code=502, detail=str(api_errors[-1]))

        # Determine new messages since the snapshot
        new_messages = list(session.messages[snapshot_len:])

        # Extract final text from the last assistant message
        final_text: str | None = None
        for msg in reversed(new_messages):
            if msg.get("role") == "assistant":
                content = msg.get("content")
                if isinstance(content, list):
                    for block in reversed(content):
                        if isinstance(block, dict) and block.get("type") == "text":
                            final_text = block["text"]
                            break
                if final_text:
                    break

        logger.info(
            "[%s] Complete: %d new messages", session_id[:8], len(new_messages)
        )
        return MessageResponse(
            new_messages=[dict(m) for m in new_messages],
            final_text=final_text,
        )
    finally:
        session.lock.release()


# -- Messages (SSE stream) --------------------------------------------------


@app.post("/sessions/{session_id}/messages/stream")
async def stream_message(
    session_id: str,
    body: SendMessageRequest,
    include_screenshots: bool = Query(default=False),
) -> EventSourceResponse:
    session = _get_session(session_id)
    await _acquire_session_lock(session)
    # Lock is acquired; the run_loop coroutine is responsible for releasing it.

    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    timeout = body.timeout or server_config.api_timeout

    def output_callback(block: BetaContentBlockParam) -> None:
        if isinstance(block, dict):
            event_type = block.get("type", "unknown")
            data: dict[str, Any] = dict(block)
            if event_type == "text":
                logger.info("[%s] Agent: %s", session_id[:8], block["text"][:200])
            elif event_type == "thinking":
                logger.info(
                    "[%s] Thinking: %s",
                    session_id[:8],
                    block.get("thinking", "")[:200],
                )
            elif event_type == "tool_use":
                logger.info(
                    "[%s] Tool call: %s(%s)",
                    session_id[:8],
                    block.get("name"),
                    json.dumps(block.get("input", {}), default=str)[:200],
                )
            if not include_screenshots and event_type == "tool_result":
                if isinstance(data.get("content"), list):
                    data["content"] = [
                        b
                        for b in data["content"]
                        if not (isinstance(b, dict) and b.get("type") == "image")
                    ]
            queue.put_nowait({"event": event_type, "data": data})

    def tool_output_callback(result: ToolResult, tool_id: str) -> None:
        data: dict[str, Any] = {
            "tool_use_id": tool_id,
            "output": result.output,
            "error": result.error,
        }
        if result.error:
            logger.warning("[%s] Tool error: %s", session_id[:8], result.error[:200])
        elif result.output:
            logger.info("[%s] Tool output: %s", session_id[:8], result.output[:200])
        elif result.base64_image:
            logger.info("[%s] Tool output: <screenshot>", session_id[:8])
        if include_screenshots and result.base64_image:
            data["base64_image"] = result.base64_image
        queue.put_nowait({"event": "tool_result", "data": data})

    def api_response_callback(
        request: httpx.Request,
        response: httpx.Response | object | None,
        error: Exception | None,
    ) -> None:
        if error:
            logger.error("[%s] API error: %s", session_id[:8], error)
            queue.put_nowait({"event": "error", "data": {"error": str(error)}})

    async def run_loop() -> None:
        try:
            session.touch()
            logger.info("[%s] SSE prompt: %s", session_id[:8], body.message)
            session.messages.append(
                {"role": "user", "content": [{"type": "text", "text": body.message}]}
            )
            session.messages = await asyncio.wait_for(
                sampling_loop(
                    **_build_loop_kwargs(session),
                    output_callback=output_callback,
                    tool_output_callback=tool_output_callback,
                    api_response_callback=api_response_callback,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            queue.put_nowait({"event": "error", "data": {"error": "Request timed out"}})
        except Exception as e:
            queue.put_nowait({"event": "error", "data": {"error": str(e)}})
        finally:
            session.lock.release()
            queue.put_nowait({"event": "done", "data": {}})

    async def event_generator():
        task = asyncio.create_task(run_loop())
        try:
            while True:
                item = await queue.get()
                yield {
                    "event": item["event"],
                    "data": json.dumps(item["data"], default=str),
                }
                if item["event"] == "done":
                    break
        finally:
            if not task.done():
                task.cancel()

    return EventSourceResponse(event_generator())


# -- Message history ---------------------------------------------------------


@app.get("/sessions/{session_id}/messages")
async def get_messages(
    session_id: str,
    include_images: bool = Query(default=False),
) -> list[dict[str, Any]]:
    session = _get_session(session_id)
    session.touch()
    if include_images:
        return [dict(m) for m in session.messages]
    return _strip_images_from_messages(session.messages)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(
        "computer_use_demo.api:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
    )
