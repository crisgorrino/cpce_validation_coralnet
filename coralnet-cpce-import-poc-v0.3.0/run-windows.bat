@echo off
setlocal
cd /d "%~dp0"
if not exist .venv (
  py -m venv .venv
)
call .venv\Scripts\activate.bat
py -m pip install --upgrade pip
py -m pip install -r requirements.txt
where jpegtran >nul 2>nul
if errorlevel 1 (
  echo.
  echo NOTE: CPCe-safe lossless JPEG optimization requires jpegtran.exe from libjpeg-turbo.
  echo Put jpegtran.exe on PATH, set JPEGTRAN_BIN, or place it in the tools folder.
  echo Validation and package preparation still work without it.
  echo.
)
py -m uvicorn app:app --reload
endlocal
