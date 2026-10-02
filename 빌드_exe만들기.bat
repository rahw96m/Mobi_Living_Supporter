@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ========================================================
echo 🛠️ 모비노기 생활 지원도구 exe 빌드 시작 (--onedir 폴더형)...
echo ========================================================
python -m PyInstaller --onedir --noconsole --name "Mobi_Living_Supporter" --icon "app_icon.ico" --add-data "app_icon.ico;." --add-data "app_icon.png;." --clean -y --version-file "file_version_info.txt" web_server.py
if exist "dist\Mobi_Living_Supporter\Mobi_Living_Supporter.exe" (
    copy /y "dist\Mobi_Living_Supporter\Mobi_Living_Supporter.exe" "dist\Mobi_Living_Supporter\모비노기_생활_지원도구.exe" >nul
    echo.
    echo ✅ dist\Mobi_Living_Supporter 폴더에 빌드가 성공적으로 완료되었습니다!
) else (
    echo.
    echo ❌ 빌드에 실패했습니다.
)
pause
