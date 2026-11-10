#!/usr/bin/env bash
cd "$(dirname "$0")"
python3 -m pip install -q -r requirements.txt
python3 -m uvicorn server.app:app --port 8000
