@echo off
rem WON-ISA: LLM-mode video only (run windows_llm_test.bat once first: it installs Ollama, the model and the venv)
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
set "LOG=%~dp0llm_record_log.txt"
echo ==== WON-ISA LLM record %date% %time% ==== > "%LOG%"
set "VPY=%LOCALAPPDATA%\won-isa\venv\Scripts\python.exe"
if not exist "%VPY%" (
  echo FAIL: venv not found. Run windows_llm_test.bat first. >> "%LOG%"
  goto :fail
)
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
if not exist "%~dp0logs" mkdir "%~dp0logs"

rem ---- Ollama: reuse the running one, otherwise start it with the same settings as windows_llm_test.bat
curl -s -o nul http://localhost:11434/api/version && goto :ollamaok
set "OLLAMA=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
if not exist "%OLLAMA%" set "OLLAMA=%ProgramFiles%\Ollama\ollama.exe"
if not exist "%OLLAMA%" (
  echo FAIL: Ollama not found. Run windows_llm_test.bat first. >> "%LOG%"
  goto :fail
)
set "OLLAMA_CONTEXT_LENGTH=16384"
set "OLLAMA_FLASH_ATTENTION=1"
set "OLLAMA_KV_CACHE_TYPE=q8_0"
rem Ollama output goes to a file, not to the window: clicking inside a console window freezes any program writing to it
start "ollama serve - keep this window open" /min cmd /c ""%OLLAMA%" serve > "%~dp0logs\ollama_serve_log.txt" 2>&1"
timeout /t 6 /nobreak >nul
:ollamaok
echo Ollama OK >> "%LOG%"

if exist "won_isa_demo_llm.mp4" (
  if exist "won_isa_demo_llm_prev.mp4" ren "won_isa_demo_llm_prev.mp4" "won_isa_demo_llm_prev_%RANDOM%.mp4"
  ren "won_isa_demo_llm.mp4" "won_isa_demo_llm_prev.mp4"
)
echo Recording... >> "%LOG%"
echo Recording LLM-mode video, about 5 minutes. Progress: llm_record_log.txt
rem the recorder starts its own server: no extra window, output in logs\server_*.log, stopped at the end
"%VPY%" -m record.record_demo --serve --out "%~dp0won_isa_demo_llm.mp4" >> "%LOG%" 2>&1
if errorlevel 1 (
  echo FAIL: recording stopped early. Partial video if any: won_isa_demo_llm_partial.mp4 >> "%LOG%"
  goto :fail
)
echo ==== DONE %date% %time% ==== >> "%LOG%"
echo DONE: won_isa_demo_llm.mp4
pause
exit /b 0

:fail
echo ==== STOPPED %date% %time% ==== >> "%LOG%"
echo STOPPED. Open llm_record_log.txt to see why.
pause
exit /b 1
