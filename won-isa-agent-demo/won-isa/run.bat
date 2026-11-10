cd /d "%~dp0"
python -m pip install -q -r requirements.txt
python -m uvicorn server.app:app --port 8000
pause
