@echo off
rem WON-ISA demo server (http://localhost:8000). LLM mode by default; "windows_llm_demo.bat scripted" for the scripted mode.
rem Run windows_llm_test.bat once first: it installs Ollama, the model and the venv.
setlocal EnableExtensions
cd /d "%~dp0"
set "VPY=%LOCALAPPDATA%\won-isa\venv\Scripts\python.exe"
if not exist "%VPY%" (
  echo venv not found. Run windows_llm_test.bat first.
  pause
  exit /b 1
)
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
if not exist "%~dp0logs" mkdir "%~dp0logs"
if /i "%~1"=="scripted" (
  set "LLM_MODE=scripted"
  echo Scripted mode: no model needed.
  goto :serve
)
curl -s -o nul http://localhost:11434/api/version && goto :serve
set "OLLAMA=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
if not exist "%OLLAMA%" set "OLLAMA=%ProgramFiles%\Ollama\ollama.exe"
set "OLLAMA_CONTEXT_LENGTH=16384"
set "OLLAMA_FLASH_ATTENTION=1"
set "OLLAMA_KV_CACHE_TYPE=q8_0"
start "ollama serve - keep this window open" /min cmd /c ""%OLLAMA%" serve > "%~dp0logs\ollama_serve_log.txt" 2>&1"
timeout /t 6 /nobreak >nul
:serve
curl -s -o nul http://localhost:8000/api/state && (
  echo Port 8000 is already in use. Close the old demo server window first.
  pause
  exit /b 1
)
echo Demo server: http://localhost:8000   (close this window to stop)
echo Server output: logs\demo_server_log.txt
start "" cmd /c "timeout /t 4 /nobreak >nul & start http://localhost:8000"
"%VPY%" -m uvicorn server.app:app --port 8000 > "%~dp0logs\demo_server_log.txt" 2>&1
