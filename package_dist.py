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
print("[모비노기 생활 지원도구] 배포 패키지 빌드 시작")
print("   - 빌드 방식: PyInstaller --onedir (폴더형 독립 패키지)")
print("   - 장점: 실시간 임시폴더 압축해제(Dropper 행위)가 없어 백신 오탐 대폭 방지 및 초고속 실행")
print("=" * 65)

# 1. PyInstaller 빌드 실행
print("\n최신 코드로 폴더형 독립 패키지를 빌드합니다...")
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
    "--add-data",
    f"{os.path.join(BASE_DIR, 'recipes_cache.json')};.",
    "--add-data",
    f"{os.path.join(BASE_DIR, 'delivery_presets.json')};.",
    "--clean",
    "-y",
    "--version-file",
    VERSION_INFO,
    os.path.join(BASE_DIR, "web_server.py"),
]

ret = subprocess.run(build_cmd, cwd=BASE_DIR)
if ret.returncode != 0 or not os.path.exists(DIST_BUILD_DIR):
    print("[오류] 빌드에 실패했습니다.")
    sys.exit(1)

# 2. 기존 배포 폴더 정리 및 새로 복사
print("\n배포 폴더를 구성합니다...")
if os.path.exists(OUTPUT_DIR):
    shutil.rmtree(OUTPUT_DIR, ignore_errors=True)

# 빌드 결과물(dist/Mobi_Living_Supporter)을 배포용 폴더로 복사
shutil.copytree(DIST_BUILD_DIR, OUTPUT_DIR, dirs_exist_ok=True)

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
echo 바탕화면에 '모비노기 생활 지원도구' 바로가기를 생성합니다...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ws = New-Object -ComObject WScript.Shell; $target = Join-Path $pwd '모비노기_생활_지원도구.exe'; if (-not (Test-Path $target)) { $target = Join-Path $pwd 'Mobi_Living_Supporter.exe' }; $ico = Join-Path $pwd 'app_icon.ico'; $s = $ws.CreateShortcut([IO.Path]::Combine([Environment]::GetFolderPath('Desktop'), '모비노기 생활 지원도구.lnk')); $s.TargetPath = $target; $s.WorkingDirectory = $pwd; if (Test-Path $ico) { $s.IconLocation = \\"$ico,0\\" } else { $s.IconLocation = \\"$target,0\\" }; $s.Description = '모비노기 생활 지원도구'; $s.Save()"
if %errorlevel% equ 0 (
    echo.
    echo 바탕화면에 '모비노기 생활 지원도구' 바로가기가 생성되었습니다!
    echo.
) else (
    echo.
    echo 바로가기 생성 중 오류가 발생했습니다. 직접 '모비노기_생활_지원도구.exe'를 우클릭하여 바로가기를 만들어주세요.
    echo.
)
pause
"""
with open(os.path.join(OUTPUT_DIR, "바탕화면_바로가기_만들기.bat"), "w", encoding="utf-8") as f:
    f.write(shortcut_bat_content)

# 6. 사용방법 안내 텍스트 파일 작성
guide_text = """[ 모비노기 생활 지원도구 ]

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

■ 실전 튜토리얼: 주간 납품 18개 한 번에 끝내기 (따라하기 가이드):
1. [주간 납품] 탭 확인:
   - 프로그램 실행 시 기본적으로 '주간 납품' 핵심 18개 의뢰(로터스 힐링 완드, 론 엣지소드S, 양털, 달걀, 우유 등)가 자동 등록되어 있습니다.
   - 필요에 따라 목표 수량을 클릭해 자유롭게 변경할 수 있습니다.
2. [인벤토리 수량 동기화] 클릭:
   - 버튼을 누르면 캐릭터 가방뿐만 아니라 '개인 창고', '계정 공용 창고'의 보유량까지 1초 만에 합산 계산하여, 이미 가진 수량은 제외하고 '정확히 부족한 수량만' 스마트하게 산출합니다.
3. [목표 완수 모드] 체크 확인:
   - 상단 마스터 바의 '■ 목표 완수 모드'를 켜두시면 재료가 모자랄 때 멈추지 않고, 원자재 자동 필드 채집 → 7슬롯 가공대 일괄 가공 → 완제품 제작까지 100% 끝날 때까지 알아서 연속 실행합니다.
4. [주간 납품 일괄 실행] 클릭:
   - 버튼을 누르면 즉시 전체 파이프라인이 가동되며, 하단 실시간 콘솔에서 캐릭터가 벌목/채집하고 대장간에서 장비를 만드는 전 과정을 확인할 수 있습니다.
5. 매주 목요일 초기화 시 꿀팁:
   - 새 주간이 시작되면 [새 주간 시작 (수량 초기화)] 버튼을 한 번만 클릭하세요. 목표 항목을 다시 입력할 필요 없이 진행도가 깔끔하게 0개로 리셋되어 즉시 새로운 한 주 작업을 시작할 수 있습니다.

■ 디스코드 웹훅 알림 연동 가이드 (외출/자리비움 시 스마트폰 결과 받기):
1. 디스코드에서 웹훅 URL 만들기 (10초 소요):
   ① 내 디스코드 서버에서 알림받을 채널(예: #알림 또는 봇 채널) 우클릭 → [채널 편집] 선택
   ② 왼쪽 메뉴의 [연동] → [웹훅 만들기] (또는 [웹훅 보기]) 클릭
   ③ 생성된 웹훅의 [웹훅 URL 복사] 버튼 클릭
2. 대시보드에 URL 등록 및 테스트:
   ① 대시보드 상단 상태 바의 [디스코드: 꺼짐] 또는 마스터 바의 [디스코드 알림] 클릭
   ② 복사한 웹훅 URL을 붙여넣고, '목표 완수 모드 완료 시 알림 활성화' 체크
   ③ 하단의 [테스트 전송]을 눌러 내 디스코드 채널에 테스트 메시지가 도착하는지 확인 후 [저장] 클릭!
3. 결과:
   - 목표 완수 모드로 켜두고 자리를 비우더라도, 작업이 끝나면 스마트폰 디스코드로 작업 소요 시간, 날개 소모량, 완성 아이템 및 가공/채집 결과 요약 리포트가 즉시 도착합니다.

■ 7슬롯 가공대 & 개별 제작 팁:
- 7슬롯 빠른 가공 탭: 금속, 목재, 가죽, 옷감 시설의 남은 슬롯과 완료 대기열을 실시간 1초 카운트다운 타이머로 동기화하며, [완료 가공품 수령]으로 모든 시설 결과물을 한 번에 수령할 수 있습니다.
- 개별 제작 탭: 원하는 장비나 소모품을 여러 개 누적 등록하면, 통합 플래너가 모든 하위 재료(원자재 채집 + 1차 가공)를 일괄 분석(BOM)하여 원스톱으로 제작해 줍니다.
- 스마트 채집: 나무 진액은 뾰족 나무, 통나무는 굵은 나무 등 최적의 타겟을 자동 선별하며, 벌목/양털 깎기 레벨이 미달하더라도 에러 없이 하위 대상으로 단계적 자동 폴백합니다.

■ 레시피 및 설정 영구 보존 & 자동 이전:
- 버전을 업데이트하여 새 버전 폴더를 받더라도, 등록/학습하신 레시피와 설정, 프리셋은 사용자의 '내 문서\\MobiLivingSupporter'에 안전하게 영구 저장됩니다.
- 이전 버전에서 사용하던 설정 파일들을 새 버전 폴더로 매번 복사해올 필요가 전혀 없습니다!
- 웹 대시보드 상단 또는 [연결 설정] 모달에서 '데이터 폴더'를 누르면 언제든 탐색기로 확인/백업할 수 있습니다.

■ 혹시 윈도우 SmartScreen / 디펜더 알림이 뜨는 경우:
- 본 프로그램은 인증서(코드사인) 비용이 없는 오픈소스 유틸리티이므로, 인터넷에서 처음 받은 파일에 대해 윈도우가 "PC 보호" 알림을 띄울 수 있습니다.
- [추가 정보] → [실행]을 눌러주시면 정상 실행됩니다.
- 만약 실행 파일이 바로 차단되거나 삭제된다면, ZIP 파일 우클릭 → [속성] → 맨 아래 [차단 해제] 체크 후 확인을 누르고 압축을 풀어주세요.

■ C드라이브가 아닌 D: 드라이브 등에 게임이 설치된 경우:
- 프로그램이 컴퓨터 내 모든 드라이브 및 실행 중인 게임을 자동 탐색하여 연결합니다.
- 만약 대시보드 상단에 'CLI 미발견'이 나타날 경우, 상단의 [연결 설정] 버튼을 눌러 마비노기 모바일 설치 폴더(예: D:\\Nexon\\MabinogiMobile)를 입력하고 [저장 & 연결]을 눌러주시면 됩니다.

■ 백그라운드 사용 시 팁:
- 마비노기 모바일 게임 창을 작업 표시줄로 [최소화(_)]하면 게임 엔진(유니티) 특성상 게임 동작과 통신이 일시 중지될 수 있습니다. 최소화하지 마시고 다른 창 뒤에 겹쳐 두시거나 전체 창모드로 유지해 주세요.
- 웹 대시보드는 백그라운드 탭이나 다른 창 뒤로 가도 Web Worker가 동작하여 가공 타이머 알람과 서버 연결이 끊기지 않고 정상 유지됩니다.

■ 주의사항:
- 컴퓨터에 파이썬이 설치되어 있지 않아도 100% 독립 실행됩니다.
- '_internal' 폴더 및 파일들은 프로그램 실행에 필요한 부속 파일이므로 삭제하거나 이동하지 마세요.

■ 게임 이용 안내 및 면책 조항 (Disclaimer):
- 본 프로그램은 마비노기 모바일 클라이언트가 제공하는 공식 AI 에이전트 CLI(MabinogiMobile_CLI)를 연동하여 사용자의 생활/가공/제작 작업을 보조하는 비공식 오픈소스 유틸리티입니다.
- 게임사의 운영정책 변경이나 모니터링 기준에 따라 과도한 자동화 사용 시 비인가 프로그램/자동 플레이로 판단되어 계정 이용 제한(임시/영구 정지 등)을 받을 위험이 항상 존재합니다.
- 본 도구의 사용 여부 결정과 사용으로 인해 발생하는 모든 결과(계정 제재, 데이터 손실 등)에 대한 책임은 전적으로 사용자 본인에게 있으며, 개발자는 이에 대해 어떠한 법적·금전적·운영적 책임도 지지 않습니다.
- 안전한 이용을 위해 장시간 무인 방치나 과도한 반복 작업을 지양해 주시기 바랍니다.
"""

with open(os.path.join(OUTPUT_DIR, "사용방법.txt"), "w", encoding="utf-8") as f:
    f.write(guide_text)

# 7. 자동 ZIP 압축 파일 생성
print("\n배포용 ZIP 압축 파일을 생성합니다...")
zip_path = shutil.make_archive(OUTPUT_DIR, "zip", OUTPUT_DIR)

# GitHub Release용 영문 명칭 ZIP 파일 복사 생성 (Mobi_Living_Supporter_v1.4.0.zip)
VERSION = "v1.4.0"
github_zip_path = os.path.join(BASE_DIR, f"Mobi_Living_Supporter_{VERSION}.zip")
shutil.copy2(zip_path, github_zip_path)

print("\n" + "=" * 65)
print("[성공] 모비노기 생활 지원도구 배포 패키지 구성 및 압축 완료!")
print(f"배포 폴더: {OUTPUT_DIR}")
print(f"압축 파일: {zip_path}")
print(f"깃허브 릴리즈용: {github_zip_path}")
print(f"안내: 위 ZIP 파일 중 하나를 그대로 공유하거나 깃허브 릴리즈에 첨부하시면 됩니다!")
print("=" * 65 + "\n")
