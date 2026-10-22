@echo off
REM ============================================================
REM  Renpu demo env setup for Windows
REM  (pure ASCII on purpose: .cmd + UTF-8 = garbled output)
REM  Builds an ISOLATED demo database so demos never touch real data.
REM ============================================================
setlocal
set "ROOT=%~dp0.."
set "DEMO_HOME=%TEMP%\renpu-demo-env"

echo -- Renpu demo environment setup
echo.

if exist "%DEMO_HOME%" rmdir /s /q "%DEMO_HOME%"
mkdir "%DEMO_HOME%" 2>nul
echo - isolated dir: %DEMO_HOME%

set "PY=%RENPU_PYTHON%"
if not defined PY for %%p in (python python3 py) do (if not defined PY for /f "delims=" %%i in ('where %%p 2^>nul') do (if not defined PY set "PY=%%i"))
if not defined PY (echo [X] python not found. Install Python 3.10+ and add it to PATH. & exit /b 1)
REM require pycryptodome, else try the next python
"%PY%" -c "from Crypto.Cipher import AES" >nul 2>&1
if errorlevel 1 (
  set "PY="
  for %%p in (python python3 py) do (if not defined PY for /f "delims=" %%i in ('where %%p 2^>nul') do (if not defined PY (%%i -c "from Crypto.Cipher import AES" >nul 2>&1 && set "PY=%%i")))
)
echo - python: %PY%

set "PYTHONIOENCODING=utf-8"
set "RENPU_DATA_HOME=%DEMO_HOME%"
set "RENPU_DB_PATH=%DEMO_HOME%\renpu.db"
set "RENPU_LOG_DIR=%DEMO_HOME%\logs"
set "RENPU_EXPORT_DIR=%DEMO_HOME%\exports"
set "RENPU_PYTHON=%PY%"

pushd "%ROOT%"
"%PY%" cli.py init
"%PY%" cli.py ingest --demo
popd

set "ACT=%ROOT%\scripts\activate-demo-env.cmd"
> "%ACT%" echo @echo off
>> "%ACT%" echo chcp 65001 ^>nul
>> "%ACT%" echo set "PYTHONIOENCODING=utf-8"
>> "%ACT%" echo set "RENPU_DATA_HOME=%DEMO_HOME%"
>> "%ACT%" echo set "RENPU_DB_PATH=%DEMO_HOME%\renpu.db"
>> "%ACT%" echo set "RENPU_LOG_DIR=%DEMO_HOME%\logs"
>> "%ACT%" echo set "RENPU_EXPORT_DIR=%DEMO_HOME%\exports"
>> "%ACT%" echo set "RENPU_PYTHON=%PY%"
>> "%ACT%" echo set "RENPU_HOME=%ROOT%"
>> "%ACT%" echo set "PATH=%ROOT%\bin;%%PATH%%"
>> "%ACT%" echo cd /d "%ROOT%"
>> "%ACT%" echo echo [OK] demo env active ^(data: %DEMO_HOME%^)

echo.
echo [OK] demo env ready
echo.
echo In the SAME cmd window, run first:
echo     call "%ACT%"
echo.
echo Then use normally (grm is on PATH now):
echo     grm doctor
echo     grm who NAME
echo     grm people --days 7
echo     grm search KEYWORD
echo     grm edges
echo     grm serve --port 8787
endlocal