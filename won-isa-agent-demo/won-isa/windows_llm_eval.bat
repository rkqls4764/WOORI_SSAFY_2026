@echo off
rem WON-ISA: LLM-mode evaluation only, GPU required (run windows_llm_test.bat once first: it installs Ollama, the model and the venv)
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
set "LOG=%~dp0llm_eval_log.txt"
echo ==== WON-ISA LLM eval %date% %time% ==== > "%LOG%"
set "VPY=%LOCALAPPDATA%\won-isa\venv\Scripts\python.exe"
if not exist "%VPY%" (
  echo FAIL: venv not found. Run windows_llm_test.bat first. >> "%LOG%"
  goto :fail
)
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
if not exist "%~dp0logs" mkdir "%~dp0logs"
nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv >> "%LOG%" 2>&1

rem ---- fresh Ollama with the same settings as windows_llm_test.bat, output to a file
set "OLLAMA=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
if not exist "%OLLAMA%" set "OLLAMA=%ProgramFiles%\Ollama\ollama.exe"
if not exist "%OLLAMA%" (
  echo FAIL: Ollama not found. Run windows_llm_test.bat first. >> "%LOG%"
  goto :fail
)
taskkill /IM "ollama app.exe" /F >nul 2>&1
taskkill /IM ollama.exe /F >nul 2>&1
set "OLLAMA_CONTEXT_LENGTH=16384"
set "OLLAMA_FLASH_ATTENTION=1"
set "OLLAMA_KV_CACHE_TYPE=q8_0"
start "ollama serve - keep this window open" /min cmd /c ""%OLLAMA%" serve > "%~dp0logs\ollama_serve_log.txt" 2>&1"
timeout /t 8 /nobreak >nul

rem ---- smoke test loads the model, then check that it runs on the GPU
echo Smoke test... >> "%LOG%"
echo [1] Smoke test (loads the model)...
"%VPY%" scripts\smoke_test.py >> "%LOG%" 2>&1
if errorlevel 1 (
  echo FAIL: smoke test. See llm_eval_log.txt >> "%LOG%"
  goto :fail
)
"%OLLAMA%" ps > "%TEMP%\wonisa_ps.txt" 2>&1
type "%TEMP%\wonisa_ps.txt" >> "%LOG%"
findstr /c:"100%% GPU" "%TEMP%\wonisa_ps.txt" >nul
if errorlevel 1 (
  echo FAIL: the model is not fully on the GPU, see the ollama ps line above. Reboot, close other GPU apps, run again. >> "%LOG%"
  echo The model is NOT fully on the GPU. Evaluation skipped so the numbers stay fair.
  goto :fail
)
echo GPU OK >> "%LOG%"

echo [2] Running evaluation on the GPU, about 30-40 minutes. Progress: eval_log.txt
echo Evaluation... >> "%LOG%"
"%VPY%" -m eval.run_eval > "%~dp0eval_log.txt" 2>&1
if errorlevel 1 (
  echo WARN: evaluation exited with an error. See eval_log.txt >> "%LOG%"
  goto :fail
)
"%OLLAMA%" ps >> "%LOG%" 2>&1
echo ==== DONE %date% %time% ==== >> "%LOG%"
echo DONE. Results: eval\REPORT_openai.md, eval\results_openai.json
pause
exit /b 0

:fail
echo ==== STOPPED %date% %time% ==== >> "%LOG%"
echo STOPPED. Open llm_eval_log.txt to see why.
pause
exit /b 1
