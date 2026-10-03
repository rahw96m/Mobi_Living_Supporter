import os
import shutil
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DIST_BUILD_DIR = os.path.join(BASE_DIR, "dist", "Mobi_Living_Supporter")
RECIPE_SRC = os.path.join(BASE_DIR, "recipes_cache.json")
OUTPUT_DIR = os.path.join(BASE_DIR, "배포용_Mobi_Living_Supporter")
VERSION_INFO = os.path.join(BASE_DIR, "file_version_info.txt")

print("=" * 65)
print("📦 [모비노기 생활 지원도구] 배포 패키지 빌드 시작")
print("   - 빌드 방식: PyInstaller --onedir (폴더형 독립 패키지)")
print("   - 장점: 실시간 임시폴더 압축해제(Dropper 행위)가 없어 백신 오탐 대폭 방지 및 초고속 실행")
print("=" * 65)

# 1. PyInstaller 빌드 실행
print("\n🔨 최신 코드로 폴더형 독립 패키지를 빌드합니다...")
build_cmd = [
    sys.executable,
    "-m",
    "PyInstaller",
    "--onedir",
    "--noconsole",
    "--name",
    "Mobi_Living_Supporter",
    "--icon",
    os.path.join(BASE_DIR, "app_icon.ico"),
    "--add-data",
    f"{os.path.join(BASE_DIR, 'app_icon.ico')};.",
    "--add-data",
    f"{os.path.join(BASE_DIR, 'app_icon.png')};.",
    "--clean",
    "-y",
    "--version-file",
    VERSION_INFO,
    os.path.join(BASE_DIR, "web_server.py"),
]

ret = subprocess.run(build_cmd, cwd=BASE_DIR)
if ret.returncode != 0 or not os.path.exists(DIST_BUILD_DIR):
    print("❌ 빌드에 실패했습니다.")
    sys.exit(1)

# 2. 기존 배포 폴더 정리 및 새로 복사
print("\n📂 배포 폴더를 구성합니다...")
if os.path.exists(OUTPUT_DIR):
    shutil.rmtree(OUTPUT_DIR, ignore_errors=True)

# 빌드 결과물(dist/Mobi_Living_Supporter)을 배포용 폴더로 복사
shutil.copytree(DIST_BUILD_DIR, OUTPUT_DIR)

# 3. 한글 이름 실행 파일도 추가 제공 (사용자 편의)
exe_src = os.path.join(OUTPUT_DIR, "Mobi_Living_Supporter.exe")
exe_kr = os.path.join(OUTPUT_DIR, "모비노기_생활_지원도구.exe")
if os.path.exists(exe_src):
    shutil.copy2(exe_src, exe_kr)
    try:
        os.utime(exe_kr, None)
        import ctypes
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x0000, None, None)
    except Exception:
        pass

# 4. 레시피 캐시 및 기본 프리셋 복사
if os.path.exists(RECIPE_SRC):
    shutil.copy2(RECIPE_SRC, os.path.join(OUTPUT_DIR, "recipes_cache.json"))

preset_src = os.path.join(BASE_DIR, "delivery_presets.json")
if os.path.exists(preset_src):
    shutil.copy2(preset_src, os.path.join(OUTPUT_DIR, "delivery_presets.json"))

# 아이콘 파일 복사
for icon_name in ("app_icon.ico", "app_icon.png"):
    src_icon = os.path.join(BASE_DIR, icon_name)
    if os.path.exists(src_icon):
        shutil.copy2(src_icon, os.path.join(OUTPUT_DIR, icon_name))

# 5. 바탕화면 바로가기 생성기(무설치 배치파일) 추가
shortcut_bat_content = """@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 📌 바탕화면에 '모비노기 생활 지원도구' 바로가기를 생성합니다...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ws = New-Object -ComObject WScript.Shell; $target = Join-Path $pwd '모비노기_생활_지원도구.exe'; if (-not (Test-Path $target)) { $target = Join-Path $pwd 'Mobi_Living_Supporter.exe' }; $ico = Join-Path $pwd 'app_icon.ico'; $s = $ws.CreateShortcut([IO.Path]::Combine([Environment]::GetFolderPath('Desktop'), '모비노기 생활 지원도구.lnk')); $s.TargetPath = $target; $s.WorkingDirectory = $pwd; if (Test-Path $ico) { $s.IconLocation = \\"$ico,0\\" } else { $s.IconLocation = \\"$target,0\\" }; $s.Description = '모비노기 생활 지원도구'; $s.Save()"
if %errorlevel% equ 0 (
    echo.
    echo ✅ 바탕화면에 '모비노기 생활 지원도구' 바로가기가 생성되었습니다!
    echo.
) else (
    echo.
    echo ❌ 바로가기 생성 중 오류가 발생했습니다. 직접 '모비노기_생활_지원도구.exe'를 우클릭하여 바로가기를 만들어주세요.
    echo.
)
pause
"""
with open(os.path.join(OUTPUT_DIR, "바탕화면_바로가기_만들기.bat"), "w", encoding="utf-8") as f:
    f.write(shortcut_bat_content)

# 6. 사용방법 안내 텍스트 파일 작성
guide_text = """[ 🗡️ 모비노기 생활 지원도구 ]

■ 실행 방법:
1. 배포받은 압축(ZIP) 파일을 원하는 폴더에 압축 해제합니다.
2. 폴더 내의 '모비노기_생활_지원도구.exe'를 더블클릭하여 실행합니다.
   * 바탕화면에 아이콘을 두고 쓰시려면 '바탕화면_바로가기_만들기.bat'을 더블클릭하시면 됩니다!
3. 브라우저 전용 대시보드가 자동으로 열립니다. 창을 닫으면 프로그램도 안전하게 자동 종료됩니다.

■ 필수 사전 설정 (중요!):
1. 마비노기 모바일 PC 클라이언트를 켜고 캐릭터로 접속합니다.
2. 게임 내 [환경설정] → [기타] (또는 게임 설정) 메뉴로 이동합니다.
3. [MM AI 에이전트 활성화] 옵션을 ON으로 켜주세요.
   * 이 옵션을 켜야 넥슨 공식 연동 CLI(MabinogiMobile_CLI.exe)가 자동 설치/작동합니다.

■ 혹시 윈도우 SmartScreen / 디펜더 알림이 뜨는 경우:
- 본 프로그램은 인증서(코드사인) 비용이 없는 오픈소스 유틸리티이므로, 인터넷에서 처음 받은 파일에 대해 윈도우가 "PC 보호" 알림을 띄울 수 있습니다.
- [추가 정보] → [실행]을 눌러주시면 정상 실행됩니다.
- 만약 실행 파일이 바로 차단되거나 삭제된다면, ZIP 파일 우클릭 → [속성] → 맨 아래 [차단 해제] 체크 후 확인을 누르고 압축을 풀어주세요.

■ C드라이브가 아닌 D: 드라이브 등에 게임이 설치된 경우:
- 프로그램이 컴퓨터 내 모든 드라이브 및 실행 중인 게임을 자동 탐색하여 연결합니다.
- 만약 대시보드 상단에 'CLI 미발견'이 나타날 경우, 상단의 [⚙️ 연결 설정] 버튼을 눌러 마비노기 모바일 설치 폴더(예: D:\\Nexon\\MabinogiMobile)를 입력하고 [저장 & 연결]을 눌러주시면 됩니다.

■ 백그라운드 사용 시 팁:
- 마비노기 모바일 게임 창을 작업 표시줄로 [최소화(_)]하면 게임 엔진(유니티) 특성상 게임 동작과 통신이 일시 중지될 수 있습니다. 최소화하지 마시고 다른 창 뒤에 겹쳐 두시거나 전체 창모드로 유지해 주세요.
- 웹 대시보드는 백그라운드 탭이나 다른 창 뒤로 가도 Web Worker가 동작하여 가공 타이머 알람과 서버 연결이 끊기지 않고 정상 유지됩니다.

■ 주의사항:
- 컴퓨터에 파이썬이 설치되어 있지 않아도 100% 독립 실행됩니다.
- '_internal' 폴더 및 파일들은 프로그램 실행에 필요한 부속 파일이므로 삭제하거나 이동하지 마세요.
"""

with open(os.path.join(OUTPUT_DIR, "사용방법.txt"), "w", encoding="utf-8") as f:
    f.write(guide_text)

# 7. 자동 ZIP 압축 파일 생성
print("\n🗜️ 배포용 ZIP 압축 파일을 생성합니다...")
zip_path = shutil.make_archive(OUTPUT_DIR, "zip", OUTPUT_DIR)

# GitHub Release용 영문 명칭 ZIP 파일 복사 생성 (Mobi_Living_Supporter_v0.3.1.zip)
VERSION = "v0.3.1"
github_zip_path = os.path.join(BASE_DIR, f"Mobi_Living_Supporter_{VERSION}.zip")
shutil.copy2(zip_path, github_zip_path)

print("\n" + "=" * 65)
print("🎉 [성공] 모비노기 생활 지원도구 배포 패키지 구성 및 압축 완료!")
print(f"📁 배포 폴더: {OUTPUT_DIR}")
print(f"📦 압축 파일: {zip_path}")
print(f"📦 깃허브 릴리즈용: {github_zip_path}")
print(f"💡 안내: 위 ZIP 파일 중 하나를 그대로 공유하거나 깃허브 릴리즈에 첨부하시면 됩니다!")
print("=" * 65 + "\n")
