@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_EXE="

where pythonw >nul 2>nul && set "PYTHON_EXE=pythonw"
if not defined PYTHON_EXE where python >nul 2>nul && set "PYTHON_EXE=python"
if not defined PYTHON_EXE where py >nul 2>nul && set "PYTHON_EXE=py -3"
if not defined PYTHON_EXE for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python*") do if exist "%%~fD\pythonw.exe" set "PYTHON_EXE=%%~fD\pythonw.exe"
if not defined PYTHON_EXE for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python*") do if exist "%%~fD\python.exe" set "PYTHON_EXE=%%~fD\python.exe"
if not defined PYTHON_EXE for /d %%D in ("C:\Program Files\Python*") do if exist "%%~fD\pythonw.exe" set "PYTHON_EXE=%%~fD\pythonw.exe"
if not defined PYTHON_EXE for /d %%D in ("C:\Program Files\Python*") do if exist "%%~fD\python.exe" set "PYTHON_EXE=%%~fD\python.exe"

if not defined PYTHON_EXE (
  echo Python non trovato.
  echo Installa Python oppure aggiungilo al PATH.
  pause
  exit /b 1
)

start "" %PYTHON_EXE% "%~dp0monitor_pc.py"
