@echo off
setlocal EnableDelayedExpansion

echo.
echo  ============================================================
echo   HAND GESTURE WINDOW CONTROLLER - Setup + Launcher v1.1
echo   (local venv, pinned versions)
echo  ============================================================
echo.

:: -- 0. LOCATION OF THIS .bat -------------------------------------
set "BASE_DIR=%~dp0"
set "VENV_DIR=%BASE_DIR%.venv"
set "SCRIPT=%BASE_DIR%gesture_window_controller.py"

:: -- 1. CHECK PYTHON ----------------------------------------------
echo [1/5] Checking Python...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo  ERROR: Python not found.
    echo  Install Python 3.10 or higher from https://python.org
    echo  Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)
set PYMAJOR=0
set PYMINOR=0
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
for /f "tokens=1,2 delims=." %%a in ("%PYVER%") do (
    set PYMAJOR=%%a
    set PYMINOR=%%b
)
if %PYMAJOR% lss 3 goto :ver_error
if %PYMAJOR% equ 3 if %PYMINOR% lss 10 goto :ver_error
echo  OK - Python %PYVER% found.
goto :ver_ok
:ver_error
echo.
echo  ERROR: Python 3.10 or higher is required.
echo  Version found: %PYVER%
echo  Download the correct version from https://python.org
echo.
pause
exit /b 1
:ver_ok

:: -- 2. CREATE VENV (only if it doesn't exist) --------------------
echo.
echo [2/5] Preparing virtual environment in: %VENV_DIR%
if exist "%VENV_DIR%\Scripts\activate.bat" (
    echo  OK - venv already exists, reusing it.
) else (
    echo  Creating venv...
    python -m venv "%VENV_DIR%"
    if !errorlevel! neq 0 (
        echo  ERROR: Could not create the venv.
        pause
        exit /b 1
    )
    echo  OK - venv created.
)

:: Activate venv
call "%VENV_DIR%\Scripts\activate.bat"
if %errorlevel% neq 0 (
    echo  ERROR: Could not activate the venv.
    pause
    exit /b 1
)

:: -- 3. REVIEW requirements.txt ----------------------------------
echo.
echo [3/5] Dependencies to be installed
echo.
if not exist "%BASE_DIR%requirements.txt" (
    echo  ERROR: requirements.txt not found next to this .bat
    echo  It is required to know which versions to install.
    pause
    exit /b 1
)
echo  ------------------------------------------------------------
type "%BASE_DIR%requirements.txt"
echo  ------------------------------------------------------------
echo.
echo  Review the versions listed above before continuing.
echo  If you want to change them, edit requirements.txt and run again.
echo.
choice /M "Have you reviewed requirements.txt and want to install these versions?"
if errorlevel 2 goto :req_cancel
goto :req_ok
:req_cancel
echo  Cancelled by the user.
pause
exit /b 1
:req_ok
echo  OK - Proceeding with the versions above.

:: -- 4. INSTALL PACKAGES ------------------------------------------
echo.
echo [4/5] Installing packages into the local venv...
echo  (nothing is installed outside this folder)
echo.

pip install --no-cache-dir --only-binary=:all: -r "%BASE_DIR%requirements.txt"

if %errorlevel% neq 0 (
    echo.
    echo  ERROR during installation.
    echo  Check your internet connection and try again.
    pause
    exit /b 1
)

:: -- 5. AUDIT VULNERABILITIES -------------------------------------
echo.
echo [5/5] Auditing installed packages with pip-audit...

pip show pip-audit >nul 2>&1
if not errorlevel 1 goto :run_audit

echo  Installing pip-audit...
pip install --quiet pip-audit
if not errorlevel 1 goto :run_audit
echo  ERROR: Could not install pip-audit. Skipping audit.
goto :launch

:run_audit
pip-audit --requirement "%BASE_DIR%requirements.txt"
if not errorlevel 1 goto :audit_ok
echo.
echo  WARNING: pip-audit reported a problem (vulnerability or network error).
echo  Review the output above before continuing.
echo.
choice /M "Continue anyway?"
if errorlevel 2 goto :audit_cancel
goto :launch

:audit_cancel
echo  Cancelled by the user.
pause
exit /b 1

:audit_ok
echo  OK - No known vulnerabilities found.

:launch
:: -- LAUNCH THE APPLICATION ---------------------------------------
echo.
echo  ============================================================
echo   All set. Starting Gesture Window Controller...
echo   Press Q or ESC in the camera window to quit.
echo  ============================================================
echo.

if not exist "%SCRIPT%" (
    echo  ERROR: gesture_window_controller.py not found
    echo  Make sure the .bat and the .py are in the same folder.
    pause
    exit /b 1
)

python "%SCRIPT%"

echo.
echo  Application closed. See you next time!
pause
