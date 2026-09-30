@echo off
cd /d "%~dp0"

echo ============================================
echo  Google Doc Notifier - build
echo ============================================
echo.

py --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed.
    echo Install it from https://www.python.org/downloads/
    echo and check "Add python.exe to PATH" during install.
    pause
    exit /b 1
)

if not exist "google_doc_notifier.py" (
    echo [ERROR] google_doc_notifier.py is not in this folder.
    pause
    exit /b 1
)

echo [1/3] Installing required packages...
py -m pip install --upgrade pyinstaller pystray pillow
if errorlevel 1 (
    echo [ERROR] Package install failed. Check the internet connection.
    pause
    exit /b 1
)

echo.
echo [2/3] Building exe... (1-2 minutes)
py -m PyInstaller --onefile --noconsole --clean --hidden-import pystray._win32 --name google_doc_notifier google_doc_notifier.py
if errorlevel 1 (
    echo [ERROR] Build failed. See the messages above.
    pause
    exit /b 1
)

echo.
echo [3/3] Cleaning up...
rmdir /s /q build >nul 2>&1
del /q google_doc_notifier.spec >nul 2>&1

echo.
echo ============================================
echo  DONE!  dist\google_doc_notifier.exe
echo ============================================
explorer dist
pause
