@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_EXE="

where python >nul 2>nul && set "PYTHON_EXE=python"
if not defined PYTHON_EXE where py >nul 2>nul && set "PYTHON_EXE=py -3"
if not defined PYTHON_EXE for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python*") do if exist "%%~fD\python.exe" set "PYTHON_EXE=%%~fD\python.exe"
if not defined PYTHON_EXE for /d %%D in ("C:\Program Files\Python*") do if exist "%%~fD\python.exe" set "PYTHON_EXE=%%~fD\python.exe"

if not defined PYTHON_EXE (
  echo Python non trovato.
  echo Installa Python e seleziona "Add Python to PATH", poi rilancia questo file.
  pause
  exit /b 1
)

set "ICON_ARGS="
if exist "%~dp0assets\MonitorPC.ico" set ICON_ARGS=--icon "%~dp0assets\MonitorPC.ico"

echo Uso Python: %PYTHON_EXE%
%PYTHON_EXE% -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
  echo PyInstaller non trovato. Provo a installarlo...
  %PYTHON_EXE% -m pip install pyinstaller
  if errorlevel 1 (
    echo Installazione PyInstaller non riuscita.
    pause
    exit /b 1
  )
)

%PYTHON_EXE% -m PyInstaller ^
  --noconsole ^
  --onefile ^
  --clean ^
  --name MonitorPC ^
  %ICON_ARGS% ^
  "%~dp0monitor_pc.py"

if errorlevel 1 (
  echo Build EXE non riuscita.
  pause
  exit /b 1
)

copy /y "%~dp0monitor_pc_config.json" "%~dp0dist\monitor_pc_config.json" >nul
if exist "%~dp0assets\MonitorPC.ico" (
  if not exist "%~dp0dist\assets" mkdir "%~dp0dist\assets"
  copy /y "%~dp0assets\MonitorPC.ico" "%~dp0dist\assets\MonitorPC.ico" >nul
)

echo.
echo EXE creato in:
echo %~dp0dist\MonitorPC.exe
pause
