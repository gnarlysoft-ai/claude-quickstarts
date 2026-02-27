#!/bin/bash
set -e

./start_all.sh
./novnc_startup.sh

python http_server.py > /tmp/server_logs.txt 2>&1 &

STREAMLIT_SERVER_PORT=8501 python -m streamlit run computer_use_demo/streamlit.py > /tmp/streamlit_stdout.log &

python -m uvicorn computer_use_demo.api:app --host 0.0.0.0 --port 8000 > /tmp/api_stdout.log 2>&1 &

echo "✨ Computer Use Demo is ready!"
echo "➡️  Open http://localhost:8080 in your browser to begin"
echo "➡️  API server: http://localhost:8000"

# Keep the container running
tail -f /dev/null
