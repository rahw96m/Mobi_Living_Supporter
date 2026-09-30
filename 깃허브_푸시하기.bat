@echo off
set "PATH=C:\Users\brigh\AppData\Local\GitHubDesktop\app-3.6.6\resources\app\git\cmd;%PATH%"
cd /d "%~dp0"
echo ========================================================
echo [Mobi Living Supporter] Pushing commits and tags to GitHub...
echo ========================================================
git push origin main --tags
echo.
if %errorlevel% equ 0 (
    echo [OK] Push completed successfully!
) else (
    echo [INFO] If prompted or failed, please click [Push origin] in GitHub Desktop.
)
echo.
pause
