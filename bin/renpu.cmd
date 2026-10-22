@echo off
REM ============================================================
REM  Renpu CLI launcher for Windows
REM
REM  IMPORTANT: keep this file ASCII-only.
REM  cmd.exe corrupts argument parsing when a .cmd file itself
REM  contains non-ASCII bytes. Chinese here = broken CLI.
REM
REM  Picks a python that can import Crypto.Cipher (pycryptodome).
REM  No hardcoded user paths.
REM
REM  Usage:
REM    renpu doctor
REM    renpu init
REM    renpu ingest --demo
REM    renpu who NAME
REM    renpu edges
REM    renpu search KEYWORD
REM    renpu serve --port 8877
REM ============================================================
setlocal
set "RENPU_PARENT=%~dp0.."
if "%RENPU_PARENT:~-1%"=="\" set "RENPU_PARENT=%RENPU_PARENT:~0,-1%"

set "RENPU_PY="
if defined RENPU_PYTHON set "RENPU_PY=%RENPU_PYTHON%"
if not defined RENPU_PY if defined GRM_PYTHON set "RENPU_PY=%GRM_PYTHON%"
if not defined RENPU_PY if exist "%LOCALAPPDATA%\renpu\.venv\Scripts\python.exe" set "RENPU_PY=%LOCALAPPDATA%\renpu\.venv\Scripts\python.exe"

REM if the candidate lacks pycryptodome, discard it and search PATH pythons
if defined RENPU_PY (
    "%RENPU_PY%" -c "from Crypto.Cipher import AES" >nul 2>&1 || set "RENPU_PY="
)
if not defined RENPU_PY (
    for %%p in (python python3 py) do (
        if not defined RENPU_PY for /f "delims=" %%i in ('where %%p 2^>nul') do (
            if not defined RENPU_PY (
                %%i -c "from Crypto.Cipher import AES" >nul 2>&1 && set "RENPU_PY=%%i"
            )
        )
    )
)

if not defined RENPU_PY (
    echo [X] No python with pycryptodome found. 1>&2
    echo     Run: pip install pycryptodome 1>&2
    echo     Or set RENPU_PYTHON to a python that already has it. 1>&2
    exit /b 1
)

"%RENPU_PY%" "%RENPU_PARENT%\cli.py" %*
endlocal