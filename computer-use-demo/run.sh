#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Use the configured image if available, otherwise fall back to the base image
IMAGE="computer-use-demo:configured"
if ! docker image inspect "$IMAGE" &>/dev/null; then
    echo "Configured image not found, falling back to base image."
    echo "To create a configured image:"
    echo "  1. Build: docker build . -t computer-use-demo:local"
    echo "  2. Run:   $0 --base"
    echo "  3. Configure Firefox/extensions via VNC at http://localhost:6080"
    echo "  4. Commit: docker commit computer-use-demo computer-use-demo:configured"
    IMAGE="computer-use-demo:local"
fi

# Allow forcing the base image with --base flag
if [[ "$1" == "--base" ]]; then
    IMAGE="computer-use-demo:local"
fi

if [ ! -f .env ]; then
    echo "Error: .env file not found. Create one with ANTHROPIC_API_KEY=your-key"
    exit 1
fi

# Stop and remove existing container if running
docker stop computer-use-demo 2>/dev/null || true
docker rm computer-use-demo 2>/dev/null || true

echo "Starting computer-use-demo with image: $IMAGE"

docker run \
    --env-file .env \
    -v "$SCRIPT_DIR/computer_use_demo:/home/computeruse/computer_use_demo/" \
    -v "$HOME/.anthropic:/home/computeruse/.anthropic" \
    -p 5901:5900 \
    -p 8501:8501 \
    -p 6080:6080 \
    -p 8080:8080 \
    -d --name computer-use-demo \
    "$IMAGE"

echo ""
echo "Computer Use Demo is starting..."
echo "  Streamlit UI: http://localhost:8501"
echo "  noVNC:        http://localhost:6080"
echo "  Web UI:       http://localhost:8080"
echo "  VNC direct:   localhost:5901"
