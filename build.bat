@echo off
echo.
echo  ✦ Nebula Killsay — Build Script
echo  ================================
echo.

where python >nul 2>&1
if %errorlevel% neq 0 (
    echo  [ERROR] Python not found. Install Python 3.10+ and try again.
    pause
    exit /b 1
)

where pyinstaller >nul 2>&1
if %errorlevel% neq 0 (
    echo  [INFO] PyInstaller not found. Installing...
    pip install pyinstaller
)

echo  [INFO] Building Nebula-Killsay.exe...
echo.

pyinstaller ^
    --onefile ^
    --windowed ^
    --name "Nebula-Killsay" ^
    --icon "assets/icon.ico" ^
    nebula.py

echo.
if exist "dist\Nebula-Killsay.exe" (
    echo  [OK] Build complete: dist\Nebula-Killsay.exe
) else (
    echo  [ERROR] Build failed. Check output above.
)

echo.
pause
