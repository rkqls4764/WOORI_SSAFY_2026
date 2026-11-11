@echo off
rem WON-ISA: run the LLM evaluation 3 times on a frozen copy of the code (GPU required)
rem The code is copied first, so editing the project while this runs does not change the runs.
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
set "LOG=%~dp0llm_eval_x3_log.txt"
echo ==== WON-ISA LLM eval x3 %date% %time% ==== > "%LOG%"
set "VPY=%LOCALAPPDATA%\won-isa\venv\Scripts\python.exe"
if not exist "%VPY%" (
  echo FAIL: venv not found. Run windows_llm_test.bat first. >> "%LOG%"
  goto :fail
)
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
if not exist "%~dp0logs" mkdir "%~dp0logs"
if not exist "%~dp0eval\repeat" mkdir "%~dp0eval\repeat"

rem ---- frozen copy of the code
set "SNAP=%LOCALAPPDATA%\won-isa\snap"
robocopy "%~dp0." "%SNAP%" /MIR /XD logs __pycache__ .venv repeat llm_runs /XF *.mp4 *_log.txt /NFL /NDL /NJH /NJS /NP >> "%LOG%" 2>&1
if errorlevel 8 (
  echo FAIL: could not copy the code >> "%LOG%"
  goto :fail
)
echo Code copied to %SNAP% >> "%LOG%"

rem ---- fresh Ollama, output to a file
set "OLLAMA=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
if not exist "%OLLAMA%" set "OLLAMA=%ProgramFiles%\Ollama\ollama.exe"
taskkill /IM "ollama app.exe" /F >nul 2>&1
taskkill /IM ollama.exe /F >nul 2>&1
set "OLLAMA_CONTEXT_LENGTH=16384"
set "OLLAMA_FLASH_ATTENTION=1"
set "OLLAMA_KV_CACHE_TYPE=q8_0"
start "ollama serve - keep this window open" /min cmd /c ""%OLLAMA%" serve > "%~dp0logs\ollama_serve_log.txt" 2>&1"
timeout /t 8 /nobreak >nul

cd /d "%SNAP%"
echo [0] Smoke test... >> "%LOG%"
"%VPY%" scripts\smoke_test.py >> "%LOG%" 2>&1
if errorlevel 1 (
  echo FAIL: smoke test >> "%LOG%"
  goto :fail
)
"%OLLAMA%" ps > "%TEMP%\wonisa_ps.txt" 2>&1
type "%TEMP%\wonisa_ps.txt" >> "%LOG%"
findstr /c:"100%% GPU" "%TEMP%\wonisa_ps.txt" >nul
if errorlevel 1 (
  echo FAIL: the model is not fully on the GPU. Reboot and run again. >> "%LOG%"
  echo The model is NOT fully on the GPU. Stopped.
  goto :fail
)
echo GPU OK >> "%LOG%"

for %%n in (1 2 3) do (
  echo [%%n/3] evaluation started !time! >> "%LOG%"
  echo [%%n/3] Running evaluation, about 15-20 minutes each. Progress: eval\repeat\run%%n_log.txt
  "%VPY%" -m eval.run_eval > "%~dp0eval\repeat\run%%n_log.txt" 2>&1
  copy /y "%SNAP%\eval\results_openai.json" "%~dp0eval\repeat\run%%n_results_openai.json" >nul
  copy /y "%SNAP%\eval\REPORT_openai.md" "%~dp0eval\repeat\run%%n_REPORT_openai.md" >nul
  robocopy "%SNAP%\eval\llm_runs" "%~dp0eval\llm_runs" /XO /NFL /NDL /NJH /NJS /NP >nul
  echo [%%n/3] done !time! >> "%LOG%"
)
"%OLLAMA%" ps >> "%LOG%" 2>&1
echo ==== DONE %date% %time% ==== >> "%LOG%"
echo DONE. Results: eval\repeat\
pause
exit /b 0

:fail
echo ==== STOPPED %date% %time% ==== >> "%LOG%"
echo STOPPED. Open llm_eval_x3_log.txt to see why.
pause
exit /b 1
