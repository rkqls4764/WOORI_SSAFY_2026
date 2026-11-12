@echo off
rem Push the Claude commit (won-isa_053b95b.bundle) to github.com/aaaaef/woori_ssafy_2026 (main).
rem Usage: push_to_fork.bat [path to an existing clone]   (default: Desktop\woori_ssafy_2026, cloned if missing)
setlocal EnableExtensions
cd /d "%~dp0"
set "BUNDLE=%~dp0won-isa_053b95b.bundle"
set "REPO=%~1"
if "%REPO%"=="" set "REPO=%USERPROFILE%\Desktop\woori_ssafy_2026"
where git >nul 2>&1 || (echo Git is not installed. Install Git for Windows first: https://git-scm.com/download/win & pause & exit /b 1)
if not exist "%BUNDLE%" (echo Bundle not found: %BUNDLE% & pause & exit /b 1)
if not exist "%REPO%\.git" (
  echo Cloning the fork into %REPO% ...
  git clone https://github.com/aaaaef/woori_ssafy_2026 "%REPO%" || goto :fail
)
git -C "%REPO%" status --porcelain | findstr . >nul && (echo %REPO% has uncommitted changes. Commit or stash them first. & pause & exit /b 1)
git -C "%REPO%" fetch origin || goto :fail
git -C "%REPO%" checkout main || goto :fail
git -C "%REPO%" merge --ff-only origin/main || goto :fail
git -C "%REPO%" fetch "%BUNDLE%" work:claude-work || goto :fail
git -C "%REPO%" merge --ff-only claude-work || goto :fail
git -C "%REPO%" log --oneline -3
git -C "%REPO%" push origin main || goto :fail
echo.
echo DONE: pushed to https://github.com/aaaaef/woori_ssafy_2026
pause
exit /b 0
:fail
echo.
echo FAILED. Copy the messages above and send them to Claude.
pause
exit /b 1
