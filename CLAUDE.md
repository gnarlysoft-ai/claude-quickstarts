# Claude Quickstarts Development Guide

## Legal

- When changes are made to files that have a copyright notice add them to that subdirectory's CHANGELOG.md file.

## Computer-Use Demo

### Setup & Development

- **Setup environment**: `./setup.sh`
- **Build Docker**: `docker build . -t computer-use-demo:local`
- **Run container**: `./run.sh` (uses configured image if available, falls back to base)
- **Run with base image**: `./run.sh --base`
- **Create .env**: Add `ANTHROPIC_API_KEY=your-key` to `computer-use-demo/.env`

### Configuring the Docker Image

The base image includes the 1Password Firefox extension pre-installed. To create a fully configured image with your Firefox profile and extensions:

1. Build the base image: `docker build . -t computer-use-demo:local`
2. Run with base: `./run.sh --base`
3. Connect via noVNC (http://localhost:6080) and configure Firefox, sign into 1Password, etc.
4. Commit the snapshot: `docker commit computer-use-demo computer-use-demo:configured`

Future runs with `./run.sh` will use the configured image automatically. Note: the `computer-use-demo:configured` image is local-only and not stored in git.

### REST API

- **API server**: Runs on port 8000 inside the container, mapped to `http://localhost:8000`
- **Health check**: `GET /health`
- **Create session**: `POST /sessions` (optional JSON body with model, provider, tool_version, etc.)
- **Send message (sync)**: `POST /sessions/{id}/messages` with `{"message": "..."}`
- **Send message (SSE)**: `POST /sessions/{id}/messages/stream` with `{"message": "..."}`
- **Get messages**: `GET /sessions/{id}/messages?include_images=false`
- **Config**: `GET /config`, `PUT /config/system-prompt`
- **Entrypoint**: `python -m uvicorn computer_use_demo.api:app --host 0.0.0.0 --port 8000`

### Testing & Code Quality

- **Lint**: `ruff check .`
- **Format**: `ruff format .`
- **Typecheck**: `pyright`
- **Run tests**: `pytest`
- **Run single test**: `pytest tests/path_to_test.py::test_name -v`

### Code Style

- **Python**: snake_case for functions/variables, PascalCase for classes
- **Imports**: Use isort with combine-as-imports
- **Error handling**: Use custom ToolError for tool errors
- **Types**: Add type annotations for all parameters and returns
- **Classes**: Use dataclasses and abstract base classes

## Customer Support Agent

### Setup & Development

- **Install dependencies**: `npm install`
- **Run dev server**: `npm run dev` (full UI)
- **UI variants**: `npm run dev:left` (left sidebar), `npm run dev:right` (right sidebar), `npm run dev:chat` (chat only)
- **Lint**: `npm run lint`
- **Build**: `npm run build` (full UI), see package.json for variants

### Code Style

- **TypeScript**: Strict mode with proper interfaces
- **Components**: Function components with React hooks
- **Formatting**: Follow ESLint Next.js configuration
- **UI components**: Use shadcn/ui components library

## Financial Data Analyst

### Setup & Development

- **Install dependencies**: `npm install`
- **Run dev server**: `npm run dev`
- **Lint**: `npm run lint`
- **Build**: `npm run build`

### Code Style

- **TypeScript**: Strict mode with proper type definitions
- **Components**: Function components with type annotations
- **Visualization**: Use Recharts library for data visualization
- **State management**: React hooks for state