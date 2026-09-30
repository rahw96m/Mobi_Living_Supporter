@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ========================================================
echo 🛠️ 마비노기 모바일 헬퍼 exe 빌드 시작 (--onedir 폴더형)...
echo ========================================================
python -m PyInstaller --onedir --noconsole --name "Mabinogi_Helper" --clean --version-file "file_version_info.txt" web_server.py
if exist "dist\Mabinogi_Helper\Mabinogi_Helper.exe" (
    copy /y "dist\Mabinogi_Helper\Mabinogi_Helper.exe" "dist\Mabinogi_Helper\마비노기_모바일_헬퍼.exe" >nul
    echo.
    echo ✅ dist\Mabinogi_Helper 폴더에 빌드가 성공적으로 완료되었습니다!
) else (
    echo.
    echo ❌ 빌드에 실패했습니다.
)
pause
