@echo off
cd /d "%~dp0"
chcp 65001 > nul
echo 바탕화면에 '마비노기 모바일 헬퍼' 바로가기를 생성합니다...
python create_shortcut.py
if %errorlevel% equ 0 (
    echo.
    echo ✅ 바탕화면에 '마비노기 모바일 헬퍼' 바로가기가 성공적으로 생성되었습니다!
    echo 이제 바탕화면 아이콘을 더블클릭하면 창 하나로 바로 실행되고 닫으면 자동 종료됩니다.
    echo.
) else (
    echo ❌ 바로가기 생성 중 오류가 발생했습니다.
)
pause
