@echo off
rem WON-ISA: Ollama install + model + LLM smoke test + LLM-mode video + evaluation (one double-click)
rem Usage: double-click, or run "windows_llm_test.bat qwen3:14b" to choose a model.
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
set "LOG=%~dp0llm_test_log.txt"
echo ==== WON-ISA LLM test %date% %time% ==== > "%LOG%"
echo Log file: %LOG%

rem ---- 1) GPU check and model choice
set "MODEL=qwen3:8b"
where nvidia-smi >nul 2>&1
if errorlevel 1 (
  set "MODEL=qwen3:4b"
  echo [1] No NVIDIA GPU found - using qwen3:4b for CPU >> "%LOG%"
) else (
  nvidia-smi --query-gpu=name,memory.total --format=csv >> "%LOG%" 2>&1
  echo [1] NVIDIA GPU found - using qwen3:8b >> "%LOG%"
)
if not "%~1"=="" set "MODEL=%~1"
echo [1] MODEL=%MODEL% >> "%LOG%"
echo [1] MODEL=%MODEL%

rem ---- 2) Ollama install (per-user): existing install -> winget -> official installer from ollama.com
set "OLLAMA=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
if not exist "%OLLAMA%" if exist "%ProgramFiles%\Ollama\ollama.exe" set "OLLAMA=%ProgramFiles%\Ollama\ollama.exe"
if not exist "%OLLAMA%" for /f "delims=" %%i in ('where ollama 2^>nul') do if not defined OLLAMA_FOUND set "OLLAMA_FOUND=1" & set "OLLAMA=%%i"
if not exist "%OLLAMA%" (
  where winget >nul 2>&1
  if not errorlevel 1 (
    echo [2] Installing Ollama with winget... >> "%LOG%"
    echo [2] Installing Ollama with winget...
    winget install --id Ollama.Ollama -e --silent --accept-source-agreements --accept-package-agreements >> "%LOG%" 2>&1
  )
)
if not exist "%OLLAMA%" (
  echo [2] winget not available - downloading the official installer from ollama.com >> "%LOG%"
  echo [2] Downloading Ollama installer from ollama.com, about 1GB...
  curl -L --fail -s -S -o "%TEMP%\OllamaSetup.exe" https://ollama.com/download/OllamaSetup.exe >> "%LOG%" 2>&1
  if exist "%TEMP%\OllamaSetup.exe" (
    echo [2] Installing Ollama for this user, silent... >> "%LOG%"
    echo [2] Installing Ollama for this user...
    start "" /wait "%TEMP%\OllamaSetup.exe" /VERYSILENT /NORESTART /SUPPRESSMSGBOXES
    set "OLLAMA=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
  )
)
if not exist "%OLLAMA%" (
  echo [2] FAIL: Ollama not found. Install it from https://ollama.com/download and run this file again. >> "%LOG%"
  goto :fail
)
echo [2] Ollama OK: %OLLAMA% >> "%LOG%"

rem ---- 3) Restart Ollama with a longer context (16384 tokens)
taskkill /IM "ollama app.exe" /F >nul 2>&1
taskkill /IM ollama.exe /F >nul 2>&1
set "OLLAMA_CONTEXT_LENGTH=16384"
rem smaller KV cache so a 8B model + 16k context fits in 8GB VRAM (faster)
set "OLLAMA_FLASH_ATTENTION=1"
set "OLLAMA_KV_CACHE_TYPE=q8_0"
if not exist "%~dp0logs" mkdir "%~dp0logs"
rem Ollama output goes to a file, not to the window: clicking inside a console window freezes any program writing to it
start "ollama serve - keep this window open" /min cmd /c ""%OLLAMA%" serve > "%~dp0logs\ollama_serve_log.txt" 2>&1"
timeout /t 6 /nobreak >nul
curl -s http://localhost:11434/api/version >> "%LOG%" 2>&1
echo. >> "%LOG%"

rem ---- 4) Download the model
echo [4] Downloading %MODEL% (several GB, may take a while)... >> "%LOG%"
echo [4] Downloading %MODEL% (several GB, may take a while)...
"%OLLAMA%" pull %MODEL% > "%~dp0ollama_pull_log.txt" 2>&1
if errorlevel 1 (
  echo [4] FAIL: model download. See ollama_pull_log.txt >> "%LOG%"
  goto :fail
)
echo [4] Model OK >> "%LOG%"

rem ---- 5) Python virtual environment (does not touch your base Python)
rem Each candidate must actually start (import encodings, venv); a broken install is skipped.
set "PYTHONHOME="
set "PYTHONPATH="
set "PY="
call :trypy "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
call :trypy "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
call :trypy "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
call :trypy "%USERPROFILE%\anaconda3\python.exe"
call :trypy "%USERPROFILE%\miniconda3\python.exe"
if "!PY!"=="" for /f "delims=" %%i in ('where python 2^>nul ^| findstr /v /i WindowsApps') do call :trypy "%%i"
if "!PY!"=="" (
  echo [5] No working Python found - installing Python 3.12 for this user only, Anaconda is not touched >> "%LOG%"
  echo [5] Installing Python 3.12 for this user, a few minutes...
  winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements >> "%LOG%" 2>&1
  call :trypy "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)
if "!PY!"=="" (
  echo [5] FAIL: no working Python >> "%LOG%"
  goto :fail
)
echo [5] Python: !PY! >> "%LOG%"
"!PY!" --version >> "%LOG%" 2>&1
set "PY=!PY!"
rem venv lives in an ASCII-only path: Python venvs can fail inside folders with Korean names
set "VENV=%LOCALAPPDATA%\won-isa\venv"
set "PIPUSER="
if not exist "%VENV%\Scripts\python.exe" "!PY!" -m venv "%VENV%" >> "%LOG%" 2>&1
set "VPY=%VENV%\Scripts\python.exe"
"%VPY%" -c "import sys; print('venv ok', sys.version.split()[0])" >> "%LOG%" 2>&1
if errorlevel 1 (
  echo [5] venv failed - falling back to base Python with --user packages >> "%LOG%"
  set "VPY=!PY!"
  set "PIPUSER=--user"
)
echo [5] Installing packages... >> "%LOG%"
echo [5] Installing packages...
"%VPY%" -m pip install -q %PIPUSER% -r requirements.txt >> "%LOG%" 2>&1
if errorlevel 1 (
  echo [5] FAIL: pip install >> "%LOG%"
  goto :fail
)

rem ---- 6) .env (kept if it already exists)
if exist ".env" (
  echo [6] Using existing .env >> "%LOG%"
) else (
  > ".env" echo LLM_MODE=openai
  >> ".env" echo LLM_BASE_URL=http://localhost:11434/v1
  >> ".env" echo LLM_API_KEY=ollama
  >> ".env" echo LLM_MODEL=%MODEL%
  >> ".env" echo LLM_NO_THINK=1
  echo [6] Wrote .env for %MODEL% >> "%LOG%"
)

rem ---- 7) Smoke test: one LLM turn
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
echo [7] Smoke test... >> "%LOG%"
echo [7] Smoke test (first call loads the model, can take a minute)...
"%VPY%" scripts\smoke_test.py >> "%LOG%" 2>&1
if errorlevel 1 (
  echo [7] FAIL: smoke test. See lines above. >> "%LOG%"
  goto :fail
)
echo [7] ollama ps - how much of the model is on the GPU: >> "%LOG%"
"%OLLAMA%" ps >> "%LOG%" 2>&1

rem ---- 8) Evaluation first: it gives the numbers for the slides (21 scenarios + system controls; slowest step)
echo [8] Running evaluation... >> "%LOG%"
echo [8] Running evaluation - slowest step, about 20-40 minutes. Progress is written to eval_log.txt
"%VPY%" -m eval.run_eval > "%~dp0eval_log.txt" 2>&1
if errorlevel 1 (
  echo [8] WARN: evaluation exited with an error. See eval_log.txt >> "%LOG%"
) else (
  echo [8] Evaluation finished. See eval_log.txt and eval\results_openai.json >> "%LOG%"
)

rem ---- 9) LLM-mode video. The recorder starts its own server: no extra window, output in logs\server_*.log, stopped at the end
echo [9] Recording LLM-mode video... >> "%LOG%"
echo [9] Recording LLM-mode video, about 5 minutes...
"%VPY%" -m playwright install chromium >> "%LOG%" 2>&1
if exist "%~dp0won_isa_demo_llm.mp4" (
  if exist "%~dp0won_isa_demo_llm_prev.mp4" ren "%~dp0won_isa_demo_llm_prev.mp4" "won_isa_demo_llm_prev_%RANDOM%.mp4"
  ren "%~dp0won_isa_demo_llm.mp4" "won_isa_demo_llm_prev.mp4"
)
"%VPY%" -m record.record_demo --serve --out "%~dp0won_isa_demo_llm.mp4" >> "%LOG%" 2>&1
if errorlevel 1 echo [9] WARN: recording stopped early. Partial video if any: won_isa_demo_llm_partial.mp4 >> "%LOG%"

echo ==== ALL DONE %date% %time% ==== >> "%LOG%"
echo.
echo ALL DONE. Results: llm_test_log.txt, eval_log.txt, eval\REPORT_openai.md, won_isa_demo_llm.mp4
pause
exit /b 0

:fail
echo ==== STOPPED %date% %time% ==== >> "%LOG%"
echo.
echo STOPPED. Open llm_test_log.txt to see why.
pause
exit /b 1

:trypy
if not "!PY!"=="" exit /b 0
if not exist "%~1" exit /b 0
"%~1" -c "import encodings, venv" >nul 2>&1
if errorlevel 1 (
  echo [5] Skipping broken Python: %~1 >> "%LOG%"
  exit /b 0
)
set "PY=%~1"
exit /b 0
