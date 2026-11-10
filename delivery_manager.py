import os
import sys
import re
import math
import time
import json
import threading
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional, Tuple, Set

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import ctypes
from mabi_cli import MabinogiCLI, MabinogiCLIError, get_user_data_dir, get_user_data_path

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_AWAYMODE_REQUIRED = 0x00000040

def enable_prevent_sleep() -> None:
    """Prevents Windows PC from entering sleep/standby mode while long automated routines run."""
    if sys.platform == "win32":
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED)
        except Exception:
            pass

def disable_prevent_sleep() -> None:
    """Restores default Windows power management state."""
    if sys.platform == "win32":
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        except Exception:
            pass

LogCallback = Callable[[str, str], None]  # (level, message)
AlarmCallback = Callable[[Dict[str, Any]], None]

if getattr(sys, "frozen", False):
    SCRIPT_DIR = os.path.dirname(sys.executable)
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RECIPE_CACHE_FILE = get_user_data_path("recipes_cache.json", SCRIPT_DIR)
ALARMS_FILE = get_user_data_path("delivery_alarms.json", SCRIPT_DIR)
DELIVERY_TARGETS_FILE = get_user_data_path("registered_deliveries.json", SCRIPT_DIR)

# 가공 시설별 슬롯 최대 한도 (게임 클라이언트 시설당 7슬롯)
MAX_SLOTS: int = 7

# Default known recipes for common weekly delivery items
DEFAULT_RECIPES: Dict[str, Dict[str, int]] = {
    "론 엣지소드S": {"합금강괴": 3, "가죽+": 3},
    "론 엣지소드": {"철괴": 2, "가죽": 2},
    "비늘 갑옷 신발S": {"합금강괴": 3, "가죽+": 2},
    "비늘 갑옷 투구S": {"합금강괴": 3, "가죽+": 2},
    "비늘 갑옷 장갑S": {"합금강괴": 2, "실크": 4},
    "비늘 갑옷 상의S": {"합금강괴": 4, "옷감+": 2},
    "비늘 갑옷 하의S": {"합금강괴": 3, "옷감+": 3},
    "로터스 힐링 완드S": {"상급 목재": 3, "합금강괴": 3},
    "로터스 힐링 완드": {"목재+": 2, "강철괴": 2},
    "린넨 로브S": {"상급 옷감": 3, "옷감+": 2},
    "린넨 로브": {"옷감": 3, "가죽": 2},
    "마블 힐링 완드S": {"상급 목재": 3, "강철괴": 3},
    "마블 힐링 완드": {"목재": 2, "철괴": 2},
    "달걀프라이": {"달걀": 1, "소금": 1},
    "삶은 달걀": {"달걀": 1, "물이 든 병": 1},
    "여행자 간식": {"달걀": 1, "고기": 1},
    "조개찜": {"조개": 10, "물이 든 병": 5, "레몬": 4, "허브": 3},
    "사과 주스": {"사과": 4, "허브": 2, "물이 든 병": 1, "설탕": 2},
    "옥수수차": {"옥수수": 4, "물이 든 병": 1},
    "녹차": {"말린 찻잎": 2, "물이 든 병": 1},
    "홍차": {"발효된 찻잎": 2, "물이 든 병": 1, "옷감": 1},
    "구운 고기": {"고기": 4, "허브": 2, "소금": 1},
    "마요네즈 고기볶음": {"고기": 4, "양배추": 1, "마요네즈": 1},
    "통감자구이": {"감자": 4, "허브": 2, "설탕": 2},
    "야채볶음": {"감자": 8, "양파": 3, "양배추": 6, "허브": 2},
    "라이트 크로스보우S": {"상급 목재": 3, "강철괴": 3},
    "가죽 갑옷 신발S": {"상급 가죽": 3, "강철괴": 2},
    "두꺼운 가죽 갑옷 신발": {"가죽+": 2, "강철괴": 1},
    "두꺼운 전투복 신발": {"옷감+": 2, "강철괴": 1},
    "사슬 갑옷 신발": {"강철괴": 2, "가죽+": 1},
    "그랜드 크로스보우": {"목재+": 2, "강철괴": 2},
    "전투복 신발S": {"상급 옷감": 3, "강철괴": 2},
    "크레센트 엣지소드": {"강철괴": 2, "가죽+": 2},
    "마법 유탄 부품": {"분해된 장비 부품": 50, "마나 허브": 10, "협곡의 마나석": 1, "연금술 부스러기": 50},
    "캠프파이어 키트": {"나뭇가지": 5, "통나무": 2},
    "숙련 캠프파이어 키트": {"나뭇가지": 5, "상급 통나무": 4, "돌멩이": 10},
    "전문 캠프파이어 키트": {"나뭇가지": 5, "상급 통나무+": 4, "돌멩이": 10},
}

# 1회 제작(craftCount 1) 당 완제품 산출량 (ProducedPerCraft)
PRODUCED_PER_CRAFT: Dict[str, int] = {
    "마법 유탄 부품": 5,
    "상급 마법 유탄 부품": 5,
    "화염 마법 유탄": 3,
    "번개 마법 유탄": 3,
    "바람 마법 유탄": 3,
    "산성 마법 유탄": 3,
}

# 4대 시설 가공품 표준 레시피 (계층형 BOM 완전 분해용 공식 테이블)
# produced: 1회 가공당 산출량, ingredients: 1회 가공당 소요 재료, tier: 가공 티어 (1~7)
STANDARD_ALTER_RECIPES: Dict[str, Dict[str, Any]] = {
    # 금속 가공 시설 (Metal)
    "철괴": {
        "facility": "금속 가공 시설",
        "produced": 3,
        "ingredients": {"철 광석": 10},
        "tier": 1,
        "display_name": "철괴(철 광석)"
    },
    "강철괴": {
        "facility": "금속 가공 시설",
        "produced": 3,
        "ingredients": {"철괴": 3, "석탄": 4},
        "tier": 2,
        "display_name": "강철괴"
    },
    "합금강괴": {
        "facility": "금속 가공 시설",
        "produced": 3,
        "ingredients": {"강철괴": 3, "동 광석": 15, "석탄": 8},
        "tier": 3,
        "display_name": "합금강괴"
    },
    "특수강괴": {
        "facility": "금속 가공 시설",
        "produced": 3,
        "ingredients": {"합금강괴": 4, "백동 광석": 20, "석탄": 12},
        "tier": 4,
        "display_name": "특수강괴"
    },
    "은합금괴": {
        "facility": "금속 가공 시설",
        "produced": 3,
        "ingredients": {"특수강괴": 5, "은 광석": 20, "석탄": 16},
        "tier": 5,
        "display_name": "은합금괴"
    },
    "운철괴": {
        "facility": "금속 가공 시설",
        "produced": 3,
        "ingredients": {"은합금괴": 5, "운철 광석": 20, "석탄": 20},
        "tier": 6,
        "display_name": "운철괴"
    },
    "백금강괴": {
        "facility": "금속 가공 시설",
        "produced": 3,
        "ingredients": {"운철괴": 5, "백금 광석": 20, "석탄": 20},
        "tier": 7,
        "display_name": "백금강괴"
    },

    # 목재 가공 시설 (Lumber)
    "목재": {
        "facility": "목재 가공 시설",
        "produced": 3,
        "ingredients": {"통나무": 10},
        "tier": 1,
        "display_name": "목재"
    },
    "목재+": {
        "facility": "목재 가공 시설",
        "produced": 3,
        "ingredients": {"목재": 3, "나무 진액": 4},
        "tier": 2,
        "display_name": "목재+"
    },
    "상급 목재": {
        "facility": "목재 가공 시설",
        "produced": 3,
        "ingredients": {"목재+": 3, "상급 통나무": 15, "나무 진액": 8},
        "tier": 3,
        "display_name": "상급 목재"
    },
    "상급 목재+": {
        "facility": "목재 가공 시설",
        "produced": 3,
        "ingredients": {"상급 목재": 4, "상급 통나무+": 20, "나무 진액": 12},
        "tier": 4,
        "display_name": "상급 목재+"
    },
    "최상급 목재": {
        "facility": "목재 가공 시설",
        "produced": 3,
        "ingredients": {"상급 목재+": 5, "최상급 통나무": 20, "나무 진액": 16},
        "tier": 5,
        "display_name": "최상급 목재"
    },
    "최상급 목재+": {
        "facility": "목재 가공 시설",
        "produced": 3,
        "ingredients": {"최상급 목재": 5, "최상급 통나무+": 20, "나무 진액": 20},
        "tier": 6,
        "display_name": "최상급 목재+"
    },
    "특급 목재": {
        "facility": "목재 가공 시설",
        "produced": 3,
        "ingredients": {"최상급 목재+": 5, "특급 통나무": 20, "나무 진액": 20},
        "tier": 7,
        "display_name": "특급 목재"
    },
    "부드러운 목재": {
        "facility": "목재 가공 시설",
        "produced": 3,
        "ingredients": {"상급 목재+": 15, "부드러운 통나무": 30, "나무 진액": 30},
        "tier": 5,
        "display_name": "부드러운 목재"
    },
    "단단한 목재": {
        "facility": "목재 가공 시설",
        "produced": 3,
        "ingredients": {"상급 목재+": 15, "단단한 통나무": 30, "나무 진액": 30},
        "tier": 5,
        "display_name": "단단한 목재"
    },

    # 가죽 가공 시설 (Leather)
    "가죽": {
        "facility": "가죽 가공 시설",
        "produced": 3,
        "ingredients": {"생가죽": 10},
        "tier": 1,
        "display_name": "가죽"
    },
    "가죽+": {
        "facility": "가죽 가공 시설",
        "produced": 3,
        "ingredients": {"가죽": 3, "타닌 가루": 4},
        "tier": 2,
        "display_name": "가죽+"
    },
    "상급 가죽": {
        "facility": "가죽 가공 시설",
        "produced": 3,
        "ingredients": {"가죽+": 3, "상급 생가죽": 15, "타닌 가루": 8},
        "tier": 3,
        "display_name": "상급 가죽"
    },
    "상급 가죽+": {
        "facility": "가죽 가공 시설",
        "produced": 3,
        "ingredients": {"상급 가죽": 4, "상급 생가죽+": 20, "타닌 가루": 12},
        "tier": 4,
        "display_name": "상급 가죽+"
    },
    "최상급 가죽": {
        "facility": "가죽 가공 시설",
        "produced": 3,
        "ingredients": {"상급 가죽+": 5, "최상급 생가죽": 20, "타닌 가루": 16},
        "tier": 5,
        "display_name": "최상급 가죽"
    },
    "최상급 가죽+": {
        "facility": "가죽 가공 시설",
        "produced": 3,
        "ingredients": {"최상급 가죽": 5, "최상급 생가죽+": 20, "타닌 가루": 20},
        "tier": 6,
        "display_name": "최상급 가죽+"
    },
    "특급 가죽": {
        "facility": "가죽 가공 시설",
        "produced": 3,
        "ingredients": {"최상급 가죽+": 5, "특급 생가죽": 20, "타닌 가루": 20},
        "tier": 7,
        "display_name": "특급 가죽"
    },

    # 옷감 가공 시설 (Cloth)
    "옷감": {
        "facility": "옷감 가공 시설",
        "produced": 3,
        "ingredients": {"양털": 10},
        "tier": 1,
        "display_name": "옷감"
    },
    "옷감+": {
        "facility": "옷감 가공 시설",
        "produced": 3,
        "ingredients": {"옷감": 3, "양털": 4},
        "tier": 2,
        "display_name": "옷감+"
    },
    "상급 옷감": {
        "facility": "옷감 가공 시설",
        "produced": 3,
        "ingredients": {"옷감+": 3, "상급 양털": 15, "양털": 8},
        "tier": 3,
        "display_name": "상급 옷감"
    },
    "상급 옷감+": {
        "facility": "옷감 가공 시설",
        "produced": 3,
        "ingredients": {"상급 옷감": 4, "상급 양털+": 20, "양털": 12},
        "tier": 4,
        "display_name": "상급 옷감+"
    },
    "최상급 옷감": {
        "facility": "옷감 가공 시설",
        "produced": 3,
        "ingredients": {"상급 옷감+": 5, "최상급 양털": 20, "양털": 16},
        "tier": 5,
        "display_name": "최상급 옷감"
    },
    "최상급 옷감+": {
        "facility": "옷감 가공 시설",
        "produced": 3,
        "ingredients": {"최상급 옷감": 5, "최상급 양털+": 20, "양털": 20},
        "tier": 6,
        "display_name": "최상급 옷감+"
    },
    "특급 옷감": {
        "facility": "옷감 가공 시설",
        "produced": 3,
        "ingredients": {"최상급 옷감+": 5, "특급 양털": 20, "양털": 20},
        "tier": 7,
        "display_name": "특급 옷감"
    },
    "두꺼운 옷감": {
        "facility": "옷감 가공 시설",
        "produced": 3,
        "ingredients": {"두꺼운 양털": 50, "상급 양털": 100, "밀랍": 2},
        "tier": 3,
        "display_name": "두꺼운 옷감"
    },

    # 실크 (Silk)
    "실크": {
        "facility": "옷감 가공 시설",
        "produced": 2,
        "ingredients": {"거미줄": 10},
        "tier": 1,
        "display_name": "실크"
    },
    "상급 실크": {
        "facility": "옷감 가공 시설",
        "produced": 2,
        "ingredients": {"실크": 4, "튼튼 버섯 진액": 8},
        "tier": 3,
        "display_name": "상급 실크"
    },
    "최상급 실크": {
        "facility": "옷감 가공 시설",
        "produced": 2,
        "ingredients": {"상급 실크": 4, "최상급 거미줄": 20, "튼튼 버섯 진액": 16},
        "tier": 5,
        "display_name": "최상급 실크"
    },
    "특급 실크": {
        "facility": "옷감 가공 시설",
        "produced": 2,
        "ingredients": {"최상급 실크": 4, "특급 거미줄": 20, "튼튼 버섯 진액": 20},
        "tier": 7,
        "display_name": "특급 실크"
    },

    # 요리 및 기타 가공
    "밀가루": {
        "facility": "요리 시설",
        "produced": 3,
        "ingredients": {"밀": 10},
        "tier": 1,
        "display_name": "밀가루"
    },
    "면": {
        "facility": "요리 시설",
        "produced": 3,
        "ingredients": {"밀가루": 3, "달걀": 5, "물이 든 병": 1},
        "tier": 2,
        "display_name": "면"
    },
    "생크림": {
        "facility": "요리 시설",
        "produced": 3,
        "ingredients": {"달걀": 6, "설탕": 2},
        "tier": 1,
        "display_name": "생크림"
    },
    "물에 불린 콩": {
        "facility": "요리 시설",
        "produced": 3,
        "ingredients": {"콩": 30, "물이 든 병": 3},
        "tier": 1,
        "display_name": "물에 불린 콩"
    },
    "두부": {
        "facility": "요리 시설",
        "produced": 3,
        "ingredients": {"물에 불린 콩": 3, "물이 든 병": 3, "옷감": 3},
        "tier": 2,
        "display_name": "두부"
    },
    "두유": {
        "facility": "요리 시설",
        "produced": 3,
        "ingredients": {"물에 불린 콩": 3, "물이 든 병": 3, "식용유": 1},
        "tier": 2,
        "display_name": "두유"
    },
    "물에 불린 쌀": {
        "facility": "요리 시설",
        "produced": 3,
        "ingredients": {"쌀": 30, "물이 든 병": 6},
        "tier": 1,
        "display_name": "물에 불린 쌀"
    },
    "밥": {
        "facility": "요리 시설",
        "produced": 3,
        "ingredients": {"물에 불린 쌀": 8, "물이 든 병": 2},
        "tier": 2,
        "display_name": "밥"
    },
    "말린 찻잎": {
        "facility": "요리 시설",
        "produced": 3,
        "ingredients": {"찻잎": 30},
        "tier": 1,
        "display_name": "말린 찻잎"
    },
    "발효된 찻잎": {
        "facility": "요리 시설",
        "produced": 3,
        "ingredients": {"말린 찻잎": 5, "옷감": 1},
        "tier": 2,
        "display_name": "발효된 찻잎"
    },
    "오트밀": {
        "facility": "요리 시설",
        "produced": 3,
        "ingredients": {"귀리": 30},
        "tier": 1,
        "display_name": "오트밀"
    },
    "마요네즈": {
        "facility": "요리 시설",
        "produced": 3,
        "ingredients": {"달걀": 3, "식용유": 3},
        "tier": 1,
        "display_name": "마요네즈"
    },

    # 약품 / 결정
    "마력 기폭제": {
        "facility": "약품 가공 시설",
        "produced": 3,
        "ingredients": {"마력 깃든 돌": 50, "마나 허브": 50, "반짝이는 이끼": 25},
        "tier": 3,
        "display_name": "마력 기폭제"
    },
}

KNOWN_GATHERABLE_ITEMS: Set[str] = {
    # 통나무 계열 (일반 및 + 재료 명확 구분)
    "통나무", "단단한 통나무", "부드러운 통나무", "상급 통나무", "상급 통나무+", "최상급 통나무",
    "최상급 통나무+", "특급 통나무", "나뭇가지", "나무 진액", "황금 나뭇가지", "벼락 맞은 나뭇가지",
    # 광석 계열
    "철 광석", "광석", "동 광석", "백동 광석", "은 광석", "운철 광석", "백금 광석", "석탄",
    # 가죽 및 양털 계열 (양털, 상급 양털, 최상급 양털 및 + 등급별 세분화)
    "생가죽", "상급 생가죽", "상급 생가죽+", "최상급 생가죽", "최상급 생가죽+", "특급 생가죽", "타닌 가루",
    "양털", "두꺼운 양털", "상급 양털", "상급 양털+", "최상급 양털", "최상급 양털+", "특급 양털", "황금 양털", "황금 양털+",
    # 거미줄
    "거미줄", "황금 거미줄", "최상급 거미줄", "특급 거미줄",
    # 허브 및 농작물/채집물
    "마나 허브", "블러디 허브", "허브", "마력 깃든 돌", "반짝이는 이끼", "황금 이끼",
    "사과", "황금 사과", "우유", "황금 우유", "양파", "옥수수", "달걀", "황금 달걀", "감자", "묵직한 감자",
    "콩", "쌀", "귀리", "밀", "긴 줄기", "황금 줄기", "찻잎", "헤이즐넛", "황금 헤이즐넛",
    "숨숨꽃", "생채기꽃", "화살꽃", "새초롱꽃", "라벤더 꽃", "진정초", "끈적 풀", "끈기 풀", "파스닙", "양배추", "호박",
    "파란 수국", "얼음", "조개", "점토", "돌멩이", "물이 든 병", "빈 병",
    # 버섯 계열
    "새록 버섯", "새록 버섯 포자", "튼튼 버섯", "튼튼 버섯 포자",
    "쑥쑥 버섯", "쑥쑥 버섯 포자", "깔끔 버섯", "깔끔 버섯 포자", "증폭 버섯", "증폭 버섯 포자",
    "솔솔 버섯", "솔솔 버섯 포자", "산뜻 버섯", "산뜻 버섯 포자",
    # 기타 데코 및 부품
    "데코 제작 부품", "데코 제작 부품+", "상급 데코 제작 부품",
}

# --- 양털 계열 다단계 채집 및 종별 드롭 특성 정의 ---
# 중요: 인게임 CLI execute_gathering({"displayName": ...})은 몬스터 이름을 직접 지정할 수 없으며,
# 반드시 채집할 '아이템 이름'을 그대로 전달해야 해당 아이템이 드롭되는 양을 채집합니다.
# - 주의: 먹구름 양(Lv.25, 상급 양털+/최상급 양털)은 '상급 양털'이나 '양털'을 드롭하지 않습니다!
#   따라서 '상급 양털' 채집 시 gather_target을 '상급 양털+'로 바꾸거나 먹구름 양으로 폴백하면 상급 양털을 1개도 얻을 수 없습니다.
# - '상급 양털'은 반드시 gather_target="상급 양털"로 호출해야 합니다 (인게임 엔진이 주변의 곱슬 양/일반 양 중 가까운 대상을 자동 선택).
WOOL_SPECIES_DATA: Dict[str, Dict[str, Any]] = {
    "최상급 양털+": {
        "species": "먹구름 양",
        "tier": 4,
        "gather_target": "최상급 양털+",
        "primary_drop": "최상급 양털+",
        "desc": "먹구름 양 (최상급 양털+/최상급 양털/상급 양털+ 드롭)"
    },
    "최상급 양털": {
        "species": "먹구름 양",
        "tier": 3,
        "gather_target": "최상급 양털",
        "primary_drop": "최상급 양털",
        "desc": "먹구름 양 (최상급 양털/상급 양털+/두꺼운 양털 드롭, 상급 양털 미드롭)"
    },
    "상급 양털+": {
        "species": "먹구름 양/곱슬 양",
        "tier": 3,
        "gather_target": "상급 양털+",
        "primary_drop": "상급 양털+",
        "desc": "먹구름 양 및 곱슬 양 (상급 양털+ 다량 드롭)"
    },
    "두꺼운 양털": {
        "species": "곱슬 양/먹구름 양",
        "tier": 2,
        "gather_target": "두꺼운 양털",
        "primary_drop": "두꺼운 양털",
        "desc": "곱슬 양 및 먹구름 양 (두꺼운 양털 드롭)"
    },
    "상급 양털": {
        "species": "곱슬 양/일반 양",
        "tier": 2,
        "gather_target": "상급 양털",
        "primary_drop": "상급 양털",
        "desc": "곱슬 양 및 일반 양 (상급 양털 드롭 - 먹구름 양은 상급 양털을 드롭하지 않음)"
    },
    "양털": {
        "species": "일반 양",
        "tier": 1,
        "gather_target": "양털",
        "primary_drop": "양털",
        "desc": "일반 양 (양털 다량 드롭)"
    }
}

WOOL_FAMILY_ITEMS: Set[str] = {
    "양털", "상급 양털", "상급 양털+", "최상급 양털", "최상급 양털+", "두꺼운 양털", "황금 양털", "황금 양털+", "특급 양털"
}

def is_wool_family(item_name: str) -> bool:
    clean = item_name.split("(")[0].strip()
    return clean in WOOL_FAMILY_ITEMS or "양털" in clean

def get_wool_gather_candidates(target_item: str, preference: str = "drop_rate") -> List[Dict[str, Any]]:
    """
    목표 양털 아이템을 실제로 드롭하는 정확한 채집 타겟 목록을 반환합니다.
    - 양털: gather_target='양털' (일반 양)
    - 상급 양털: gather_target='상급 양털' (곱슬 양/일반 양 — 먹구름 양은 상급 양털을 드롭하지 않으므로 제외!)
    - 두꺼운 양털: gather_target='두꺼운 양털' (곱슬 양/먹구름 양)
    - 상급 양털+: gather_target='상급 양털+' (먹구름 양/곱슬 양)
    - 최상급 양털: gather_target='최상급 양털' (먹구름 양)
    """
    cloud_plus = WOOL_SPECIES_DATA["최상급 양털+"]
    cloud = WOOL_SPECIES_DATA["최상급 양털"]
    high_plus = WOOL_SPECIES_DATA["상급 양털+"]
    thick = WOOL_SPECIES_DATA["두꺼운 양털"]
    high = WOOL_SPECIES_DATA["상급 양털"]
    normal = WOOL_SPECIES_DATA["양털"]

    clean = target_item.split("(")[0].strip()

    if clean == "양털":
        return [normal]
    elif clean == "상급 양털":
        # 먹구름 양(상급 양털+/최상급 양털)은 상급 양털을 드롭하지 않으므로 절대 후보에 넣지 않음
        return [high, normal]
    elif clean == "두꺼운 양털":
        return [thick, high]
    elif clean == "상급 양털+":
        return [high_plus, high]
    elif clean == "최상급 양털":
        return [cloud, high_plus]
    elif clean == "최상급 양털+":
        return [cloud_plus, cloud]
    else:
        return [{
            "species": clean,
            "tier": 4,
            "gather_target": clean,
            "primary_drop": clean,
            "desc": clean
        }]

# --- 벌목 및 통나무/나무 진액 계열 다단계 채집 및 종별 드롭 특성 정의 ---
WOOD_SPECIES_DATA: Dict[str, Dict[str, Any]] = {
    "최상급 통나무": {
        "species": "최상급 나무",
        "tier": 4,
        "gather_target": "최상급 통나무",
        "primary_drop": "최상급 통나무",
        "desc": "최상급 나무 (최상급 통나무 드롭)"
    },
    "상급 통나무+": {
        "species": "상급 나무+",
        "tier": 3,
        "gather_target": "상급 통나무+",
        "primary_drop": "상급 통나무+",
        "desc": "상급 나무+ (상급 통나무+ 다량 드롭)"
    },
    "상급 통나무": {
        "species": "상급 나무",
        "tier": 2,
        "gather_target": "상급 통나무",
        "primary_drop": "상급 통나무",
        "desc": "상급 나무 (상급 통나무 다량 드롭)"
    },
    "통나무": {
        "species": "굵은 나무",
        "tier": 1,
        "gather_target": "통나무",
        "primary_drop": "통나무",
        "desc": "굵은 나무 (통나무 다량 드롭, 나무 진액/나뭇가지 부가 드롭)"
    },
    "나무 진액": {
        "species": "뾰족 나무",
        "tier": 1,
        "gather_target": "나무 진액",
        "primary_drop": "나무 진액",
        "desc": "뾰족 나무 (나무 진액 다량 드롭, 통나무/나뭇가지 부가 드롭)"
    }
}

WOOD_FAMILY_ITEMS: Set[str] = {
    "통나무", "단단한 통나무", "부드러운 통나무", "상급 통나무", "상급 통나무+", 
    "최상급 통나무", "최상급 통나무+", "특급 통나무", "나무 진액", "나뭇가지", 
    "황금 나뭇가지", "벼락 맞은 나뭇가지"
}

def is_wood_family(item_name: str) -> bool:
    clean = item_name.split("(")[0].strip()
    return clean in WOOD_FAMILY_ITEMS or any(k in clean for k in ["통나무", "나무 진액", "나뭇가지"])

# 채집이나 시설 가공으로 얻을 수 없고 상점(식료품점/잡화점) 구매 또는 몬스터 사냥으로 확보해야 하는 재료 목록
KNOWN_NON_GATHERABLE_MATERIALS: Set[str] = {
    "소금", "설탕", "마요네즈", "밀랍", "후추", "식용유", "버터",
    "분해된 장비 부품", "연금술 부스러기", "협곡의 마나석", "마나석",
    "마력 기폭제", "마나 에센스"
}

def is_non_gatherable_material(item_name: str) -> bool:
    clean = item_name.split("(")[0].strip()
    return clean in KNOWN_NON_GATHERABLE_MATERIALS

def get_wood_gather_candidates(target_item: str, preference: str = "drop_rate") -> List[Dict[str, Any]]:
    """
    목표 목재/진액 아이템을 실제로 드롭하는 정확한 채집 타겟 목록을 반환합니다.
    """
    top_wood = WOOD_SPECIES_DATA["최상급 통나무"]
    cloud = WOOD_SPECIES_DATA["상급 통나무+"]
    high = WOOD_SPECIES_DATA["상급 통나무"]
    thick = WOOD_SPECIES_DATA["통나무"]    # 굵은 나무
    pointy = WOOD_SPECIES_DATA["나무 진액"] # 뾰족 나무

    clean = target_item.split("(")[0].strip()

    if "진액" in clean:
        return [pointy]
    elif clean == "통나무":
        return [thick, pointy]
    elif clean == "상급 통나무":
        return [high, thick]
    elif clean == "상급 통나무+":
        return [cloud, high]
    elif clean == "최상급 통나무":
        return [top_wood, cloud]
    else:
        return [{
            "species": clean,
            "tier": 3,
            "gather_target": clean,
            "primary_drop": clean,
            "desc": clean
        }]

FACILITY_MAP: Dict[str, str] = {
    # 금속
    "철괴": "금속 가공 시설", "합금강괴": "금속 가공 시설", "강철괴": "금속 가공 시설",
    "특수강괴": "금속 가공 시설", "백금강괴": "금속 가공 시설", "은합금괴": "금속 가공 시설",
    "운철괴": "금속 가공 시설",
    # 가죽
    "가죽": "가죽 가공 시설", "가죽+": "가죽 가공 시설", "상급 가죽": "가죽 가공 시설",
    "상급 가죽+": "가죽 가공 시설", "최상급 가죽": "가죽 가공 시설", "최상급 가죽+": "가죽 가공 시설",
    "특급 가죽": "가죽 가공 시설",
    # 목재
    "목재": "목재 가공 시설", "목재+": "목재 가공 시설", "상급 목재": "목재 가공 시설",
    "상급 목재+": "목재 가공 시설", "최상급 목재": "목재 가공 시설", "최상급 목재+": "목재 가공 시설",
    "특급 목재": "목재 가공 시설", "부드러운 목재": "목재 가공 시설", "단단한 목재": "목재 가공 시설",
    # 옷감
    "옷감": "옷감 가공 시설", "옷감+": "옷감 가공 시설", "두꺼운 옷감": "옷감 가공 시설",
    "상급 옷감": "옷감 가공 시설", "상급 옷감+": "옷감 가공 시설", "최상급 옷감": "옷감 가공 시설",
    "최상급 옷감+": "옷감 가공 시설", "특급 옷감": "옷감 가공 시설", "실크": "옷감 가공 시설",
    "상급 실크": "옷감 가공 시설", "최상급 실크": "옷감 가공 시설", "특급 실크": "옷감 가공 시설",
    # 약품
    "항마석 가루": "약품 가공 시설", "상급 항마석 가루": "약품 가공 시설",
}

def get_facility_for_material(name: str) -> str:
    if name in FACILITY_MAP:
        return FACILITY_MAP[name]
    if name in STANDARD_ALTER_RECIPES:
        return STANDARD_ALTER_RECIPES[name]["facility"]
    if any(k in name for k in ["괴", "철", "금속"]): return "금속 가공 시설"
    if any(k in name for k in ["가죽", "피혁"]): return "가죽 가공 시설"
    if any(k in name for k in ["목재", "원목", "통나무"]): return "목재 가공 시설"
    if any(k in name for k in ["옷감", "실크", "천", "실"]): return "옷감 가공 시설"
    if any(k in name for k in ["물약", "비약", "가루", "항마석"]): return "약품 가공 시설"
    return "가공 시설"

EQUIPMENT_KEYWORDS = [
    # 무기류
    "완드", "소드", "검", "활", "크로스보우", "너클", "둔기", "도끼", "스태프", "랜스", "체인",
    "실린더", "가드실린더", "핸들", "듀얼건", "마도서", "표창", "아틀라틀", "방패",
    # 방어구 및 의복류
    "갑옷", "투구", "신발", "장갑", "로브", "의복", "전투복", "모자", "옷", "드레스", "슈트",
    "자켓", "코트", "팬츠", "스커트", "부츠", "건틀렛", "그리브",
    # 장신구 및 머리장식
    "서클릿", "헤어밴드", "안경", "베일", "티아라", "가발", "링", "목걸이", "귀걸이", "벨트",
    # 악기류
    "악기", "리라", "만돌린", "플루트", "샬루모", "휘슬", "튜바", "피아노", "드럼", "바이올린", "첼로",
    # 특수 완제품 (get_items에서 누락되는 완제품류)
    "키트", "캠프파이어"
]

def is_equipment_item(item_name: str) -> bool:
    """Returns True if item_name is an equipment/weapon/armor piece or special finished good."""
    if not item_name:
        return False
    return any(k in item_name for k in EQUIPMENT_KEYWORDS)

class DeliveryTask:
    def __init__(self, quest_title: str, item_name: str, current: int, goal: int, is_completed: bool):
        self.quest_title = quest_title
        self.item_name = item_name
        self.current = current
        self.goal = goal
        self.needed = max(0, goal - current)
        self.is_completed = is_completed

    def to_dict(self) -> Dict[str, Any]:
        return {
            "quest_title": self.quest_title,
            "item_name": self.item_name,
            "current": self.current,
            "goal": self.goal,
            "needed": self.needed,
            "is_completed": self.is_completed
        }

class DeliveryAlarmStore:
    """Manages persistent alarms specifically registered for weekly delivery tasks."""
    def __init__(self, filepath: str = ALARMS_FILE):
        self.filepath = filepath
        self.alarms: Dict[str, Dict[str, Any]] = {}
        self.load()

    def load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    self.alarms = json.load(f)
            except Exception:
                self.alarms = {}

    def save(self):
        try:
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(self.alarms, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def add_facility_alarm(
        self, 
        facility: str, 
        items_summary: str, 
        total_remaining_seconds: int, 
        source: str = "가공 알람",
        works_count: int = 1
    ) -> Dict[str, Any]:
        """
        Registers or updates a single alarm for a facility, setting the trigger time
        to the completion of the LAST work at this facility.
        """
        now_ms = int(time.time() * 1000)
        target_ms = now_ms + int(total_remaining_seconds * 1000)
        key = facility

        if key in self.alarms:
            existing = self.alarms[key]
            # Synchronize targetTimestamp to latest accurate completion
            if total_remaining_seconds > 0:
                existing["targetTimestamp"] = target_ms
                existing["notified"] = False
            elif existing.get("total_remaining_seconds", 0) != 0:
                existing["total_remaining_seconds"] = 0
                existing["targetTimestamp"] = min(existing.get("targetTimestamp", now_ms), now_ms)
            existing["item"] = items_summary
            existing["worksCount"] = works_count
            existing["source"] = source
            existing["total_remaining_seconds"] = total_remaining_seconds
            self.save()
            return existing

        data = {
            "key": key,
            "facility": facility,
            "item": items_summary,
            "targetTimestamp": target_ms,
            "total_remaining_seconds": total_remaining_seconds,
            "source": source,
            "worksCount": works_count,
            "isDeliveryTarget": (source == "주간 납품"),
            "notified": False,
            "createdAt": now_ms
        }
        self.alarms[key] = data
        self.save()
        return data

    def add_alarm(self, facility: str, item_name: str, remaining_seconds: int, source: str = "주간 납품") -> Dict[str, Any]:
        """Backward-compatible wrapper mapping to facility-based alarm."""
        return self.add_facility_alarm(facility, item_name, remaining_seconds, source=source)

    def sync_facility_alarms(self, cli, default_source: str = "가공 알람") -> List[Dict[str, Any]]:
        """
        Scans all in-progress altering works across all facilities and registers/updates
        exactly ONE alarm per facility pointing to when the LAST work finishes.
        """
        try:
            works_info = cli.get_altering_works()
            works = works_info.get("works", [])
        except Exception:
            return list(self.alarms.values())

        from collections import defaultdict
        facility_works = defaultdict(list)
        for w in works:
            fac = w.get("FacilityName")
            if fac:
                facility_works[fac].append(w)

        updated_alarms = []
        changed = False
        for fac, f_works in facility_works.items():
            incomplete = [w for w in f_works if not w.get("IsCompleted") and int(w.get("RemainingSeconds", 0)) > 0]
            if not incomplete:
                # All slots at this facility are finished and waiting to be collected
                if fac in self.alarms:
                    if self.alarms[fac].get("total_remaining_seconds") != 0:
                        self.alarms[fac]["total_remaining_seconds"] = 0
                        self.alarms[fac]["targetTimestamp"] = min(self.alarms[fac].get("targetTimestamp", int(time.time() * 1000)), int(time.time() * 1000))
                        changed = True
                    updated_alarms.append(self.alarms[fac])
                continue

            # Sequential slots at the same facility: sum of RemainingSeconds of all incomplete works
            total_sec = sum(int(w.get("RemainingSeconds", 0)) for w in incomplete)
            item_names = list(dict.fromkeys(w.get("DisplayName", "") for w in incomplete if w.get("DisplayName")))
            if len(item_names) == 1:
                summary = f"{item_names[0]} ({len(incomplete)}슬롯)"
            else:
                summary = f"{item_names[0]} 외 {len(incomplete)-1}건 ({len(incomplete)}슬롯)"

            al = self.add_facility_alarm(
                facility=fac,
                items_summary=summary,
                total_remaining_seconds=total_sec,
                source=default_source,
                works_count=len(incomplete)
            )
            updated_alarms.append(al)

        # Clean up alarms for facilities that have no works left at all (collected)
        for k in list(self.alarms.keys()):
            if k not in facility_works:
                del self.alarms[k]
                changed = True

        if changed:
            self.save()

        return updated_alarms

    def delete_alarm(self, key: str):
        if key in self.alarms:
            del self.alarms[key]
            self.save()

    def clear(self):
        self.alarms = {}
        self.save()

    def get_all(self) -> List[Dict[str, Any]]:
        return list(self.alarms.values())

alarm_store = DeliveryAlarmStore()

def _extract_stat_int(val: Any) -> int:
    if isinstance(val, dict):
        val = val.get("Value", 0)
    try:
        return int(float(val or 0))
    except Exception:
        return 0


def extract_character_signature(char_info: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Extracts a comparable character fingerprint from CLI get_my_info() output."""
    if not char_info or not isinstance(char_info, dict):
        return None
    realm = str(char_info.get("RealmName") or "").strip()
    level = _extract_stat_int(char_info.get("Level"))
    job = str(
        char_info.get("EnabledCombatJobDisplayName")
        or char_info.get("CombatJobDisplayName")
        or char_info.get("JobDisplayName")
        or char_info.get("CombatJob")
        or char_info.get("Job")
        or ""
    ).strip()
    title = str(char_info.get("Title") or "").strip()
    living_score = _extract_stat_int(char_info.get("LivingScore"))
    combat_score = _extract_stat_int(char_info.get("CombatScore"))
    attractiveness_score = _extract_stat_int(char_info.get("AttractivenessScore"))
    if level <= 0 and not job and not realm:
        return None
    return {
        "realm": realm,
        "level": level,
        "job": job,
        "title": title,
        "living_score": living_score,
        "combat_score": combat_score,
        "attractiveness_score": attractiveness_score,
    }


def format_signature_label(sig: Optional[Dict[str, Any]]) -> str:
    if not sig or not isinstance(sig, dict):
        return "인게임 미연동"
    realm = sig.get("realm") or ""
    job = sig.get("job") or "캐릭터"
    level = int(sig.get("level") or 0)
    living = int(sig.get("living_score") or 0)
    parts = []
    if realm:
        parts.append(realm)
    parts.append(f"{job} Lv.{level}" if level > 0 else job)
    if living > 0:
        parts.append(f"생활력 {living:,}")
    return " · ".join(parts)


def is_same_character_signature(sig_a: Optional[Dict[str, Any]], sig_b: Optional[Dict[str, Any]]) -> bool:
    """Checks whether two character signatures represent the same character."""
    if not sig_a or not sig_b or not isinstance(sig_a, dict) or not isinstance(sig_b, dict):
        return False
    realm_a = str(sig_a.get("realm") or "").strip()
    realm_b = str(sig_b.get("realm") or "").strip()
    if realm_a and realm_b and realm_a != realm_b:
        return False

    lvl_a = int(sig_a.get("level") or 0)
    lvl_b = int(sig_b.get("level") or 0)
    if lvl_a > 0 and lvl_b > 0 and lvl_a != lvl_b:
        return False

    job_a = str(sig_a.get("job") or "").strip()
    job_b = str(sig_b.get("job") or "").strip()
    ls_a = int(sig_a.get("living_score") or 0)
    ls_b = int(sig_b.get("living_score") or 0)
    att_a = int(sig_a.get("attractiveness_score") or 0)
    att_b = int(sig_b.get("attractiveness_score") or 0)

    # Same job + same level + close living score (allow up to 3000 growth between sessions)
    if job_a and job_b and job_a == job_b:
        if ls_a > 0 and ls_b > 0:
            return abs(ls_a - ls_b) <= 3000
        return True

    # If the user changed combat class on the same character, living_score + attractiveness_score + level stay identical
    if ls_a > 0 and ls_b > 0 and abs(ls_a - ls_b) <= 150:
        if att_a > 0 and att_b > 0 and abs(att_a - att_b) <= 50:
            return True

    return False


KST_TZ = timezone(timedelta(hours=9))


def get_current_week_key(now_dt: Optional[datetime] = None) -> str:
    """
    Returns the Monday date string ('YYYY-MM-DD') for the current weekly cycle,
    where each weekly cycle starts on Monday at 06:00 AM KST (UTC+9).
    """
    if now_dt is None:
        now_kst = datetime.now(KST_TZ)
    elif now_dt.tzinfo is None:
        now_kst = now_dt.replace(tzinfo=KST_TZ)
    else:
        now_kst = now_dt.astimezone(KST_TZ)
    shifted = now_kst - timedelta(hours=6)
    monday = shifted - timedelta(days=shifted.weekday())
    return monday.strftime("%Y-%m-%d")


class RegisteredDeliveryStore:
    """Manages persistent weekly delivery target items registered by the user (supports multi-character profiles)."""
    def __init__(self, filepath: str = DELIVERY_TARGETS_FILE, multi_character: bool = True):
        self.filepath = filepath
        self.multi_character = multi_character
        self._flat_targets: Dict[str, Dict[str, Any]] = {}
        self.characters: Dict[str, Dict[str, Any]] = {}
        self.active_character_id: str = "char_1"
        self.auto_switch_character: bool = True
        self.last_ingame_signature: Optional[Dict[str, Any]] = None
        self.matched_ingame_character_id: Optional[str] = None
        self._lock = threading.RLock()
        self.load()

    @property
    def targets(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            if not self.multi_character:
                return self._flat_targets
            self._ensure_active_character_locked()
            return self.characters[self.active_character_id]["targets"]

    @targets.setter
    def targets(self, value: Dict[str, Dict[str, Any]]):
        with self._lock:
            if not self.multi_character:
                self._flat_targets = value if isinstance(value, dict) else {}
            else:
                self._ensure_active_character_locked()
                self.characters[self.active_character_id]["targets"] = value if isinstance(value, dict) else {}

    def _check_weekly_reset_locked(self) -> bool:
        """
        Resets weekly_delivered completion marks across all characters if the Monday 06:00 AM KST
        boundary has passed (i.e. delivered_week_key != get_current_week_key()).
        """
        if not self.multi_character or not self.characters:
            return False
        cur_week = get_current_week_key()
        changed = False
        for cinfo in self.characters.values():
            if cinfo.get("weekly_delivered"):
                if cinfo.get("delivered_week_key") != cur_week:
                    cinfo["weekly_delivered"] = False
                    cinfo["delivered_week_key"] = None
                    cinfo["delivered_at"] = None
                    changed = True
        return changed

    def _ensure_active_character_locked(self):
        if not self.characters:
            self.characters["char_1"] = {
                "id": "char_1",
                "name": "캐릭터 1",
                "signature": None,
                "weekly_delivered": False,
                "delivered_week_key": None,
                "delivered_at": None,
                "targets": self._build_default_18_targets(),
                "created_at": int(time.time() * 1000),
            }
            self.active_character_id = "char_1"
        elif self.active_character_id not in self.characters:
            self.active_character_id = next(iter(self.characters.keys()))
        if self._check_weekly_reset_locked():
            try:
                payload = {
                    "version": 2,
                    "active_character_id": self.active_character_id,
                    "auto_switch_character": self.auto_switch_character,
                    "characters": self.characters,
                }
                with open(self.filepath, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

    def _build_default_18_targets(self) -> Dict[str, Dict[str, Any]]:
        """Loads the default 18 weekly delivery items from bundled delivery_presets.json."""
        result: Dict[str, Dict[str, Any]] = {}
        bundled_preset = os.path.join(SCRIPT_DIR, "delivery_presets.json")
        if os.path.exists(bundled_preset):
            try:
                with open(bundled_preset, "r", encoding="utf-8") as f:
                    presets = json.load(f)
                    preset_data = presets.get("주간 납품") or (next(iter(presets.values())) if presets else None)
                    if preset_data and "items" in preset_data:
                        now_ms = int(time.time() * 1000)
                        for item in preset_data["items"]:
                            name = item.get("item_name")
                            goal = int(item.get("goal", 1))
                            qtitle = item.get("quest_title", f"{name} 구하기")
                            if name:
                                result[name] = {
                                    "quest_title": qtitle,
                                    "item_name": name,
                                    "goal": goal,
                                    "current": 0,
                                    "needed": goal,
                                    "is_completed": False,
                                    "override_current": None,
                                    "updated_at": now_ms,
                                }
            except Exception:
                pass
        return result

    def load(self):
        with self._lock:
            if not self.multi_character:
                loaded = False
                if os.path.exists(self.filepath):
                    try:
                        with open(self.filepath, "r", encoding="utf-8") as f:
                            data = json.load(f)
                            if isinstance(data, dict):
                                self._flat_targets = data
                                loaded = True
                    except Exception:
                        self._flat_targets = {}
                if not loaded:
                    self._flat_targets = {}
                return

            loaded = False
            if os.path.exists(self.filepath):
                try:
                    with open(self.filepath, "r", encoding="utf-8") as f:
                        raw = json.load(f)
                    if isinstance(raw, dict):
                        if "characters" in raw and isinstance(raw.get("characters"), dict) and raw["characters"]:
                            # v2 multi-character schema
                            self.characters = {}
                            for cid, cinfo in raw["characters"].items():
                                if not isinstance(cinfo, dict):
                                    continue
                                t_dict = cinfo.get("targets")
                                if not isinstance(t_dict, dict):
                                    t_dict = {}
                                self.characters[cid] = {
                                    "id": cid,
                                    "name": str(cinfo.get("name") or cid).strip() or cid,
                                    "signature": cinfo.get("signature") if isinstance(cinfo.get("signature"), dict) else None,
                                    "weekly_delivered": bool(cinfo.get("weekly_delivered", False)),
                                    "delivered_week_key": cinfo.get("delivered_week_key"),
                                    "delivered_at": cinfo.get("delivered_at"),
                                    "targets": t_dict,
                                    "created_at": int(cinfo.get("created_at") or int(time.time() * 1000)),
                                }
                            self.active_character_id = str(raw.get("active_character_id") or "")
                            self.auto_switch_character = bool(raw.get("auto_switch_character", True))
                            self._ensure_active_character_locked()
                            loaded = True
                        elif raw:
                            # Migrate v1 flat targets dict -> v2 under char_1
                            migrated_targets = {}
                            for k, v in raw.items():
                                if isinstance(v, dict) and "item_name" in v:
                                    migrated_targets[k] = v
                            if migrated_targets:
                                self.characters = {
                                    "char_1": {
                                        "id": "char_1",
                                        "name": "캐릭터 1",
                                        "signature": None,
                                        "weekly_delivered": False,
                                        "delivered_week_key": None,
                                        "delivered_at": None,
                                        "targets": migrated_targets,
                                        "created_at": int(time.time() * 1000),
                                    }
                                }
                                self.active_character_id = "char_1"
                                self.auto_switch_character = True
                                loaded = True
                                self.save()
                except Exception:
                    self.characters = {}

            if not loaded or not self.characters:
                self.characters = {
                    "char_1": {
                        "id": "char_1",
                        "name": "캐릭터 1",
                        "signature": None,
                        "weekly_delivered": False,
                        "delivered_week_key": None,
                        "delivered_at": None,
                        "targets": self._build_default_18_targets(),
                        "created_at": int(time.time() * 1000),
                    }
                }
                self.active_character_id = "char_1"
                self.auto_switch_character = True
                self.save()

    def save(self):
        with self._lock:
            try:
                if not self.multi_character:
                    with open(self.filepath, "w", encoding="utf-8") as f:
                        json.dump(self._flat_targets, f, ensure_ascii=False, indent=2)
                else:
                    self._ensure_active_character_locked()
                    payload = {
                        "version": 2,
                        "active_character_id": self.active_character_id,
                        "auto_switch_character": self.auto_switch_character,
                        "characters": self.characters,
                    }
                    with open(self.filepath, "w", encoding="utf-8") as f:
                        json.dump(payload, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

    # --- Multi-Character Profile Management ---

    def _find_matching_character_id_locked(self, sig: Optional[Dict[str, Any]]) -> Optional[str]:
        if not sig:
            return None
        for cid, cinfo in self.characters.items():
            csig = cinfo.get("signature")
            if csig and is_same_character_signature(csig, sig):
                return cid
        return None

    def is_active_character_ingame(self) -> bool:
        """
        Returns True when the currently selected character tab corresponds to the
        logged-in character in-game (or when no in-game mismatch exists).
        Returns False when viewing another character's tab so its stored delivery counts
        are not overwritten by the logged-in character's bag inventory.
        """
        with self._lock:
            if not self.multi_character:
                return True
            self._ensure_active_character_locked()
            if not self.last_ingame_signature:
                return True
            if self.matched_ingame_character_id:
                return self.active_character_id == self.matched_ingame_character_id
            active_char = self.characters.get(self.active_character_id, {})
            # If the current in-game character is unlinked, allow live updates only if active tab is also unlinked
            return active_char.get("signature") is None

    def on_ingame_character_seen(self, char_info: Optional[Dict[str, Any]], allow_auto_switch: bool = True) -> Optional[str]:
        """
        Called when CLI get_my_info() returns character data.
        Updates matched_ingame_character_id, auto-links if only 1 unlinked character exists,
        and auto-switches active_character_id if the in-game character changed.
        Returns the newly switched character ID if an auto-switch happened, else None.
        """
        with self._lock:
            if not self.multi_character:
                return None
            sig = extract_character_signature(char_info)
            if not sig:
                return None
            self._ensure_active_character_locked()

            prev_sig = self.last_ingame_signature
            ingame_changed = (prev_sig is None) or (not is_same_character_signature(prev_sig, sig))
            self.last_ingame_signature = sig

            matched_id = self._find_matching_character_id_locked(sig)

            # Auto-bind initial single character if it has no signature yet
            if matched_id is None and len(self.characters) == 1:
                only_cid = next(iter(self.characters.keys()))
                if self.characters[only_cid].get("signature") is None:
                    self.characters[only_cid]["signature"] = sig
                    if self.characters[only_cid].get("name") == "캐릭터 1" and sig.get("job"):
                        self.characters[only_cid]["name"] = f"캐릭터 1 ({sig['job']})"
                    matched_id = only_cid
                    self.save()

            if matched_id and matched_id in self.characters:
                # Keep signature stats fresh (e.g. slight living_score growth)
                if self.characters[matched_id].get("signature") != sig:
                    self.characters[matched_id]["signature"] = sig
                    self.save()

            self.matched_ingame_character_id = matched_id

            if allow_auto_switch and self.auto_switch_character and matched_id and ingame_changed:
                if self.active_character_id != matched_id:
                    self.active_character_id = matched_id
                    self.save()
                    return matched_id
            return None

    def get_characters_summary(self) -> Dict[str, Any]:
        with self._lock:
            self._ensure_active_character_locked()
            cur_week = get_current_week_key()
            char_list = []
            total_chars = len(self.characters)
            completed_chars = 0
            weekly_delivered_chars = 0
            overall_total_items = 0
            overall_completed_items = 0

            for cid, cinfo in self.characters.items():
                t_map = cinfo.get("targets") or {}
                total_cnt = len(t_map)
                comp_cnt = 0
                for t in t_map.values():
                    goal = int(t.get("goal", 1))
                    cur = int(t.get("current", 0))
                    if t.get("is_completed") or cur >= goal:
                        comp_cnt += 1
                is_all_done = (total_cnt > 0 and comp_cnt >= total_cnt)
                if is_all_done:
                    completed_chars += 1
                is_weekly_delivered = bool(cinfo.get("weekly_delivered", False)) and (cinfo.get("delivered_week_key") == cur_week)
                if is_weekly_delivered:
                    weekly_delivered_chars += 1
                overall_total_items += total_cnt
                overall_completed_items += comp_cnt

                sig = cinfo.get("signature")
                char_list.append({
                    "id": cid,
                    "name": cinfo.get("name", cid),
                    "signature": sig,
                    "signature_label": format_signature_label(sig),
                    "is_active": (cid == self.active_character_id),
                    "is_ingame": (cid == self.matched_ingame_character_id),
                    "completed_count": comp_cnt,
                    "total_count": total_cnt,
                    "is_all_completed": is_all_done,
                    "weekly_delivered": is_weekly_delivered,
                    "delivered_week_key": cinfo.get("delivered_week_key"),
                    "delivered_at": cinfo.get("delivered_at"),
                })

            active_char = self.characters.get(self.active_character_id, {})
            active_delivered = bool(active_char.get("weekly_delivered", False)) and (active_char.get("delivered_week_key") == cur_week)
            return {
                "active_character_id": self.active_character_id,
                "active_character_name": active_char.get("name", "캐릭터 1"),
                "active_weekly_delivered": active_delivered,
                "current_week_key": cur_week,
                "auto_switch_character": self.auto_switch_character,
                "matched_ingame_character_id": self.matched_ingame_character_id,
                "is_active_ingame": self.is_active_character_ingame(),
                "last_ingame_signature": self.last_ingame_signature,
                "last_ingame_label": format_signature_label(self.last_ingame_signature) if self.last_ingame_signature else "",
                "characters": char_list,
                "overall": {
                    "total_characters": total_chars,
                    "completed_characters": completed_chars,
                    "weekly_delivered_characters": weekly_delivered_chars,
                    "total_items": overall_total_items,
                    "completed_items": overall_completed_items,
                },
            }

    def select_character(self, char_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            self._ensure_active_character_locked()
            if char_id not in self.characters:
                return None
            self.active_character_id = char_id
            self.save()
            return self.characters[char_id]

    def add_character(
        self,
        name: Optional[str] = None,
        init_mode: str = "default_18",
        link_current_ingame: bool = False,
        ingame_char_info: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            self._ensure_active_character_locked()
            idx = len(self.characters) + 1
            while f"char_{idx}" in self.characters:
                idx += 1
            new_id = f"char_{idx}"

            sig = None
            if link_current_ingame:
                sig = extract_character_signature(ingame_char_info) or self.last_ingame_signature
                if sig:
                    for c in self.characters.values():
                        if c.get("signature") and is_same_character_signature(c.get("signature"), sig):
                            c["signature"] = None

            clean_name = (name or "").strip()
            if not clean_name:
                if sig and sig.get("job"):
                    clean_name = f"캐릭터 {idx} ({sig['job']})"
                else:
                    clean_name = f"캐릭터 {idx}"

            cur_active_targets = self.characters.get(self.active_character_id, {}).get("targets", {})
            if init_mode == "copy_current":
                now_ms = int(time.time() * 1000)
                new_targets = {}
                for k, v in cur_active_targets.items():
                    goal = int(v.get("goal", 1))
                    new_targets[k] = {
                        "quest_title": v.get("quest_title", "주간 납품"),
                        "item_name": v.get("item_name", k),
                        "goal": goal,
                        "current": 0,
                        "needed": goal,
                        "is_completed": False,
                        "override_current": None if link_current_ingame else 0,
                        "updated_at": now_ms,
                    }
            elif init_mode == "empty":
                new_targets = {}
            else:
                new_targets = self._build_default_18_targets()
                # Preserve the user's preferred card ordering if customized, while keeping counts independent (0)
                if cur_active_targets:
                    ordered_new: Dict[str, Dict[str, Any]] = {}
                    for k in cur_active_targets.keys():
                        if k in new_targets:
                            ordered_new[k] = new_targets[k]
                    for k, v in new_targets.items():
                        if k not in ordered_new:
                            ordered_new[k] = v
                    new_targets = ordered_new
                if not link_current_ingame:
                    for t in new_targets.values():
                        t["override_current"] = 0

            self.characters[new_id] = {
                "id": new_id,
                "name": clean_name,
                "signature": sig,
                "weekly_delivered": False,
                "delivered_week_key": None,
                "delivered_at": None,
                "targets": new_targets,
                "created_at": int(time.time() * 1000),
            }
            self.active_character_id = new_id
            if sig:
                self.matched_ingame_character_id = new_id
            self.save()
            return self.characters[new_id]

    def rename_character(self, char_id: str, new_name: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            self._ensure_active_character_locked()
            target_id = char_id or self.active_character_id
            if target_id not in self.characters:
                return None
            clean = (new_name or "").strip()
            if not clean:
                return None
            self.characters[target_id]["name"] = clean
            self.save()
            return self.characters[target_id]

    def delete_character(self, char_id: str) -> Tuple[bool, str]:
        with self._lock:
            self._ensure_active_character_locked()
            target_id = char_id or self.active_character_id
            if target_id not in self.characters:
                return False, "해당 캐릭터 프로필을 찾을 수 없습니다."
            if len(self.characters) <= 1:
                return False, "최소 1개의 캐릭터 프로필은 유지되어야 합니다."
            deleted_name = self.characters[target_id].get("name", target_id)
            del self.characters[target_id]
            if self.matched_ingame_character_id == target_id:
                self.matched_ingame_character_id = None
            self._ensure_active_character_locked()
            self.save()
            return True, f"'{deleted_name}' 캐릭터 프로필이 삭제되었습니다."

    def link_character_to_ingame(self, char_id: Optional[str] = None, char_info: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
        with self._lock:
            self._ensure_active_character_locked()
            target_id = char_id or self.active_character_id
            if target_id not in self.characters:
                return False, "연동할 캐릭터 탭을 찾을 수 없습니다."
            sig = extract_character_signature(char_info) or self.last_ingame_signature
            if not sig:
                return False, "현재 인게임 캐릭터 정보를 가져올 수 없습니다. 게임에 접속 중인지 확인해주세요."

            # Unlink from any other character that had the same signature
            for cid, cinfo in self.characters.items():
                if cid != target_id and cinfo.get("signature") and is_same_character_signature(cinfo.get("signature"), sig):
                    cinfo["signature"] = None

            self.characters[target_id]["signature"] = sig
            self.last_ingame_signature = sig
            self.matched_ingame_character_id = target_id
            # Clear initial 0 overrides if any so live inventory immediately syncs
            for t in self.characters[target_id].get("targets", {}).values():
                if t.get("override_current") == 0 and int(t.get("current", 0)) == 0:
                    t["override_current"] = None
            self.save()
            label = format_signature_label(sig)
            return True, f"'{self.characters[target_id]['name']}' 프로필이 현재 접속 중인 캐릭터({label})와 연동되었습니다."

    def set_auto_switch(self, enabled: bool) -> bool:
        with self._lock:
            self.auto_switch_character = bool(enabled)
            self.save()
            return self.auto_switch_character

    def complete_character_delivery(
        self,
        char_id: Optional[str] = None,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Marks the target character's weekly delivery as completed for the current Monday 06:00 AM KST week,
        and deducts each target item's goal quantity from that character's current owned quantity (max(0, current - goal)).
        """
        with self._lock:
            self._ensure_active_character_locked()
            target_id = char_id or self.active_character_id
            if target_id not in self.characters:
                return False, "납품 완료 처리할 캐릭터 프로필을 찾을 수 없습니다.", {}

            cinfo = self.characters[target_id]
            char_name = cinfo.get("name", target_id)
            t_map = cinfo.get("targets") or {}
            now_ms = int(time.time() * 1000)
            cur_week = get_current_week_key()

            deducted_items = []
            for item_key, t in t_map.items():
                item_name = t.get("item_name", item_key)
                goal = max(1, int(t.get("goal", 1)))
                old_cur = max(0, int(t.get("current", 0)))
                new_cur = max(0, old_cur - goal)
                deducted_amt = old_cur - new_cur

                t["current"] = new_cur
                t["needed"] = max(0, goal - new_cur)
                t["is_completed"] = (new_cur >= goal)
                t["override_current"] = new_cur
                t["inventory_count"] = new_cur
                t["updated_at"] = now_ms
                t_map[item_key] = t

                deducted_items.append({
                    "item_name": item_name,
                    "goal": goal,
                    "before": old_cur,
                    "deducted": deducted_amt,
                    "after": new_cur,
                })

            cinfo["weekly_delivered"] = True
            cinfo["delivered_week_key"] = cur_week
            cinfo["delivered_at"] = now_ms
            self.save()

            msg = (
                f"[{char_name}] 주간 납품 완료 처리됨! "
                f"(총 {len(deducted_items)}개 품목 목표 수량만큼 보유 수량 차감 완료 · 월요일 오전 6시 초기화)"
            )
            return True, msg, {
                "character_id": target_id,
                "character_name": char_name,
                "week_key": cur_week,
                "deducted_count": len(deducted_items),
                "deducted_items": deducted_items,
            }

    def set_character_weekly_delivered(self, char_id: Optional[str] = None, delivered: bool = False) -> Tuple[bool, str]:
        """Sets or clears the weekly_delivered badge on a character without modifying item counts."""
        with self._lock:
            self._ensure_active_character_locked()
            target_id = char_id or self.active_character_id
            if target_id not in self.characters:
                return False, "해당 캐릭터 프로필을 찾을 수 없습니다."
            cinfo = self.characters[target_id]
            char_name = cinfo.get("name", target_id)
            if delivered:
                cinfo["weekly_delivered"] = True
                cinfo["delivered_week_key"] = get_current_week_key()
                cinfo["delivered_at"] = int(time.time() * 1000)
                msg = f"'{char_name}' 캐릭터에 주간 납품 완료 마크를 표시했습니다."
            else:
                cinfo["weekly_delivered"] = False
                cinfo["delivered_week_key"] = None
                cinfo["delivered_at"] = None
                msg = f"'{char_name}' 캐릭터의 주간 납품 완료 마크가 해제되었습니다."
            self.save()
            return True, msg

    def reorder_targets(self, ordered_names: List[str], sync_order_all_characters: bool = True) -> List[Dict[str, Any]]:
        """Reorders the registered targets dictionary according to ordered_names."""
        with self._lock:
            clean_order = [str(n).strip() for n in ordered_names if str(n).strip()]
            if not clean_order:
                return self.get_all()

            def _apply_order(t_dict: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
                new_dict: Dict[str, Dict[str, Any]] = {}
                key_lookup = {k.strip(): k for k in t_dict.keys()}
                for name in clean_order:
                    real_k = key_lookup.get(name)
                    if real_k is not None and real_k not in new_dict:
                        new_dict[real_k] = t_dict[real_k]
                for k, v in t_dict.items():
                    if k not in new_dict:
                        new_dict[k] = v
                return new_dict

            if not self.multi_character:
                self._flat_targets = _apply_order(self._flat_targets)
            else:
                self._ensure_active_character_locked()
                if sync_order_all_characters:
                    for cinfo in self.characters.values():
                        if isinstance(cinfo.get("targets"), dict):
                            cinfo["targets"] = _apply_order(cinfo["targets"])
                else:
                    cinfo = self.characters[self.active_character_id]
                    cinfo["targets"] = _apply_order(cinfo.get("targets", {}))
            self.save()
            return self.get_all()

    def move_target_position(
        self,
        from_item: str,
        to_item: str,
        mode: str = "swap",
        sync_order_all_characters: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Swaps or moves `from_item` relative to `to_item` in the targets list.
        mode='swap': exchanges the exact positions of from_item and to_item.
        mode='move': moves from_item to the index of to_item.
        """
        with self._lock:
            keys = [k.strip() for k in self.targets.keys()]
            f_clean = from_item.strip()
            t_clean = to_item.strip()
            if f_clean not in keys or t_clean not in keys or f_clean == t_clean:
                return self.get_all()
            idx_f = keys.index(f_clean)
            idx_t = keys.index(t_clean)
            if mode == "swap":
                keys[idx_f], keys[idx_t] = keys[idx_t], keys[idx_f]
            else:
                item = keys.pop(idx_f)
                keys.insert(idx_t, item)
            return self.reorder_targets(keys, sync_order_all_characters=sync_order_all_characters)

    def add_or_update(self, item_name: str, goal: int, current: int = 0, quest_title: str = "주간 납품") -> Dict[str, Any]:
        with self._lock:
            key = item_name.strip()
            needed = max(0, int(goal) - int(current))
            data = {
                "quest_title": quest_title,
                "item_name": key,
                "goal": int(goal),
                "current": int(current),
                "needed": needed,
                "is_completed": (int(current) >= int(goal)),
                "override_current": None,
                "updated_at": int(time.time() * 1000)
            }
            self.targets[key] = data
            self.save()
            return data

    def update_current(self, item_name: str, current: int) -> Optional[Dict[str, Any]]:
        """Directly sets the current owned count for item_name and recalculates completion status."""
        with self._lock:
            target_key = item_name.strip()
            found_key = None
            for k in self.targets.keys():
                if k == item_name or k.strip() == target_key:
                    found_key = k
                    break
            if found_key:
                t = self.targets[found_key]
                goal = int(t.get("goal", 1))
                cur = max(0, int(current))
                t["current"] = cur
                t["needed"] = max(0, goal - cur)
                t["is_completed"] = (cur >= goal)
                t["override_current"] = cur
                t["updated_at"] = int(time.time() * 1000)
                self.targets[found_key] = t
                self.save()
                return t
            return None

    def delete(self, item_name: str):
        with self._lock:
            target_key = item_name.strip()
            keys_to_del = [k for k in list(self.targets.keys()) if k == item_name or k.strip() == target_key]
            for k in keys_to_del:
                del self.targets[k]
            self.save()

    def clear(self):
        with self._lock:
            self.targets = {}
            self.save()

    def reset_all_current(self) -> int:
        """Resets the current count of all registered targets to 0."""
        with self._lock:
            count = 0
            for item_name, t in self.targets.items():
                goal = int(t.get("goal", 1))
                t["current"] = 0
                t["needed"] = goal
                t["is_completed"] = False
                t["inventory_count"] = 0
                t["override_current"] = 0
                t["updated_at"] = int(time.time() * 1000)
                count += 1
            self.save()
            return count

    def clear_all_overrides(self) -> int:
        """Clears manual overrides on all targets to re-sync with game inventory."""
        with self._lock:
            count = 0
            for item_name, t in self.targets.items():
                t["override_current"] = None
                t["updated_at"] = int(time.time() * 1000)
                count += 1
            self.save()
            return count

    def get_all(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(t) for t in self.targets.values()]

delivery_target_store = RegisteredDeliveryStore()

CUSTOM_TARGETS_FILE = get_user_data_path("custom_targets.json", SCRIPT_DIR)

class CustomTargetStore(RegisteredDeliveryStore):
    """Manages user-specified custom craft targets (개별 아이템 지정 제작 목표 관리)."""
    def __init__(self, filepath: str = CUSTOM_TARGETS_FILE):
        super().__init__(filepath=filepath, multi_character=False)

custom_target_store = CustomTargetStore()

PRESETS_FILE = get_user_data_path("delivery_presets.json", SCRIPT_DIR)

class DeliveryPresetStore:
    """Manages saved weekly delivery quest presets for reuse across weeks."""
    def __init__(self, filepath: str = PRESETS_FILE):
        self.filepath = filepath
        self.presets: Dict[str, Dict[str, Any]] = {}
        self.load()

    def load(self):
        # 1. Base load from bundled defaults if present in program dir
        bundled_preset_file = os.path.join(SCRIPT_DIR, "delivery_presets.json")
        if os.path.normpath(bundled_preset_file) != os.path.normpath(self.filepath) and os.path.exists(bundled_preset_file):
            try:
                with open(bundled_preset_file, "r", encoding="utf-8") as f:
                    bundled = json.load(f)
                    if isinstance(bundled, dict):
                        self.presets.update(bundled)
            except Exception:
                pass

        # 2. Overlay user's persistent presets from Documents
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    user_presets = json.load(f)
                    if isinstance(user_presets, dict):
                        self.presets.update(user_presets)
            except Exception:
                pass

    def save(self):
        try:
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(self.presets, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def save_preset(self, name: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Save current delivery target list as a named preset."""
        data = {
            "name": name,
            "items": items,
            "created_at": int(time.time() * 1000),
            "updated_at": int(time.time() * 1000),
        }
        self.presets[name] = data
        self.save()
        return data

    def delete_preset(self, name: str):
        if name in self.presets:
            del self.presets[name]
            self.save()

    def get_all(self) -> List[Dict[str, Any]]:
        return list(self.presets.values())

    def get_preset(self, name: str) -> Optional[Dict[str, Any]]:
        return self.presets.get(name)

delivery_preset_store = DeliveryPresetStore()

SETTINGS_FILE = get_user_data_path("settings.json", SCRIPT_DIR)

class SettingsStore:
    """Manages persistent application settings (e.g. alter order, wool gather order, wood gather order, etc.)."""
    DEFAULT_SETTINGS = {
        "alter_order": "high_tier",  # "high_tier" (상위 티어부터 / 오래 걸리는 가공 우선) or "low_tier" (하위 티어부터 / 기초 재료부터)
        "goal_fulfillment_mode": True,  # 목표 완수 모드 (완제품까지 연속 실행)
        "sound_alarm_enabled": False,   # 완료 효과음 알림 (기본값 OFF, 무음)
        "auto_collect_enabled": False,  # 가공 완료 시 무료 자동 수령 (기본값 OFF)
        "auto_play_music_enabled": False, # 작업 완료/대기 중 자동 악보 연주 (기본값 OFF)
        "auto_play_music_title": "",    # 자동 연주할 악보 제목 (비어 있으면 첫 번째 보유 악보)
        "safety_stocks": {},            # 상비약/요리 최소 유지 재고 프리셋 {"아이템명": 최소수량}
        "discord_webhook_url": "",      # 디스코드 웹훅 URL
        "discord_alarm_enabled": False, # 디스코드 완료 알림 활성화 여부
        "weight_limit_ratio": 0.98,     # 작업 안전 중지 가방 무게 기준 (기본 98%)
    }

    def __init__(self, filepath: str = SETTINGS_FILE):
        self.filepath = filepath
        self.settings: Dict[str, Any] = dict(self.DEFAULT_SETTINGS)
        self.load()

    def load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        self.settings.update(data)
            except Exception:
                pass

    def save(self):
        try:
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def get(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, default if default is not None else self.DEFAULT_SETTINGS.get(key))

    def set(self, key: str, value: Any):
        self.settings[key] = value
        self.save()

    def get_all(self) -> Dict[str, Any]:
        return dict(self.settings)

    def update(self, new_settings: Dict[str, Any]):
        self.settings.update(new_settings)
        self.save()

settings_store = SettingsStore()

MAX_SAFE_WEIGHT_RATIO = 0.98  # 작업 안전 중지 가방 무게 기준 (98%)


def get_weight_limit_ratio() -> float:
    """작업 안전 중지 가방 무게 기준 비율을 반환합니다 (기본 0.98, 98%)."""
    try:
        val = float(settings_store.get("weight_limit_ratio", MAX_SAFE_WEIGHT_RATIO))
        if 0.5 <= val <= 1.0:
            return val
    except Exception:
        pass
    return MAX_SAFE_WEIGHT_RATIO


def send_discord_webhook(webhook_url: str, payload: Dict[str, Any], timeout: int = 10) -> Tuple[bool, str]:
    """Sends a JSON payload to a Discord webhook URL using urllib."""
    if not webhook_url or not isinstance(webhook_url, str) or not webhook_url.startswith("http"):
        return False, "유효한 웹훅 URL이 아닙니다."
    try:
        import urllib.request
        import urllib.error
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            webhook_url.strip(),
            data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "MobiLivingSupporter/1.1",
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status in (200, 204):
                return True, "전송 성공"
            return False, f"HTTP {response.status}"
    except urllib.error.HTTPError as e:
        return False, f"전송 실패 (HTTP {e.code}): {e.reason}"
    except Exception as e:
        return False, f"전송 오류: {str(e)}"


def send_discord_test_message(webhook_url: str) -> Tuple[bool, str]:
    """Sends a verification test embed to the user's Discord webhook."""
    from datetime import datetime
    payload = {
        "content": "🔔 **[모비노기 지원도구]** 디스코드 웹훅 연동 테스트 성공!",
        "embeds": [
            {
                "title": "🔔 디스코드 웹훅 알림 연동 완료",
                "description": "모비노기 지원도구와 디스코드 채널이 정상적으로 연결되었습니다.\n**[생활 지원 목표 완수 모드]** 및 **[숙제 지원 던전·어비스·사냥터 클리어 알림]**이 이 채널로 전송됩니다.",
                "color": 0x5865F2,  # Discord Blurple
                "fields": [
                    {
                        "name": "🎯 알림 연동 대상",
                        "value": "• 생활 지원: 주간 납품 / 개별 지정 제작 목표 완수 리포트\n• 숙제 지원: 던전 / 어비스 / 사냥터 / 채집·가공 완료 알림",
                        "inline": False
                    },
                    {
                        "name": "⚙️ 전송 정보",
                        "value": "캐릭터명, 클리어 던전/임무, 소요 시간, 날개 소모, 제작·가공·채집 내역",
                        "inline": False
                    }
                ],
                "footer": {
                    "text": f"모비노기 지원도구 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                }
            }
        ]
    }
    return send_discord_webhook(webhook_url, payload)


def notify_discord_goal_completion(summary: Dict[str, Any], context_label: str = "주간 납품") -> bool:
    """Sends a rich Discord embed summary when Goal Fulfillment Mode finishes all available tasks."""
    webhook_url = settings_store.get("discord_webhook_url", "")
    if isinstance(webhook_url, str):
        webhook_url = webhook_url.strip()
    alarm_enabled = bool(settings_store.get("discord_alarm_enabled", False))

    if not alarm_enabled or not webhook_url:
        return False

    from datetime import datetime

    status = summary.get("status", "completed")
    status_text = summary.get("status_text", "완료")
    duration_text = summary.get("duration_text", "0초")
    wings = summary.get("wings", {})
    craft = summary.get("craft", {})
    alter = summary.get("alter", {})
    gather = summary.get("gather", {})
    collected = summary.get("collected_facilities", [])
    special = summary.get("special_materials", [])
    deferred = summary.get("deferred_works", [])

    # Check if a popup window or blocked state occurred
    blocked_reason = ""
    for dw in deferred:
        r = str(dw.get("reason", ""))
        if any(k in r.lower() for k in ["blocked", "popup", "팝업", "차단"]):
            blocked_reason = r
            break
    if not blocked_reason:
        for g in gather.get("items", []):
            err = str(g.get("error", ""))
            if any(k in err.lower() for k in ["blocked", "popup", "팝업", "차단"]):
                blocked_reason = err
                break

    # Color & title based on result
    if blocked_reason:
        title = f"⚠️ [{context_label}] 게임 내 팝업창 감지 / 작업 중단"
        desc = "🔔 게임 화면에 확인 팝업(1시간 안전 확인 등) 또는 상호작용 UI가 발생하여 캐릭터 행동이 차단되었습니다.\n게임 클라이언트에서 [확인]을 눌러 팝업을 닫아주세요."
        color = 0xE74C3C  # Red
    elif status == "completed":
        title = f"🎯 [{context_label}] 목표 완수 모드 100% 달성 완료!"
        desc = "🎉 등록된 모든 목표 아이템의 제작/납품 수량이 100% 완수되었습니다!"
        color = 0x2ECC71  # Green
    elif status == "waiting_in_background":
        title = f"🎯 [{context_label}] 목표 완수 모드 가용 작업 완료 안내"
        desc = "ℹ️ 현재 보유한 원자재로 진행 가능한 모든 가공 및 제작이 완료되었습니다.\n(원자재 소진 또는 대기열 진행 중)"
        color = 0x3498DB  # Blue
    elif status == "aborted":
        title = f"⏹️ [{context_label}] 목표 완수 모드 사용자 중지"
        desc = "사용자의 중단 요청으로 작업이 중간에 안전하게 중단되었습니다."
        color = 0x95A5A6  # Gray
    elif status in ("error", "failed"):
        title = f"⚠️ [{context_label}] 목표 완수 모드 작업 오류 중단"
        desc = "작업 수행 중 예기치 않은 오류가 발생하여 파이프라인이 중단되었습니다."
        color = 0xE74C3C  # Red
    else:
        title = f"⚠️ [{context_label}] 목표 완수 모드 작업 종료 ({status_text})"
        desc = "가방 과적, 정령의 날개 부족 또는 안전장치로 인해 작업이 마무리되었습니다."
        color = 0xE67E22  # Orange

    fields = [
        {
            "name": "⏱️ 소요 시간",
            "value": duration_text or "-",
            "inline": True
        },
        {
            "name": "🪶 정령의 날개",
            "value": f"{wings.get('used', 0):,}개 소모 (잔여: {wings.get('final', 0):,}개)",
            "inline": True
        }
    ]

    if blocked_reason:
        fields.append({
            "name": "🚨 차단 상세 원인",
            "value": f"게임 클라이언트 응답: `{blocked_reason}`\n(게임 내 확인 팝업창을 닫은 뒤 대시보드에서 일괄 실행을 다시 누르면 이어서 진행됩니다)",
            "inline": False
        })

    # Crafted items
    craft_items = craft.get("items", [])
    if craft_items:
        craft_lines = [f"• **{c.get('item', c.get('item_name', ''))}**: {c.get('crafted', c.get('count', 0)):,}개" for c in craft_items[:10]]
        if len(craft_items) > 10:
            craft_lines.append(f"• 외 {len(craft_items) - 10}건...")
        fields.append({
            "name": f"🔨 완제품 제작 완료 (총 {craft.get('total_crafted', len(craft_items)):,}개)",
            "value": "\n".join(craft_lines),
            "inline": False
        })
    elif status == "completed":
        fields.append({
            "name": "🔨 완제품 제작",
            "value": "모든 목표 수량이 이미 달성되어 제작 생략",
            "inline": False
        })

    # Altering items
    alter_items = alter.get("items", [])
    if alter_items:
        alter_lines = [
            f"• [{a.get('facility', '').replace(' 가공 시설', '')}] {a.get('recipe', a.get('item', ''))}: {a.get('registered', a.get('slots', 1))}슬롯"
            f" (예상 산출 {a.get('yield_est', 0):,}개)"
            for a in alter_items[:8]
        ]
        if len(alter_items) > 8:
            alter_lines.append(f"• 외 {len(alter_items) - 8}건...")
        fields.append({
            "name": f"⚙️ 시설 가공 등록 (총 {alter.get('total_slots', len(alter_items))}슬롯)",
            "value": "\n".join(alter_lines),
            "inline": False
        })

    # Gathered items
    gather_items = gather.get("items", [])
    if gather_items:
        gather_lines = [f"• **{g.get('item', g.get('name', ''))}**: +{g.get('gained', 0):,}개" for g in gather_items[:8]]
        if len(gather_items) > 8:
            gather_lines.append(f"• 외 {len(gather_items) - 8}건...")
        fields.append({
            "name": f"🌿 원자재 필드 채집 (총 {gather.get('total_gained', len(gather_items)):,}개)",
            "value": "\n".join(gather_lines),
            "inline": False
        })

    # Pre-collected facilities
    if collected:
        clean_facs = [f.replace(' 가공 시설', '') for f in collected]
        fields.append({
            "name": "📦 사전 수령 완료 시설",
            "value": ", ".join(clean_facs),
            "inline": False
        })

    # Special / missing materials alert
    if special:
        sp_lines = [f"• **{sm.get('item_name', '')}**: {sm.get('deficit', 0)}개 부족 (상점 구매 또는 사냥 필요)" for sm in special[:5]]
        fields.append({
            "name": "⚠️ 별도 확보 필요 재료",
            "value": "\n".join(sp_lines),
            "inline": False
        })

    # Deferred works
    if deferred and status != "completed":
        fields.append({
            "name": "⏳ 가공 보류/대기",
            "value": f"총 {len(deferred)}건의 가공이 슬롯 또는 하위 재료 대기로 보류되었습니다.",
            "inline": False
        })

    payload = {
        "content": f"🔔 **[모비노기 지원도구]** {title}",
        "embeds": [
            {
                "title": title,
                "description": desc,
                "color": color,
                "fields": fields,
                "footer": {
                    "text": f"모비노기 지원도구 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                }
            }
        ]
    }

    success, _ = send_discord_webhook(webhook_url, payload)
    return success

class DeliveryManager:
    def __init__(self, cli: Optional[MabinogiCLI] = None):
        self.cli = cli or MabinogiCLI()
        self.recipes: Dict[str, Dict[str, int]] = dict(DEFAULT_RECIPES)
        self.abort_requested: bool = False
        self.on_altering_updated: Optional[Callable[[Dict[str, Any]], None]] = None
        self._cached_quests: Optional[List[DeliveryTask]] = None
        self._cached_quests_time: float = 0.0
        self.load_recipe_cache()

    def notify_altering_updated(self, works_info: Optional[Dict[str, Any]] = None):
        """Notifies registered callback (e.g. web server) of fresh altering queue status without overhead."""
        if works_info is None:
            try:
                works_info = self.cli.get_altering_works()
            except Exception:
                return
        if callable(self.on_altering_updated):
            try:
                self.on_altering_updated(works_info)
            except Exception:
                pass

    def request_abort(self):
        """Signals any running operation (gathering, altering, etc.) to abort immediately."""
        self.abort_requested = True
        try:
            self.cli.stop_action()
        except Exception:
            pass

    def reset_abort(self):
        self.abort_requested = False

    def _force_stop_worker(self, worker_thread: Optional[threading.Thread] = None, max_attempts: int = 8):
        """Repeatedly issues stop_action until worker_thread exits and character stops."""
        for _ in range(max_attempts):
            try:
                self.cli.stop_action()
            except Exception:
                pass
            if worker_thread:
                worker_thread.join(timeout=0.6)
                if not worker_thread.is_alive():
                    break
            else:
                time.sleep(0.4)
                break
        if worker_thread and worker_thread.is_alive():
            worker_thread.join(timeout=1.5)

    def load_recipe_cache(self):
        # 1. Base load from bundled defaults if present in program dir
        bundled_recipe_file = os.path.join(SCRIPT_DIR, "recipes_cache.json")
        if os.path.normpath(bundled_recipe_file) != os.path.normpath(RECIPE_CACHE_FILE) and os.path.exists(bundled_recipe_file):
            try:
                with open(bundled_recipe_file, "r", encoding="utf-8") as f:
                    bundled = json.load(f)
                    if isinstance(bundled, dict):
                        self.recipes.update(bundled)
            except Exception:
                pass

        # 2. Overlay user's persistent recipes from Documents
        if os.path.exists(RECIPE_CACHE_FILE):
            try:
                with open(RECIPE_CACHE_FILE, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                    needs_save = False
                    # Self-heal against incomplete or outdated known recipes
                    for default_item, default_ings in DEFAULT_RECIPES.items():
                        if default_item in cached:
                            cached_ings = cached[default_item]
                            if not isinstance(cached_ings, dict):
                                cached[default_item] = dict(default_ings)
                                needs_save = True
                            else:
                                for k, v in default_ings.items():
                                    if k not in cached_ings:
                                        cached_ings[k] = v
                                        needs_save = True
                                    elif k == "마나 허브" and default_item == "마법 유탄 부품" and cached_ings[k] < 10:
                                        cached_ings[k] = 10
                                        needs_save = True
                    self.recipes.update(cached)
                    if needs_save:
                        self.save_recipe_cache()
            except Exception:
                pass

    def update_recipe(self, item_name: str, ingredients: Dict[str, int]):
        """Manually sets/updates the recipe for item_name and persists to cache."""
        item_name = item_name.strip()
        self.recipes[item_name] = dict(ingredients)
        self.save_recipe_cache()

    def save_recipe_cache(self):
        try:
            with open(RECIPE_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.recipes, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def log(self, message: str, level: str = "info", callback: Optional[LogCallback] = None):
        if callback:
            callback(level, message)
        else:
            prefix = {
                "info": "[INFO]",
                "success": "[SUCCESS]",
                "warn": "[WARN]",
                "error": "[ERROR]",
                "action": "[ACTION]"
            }.get(level, "[LOG]")
            print(f"{prefix} {message}")

    def _safe_get_wings(self, fallback: int = 0) -> int:
        """Safely returns current Spirit Wings count or fallback on error."""
        try:
            return self.cli.get_wings_count()
        except Exception:
            return fallback

    def format_and_log_summary(
        self,
        summary_title: str,
        initial_wings: int,
        final_wings: int,
        gathered_list: List[Dict[str, Any]],
        altered_list: List[Dict[str, Any]],
        crafted_list: List[Dict[str, Any]],
        collected_facilities: List[str],
        deferred_works: Optional[List[Dict[str, Any]]] = None,
        duration_sec: float = 0.0,
        status: str = "completed",
        callback: Optional[LogCallback] = None,
        special_materials: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Builds a comprehensive, aggregated execution report detailing:
        - Total Spirit Wings consumed (initial -> final)
        - Raw materials gathered (item, quantity, goal)
        - Altering works queued (facility, recipe, slots, estimated yield)
        - Finished goods crafted (item, quantity)
        - Pre-collected completed alters & deferred tasks
        - Non-gatherable materials requiring manual acquisition (e.g. 소금)
        Logs lines via self.log and returns structured dictionary.
        """
        wings_used = max(0, initial_wings - final_wings)

        # Aggregate gathered items
        agg_gathered: Dict[str, Dict[str, Any]] = {}
        for g in (gathered_list or []):
            item = g.get("item", "") or g.get("name", "")
            if not item:
                continue
            if item not in agg_gathered:
                agg_gathered[item] = {
                    "item": item,
                    "name": item,
                    "gained": 0,
                    "required": g.get("required", 0),
                    "status": g.get("status", "completed"),
                }
            agg_gathered[item]["gained"] += g.get("gained", 0)
            if g.get("required", 0) > agg_gathered[item]["required"]:
                agg_gathered[item]["required"] = g.get("required", 0)
        gathered_aggregated = list(agg_gathered.values())

        # Aggregate altered items
        agg_altered: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for a in (altered_list or []):
            fac = a.get("facility", "")
            rec = a.get("recipe") or a.get("item", "")
            key = (fac, rec)
            if key not in agg_altered:
                agg_altered[key] = {
                    "facility": fac,
                    "recipe": rec,
                    "item": a.get("item", rec),
                    "name": rec,
                    "registered": 0,
                    "slots": 0,
                    "yield_est": 0,
                    "expected_yield": 0,
                }
            reg_cnt = a.get("registered", a.get("slots", 1))
            y_cnt = a.get("yield_est", a.get("expected_yield", reg_cnt * 3))
            agg_altered[key]["registered"] += reg_cnt
            agg_altered[key]["slots"] += reg_cnt
            agg_altered[key]["yield_est"] += y_cnt
            agg_altered[key]["expected_yield"] += y_cnt
        altered_aggregated = list(agg_altered.values())

        # Aggregate crafted items
        agg_crafted: Dict[str, Dict[str, Any]] = {}
        for c in (crafted_list or []):
            item = c.get("item_name") or c.get("item", "") or c.get("name", "")
            if not item:
                continue
            if item not in agg_crafted:
                agg_crafted[item] = {
                    "item": item,
                    "item_name": item,
                    "name": item,
                    "crafted": 0,
                    "count": 0,
                    "status": c.get("status", "success"),
                }
            crafted_qty = c.get("crafted", c.get("count", 0))
            agg_crafted[item]["crafted"] += crafted_qty
            agg_crafted[item]["count"] += crafted_qty
        crafted_aggregated = list(agg_crafted.values())

        unique_collected = list(dict.fromkeys(collected_facilities or []))
        total_gathered_qty = sum(g["gained"] for g in gathered_aggregated)
        total_altered_slots = sum(a["registered"] for a in altered_aggregated)
        total_crafted_qty = sum(c["crafted"] for c in crafted_aggregated)

        # Build clean formatted report
        lines = [
            "======================================================================",
            f"[{summary_title}]",
            "======================================================================",
        ]

        # 1. Wings
        if wings_used > 0:
            lines.append(f"[정령의 날개 소모] 총 {wings_used:,}개 사용 (시작: {initial_wings:,}개 -> 현재: {final_wings:,}개)")
        else:
            lines.append(f"[정령의 날개 소모] 0개 사용 (날개 차감 없음 | 현재 보유: {final_wings:,}개)")

        # 2. Gather
        lines.append("")
        if gathered_aggregated:
            lines.append(f"[원자재 맞춤 채집] 총 {len(gathered_aggregated)}종 / {total_gathered_qty:,}개 채집 완료:")
            for g in gathered_aggregated:
                req_info = f" (목표: {g['required']:,}개)" if g.get("required") else ""
                lines.append(f"   • {g['item']}: +{g['gained']:,}개 획득{req_info}")
        else:
            lines.append("[원자재 맞춤 채집] 채집 작업 없음 (필요 원자재가 가방/창고에 충분함)")

        # 2.5 Non-gatherable / Special Materials Alert
        if special_materials:
            lines.append("")
            lines.append(f"[별도 확보 필요 재료] 상점 구매 또는 사냥으로 직접 확보 필요한 재료 {len(special_materials)}종:")
            for sm in special_materials:
                lines.append(f"   • {sm['item_name']}: {sm['deficit']}개 부족 (가방 {sm.get('inventory_count', 0)}개 / 필요 {sm.get('total_needed', 0)}개) -> 잡화점/식료품점 구매 또는 사냥 필요")

        # 3. Alter
        lines.append("")
        if altered_aggregated:
            lines.append(f"[시설 가공 등록] 총 {len(altered_aggregated)}개 품목 / {total_altered_slots}슬롯 등록 완료:")
            for a in altered_aggregated:
                yield_info = f" (예상 산출량: 약 {a['yield_est']:,}개)" if a.get("yield_est") else ""
                lines.append(f"   • [{a['facility']}] '{a['recipe']}': {a['registered']}슬롯{yield_info}")
        else:
            lines.append("[시설 가공 등록] 신규 가공 등록 없음")

        # 4. Craft
        lines.append("")
        if crafted_aggregated:
            lines.append(f"[완제품 납품 제작] 총 {len(crafted_aggregated)}개 품목 / {total_crafted_qty:,}개 제작 완료:")
            for c in crafted_aggregated:
                lines.append(f"   • '{c['item']}': {c['crafted']:,}개 제작 완료")
        else:
            lines.append("[완제품 납품 제작] 완제품 제작 없음 (현재 가공 대기열 진행 중)")

        # 5. Pre-collection
        if unique_collected:
            lines.append("")
            lines.append(f"[사전 가공품 수령] {', '.join(unique_collected)} 완료 가공품 수령 완료 (슬롯 확보 및 재료 입고)")

        # 6. Deferred
        if deferred_works:
            lines.append("")
            lines.append(f"[가공 보류/대기] 총 {len(deferred_works)}건의 가공이 슬롯 또는 하위 재료 대기로 보류되었습니다.")
            for d in deferred_works[:3]:
                fac = d.get("facility", "")
                it = d.get("item_name", "")
                cnt = d.get("deferred", d.get("needed", 0))
                lines.append(f"   • [{fac}] '{it}': {cnt}회 보류")
            if len(deferred_works) > 3:
                lines.append(f"   • 외 {len(deferred_works) - 3}건 보류")

        if status == "waiting_in_background":
            lines.append("")
            lines.append("[알람 동기화] 등록된 가공이 백그라운드에서 진행 중입니다. 완료 알람 후 '일괄 제작'을 다시 실행하세요!")
        elif status == "aborted":
            lines.append("")
            lines.append("[사용자 중지] 사용자의 요청으로 작업이 중간에 중단되었습니다. 중단 시점까지의 결과가 요약되었습니다.")

        lines.append("======================================================================")

        for l in lines:
            self.log(l, "success" if status in ("completed", "waiting_in_background") else "warn", callback)

        m, s = divmod(int(duration_sec), 60)
        dur_str = f"{m}분 {s}초" if m > 0 else f"{s}초"

        status_text = {
            "completed": "완료",
            "waiting_in_background": "완료",
            "aborted": "사용자 중지",
            "error": "오류 중단"
        }.get(status, "완료")

        return {
            "title": summary_title,
            "status": status,
            "status_text": status_text,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "duration_seconds": round(duration_sec, 1),
            "duration_text": dur_str,
            "wings": {
                "initial": initial_wings,
                "final": final_wings,
                "used": wings_used,
            },
            "gathered": gathered_aggregated,
            "altered": altered_aggregated,
            "crafted": crafted_aggregated,
            "gather": {
                "count": len(gathered_aggregated),
                "total_qty": total_gathered_qty,
                "items": gathered_aggregated,
            },
            "alter": {
                "count": total_altered_slots,
                "total_recipes": len(altered_aggregated),
                "items": altered_aggregated,
            },
            "craft": {
                "count": total_crafted_qty,
                "total_items": len(crafted_aggregated),
                "items": crafted_aggregated,
            },
            "collected": unique_collected,
            "collected_facilities": unique_collected,
            "deferred": deferred_works or [],
            "deferred_works": deferred_works or [],
            "special_materials": special_materials or [],
            "total_gathered_count": total_gathered_qty,
            "total_altered_slots": total_altered_slots,
            "total_crafted_count": total_crafted_qty,
            "summary_text": "\n".join(lines),
        }

    def clean_text(self, text: str) -> str:
        """Removes <color=...> and other HTML/formatting tags."""
        return re.sub(r"<[^>]+>", "", text).strip()

    def detect_delivery_quests(self) -> List[DeliveryTask]:
        """
        Scans actively tracked quest tracker entries (shortcut, pinned_sub, auto_register_sub)
        for genuine item delivery / crafting / gathering objectives:
        e.g., '<color=orange>달걀</color> 보유 20/60', '<color=orange>론 엣지소드S</color> 보유 3/6'
        Strictly excludes combat/dungeon clear objectives (클리어, 처치, 사냥, 토벌, 대화 등)
        and non-active background quest lines (main, auto_register_candidate_sub).
        """
        now = time.time()
        if self._cached_quests is not None and (now - self._cached_quests_time) < 3.0:
            return list(self._cached_quests)

        quests = self.cli.get_quests()
        if not quests:
            self._cached_quests = []
            self._cached_quests_time = now
            return []

        # 1. Only process active / pinned sources.
        # 'shortcut' = actively tracked HUD quest widget
        # 'pinned_sub' = player-pinned sub/side quest tracker items
        # 'auto_register_sub' = automatically pinned sub quest
        # Excludes: 'main' (story/dungeon orders), 'auto_register_candidate_sub' (unpinned candidates), 'goddess_mission', etc.
        valid_sources = ("shortcut", "pinned_sub", "auto_register_sub")
        active_quests = [q for q in quests if q.get("Source") in valid_sources]
        if not active_quests:
            return []

        EXCLUDE_VERBS = (
            "클리어", "처치", "사냥", "토벌", "대화", "만나기", "방문",
            "완료", "달성", "진행", "보고", "승리", "참여", "탐색", "조사"
        )
        DELIVERY_VERBS = ("보유", "제작", "만들기", "납품", "전달", "수집", "구하기", "채집")
        GENERIC_TITLES = ("퀘스트 클리어", "임무 진행", "바로가기", "숏컷", "임무 완료", "퀘스트")

        # Extract title hints from shortcut objectives (e.g. '<color=orange>달걀 구하기</color> 진행')
        title_hints: Dict[str, str] = {}
        for q in active_quests:
            if q.get("Source") == "shortcut":
                q_raw_title = self.clean_text(q.get("QuestTitle", ""))
                for obj in q.get("Objectives", []):
                    desc = obj.get("Description", "")
                    m = re.search(r"<color=[^>]+>(.*?)</color>\s*진행", desc)
                    if m:
                        title_hints[q_raw_title] = m.group(1).strip()

        # Build map of specific titles from pinned quests by item
        pinned_title_by_item: Dict[str, str] = {}
        for q in active_quests:
            if q.get("Source") == "pinned_sub":
                pq_title = self.clean_text(q.get("QuestTitle", ""))
                if pq_title and pq_title not in GENERIC_TITLES:
                    for obj in q.get("Objectives", []):
                        raw_desc = obj.get("Description", "")
                        c_m = re.search(r"<color=[^>]+>(.*?)</color>", raw_desc)
                        if c_m:
                            it_name = c_m.group(1).strip()
                            it_name = re.sub(r"^(?:보유|제작|만들기|납품|전달|수집|구하기|채집)\s*", "", it_name).strip()
                            it_name = re.sub(r"\s*(?:보유|제작|만들기|납품|전달|수집|구하기|채집)$", "", it_name).strip()
                            if it_name:
                                pinned_title_by_item[it_name] = pq_title

        tasks_dict: Dict[str, DeliveryTask] = {}

        for q in active_quests:
            raw_title = self.clean_text(q.get("QuestTitle", ""))
            if raw_title in GENERIC_TITLES and raw_title in title_hints:
                quest_title = title_hints[raw_title]
            else:
                quest_title = raw_title

            objectives = q.get("Objectives", [])
            for obj in objectives:
                raw_desc = obj.get("Description", "")

                color_m = re.search(r"<color=[^>]+>(.*?)</color>\s*([^\d]*?)\s*(\d+)\s*/\s*(\d+)", raw_desc)
                parsed_item = None
                parsed_cur = 0
                parsed_goal = 0

                if color_m:
                    raw_item = color_m.group(1).strip()
                    verb = color_m.group(2).strip()
                    cur = int(color_m.group(3))
                    goal = int(color_m.group(4))

                    # Check for combat / non-delivery exclusions
                    if any(ev in verb for ev in EXCLUDE_VERBS) or any(ev in raw_item for ev in EXCLUDE_VERBS):
                        continue

                    # Require delivery verb or empty verb
                    if any(dv in verb for dv in DELIVERY_VERBS) or not verb:
                        clean_item = re.sub(r"^(?:보유|제작|만들기|납품|전달|수집|구하기|채집)\s*", "", raw_item).strip()
                        clean_item = re.sub(r"\s*(?:보유|제작|만들기|납품|전달|수집|구하기|채집)$", "", clean_item).strip()
                        if clean_item:
                            parsed_item = clean_item
                            parsed_cur = cur
                            parsed_goal = goal

                # Plain text fallback if no color tag
                if not parsed_item:
                    clean_desc = self.clean_text(raw_desc)
                    if any(ev in clean_desc for ev in EXCLUDE_VERBS):
                        continue
                    plain_m = re.search(r"^(.*?)\s*(?:보유|제작|만들기|납품|전달|수집|구하기|채집)\s*(\d+)\s*/\s*(\d+)$", clean_desc)
                    if plain_m:
                        item_candidate = plain_m.group(1).strip()
                        if item_candidate and not any(ev in item_candidate for ev in EXCLUDE_VERBS):
                            parsed_item = item_candidate
                            parsed_cur = int(plain_m.group(2))
                            parsed_goal = int(plain_m.group(3))

                # Number-only description fallback: '0/6' with delivery quest title
                if not parsed_item:
                    num_m = re.match(r"^\s*(\d+)\s*/\s*(\d+)\s*$", self.clean_text(raw_desc))
                    if num_m and quest_title:
                        title_cand = re.sub(r"\s*(?:만들기|구하기|제작|납품|전달|채집|수집)$", "", quest_title).strip()
                        if title_cand != quest_title and not any(ev in title_cand for ev in EXCLUDE_VERBS):
                            parsed_item = title_cand
                            parsed_cur = int(num_m.group(1))
                            parsed_goal = int(num_m.group(2))

                if parsed_item and parsed_goal > 0:
                    is_completed = obj.get("IsCompleted", parsed_cur >= parsed_goal)
                    final_title = pinned_title_by_item.get(parsed_item, quest_title)
                    if final_title in GENERIC_TITLES and parsed_item in pinned_title_by_item:
                        final_title = pinned_title_by_item[parsed_item]

                    if parsed_item not in tasks_dict:
                        tasks_dict[parsed_item] = DeliveryTask(final_title, parsed_item, parsed_cur, parsed_goal, is_completed)
                    else:
                        existing = tasks_dict[parsed_item]
                        new_cur = max(existing.current, parsed_cur)
                        best_title = existing.quest_title
                        if best_title in GENERIC_TITLES and final_title not in GENERIC_TITLES:
                            best_title = final_title
                        tasks_dict[parsed_item] = DeliveryTask(best_title, parsed_item, new_cur, parsed_goal, new_cur >= parsed_goal)

        res_list = list(tasks_dict.values())
        self._cached_quests = res_list
        self._cached_quests_time = time.time()
        return list(res_list)

    def get_effective_owned(self, item_name: str, include_storage: bool = True) -> int:
        """
        Returns how many of item_name the player owns (in bag, character storage, and account storage).
        For equipment/finished goods (which get_items omits), checks active quest objective count if available,
        or falls back to persistent registered target count if present.
        """
        total_count = self.cli.count_item(item_name, include_storage=include_storage)
        if total_count > 0:
            return total_count

        quest_current = 0
        for task in self.detect_delivery_quests():
            if task.item_name == item_name:
                quest_current = max(quest_current, task.current)

        # Fallback to persistent registered target count if recorded
        store_current = 0
        if item_name in delivery_target_store.targets:
            store_current = int(delivery_target_store.targets[item_name].get("current", 0))

        return max(quest_current, store_current)

    def get_registered_deliveries_with_status(self) -> List[Dict[str, Any]]:
        """
        Returns all registered weekly delivery targets, with real-time current counts
        queried from the game client.
        CRITICAL: For direct delivery targets (items to be handed in directly, e.g. 마법 유탄 부품, 우유, 달걀),
        warehouse/storage (창고) counts are EXCLUDED from current owned count so only inventory (가방) count
        is counted towards completion. Storage counts remain displayed in breakdown for user reference.
        Never overwrites a known positive count with 0 when an item is unpinned or not returned by get_items.
        Never resurrects targets deleted by the user while status polling was in progress.
        """
        targets = delivery_target_store.get_all()
        is_ingame_char = delivery_target_store.is_active_character_ingame()
        active_delivery_tasks = {t.item_name: t for t in self.detect_delivery_quests()} if is_ingame_char else {}
        dirty = False

        for t in targets:
            item_name = t["item_name"]
            goal = int(t.get("goal", 1))
            prev_current = int(t.get("current", 0))

            if is_ingame_char:
                breakdown = self.cli.get_item_location_breakdown(item_name)
                inv_count = breakdown["inventory"]

                # Check active quest tracker
                quest_detected = 0
                if item_name in active_delivery_tasks:
                    quest_detected = active_delivery_tasks[item_name].current

                # Direct delivery target: count ONLY inventory (bag), EXCLUDE warehouse / storage!
                if t.get("override_current") is not None:
                    current = int(t["override_current"])
                elif inv_count > 0:
                    current = inv_count
                elif quest_detected > 0:
                    current = quest_detected
                elif prev_current > 0 and is_equipment_item(item_name):
                    current = prev_current
                else:
                    current = inv_count

                t["inventory_count"] = max(inv_count, current if (is_equipment_item(item_name) or breakdown["total"] == 0) else 0)
                t["character_storage_count"] = breakdown["character_storage"]
                t["account_storage_count"] = breakdown["account_storage"]
                t["storage_count"] = breakdown["storage_total"]
            else:
                # Viewing an offline character tab: do NOT overwrite its bag progress with the logged-in character's bag
                if t.get("override_current") is not None:
                    current = int(t["override_current"])
                else:
                    current = prev_current
                t["inventory_count"] = int(t.get("inventory_count", current))
                t["character_storage_count"] = int(t.get("character_storage_count", 0))
                t["account_storage_count"] = int(t.get("account_storage_count", 0))
                t["storage_count"] = int(t.get("storage_count", 0))

            needed = max(0, goal - current)
            is_completed = (current >= goal)

            if current != prev_current or t.get("needed") != needed or t.get("is_completed") != is_completed:
                dirty = True

            t["current"] = current
            t["needed"] = needed
            t["is_completed"] = is_completed

            rec = self.get_recipe_ingredients(item_name)
            t["recipe"] = rec
            t["is_recipe_known"] = bool(rec)

            # Guard against resurrecting items deleted while this polling cycle was running
            with delivery_target_store._lock:
                if item_name not in delivery_target_store.targets and item_name.strip() not in [k.strip() for k in delivery_target_store.targets.keys()]:
                    continue
                delivery_target_store.targets[item_name] = t

        if dirty:
            delivery_target_store.save()
        return delivery_target_store.get_all()

    def add_detected_quests_to_targets(self) -> List[Dict[str, Any]]:
        """
        Scans currently active quests via detect_delivery_quests() and adds or updates
        them in the persistent registered delivery targets store.
        Excludes warehouse/storage counts for delivery targets (counts only bag inventory).
        """
        detected = self.detect_delivery_quests()
        added = []
        for q in detected:
            cur = self.get_effective_owned(q.item_name, include_storage=False)
            cur = max(cur, q.current)
            entry = delivery_target_store.add_or_update(
                item_name=q.item_name,
                goal=q.goal,
                current=cur,
                quest_title=q.quest_title
            )
            added.append(entry)
        return added

    def get_custom_targets_with_status(self) -> List[Dict[str, Any]]:
        """
        Returns all registered custom craft targets with real-time current inventory/storage breakdown.
        Never resurrects targets deleted by the user while status polling was in progress.
        """
        targets = custom_target_store.get_all()
        dirty = False

        for t in targets:
            item_name = t["item_name"]
            goal = int(t.get("goal", 1))
            prev_current = int(t.get("current", 0))

            breakdown = self.cli.get_item_location_breakdown(item_name)

            if t.get("override_current") is not None:
                current = int(t["override_current"])
            elif breakdown["total"] > 0:
                current = breakdown["total"]
            elif prev_current > 0 or is_equipment_item(item_name):
                current = prev_current
            else:
                current = 0

            needed = max(0, goal - current)
            is_completed = (current >= goal)

            if current != prev_current or t.get("needed") != needed or t.get("is_completed") != is_completed:
                dirty = True

            t["current"] = current
            t["needed"] = needed
            t["is_completed"] = is_completed
            t["inventory_count"] = max(breakdown["inventory"], current if (is_equipment_item(item_name) or breakdown["total"] == 0) else 0)
            t["character_storage_count"] = breakdown["character_storage"]
            t["account_storage_count"] = breakdown["account_storage"]
            t["storage_count"] = breakdown["storage_total"]

            rec = self.get_recipe_ingredients(item_name)
            t["recipe"] = rec
            t["is_recipe_known"] = bool(rec)

            # Guard against resurrecting items deleted while this polling cycle was running
            with custom_target_store._lock:
                if item_name not in custom_target_store.targets and item_name.strip() not in [k.strip() for k in custom_target_store.targets.keys()]:
                    continue
                custom_target_store.targets[item_name] = t

        if dirty:
            custom_target_store.save()
        return custom_target_store.get_all()

    def analyze_custom_plan(self, alter_order: Optional[str] = None) -> Dict[str, Any]:
        """
        Runs full recursive BOM and production planner for custom craft targets.
        """
        custom_targets = self.get_custom_targets_with_status()
        tasks: List[Dict[str, Any]] = []
        for t in custom_targets:
            tasks.append({
                "quest_title": "개별 지정 제작",
                "item_name": t["item_name"],
                "current": t["current"],
                "goal": t["goal"],
                "needed": t["needed"],
                "override_current": t.get("override_current"),
                "is_completed": t.get("is_completed", False)
            })
        res = self.analyze_batch_plan(custom_tasks=tasks, alter_order=alter_order)
        res["plan_source"] = "custom_targets"
        return res

    def find_craft_recipe(self, item_name: str) -> Optional[Dict[str, Any]]:
        """Finds craft recipe for the exact item_name."""
        res = self.cli.get_craftable_items(item_name)
        for item in res.get("items", []):
            if item.get("DisplayName") == item_name:
                # Dynamically cache ProducedPerCraft if provided by CLI
                if "ProducedPerCraft" in item:
                    ppc = int(item.get("ProducedPerCraft", 1))
                    if ppc > 0:
                        PRODUCED_PER_CRAFT[item_name] = ppc
                # Dynamically update recipe cache if MissingIngredients present (merge, don't overwrite)
                missing = item.get("MissingIngredients", [])
                if missing:
                    existing = self.recipes.get(item_name, {})
                    updated = dict(existing)
                    for ing in missing:
                        updated[ing["DisplayName"]] = int(ing.get("Required", 1))

                    # Heuristic: check if raw material is in item_name (e.g. "달걀" in "삶은 달걀")
                    for raw_cand in ["달걀", "우유", "사과", "감자", "조개", "옥수수", "양파", "양배추", "통나무", "마나 허브"]:
                        if raw_cand in item_name and raw_cand != item_name and raw_cand not in updated:
                            try:
                                cand_res = self.cli.get_craftable_items(raw_cand)
                                if any(it.get("DisplayName") == item_name for it in cand_res.get("items", [])):
                                    updated[raw_cand] = 1
                            except Exception:
                                pass

                    self.recipes[item_name] = updated
                    self.save_recipe_cache()
                return item
        return None

    def get_produced_per_craft(self, item_name: str) -> int:
        """Returns how many pieces 1 craft produces (e.g. 마법 유탄 부품 = 5)."""
        if item_name in PRODUCED_PER_CRAFT:
            return PRODUCED_PER_CRAFT[item_name]
        try:
            recipe_data = self.find_craft_recipe(item_name)
            if recipe_data and "ProducedPerCraft" in recipe_data:
                ppc = int(recipe_data.get("ProducedPerCraft", 1))
                if ppc > 0:
                    PRODUCED_PER_CRAFT[item_name] = ppc
                    return ppc
        except Exception:
            pass
        return 1

    def get_recipe_ingredients(self, item_name: str) -> Dict[str, int]:
        """Returns ingredient dict {ing_name: qty_per_craft} for item_name."""
        if item_name in self.recipes and self.recipes[item_name]:
            return self.recipes[item_name]

        # Try querying CLI
        recipe_data = self.find_craft_recipe(item_name)
        if item_name in self.recipes and self.recipes[item_name]:
            return self.recipes[item_name]

        if recipe_data and recipe_data.get("MissingIngredients"):
            rec_dict = {ing["DisplayName"]: int(ing.get("Required", 1)) for ing in recipe_data["MissingIngredients"]}
            existing = self.recipes.get(item_name, {})
            updated = dict(existing)
            updated.update(rec_dict)

            # Heuristic check
            for raw_cand in ["달걀", "우유", "사과", "감자", "조개", "옥수수", "양파", "양배추", "통나무", "마나 허브"]:
                if raw_cand in item_name and raw_cand != item_name and raw_cand not in updated:
                    try:
                        cand_res = self.cli.get_craftable_items(raw_cand)
                        if any(it.get("DisplayName") == item_name for it in cand_res.get("items", [])):
                            updated[raw_cand] = 1
                    except Exception:
                        pass

            self.recipes[item_name] = updated
            self.save_recipe_cache()
            return updated

        return {}

    SHORTCUT_RECIPE_TARGETS = {"은합금괴", "최상급 목재", "최상급 옷감", "최상급 가죽"}

    # 철괴: "철괴(광석)" 레시피를 제외하고 "철괴(철 광석)" 레시피만 사용
    BLOCKED_ALTER_RECIPES = {"철괴(광석)"}

    def filter_standard_alter_recipes(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Filters out inefficient shortcut recipes that skip lower-tier processed goods:
        - 은합금괴: skips 특수강괴 and uses 마력 깃든 돌
        - 최상급 목재: skips 상급 목재+ and uses 단단한 통나무 directly
        - 최상급 옷감: skips 상급 옷감+ and uses 두꺼운 양털 directly
        - 최상급 가죽: skips 상급 가죽+
        These appear as secondary duplicate occurrences in get_alterable_items (indices >= 70).
        Standard recipes appear first in the game data and require stepwise lower-tier materials.
        """
        seen = set()
        filtered = []
        for it in items:
            name = it.get("DisplayName", "")
            missing = it.get("MissingIngredients", [])
            has_magic_stone = any("마력" in m.get("DisplayName", "") for m in missing)
            if has_magic_stone:
                continue
            # 특정 비효율 레시피를 DisplayName 기준으로 차단 (예: "철괴(광석)")
            if name in self.BLOCKED_ALTER_RECIPES:
                continue
            if name in self.SHORTCUT_RECIPE_TARGETS:
                if name in seen:
                    continue
                seen.add(name)
            filtered.append(it)
        return filtered

    def find_alter_recipe(self, item_name: str) -> Optional[Dict[str, Any]]:
        """Finds altering recipe for the exact item_name (excluding inefficient shortcut recipes)."""
        clean_name = item_name.split("(")[0].strip()
        try:
            # Query unfiltered list first (shared 10s TTL cache across all items)
            res = self.cli.get_alterable_items()
        except Exception:
            try:
                res = self.cli.get_alterable_items(clean_name)
            except Exception:
                return None
        items = self.filter_standard_alter_recipes(res.get("items", []))
        # 1. Exact match
        for item in items:
            if item.get("DisplayName") == item_name or item.get("DisplayName") == clean_name:
                return item
        # 2. Fallback: match "clean_name(...)" variants (e.g. "철괴" -> "철괴(철 광석)")
        for item in items:
            dn = item.get("DisplayName", "")
            if dn.startswith(clean_name + "("):
                return item
        return None

    def get_alter_info(self, mat_name: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """
        Returns (is_alterable, info_dict) containing:
        facility, produced, tier, ingredients (dict), display_name.
        """
        clean_name = mat_name.split("(")[0].strip()
        if clean_name in STANDARD_ALTER_RECIPES:
            info = STANDARD_ALTER_RECIPES[clean_name]
            return True, {
                "facility": info["facility"],
                "produced": info["produced"],
                "tier": info["tier"],
                "ingredients": dict(info["ingredients"]),
                "display_name": info["display_name"]
            }

        # Dynamic lookup via CLI get_alterable_items
        alter_rec = self.find_alter_recipe(mat_name)
        if alter_rec:
            fac = alter_rec.get("FacilityName") or get_facility_for_material(mat_name)
            prod = alter_rec.get("ProducedPerWork", 3)
            ing_dict = {}
            if alter_rec.get("MissingIngredients"):
                for mi in alter_rec["MissingIngredients"]:
                    ing_dict[mi["DisplayName"]] = int(mi.get("Required", 1))
            return True, {
                "facility": fac,
                "produced": prod,
                "tier": 1,
                "ingredients": ing_dict,
                "display_name": alter_rec.get("DisplayName", mat_name)
            }
        return False, None

    def is_gatherable(self, item_name: str) -> Tuple[bool, bool]:
        """Returns (is_gatherable, tool_ok)."""
        clean_name = item_name.split("(")[0].strip()

        # Fast-path non-gatherable items (shop/monster drops, finished equipment, processed materials)
        if is_non_gatherable_material(clean_name) or is_equipment_item(clean_name) or clean_name in STANDARD_ALTER_RECIPES:
            return False, False

        # 1. 양털 계열의 경우: 후보 양들 중 하나라도 채집 가능한지 확인
        if is_wool_family(clean_name):
            try:
                res = self.cli.get_gatherable_items()
                items = res.get("items", [])
                items_map = {it.get("DisplayName"): it for it in items}
                candidates = get_wool_gather_candidates(clean_name)
                has_any_gatherable = False
                for cand in candidates:
                    tgt = cand["gather_target"]
                    if tgt in items_map:
                        has_any_gatherable = True
                        if items_map[tgt].get("ToolOk", False):
                            return True, True
                if has_any_gatherable:
                    return True, False
            except Exception:
                pass
            if clean_name in KNOWN_GATHERABLE_ITEMS or "양털" in clean_name:
                return True, True

        # 2. 벌목 및 통나무/나무 진액 계열의 경우: 후보 나무들 중 하나라도 채집 가능한지 확인
        if is_wood_family(clean_name):
            try:
                res = self.cli.get_gatherable_items()
                items = res.get("items", [])
                items_map = {it.get("DisplayName"): it for it in items}
                candidates = get_wood_gather_candidates(clean_name)
                has_any_gatherable = False
                for cand in candidates:
                    tgt = cand["gather_target"]
                    if tgt in items_map:
                        has_any_gatherable = True
                        if items_map[tgt].get("ToolOk", False):
                            return True, True
                if has_any_gatherable:
                    return True, False
            except Exception:
                pass
            if clean_name in KNOWN_GATHERABLE_ITEMS or any(k in clean_name for k in ["통나무", "나무 진액", "나뭇가지"]):
                return True, True

        # 3. 일반 채집물: 전체 채집 가능 목록(15초 TTL 캐시)에서 우선 조회하여 프로세스 중복 호출 방지
        try:
            res = self.cli.get_gatherable_items()
            for item in res.get("items", []):
                if item.get("DisplayName") in (item_name, clean_name):
                    return True, bool(item.get("ToolOk", False))
        except Exception:
            pass

        if item_name in KNOWN_GATHERABLE_ITEMS or clean_name in KNOWN_GATHERABLE_ITEMS:
            return True, True
        return False, False

    def auto_gather(self, item_name: str, required_count: int, callback: Optional[LogCallback] = None, is_delivery: bool = False, **kwargs) -> Dict[str, Any]:
        """
        Gathers only the required deficient amount of item_name:
        1. Checks initial inventory count. If already satisfied, returns immediately.
        2. Supports multi-tier fallback for wool & wood family based on living-skill level and drop efficiency.
        3. Starts execute_gathering in a worker thread.
        4. Polls inventory count periodically (every ~1.0-1.2s).
        5. When target_count is reached (or abort requested), stops the worker immediately.
        6. If execute_gathering finishes before target_count is reached (e.g. 100-item cap per call
           or ore/tree node depleted), automatically re-launches execute_gathering in a loop
           until the full required_count is collected!
        """
        if self.abort_requested:
            self.log("[사용자 중지] 채집 시작 전 중지 요청이 확인되었습니다.", "warn", callback)
            return {"status": "aborted", "item": item_name, "gathered": 0}

        if required_count <= 0:
            self.log(f"'{item_name}'은(는) 이미 충분하여 채집을 건너뜁니다.", "info", callback)
            return {"status": "satisfied", "item": item_name, "gathered": 0}

        is_gath, tool_ok = self.is_gatherable(item_name)
        if not is_gath:
            raise MabinogiCLIError(f"'{item_name}'은(는) 필드 채집 목록에 없습니다.")
        if not tool_ok:
            raise MabinogiCLIError(f"'{item_name}' 채집 도구가 없거나 내구도가 0입니다.")

        # Zero-overhead snapshot from cached locations map to track byproducts later
        try:
            initial_locs_map = self.cli.get_all_item_locations_map(bypass_cache=True)
            initial_inv_snapshot = {k: v.get("inventory", 0) for k, v in initial_locs_map.items()}
            initial_count = initial_inv_snapshot.get(item_name, 0)
        except Exception:
            initial_inv_snapshot = {}
            initial_count = self.cli.count_item(item_name, include_storage=False, bypass_cache=True)
        target_count = initial_count + required_count

        # --- 양털 및 벌목 계열 다단계 우선순위 및 레벨 필터링 설정 ---
        is_wool = is_wool_family(item_name)
        is_wood = is_wood_family(item_name)
        candidate_list: List[Dict[str, Any]] = []

        if is_wool:
            all_cands = get_wool_gather_candidates(item_name)

            # 게임 클라이언트에서 생활 레벨이 충족된 채집물 확인
            try:
                g_res = self.cli.get_gatherable_items()
                g_items = g_res.get("items", [])
                g_map = {it.get("DisplayName"): it for it in g_items}
            except Exception:
                g_map = {}

            if g_map:
                valid_cands = [c for c in all_cands if c["gather_target"] in g_map and g_map[c["gather_target"]].get("ToolOk", False)]
                if not valid_cands:
                    valid_cands = [c for c in all_cands if c["gather_target"] in g_map]
                candidate_list = valid_cands if valid_cands else all_cands
            else:
                candidate_list = all_cands

            target_sheep = candidate_list[0]
            self.log(
                f"[양털 맞춤 채집 시작] 목표: '{item_name}' {required_count}개 "
                f"(가방: {initial_count}개 -> 목표: {target_count}개) | "
                f"최적 대상: {target_sheep['species']}({target_sheep['gather_target']})",
                "action",
                callback
            )
            if len(candidate_list) > 1:
                cand_names = " -> ".join(dict.fromkeys(c["species"] for c in candidate_list))
                self.log(
                    f"[단계적 폴백 활성화] 생활 레벨 및 채집 가능 여부에 따라 단계적 순차 채집: {cand_names}",
                    "info",
                    callback
                )
        elif is_wood:
            all_cands = get_wood_gather_candidates(item_name)

            try:
                g_res = self.cli.get_gatherable_items()
                g_items = g_res.get("items", [])
                g_map = {it.get("DisplayName"): it for it in g_items}
            except Exception:
                g_map = {}

            if g_map:
                valid_cands = [c for c in all_cands if c["gather_target"] in g_map and g_map[c["gather_target"]].get("ToolOk", False)]
                if not valid_cands:
                    valid_cands = [c for c in all_cands if c["gather_target"] in g_map]
                candidate_list = valid_cands if valid_cands else all_cands
            else:
                candidate_list = all_cands

            target_tree = candidate_list[0]
            self.log(
                f"[벌목 맞춤 채집 시작] 목표: '{item_name}' {required_count}개 "
                f"(가방: {initial_count}개 -> 목표: {target_count}개) | "
                f"최적 대상: {target_tree['species']}({target_tree['gather_target']})",
                "action",
                callback
            )
            if len(candidate_list) > 1:
                cand_names = " -> ".join(dict.fromkeys(c["species"] for c in candidate_list))
                self.log(
                    f"[단계적 폴백 활성화] 생활 레벨 및 채집 가능 여부에 따라 단계적 순차 채집: {cand_names}",
                    "info",
                    callback
                )
        else:
            candidate_list = [{"gather_target": item_name, "species": item_name, "tier": 1, "desc": item_name}]
            self.log(
                f"[부족 수량 맞춤 채집 시작] '{item_name}' 목표: {required_count}개 추가 채집 "
                f"(현재 가방: {initial_count}개 -> 목표: {target_count}개)", 
                "action", 
                callback
            )

        candidate_idx = 0
        stopped_by_target = False
        stopped_by_overweight = False
        poll_interval = 1.0
        last_logged_gained = -1
        consecutive_zero_gains = 0

        while not self.abort_requested and candidate_idx < len(candidate_list):
            curr_count = self.cli.count_item(item_name, include_storage=False, bypass_cache=True)
            gained = max(0, curr_count - initial_count)
            if curr_count >= target_count or gained >= required_count:
                stopped_by_target = True
                break

            current_cand = candidate_list[candidate_idx]
            gather_target_item = current_cand["gather_target"]
            species_label = current_cand.get("species", gather_target_item)

            # Check tool status before launching or re-launching
            is_gath, tool_ok = self.is_gatherable(gather_target_item)
            if not tool_ok:
                self.log(f"'{species_label}'({gather_target_item}) 채집 도구가 소모되었거나 내구도가 0입니다. 채집을 중단합니다.", "warn", callback)
                break

            # 0. Safety check: bag weight before starting gather round (98% limit)
            try:
                inv_info = self.cli.get_inventory()
                cur_w = float(inv_info.get("CurrentInventoryWeightAsDecimal", 0) or inv_info.get("CurrentInventoryWeight", 0) or 0)
                max_w = float(inv_info.get("MaxInventoryWeightAsDecimal", 1) or inv_info.get("MaxInventoryWeight", 1) or 1)
                limit_ratio = get_weight_limit_ratio()
                if max_w > 0 and (cur_w / max_w) >= limit_ratio:
                    pct = (cur_w / max_w) * 100
                    limit_pct = limit_ratio * 100
                    self.log(
                        f"[가방 무게 경고] 가방 무게가 {pct:.1f}%({cur_w:.1f}/{max_w:.1f})로 {limit_pct:.0f}% 기준 이상 차 있어 과적 방지를 위해 채집을 즉시 중단합니다.",
                        "warn",
                        callback
                    )
                    stopped_by_overweight = True
                    break
            except Exception:
                pass

            worker_error = []
            worker_result = []

            def gather_worker():
                try:
                    res = self.cli.execute_gathering(gather_target_item)
                    worker_result.append(res)
                except Exception as e:
                    worker_error.append(e)

            worker_thread = threading.Thread(target=gather_worker, daemon=True)
            worker_thread.start()

            round_initial_count = curr_count
            round_start_time = time.time()
            refreshed_by_timer = False
            idle_counter = 0

            # Monitoring loop for current execute_gathering call
            while True:
                time.sleep(poll_interval)

                # Check abort request
                if self.abort_requested:
                    self.log(f"[사용자 중지 요청] '{item_name}' 채집을 즉시 중단합니다.", "warn", callback)
                    self._force_stop_worker(worker_thread)
                    return {
                        "status": "aborted",
                        "item": item_name,
                        "required": required_count,
                        "gained": max(0, self.cli.count_item(item_name, include_storage=False, bypass_cache=True) - initial_count)
                    }

                # 0-1. 55-minute single-action cycle refresh (prevents 1-hour continuous action popup)
                if (time.time() - round_start_time) >= 3300:
                    self.log(
                        f"[55분 주기 갱신] '{item_name}' 단일 연속 채집이 55분 경과하여 1시간 확인창 방지를 위해 잠시 멈추고 즉시 이어서 재시작합니다.",
                        "info",
                        callback
                    )
                    refreshed_by_timer = True
                    self._force_stop_worker(worker_thread)
                    time.sleep(1.5)
                    break

                # 1. Safety check
                activity = {}
                try:
                    activity = self.cli.get_activity()
                    if activity.get("IsDead") or activity.get("IsReviving"):
                        self.log(f"캐릭터가 행동 불능 상태입니다. 채집을 중단합니다.", "warn", callback)
                        self._force_stop_worker(worker_thread)
                        return {
                            "status": "dead",
                            "item": item_name,
                            "required": required_count,
                            "gained": max(0, self.cli.count_item(item_name, include_storage=False, bypass_cache=True) - initial_count)
                        }
                except Exception:
                    pass

                # 1-1. Weight check to prevent overweight (98% limit)
                try:
                    inv_info = self.cli.get_inventory()
                    cur_w = float(inv_info.get("CurrentInventoryWeightAsDecimal", 0) or inv_info.get("CurrentInventoryWeight", 0) or 0)
                    max_w = float(inv_info.get("MaxInventoryWeightAsDecimal", 1) or inv_info.get("MaxInventoryWeight", 1) or 1)
                    limit_ratio = get_weight_limit_ratio()
                    if max_w > 0 and (cur_w / max_w) >= limit_ratio:
                        pct = (cur_w / max_w) * 100
                        limit_pct = limit_ratio * 100
                        self.log(
                            f"[가방 무게 경고] 가방 무게가 {pct:.1f}%({cur_w:.1f}/{max_w:.1f})에 도달하여 {limit_pct:.0f}% 과적 방지 기준에 따라 채집을 즉시 안전 중단합니다.",
                            "warn",
                            callback
                        )
                        self._force_stop_worker(worker_thread)
                        stopped_by_overweight = True
                        break
                except Exception:
                    pass

                # 2. Check current count in inventory (항상 원래 목표 아이템 실시간 추적)
                try:
                    curr_count = self.cli.count_item(item_name, include_storage=False, bypass_cache=True)
                except Exception:
                    continue
                gained = max(0, curr_count - initial_count)

                if gained != last_logged_gained and gained > 0:
                    last_logged_gained = gained
                    extra_info = f" | 대상: {species_label}" if (is_wool or is_wood) and species_label != item_name else ""
                    self.log(f"'{item_name}' 채집 진행 중... ({gained}/{required_count}개 획득, 현재 가방: {curr_count}개{extra_info})", "info", callback)

                # 3. Check if target count is reached
                if curr_count >= target_count or gained >= required_count:
                    self.log(
                        f"[목표 달성] '{item_name}' 부족 수량 {required_count}개 채집 완료! "
                        f"(가방: {curr_count}개, 획득: {gained}개) 채집을 즉시 자동 중단합니다.", 
                        "success", 
                        callback
                    )
                    stopped_by_target = True
                    self._force_stop_worker(worker_thread)
                    break

                # 4. Check if character stopped auto-play/traveling in field (node depleted or out of range)
                try:
                    is_auto = activity.get("IsAutoPlaying", False) or activity.get("IsAutoTraveling", False)
                    if not is_auto:
                        idle_counter += 1
                        # After 8s of not moving/playing, safely recognize that field node is gone
                        if idle_counter >= 8:
                            self.log(f"필드에 더 이상 상호작용 가능한 '{species_label}' 노드가 없어 채집 동작을 일시 종료합니다.", "info", callback)
                            self._force_stop_worker(worker_thread)
                            break
                    else:
                        idle_counter = 0
                except Exception:
                    pass

                # 5. Check if worker thread finished
                if not worker_thread.is_alive():
                    if worker_result and isinstance(worker_result[0], dict) and worker_result[0].get("result") == "started":
                        continue
                    else:
                        break

            # Ensure current worker is stopped if still running
            if worker_thread.is_alive():
                self._force_stop_worker(worker_thread, max_attempts=4)

            if stopped_by_target or stopped_by_overweight or self.abort_requested:
                break

            # Check if an error occurred during gathering
            if worker_error and not refreshed_by_timer:
                err = worker_error[0]
                err_msg = str(err)
                self.log(f"'{species_label}'({gather_target_item}) 채집 중 응답: {err_msg}", "warn", callback)

                # 단계적 폴백 조건: 생활 레벨 부족, 경로 없음, 노드 없음 등의 경우 다음 하위 후보로 전환
                is_level_or_route_err = any(k in err_msg for k in ["insufficient_living_skill_level", "no_route", "not_found", "locked"])
                if is_level_or_route_err and candidate_idx + 1 < len(candidate_list):
                    candidate_idx += 1
                    next_cand = candidate_list[candidate_idx]
                    self.log(
                        f"[단계적 폴백] '{species_label}' 채집 불가 ({err_msg}). "
                        f"생활 레벨 조건을 고려하여 다음 순위인 '{next_cand['species']}'({next_cand['gather_target']})(으)로 자동 전환합니다.",
                        "warn",
                        callback
                    )
                    consecutive_zero_gains = 0
                    time.sleep(1.0)
                    continue

                if any(k in err_msg for k in ["tool", "overweight", "blocked", "not_in_field"]):
                    raise err
                break

            # Check if this round gained any items
            curr_count = self.cli.count_item(item_name, include_storage=False, bypass_cache=True)
            round_gained = curr_count - round_initial_count
            if round_gained <= 0:
                consecutive_zero_gains += 1
                if consecutive_zero_gains >= 3:
                    # 현재 양에서 더 이상 획득이 안 되면 다음 후보 양으로 폴백 시도
                    if candidate_idx + 1 < len(candidate_list):
                        candidate_idx += 1
                        next_cand = candidate_list[candidate_idx]
                        self.log(
                            f"[단계적 폴백] '{species_label}'에서 연속 획득 실패. "
                            f"다음 후보인 '{next_cand['species']}'({next_cand['gather_target']})(으)로 전환하여 채집을 이어갑니다.",
                            "warn",
                            callback
                        )
                        consecutive_zero_gains = 0
                        time.sleep(1.0)
                        continue
                    else:
                        self.log(f"'{item_name}' 채집 시도에서 더 이상 아이템을 획득하지 못하여 채집을 종료합니다.", "warn", callback)
                        break
            else:
                consecutive_zero_gains = 0

            # If still deficient, notify and continue next round (e.g. 100-cap or node depleted)
            remaining_deficit = target_count - curr_count
            if remaining_deficit > 0:
                self.log(
                    f"'{species_label}' 1회 채집 구간 종료 (현재 가방: {curr_count}개, 총 획득: {curr_count - initial_count}/{required_count}개). "
                    f"남은 부족분 {remaining_deficit}개를 마저 채집하기 위해 연속 채집을 계속 진행합니다...", 
                    "action", 
                    callback
                )
                time.sleep(1.0)

        # Final recount & Zero-overhead Byproduct Detection (single get_all_item_locations_map call)
        byproducts: Dict[str, int] = {}
        try:
            final_locs_map = self.cli.get_all_item_locations_map(bypass_cache=True)
            final_count = final_locs_map.get(item_name, {}).get("inventory", 0)
            if initial_inv_snapshot:
                for k, v in final_locs_map.items():
                    if k == item_name:
                        continue
                    diff = v.get("inventory", 0) - initial_inv_snapshot.get(k, 0)
                    if diff > 0:
                        byproducts[k] = diff
        except Exception:
            final_count = self.cli.count_item(item_name, include_storage=False, bypass_cache=True)
        total_gained = max(0, final_count - initial_count)

        if byproducts:
            bp_str = ", ".join(f"{bk} +{bv}개" for bk, bv in list(byproducts.items())[:5])
            self.log(f"🎁 [채집 부산물 동시 획득] '{item_name}' 채집 중 부수 재료 획득: {bp_str}", "info", callback)

        if stopped_by_overweight:
            limit_pct = get_weight_limit_ratio() * 100
            self.log(
                f"[채집 과적 중단] 가방 무게 {limit_pct:.0f}% 기준 도달로 '{item_name}' 채집을 안전 종료합니다. (현재 획득: {total_gained}/{required_count}개, 가방: {final_count}개)",
                "warn",
                callback
            )
        elif total_gained >= required_count or final_count >= target_count:
            self.log(
                f"[맞춤 채집 완료] '{item_name}' 총 {total_gained}개 획득 (가방: {final_count}개 / 필요: {required_count}개)", 
                "success", 
                callback
            )
        else:
            self.log(
                f"[채집 조기 종료] '{item_name}' {total_gained}/{required_count}개 획득 (가방: {final_count}개 / 목표: {target_count}개)", 
                "warn", 
                callback
            )

        # Update delivery_target_store if this item is in registered targets
        if item_name in delivery_target_store.targets and total_gained > 0:
            t = delivery_target_store.targets[item_name]
            prev = int(t.get("current", 0))
            new_cur = prev + total_gained
            goal = int(t.get("goal", 1))
            t["current"] = new_cur
            if t.get("override_current") is not None:
                t["override_current"] = new_cur
            t["needed"] = max(0, goal - new_cur)
            t["is_completed"] = (new_cur >= goal)
            t["updated_at"] = int(time.time() * 1000)
            delivery_target_store.targets[item_name] = t
            delivery_target_store.save()
            self.log(f"[납품 목표 갱신] '{item_name}' 채집 완료({total_gained}개 획득) 반영 -> 현재 {new_cur}/{goal}개", "info", callback)

        # Update custom_target_store if this item is in registered custom targets
        if item_name in custom_target_store.targets and total_gained > 0:
            ct = custom_target_store.targets[item_name]
            prev_c = int(ct.get("current", 0))
            new_cur_c = prev_c + total_gained
            goal_c = int(ct.get("goal", 1))
            ct["current"] = new_cur_c
            if ct.get("override_current") is not None:
                ct["override_current"] = new_cur_c
            ct["needed"] = max(0, goal_c - new_cur_c)
            ct["is_completed"] = (new_cur_c >= goal_c)
            ct["updated_at"] = int(time.time() * 1000)
            custom_target_store.targets[item_name] = ct
            custom_target_store.save()
            self.log(f"[개별 제작 목표 갱신] '{item_name}' 채집 완료({total_gained}개 획득) 반영 -> 현재 {new_cur_c}/{goal_c}개", "info", callback)

        return {
            "status": "overweight" if stopped_by_overweight else ("completed" if (total_gained >= required_count or final_count >= target_count) else "partial"),
            "item": item_name,
            "required": required_count,
            "gained": total_gained,
            "current_owned": final_count,
            "stopped_by_target": stopped_by_target,
            "stopped_by_overweight": stopped_by_overweight
        }


    def collect_completed_altering_works(
        self, 
        preferred_facility: Optional[str] = None, 
        callback: Optional[LogCallback] = None,
        only_fully_completed: bool = False
    ) -> List[str]:
        """
        Checks current altering queue across all facilities.
        If only_fully_completed is True, collects ONLY from facilities where ALL queued works are completed
        (skipping any facility that still has in-progress or waiting works).
        If preferred_facility is provided, processes that facility first.
        """
        self._last_collect_blocked_reason: Optional[str] = None
        try:
            works_info = self.cli.get_altering_works()
        except Exception as e:
            self.log(f"가공 현황 조회 중 오류 (수령 건너뜀): {e}", "warn", callback)
            return []

        works = works_info.get("works", [])
        if not works:
            return []

        # Group all works by facility
        facility_works: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for w in works:
            fac = w.get("FacilityName") or get_facility_for_material(w.get("DisplayName", ""))
            if fac:
                facility_works[fac].append(w)

        # Determine which facilities are eligible for collection
        facility_to_items: Dict[str, List[str]] = {}
        for fac, f_works in facility_works.items():
            completed_in_fac = [
                w for w in f_works 
                if w.get("IsCompleted") or (int(w.get("RemainingSeconds", 1)) <= 0 and w.get("State") == "Completed")
            ]
            incomplete_in_fac = [
                w for w in f_works 
                if not w.get("IsCompleted") and int(w.get("RemainingSeconds", 0)) > 0
            ]

            if not completed_in_fac:
                continue

            if only_fully_completed and incomplete_in_fac:
                # Facility still has works in progress! Skip it (ignore partial completion).
                self.log(
                    f"[{fac}] 아직 진행 중인 작업({len(incomplete_in_fac)}슬롯)이 남아있어 자동 수령을 대기합니다. "
                    f"시설의 모든 작업이 끝나면 일괄 수령합니다. ({len(completed_in_fac)}슬롯 완료 대기 중)",
                    "info", callback
                )
                continue

            facility_to_items[fac] = [w.get("DisplayName") for w in completed_in_fac if w.get("DisplayName")]

        if not facility_to_items:
            return []

        collected_facilities = []
        facilities = list(facility_to_items.keys())
        if preferred_facility and preferred_facility in facilities:
            facilities.remove(preferred_facility)
            facilities.insert(0, preferred_facility)

        total_collectable_slots = sum(len(items) for items in facility_to_items.values())
        mode_desc = "모든 작업 완료 시설" if only_fully_completed else "완료 가공품"
        self.log(f"[{mode_desc} 감지] 총 {len(facilities)}개 시설({total_collectable_slots}슬롯)의 수령을 진행합니다.", "action", callback)

        for fac in facilities:
            if self.abort_requested:
                break
            # Weight check before collecting next facility (98% limit)
            try:
                inv = self.cli.get_inventory()
                cur_w = float(inv.get("CurrentInventoryWeightAsDecimal", 0) or inv.get("CurrentInventoryWeight", 0) or 0)
                max_w = float(inv.get("MaxInventoryWeightAsDecimal", 100) or inv.get("MaxInventoryWeight", 100) or 100)
                limit_ratio = get_weight_limit_ratio()
                if max_w > 0 and (cur_w / max_w) >= limit_ratio:
                    pct = (cur_w / max_w) * 100
                    limit_pct = limit_ratio * 100
                    self._last_collect_blocked_reason = "overweight"
                    self.log(f"[가방 과적 경고] 가방 무게({cur_w:.1f}/{max_w:.1f}, {pct:.1f}%)가 {limit_pct:.0f}% 기준에 도달하여 가공품 수령을 안전하게 멈춥니다.", "warn", callback)
                    break
            except Exception:
                pass
            items = facility_to_items[fac]
            rep_item = items[0]
            self.log(f"   ▶ '{fac}' 걸어둔 작업 전체 완료 수령 진행 ({len(items)}건, '{rep_item}' 등)...", "action", callback)
            try:
                res = self.cli.complete_altering_work(rep_item)
                collected_facilities.append(fac)
                crit_rewards = res.get("criticalRewards", []) if isinstance(res, dict) else []
                crit_str = ""
                if crit_rewards:
                    c_parts = [f"{cr.get('Name', rep_item)} +{cr.get('Amount', 0)}개" for cr in crit_rewards if cr.get("Amount", 0) > 0]
                    if c_parts:
                        crit_str = f" (✨ 대성공 추가 획득: {', '.join(c_parts)})"
                self.log(f"     '{fac}' 완료 가공품 수령 완료!{crit_str} (슬롯 확보 완료)", "success", callback)
                # Clean up alarms for this facility
                alarm_store.delete_alarm(fac)
                for it in items:
                    alarm_store.delete_alarm(f"{fac}_{it}")
                time.sleep(0.35)
            except Exception as ex:
                ex_str = str(ex)
                ex_data = ex.response_data if isinstance(ex, MabinogiCLIError) and isinstance(ex.response_data, dict) else {}
                if "continuous_run_limit" in ex_str or ex_data.get("kind") == "continuous_run_limit":
                    self._last_collect_blocked_reason = "continuous_run_limit"
                    self.log(
                        f"     '{fac}' 수령 중 연속 실행 확인 팝업 감지: 인게임에서 '확인' 버튼을 눌러주시면 자동으로 수령을 재개합니다.",
                        "warn", callback
                    )
                    break
                self.log(f"     '{fac}' 수령 중 알림/오류: {ex}", "warn", callback)
        if collected_facilities:
            self.notify_altering_updated()

        return collected_facilities

    def auto_alter(self, item_name: str, works_needed: int, callback: Optional[LogCallback] = None, is_delivery: bool = True, skip_pre_collect: bool = False):
        """Queues works_needed altering tasks, sets delivery alarm, and manages waiting."""
        recipe = self.find_alter_recipe(item_name)
        if not recipe:
            raise MabinogiCLIError(f"'{item_name}' 가공 레시피를 찾을 수 없습니다.")

        # Use the recipe's actual DisplayName for execute_altering
        # e.g. "철괴" -> "철괴(철 광석)" to ensure the correct recipe variant is used
        alter_display_name = recipe.get("DisplayName", item_name)

        target_facility = get_facility_for_material(item_name)

        # 1. Check and collect any completed altering slots across facilities before submitting new work!
        if not skip_pre_collect:
            self.collect_completed_altering_works(preferred_facility=target_facility, callback=callback)

        successful_works = 0
        for i in range(works_needed):
            if self.abort_requested:
                self.log("[사용자 중지] 가공 등록이 중단되었습니다.", "warn", callback)
                break
            self.log(f"[가공 등록 {i+1}/{works_needed}] '{alter_display_name}' 가공 대기열에 등록합니다.", "action", callback)
            try:
                res = self.cli.execute_altering(alter_display_name)
                self.log(f"   등록 결과: {res}", "info", callback)
                successful_works += 1
            except MabinogiCLIError as err:
                err_str = str(err).lower()
                err_data = err.response_data if isinstance(err.response_data, dict) else {}
                err_code = str(err_data.get("error", "")).lower()

                # Self-healing: In-game transmitter remainder shortage (1~3 items in bag)
                if "not_enough_ingredient" in err_str or "not_enough_ingredient" in err_code or "재료 부족" in err_str:
                    is_alt, alt_info = self.get_alter_info(item_name)
                    healed = False
                    if is_alt and alt_info and alt_info.get("ingredients"):
                        for ing_name, ing_req in alt_info["ingredients"].items():
                            inv_cnt = self.cli.count_item(ing_name, include_storage=False)
                            if inv_cnt < ing_req:
                                deficit_for_one = ing_req - inv_cnt
                                is_gath, _ = self.is_gatherable(ing_name)
                                if is_gath:
                                    self.log(
                                        f"[전송기 보충 채집] 가방에 '{ing_name}'이(가) {inv_cnt}개 남아 1회분({ing_req}개) 합산 결제가 지연되었습니다. "
                                        f"가방 보충을 위해 부족분 {deficit_for_one}개를 즉시 현장 채집합니다.",
                                        "action",
                                        callback
                                    )
                                    gather_res = self.auto_gather(ing_name, deficit_for_one, is_delivery=False, callback=callback)
                                    if gather_res.get("gained", 0) > 0 or gather_res.get("status") == "success":
                                        healed = True
                    if healed:
                        try:
                            res = self.cli.execute_altering(alter_display_name)
                            self.log(f"   [전송기 보충 후 재시도 성공] {res}", "success", callback)
                            successful_works += 1
                            time.sleep(1)
                            continue
                        except MabinogiCLIError as retry_err:
                            self.log(f"   보충 후 가공 재시도 실패 ({retry_err}). 등록을 보류하고 다음 작업으로 진행합니다.", "warn", callback)
                            return {"status": "ingredient_missing_skipped", "item": item_name, "registered": successful_works, "facility": target_facility, "error": str(retry_err)}
                    else:
                        self.log(f"   '{alter_display_name}' 가공 재료 부족 ({err}). 남은 {works_needed - successful_works}회 등록을 보류하고 다음 작업으로 진행합니다.", "warn", callback)
                        return {"status": "ingredient_missing_skipped", "item": item_name, "registered": successful_works, "facility": target_facility, "error": str(err)}

                self.log(f"   가공 등록 중 오류: {err}. 완료 가공 슬롯 재확인 후 1회 재시도합니다.", "warn", callback)
                self.collect_completed_altering_works(preferred_facility=target_facility, callback=callback)
                try:
                    res = self.cli.execute_altering(alter_display_name)
                    self.log(f"   재시도 등록 결과: {res}", "success", callback)
                    successful_works += 1
                except MabinogiCLIError as retry_err:
                    self.log(f"   '{alter_display_name}' 가공 슬롯 부족/오류 ({retry_err}). 남은 {works_needed - successful_works}회 등록을 보류하고 다음 작업으로 진행합니다.", "warn", callback)
                    return {"status": "slot_full_skipped", "item": item_name, "registered": successful_works, "facility": target_facility, "error": str(retry_err)}
            time.sleep(0.35)

        if successful_works == 0:
            return {"status": "slot_full_skipped", "item": item_name, "registered": 0, "facility": target_facility}

        # Check remaining time after queueing
        works_info = self.cli.get_altering_works()
        pending_works = [w for w in works_info.get("works", []) if w.get("DisplayName") == item_name and not w.get("IsCompleted")]
        max_remaining = max([w.get("RemainingSeconds", 0) for w in pending_works], default=0)

        # Check all works at this facility to find when the LAST work finishes
        facility_name = get_facility_for_material(item_name)
        f_works = [
            w for w in works_info.get("works", [])
            if (w.get("FacilityName") == facility_name or get_facility_for_material(w.get("DisplayName", "")) == facility_name)
            and not w.get("IsCompleted")
        ]
        if f_works and f_works[0].get("FacilityName"):
            facility_name = f_works[0].get("FacilityName")

        total_facility_remaining = sum(int(w.get("RemainingSeconds", 0)) for w in f_works)
        if total_facility_remaining == 0:
            total_facility_remaining = max_remaining

        # Register facility-wide alarm on the LAST work completion
        if is_delivery and total_facility_remaining > 0:
            item_summary = f"{item_name} ({len(f_works)}슬롯)" if len(f_works) > 1 else item_name
            alarm_store.add_facility_alarm(
                facility=facility_name,
                items_summary=item_summary,
                total_remaining_seconds=total_facility_remaining,
                source="주간 납품",
                works_count=len(f_works)
            )
            rem_m = math.ceil(total_facility_remaining / 60)
            self.log(f"[가공시설 알람 등록] '{facility_name}'의 마지막 가공 완료 시각(약 {rem_m}분 뒤)에 맞춰 알람이 등록되었습니다.", "info", callback)

        # Non-blocking transition if remaining time > 45s
        if total_facility_remaining > 45:
            remaining_min = math.ceil(total_facility_remaining / 60)
            self.log(f"[비동기 백그라운드 전환] '{facility_name}' 가공 완료까지 약 {remaining_min}분({total_facility_remaining}초) 소요됩니다.", "info", callback)
            self.log(f"캐릭터 조작이 즉시 자유화되었습니다! 편하게 사냥, 던전 등 다른 콘텐츠를 플레이하세요.", "success", callback)
            self.log(f"마지막 가공이 끝나면 알람이 울리며 웹 대시보드에서 즉시 수령 후 제작을 진행할 수 있습니다.", "info", callback)
            return {"status": "waiting_in_background", "item": item_name, "remaining_seconds": total_facility_remaining, "registered": successful_works, "facility": facility_name}

        # Short wait
        self.log(f"[가공 대기] '{item_name}' 가공 완료를 잠시 기다립니다 (약 {max_remaining}초)...", "info", callback)
        while True:
            works_info = self.cli.get_altering_works()
            pending_works = [w for w in works_info.get("works", []) if w.get("DisplayName") == item_name and not w.get("IsCompleted")]

            if not pending_works:
                self.log(f"[가공 완료 확인] 모든 '{item_name}' 작업이 완료되었습니다.", "success", callback)
                break

            remaining_sec = max([w.get("RemainingSeconds", 0) for w in pending_works], default=5)
            time.sleep(min(5, max(1, remaining_sec)))

        # Collect
        self.log(f"[가공품 수령] '{item_name}' 가공 결과물을 수령합니다.", "action", callback)
        coll_res = self.cli.complete_altering_work(item_name)
        self.log(f"   수령 결과: {coll_res}", "success", callback)
        return coll_res

    def auto_craft(self, item_name: str, craft_count: int, callback: Optional[LogCallback] = None) -> int:
        """
        Crafts item_name with batching support, dynamic ProducedPerCraft ratio,
        and immediate early-exit once the required quantity is fulfilled to prevent over-crafting.
        """
        needed_target = max(1, craft_count)
        produced_per_craft = max(1, self.get_produced_per_craft(item_name))
        total_produced = 0
        remaining_needed = needed_target
        max_batch_limit = None

        try:
            while remaining_needed > 0:
                crafts_needed = math.ceil(remaining_needed / produced_per_craft)
                batch = crafts_needed if max_batch_limit is None else min(crafts_needed, max_batch_limit)
                self.log(
                    f"[제작 시작] '{item_name}' {batch}회 제작 시도... "
                    f"(필요: {remaining_needed}개, 1회당 산출: {produced_per_craft}개)",
                    "action",
                    callback
                )
                try:
                    res = self.cli.execute_crafting(item_name, craft_count=batch)
                    produced_now = 0
                    crit_now = 0
                    for r in res.get("rewards", []):
                        if r.get("Name") == item_name:
                            produced_now += r.get("Amount", 0)
                    for r in res.get("criticalRewards", []):
                        if r.get("Name") == item_name:
                            c_amt = r.get("Amount", 0)
                            produced_now += c_amt
                            crit_now += c_amt

                    items_made = max(produced_now, batch * produced_per_craft)
                    total_produced += items_made
                    crit_msg = f" [✨ 대성공 보너스 +{crit_now}개!]" if crit_now > 0 else ""
                    self.log(f"[제작 성공] {batch}회 제작 완료 (산출: +{items_made}개{crit_msg} / 누적: {total_produced}/{needed_target}개)", "success", callback)

                    remaining_needed = max(0, needed_target - total_produced)
                    if remaining_needed <= 0:
                        self.log(f"[목표 달성] '{item_name}' 필요 수량({needed_target}개) 제작이 완료되어 추가 제작을 종료합니다.", "success", callback)
                        break
                except MabinogiCLIError as err:
                    data = err.response_data or {}
                    err_code = data.get("error") if isinstance(data, dict) else ""

                    if err_code == "invalid_count":
                        max_count = data.get("maxCount", 1)
                        max_batch_limit = max_count
                        batch = min(crafts_needed, max_count)
                        self.log(f"1회 최대 제작량 제한({max_count}회)으로 분할 제작합니다.", "warn", callback)
                        try:
                            res = self.cli.execute_crafting(item_name, craft_count=batch)
                            produced_now = 0
                            for r in res.get("rewards", []):
                                if r.get("Name") == item_name:
                                    produced_now += r.get("Amount", 0)
                            for r in res.get("criticalRewards", []):
                                if r.get("Name") == item_name:
                                    produced_now += r.get("Amount", 0)
                            items_made = max(produced_now, batch * produced_per_craft)
                            total_produced += items_made
                            remaining_needed = max(0, needed_target - total_produced)
                            self.log(f"[분할 제작 성공] {batch}회 완료 (산출: +{items_made}개 / 누적: {total_produced}/{needed_target}개)", "success", callback)
                            if remaining_needed <= 0:
                                break
                            continue
                        except Exception as retry_err:
                            self.log(f"분할 제작 시도 중 오류: {retry_err}", "warn", callback)
                            raise retry_err
                    elif err_code == "not_enough_ingredient":
                        if batch > 1:
                            batch = max(1, batch // 2)
                            self.log(f"재료 부족으로 제작 수량을 {batch}회로 낮추어 재시도합니다.", "warn", callback)
                            try:
                                res = self.cli.execute_crafting(item_name, craft_count=batch)
                                produced_now = 0
                                for r in res.get("rewards", []):
                                    if r.get("Name") == item_name:
                                        produced_now += r.get("Amount", 0)
                                for r in res.get("criticalRewards", []):
                                    if r.get("Name") == item_name:
                                        produced_now += r.get("Amount", 0)
                                items_made = max(produced_now, batch * produced_per_craft)
                                total_produced += items_made
                                remaining_needed = max(0, needed_target - total_produced)
                                self.log(f"[감소 제작 성공] {batch}회 완료 (산출: +{items_made}개)", "success", callback)
                                if remaining_needed <= 0:
                                    break
                                continue
                            except Exception:
                                pass
                        # Self-healing for crafting:
                        recipe = self.get_recipe_ingredients(item_name)
                        healed = False
                        if recipe:
                            for ing_name, per_craft in recipe.items():
                                inv_cnt = self.cli.count_item(ing_name, include_storage=False)
                                if inv_cnt < per_craft:
                                    deficit_for_one = per_craft - inv_cnt
                                    is_gath, _ = self.is_gatherable(ing_name)
                                    if is_gath:
                                        self.log(
                                            f"[전송기 보충 채집] 가방에 '{ing_name}'이(가) {inv_cnt}개 남아 1회분({per_craft}개) 합산 결제가 지연되었습니다. "
                                            f"가방 보충을 위해 부족분 {deficit_for_one}개를 즉시 현장 채집합니다.",
                                            "action",
                                            callback
                                        )
                                        gather_res = self.auto_gather(ing_name, deficit_for_one, is_delivery=False, callback=callback)
                                        if gather_res.get("gained", 0) > 0 or gather_res.get("status") == "success":
                                            healed = True
                        if healed:
                            try:
                                res = self.cli.execute_crafting(item_name, craft_count=1)
                                produced_now = 0
                                for r in res.get("rewards", []):
                                    if r.get("Name") == item_name:
                                        produced_now += r.get("Amount", 0)
                                for r in res.get("criticalRewards", []):
                                    if r.get("Name") == item_name:
                                        produced_now += r.get("Amount", 0)
                                items_made = max(produced_now, produced_per_craft)
                                total_produced += items_made
                                remaining_needed = max(0, needed_target - total_produced)
                                self.log(f"[전송기 보충 후 제작 성공] 1회 제작 완료 (산출: +{items_made}개)", "success", callback)
                                if remaining_needed <= 0:
                                    break
                                continue
                            except MabinogiCLIError as retry_err:
                                self.log(f"보충 후 제작 재시도 실패 ({retry_err})", "warn", callback)
                                raise retry_err
                        else:
                            raise err
                    else:
                        raise err
        finally:
            # Always record any produced items (including partial crafts before an error)
            if total_produced > 0:
                if item_name in delivery_target_store.targets:
                    t = delivery_target_store.targets[item_name]
                    prev = int(t.get("current", 0))
                    new_cur = prev + total_produced
                    goal = int(t.get("goal", 1))
                    t["current"] = new_cur
                    if t.get("override_current") is not None:
                        t["override_current"] = new_cur
                    t["needed"] = max(0, goal - new_cur)
                    t["is_completed"] = (new_cur >= goal)
                    t["last_crafted_at"] = time.time()
                    t["updated_at"] = int(time.time() * 1000)
                    delivery_target_store.targets[item_name] = t
                    delivery_target_store.save()
                    self.log(f"[납품 목표 갱신] '{item_name}' 제작 완료(+{total_produced}개 산출) 반영 -> 현재 {new_cur}/{goal}개", "info", callback)

                if item_name in custom_target_store.targets:
                    ct = custom_target_store.targets[item_name]
                    prev_c = int(ct.get("current", 0))
                    new_cur_c = prev_c + total_produced
                    goal_c = int(ct.get("goal", 1))
                    ct["current"] = new_cur_c
                    if ct.get("override_current") is not None:
                        ct["override_current"] = new_cur_c
                    ct["needed"] = max(0, goal_c - new_cur_c)
                    ct["is_completed"] = (new_cur_c >= goal_c)
                    ct["updated_at"] = int(time.time() * 1000)
                    custom_target_store.targets[item_name] = ct
                    custom_target_store.save()
                    self.log(f"[개별 제작 목표 갱신] '{item_name}' 제작 완료(+{total_produced}개 산출) 반영 -> 현재 {new_cur_c}/{goal_c}개", "info", callback)

        return total_produced

    @staticmethod
    def _record_task_progress(tasks: List[Dict[str, Any]], item_name: str, delta_qty: int) -> None:
        """Applies crafted/gathered progress directly to in-memory batch tasks so BOM never stays stale."""
        if delta_qty <= 0 or not item_name:
            return
        for t in tasks:
            if t.get("item_name") != item_name:
                continue
            prev_cur = int(t.get("current", 0) or 0)
            goal = int(t.get("goal", 1) or 1)
            prev_needed = int(t.get("needed", max(0, goal - prev_cur)) or 0)
            new_cur = prev_cur + delta_qty
            new_needed = max(0, prev_needed - delta_qty)
            if t.get("quest_title") != "개별 지정 제작" and t.get("override_current") is None:
                new_needed = min(new_needed, max(0, goal - new_cur))
            t["current"] = new_cur
            if t.get("override_current") is not None:
                t["override_current"] = new_cur
            t["needed"] = new_needed
            t["is_completed"] = (new_needed <= 0)

    def _refresh_custom_tasks_from_inventory(self, tasks: List[Dict[str, Any]]) -> None:
        """Updates non-overridden ad-hoc tasks against live inventory without losing recorded equipment progress."""
        for t in tasks:
            if t.get("override_current") is not None:
                continue
            item_name = t.get("item_name", "")
            if not item_name:
                continue
            is_custom_t = (t.get("quest_title") == "개별 지정 제작")
            goal = int(t.get("goal", 1) or 1)
            prev_cur = int(t.get("current", 0) or 0)
            live_cnt = self.cli.count_item(item_name, include_storage=is_custom_t)
            # Keep max with prev_cur because get_items may omit crafted equipment
            new_cur = max(prev_cur, live_cnt)
            new_needed = max(0, goal - new_cur)
            t["current"] = new_cur
            t["needed"] = new_needed
            t["is_completed"] = (new_needed <= 0)

    def _reanalyze_execution_plan(
        self,
        plan_source: str,
        tasks: List[Dict[str, Any]],
        alter_order: str,
        session_crafted_items: Optional[set] = None,
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """Invalidates CLI caches and recomputes the live batch BOM using current targets & stock."""
        self.cli.invalidate_cache()
        prev_by_item = {t.get("item_name"): t for t in tasks if t.get("item_name")}

        if plan_source == "weekly_delivery":
            fresh_plan = self.analyze_batch_plan(custom_tasks=None, alter_order=alter_order)
            # Guard unregistered quest-only crafted equipment omitted by get_items/quest tracker
            if session_crafted_items:
                needs_patch = False
                for t_new in fresh_plan.get("tasks", []):
                    nm = t_new.get("item_name")
                    prev_t = prev_by_item.get(nm)
                    if nm in session_crafted_items and prev_t and int(t_new.get("needed", 0) or 0) > int(prev_t.get("needed", 0) or 0):
                        t_new["current"] = max(int(t_new.get("current", 0) or 0), int(prev_t.get("current", 0) or 0))
                        t_new["needed"] = int(prev_t.get("needed", 0) or 0)
                        t_new["is_completed"] = (t_new["needed"] <= 0)
                        needs_patch = True
                if needs_patch:
                    fresh_plan = self.analyze_batch_plan(custom_tasks=fresh_plan.get("tasks", []), alter_order=alter_order)
                    fresh_plan["plan_source"] = "weekly_delivery"
        elif plan_source == "custom_targets":
            fresh_plan = self.analyze_custom_plan(alter_order=alter_order)
        else:
            self._refresh_custom_tasks_from_inventory(tasks)
            fresh_plan = self.analyze_batch_plan(custom_tasks=tasks, alter_order=alter_order)

        return fresh_plan, fresh_plan.get("tasks", tasks)

    def _sleep_with_abort(
        self,
        seconds: float,
        status_reporter: Optional[Callable[[str], None]] = None,
        status_text: str = "",
    ) -> None:
        """Sleeps in short increments while honoring abort_requested and reporting status."""
        wake_time = time.time() + max(0.0, seconds)
        while time.time() < wake_time:
            if self.abort_requested:
                break
            rem = max(0.0, wake_time - time.time())
            if status_reporter and status_text:
                status_reporter(status_text)
            time.sleep(min(1.0, max(0.2, rem)))

    # =========================================================================
    # UNIFIED BATCH PRODUCTION PLANNER (통합 일괄 납품 플래너)
    # =========================================================================

    def analyze_batch_plan(self, custom_tasks: Optional[List[Dict[str, Any]]] = None, alter_order: Optional[str] = None) -> Dict[str, Any]:
        """
        Full Recursive Bill-of-Materials (BOM) Batch Planner:
        1. Determines active/registered tasks to produce.
        2. Queries inventory, character storage, account storage, and altering works once.
        3. Recursively expands intermediate materials down to field gatherables:
           - Correctly traverses multi-tier dependencies (e.g. 합금강괴 -> 강철괴 -> 철괴 -> 철 광석).
           - Accounts for items already owned or queued in altering.
           - Calculates exact deficit for each field gatherable item.
        4. Sorts altering works according to alter_order preference (high_tier: T7->T1, low_tier: T1->T7).
        5. Computes per-facility slot usage (out of 7) and slot warnings.
        """
        if alter_order is None:
            alter_order = settings_store.get("alter_order", "high_tier")
        if alter_order not in ("high_tier", "low_tier"):
            alter_order = "high_tier"

        tasks_to_plan: List[Dict[str, Any]] = []

        if custom_tasks is not None:
            tasks_to_plan = custom_tasks
        else:
            # 1. First check registered targets
            registered = self.get_registered_deliveries_with_status()
            existing_items = set()
            if registered:
                for t in registered:
                    existing_items.add(t["item_name"])
                    tasks_to_plan.append({
                        "quest_title": t.get("quest_title", "주간 납품"),
                        "item_name": t["item_name"],
                        "current": t["current"],
                        "goal": t["goal"],
                        "needed": t["needed"],
                        "override_current": t.get("override_current"),
                        "is_completed": t.get("is_completed", False)
                    })
            # 2. Automatically merge active quests in Quest Tracker that are not yet in registered list (only when active character is logged in)
            if delivery_target_store.is_active_character_ingame():
                active_quests = self.detect_delivery_quests()
                for q in active_quests:
                    if q.item_name not in existing_items:
                        tasks_to_plan.append({
                            "quest_title": q.quest_title,
                            "item_name": q.item_name,
                            "current": q.current,
                            "goal": q.goal,
                            "needed": q.needed,
                            "is_completed": q.is_completed
                        })

        if not tasks_to_plan:
            return {
                "plan_source": "custom_tasks" if custom_tasks is not None else "weekly_delivery",
                "tasks": [],
                "intermediate_requirements": [],
                "raw_materials": [],
                "can_craft_immediately": False,
                "all_completed": False,
                "alter_order": alter_order,
                "message": "등록되거나 진행 중인 주간 납품 퀘스트가 없습니다."
            }

        # 1. Query altering works queue
        try:
            works_info = self.cli.get_altering_works()
        except Exception:
            works_info = {"works": []}
        queued_works = works_info.get("works", [])

        MAX_SLOTS = 7
        facility_slot_usage: Dict[str, Dict[str, int]] = defaultdict(lambda: {
            "in_progress": 0,
            "completed": 0,
            "total_used": 0,
            "available": MAX_SLOTS,
            "in_progress_items": defaultdict(int),
            "completed_items": defaultdict(int),
        })
        completed_yield_by_item = defaultdict(int)
        queued_yield_by_item = defaultdict(int)
        for w in queued_works:
            w_name = w.get("DisplayName", "")
            clean_w = w_name.split("(")[0].strip()
            fac = w.get("FacilityName") or get_facility_for_material(w_name)
            is_comp = bool(w.get("IsCompleted"))
            is_alt, alt_inf = self.get_alter_info(w_name)
            yield_pw = alt_inf.get("produced", 3) if is_alt else 3

            if is_comp:
                facility_slot_usage[fac]["completed"] += 1
                facility_slot_usage[fac]["completed_items"][clean_w] += 1
                completed_yield_by_item[clean_w] += yield_pw
            else:
                facility_slot_usage[fac]["in_progress"] += 1
                facility_slot_usage[fac]["in_progress_items"][clean_w] += 1
                queued_yield_by_item[clean_w] += yield_pw

            facility_slot_usage[fac]["total_used"] = (
                facility_slot_usage[fac]["in_progress"] + facility_slot_usage[fac]["completed"]
            )
            facility_slot_usage[fac]["available"] = max(
                0, MAX_SLOTS - facility_slot_usage[fac]["total_used"]
            )

        # 2. Stock cache & virtual stock
        stock_cache: Dict[str, Dict[str, int]] = {}
        def get_stock(name: str) -> Dict[str, int]:
            clean_n = name.split("(")[0].strip()
            if clean_n not in stock_cache:
                try:
                    stock_cache[clean_n] = self.cli.get_item_location_breakdown(clean_n)
                except Exception:
                    stock_cache[clean_n] = {
                        "inventory": 0, "character_storage": 0,
                        "account_storage": 0, "storage_total": 0, "total": 0
                    }
            return stock_cache[clean_n]

        virtual_stock: Dict[str, int] = defaultdict(int)
        def get_v_stock(name: str) -> int:
            clean_n = name.split("(")[0].strip()
            if clean_n not in virtual_stock:
                bd = get_stock(clean_n)
                # Materials in inventory or personal/account storage (via Material Transmitter) are consumable.
                # Combined stock prevents unnecessary gathering and saves Spirit Wings & inventory weight.
                tot = bd["total"] + completed_yield_by_item[clean_n] + queued_yield_by_item[clean_n]
                virtual_stock[clean_n] = tot
            return virtual_stock[clean_n]

        def consume_v_stock(name: str, needed_qty: int) -> int:
            clean_n = name.split("(")[0].strip()
            avail = get_v_stock(clean_n)
            used = min(avail, needed_qty)
            virtual_stock[clean_n] = avail - used
            return used

        # 3. Resolve Finished Goods, Alterable Delivery Items & Direct Gatherable Delivery Items
        recipe_details: Dict[str, Dict[str, int]] = {}
        pending_demands: Dict[str, int] = defaultdict(int)
        raw_gather_demands: Dict[str, int] = defaultdict(int)
        raw_total_demands: Dict[str, int] = defaultdict(int)
        alter_plan: Dict[str, Dict[str, Any]] = {}
        intermediate_total_demands: Dict[str, int] = defaultdict(int)
        craftable_tasks_count = 0

        for t in tasks_to_plan:
            it_name = t["item_name"]
            needed = t.get("needed", 0)
            clean_it = it_name.split("(")[0].strip()
            is_gath, _ = self.is_gatherable(it_name)

            # Check if this delivery item is a raw gatherable item (e.g. 양털, 우유, 달걀, 사과, 감자 등)
            if is_gath or clean_it in KNOWN_GATHERABLE_ITEMS:
                t["is_gather_item"] = True
                recipe_details[it_name] = {}
                # CRITICAL: Direct delivery targets MUST be physically in the inventory (가방) to submit to NPC!
                # Warehouse / storage counts cannot be submitted directly without manual withdrawal.
                # Therefore, only inventory count satisfies direct delivery demand:
                bd_stock = get_stock(clean_it)
                if t.get("override_current") is not None:
                    cur_bag = int(t["override_current"])
                elif custom_tasks is None and not delivery_target_store.is_active_character_ingame():
                    cur_bag = int(t.get("current", 0))
                else:
                    cur_bag = bd_stock["inventory"]
                goal_qty = t.get("goal") or (cur_bag + needed)
                t["current"] = cur_bag
                net_def = max(0, goal_qty - cur_bag)
                t["needed"] = net_def
                t["is_completed"] = (cur_bag >= goal_qty)

                if goal_qty > 0:
                    raw_total_demands[clean_it] += goal_qty

                # Deduct only used inventory amount from virtual_stock so other downstream crafts don't double-consume it:
                used_inv = min(cur_bag, goal_qty)
                if clean_it not in virtual_stock:
                    get_v_stock(clean_it)
                virtual_stock[clean_it] = max(0, virtual_stock[clean_it] - used_inv)

                if net_def > 0:
                    raw_gather_demands[clean_it] += net_def
                continue

            if needed <= 0:
                continue

            # Check if this delivery item is an alterable item (e.g. 옷감+, 강철괴, 목재+ 등)
            is_alt, alt_info = self.get_alter_info(clean_it)
            if is_alt and alt_info:
                t["is_alter_item"] = True
                recipe_details[it_name] = alt_info.get("ingredients", {})
                pending_fin = completed_yield_by_item.get(clean_it, 0) + queued_yield_by_item.get(clean_it, 0)
                net_needed = max(0, needed - pending_fin)
                yield_pw = alt_info.get("produced", 3)
                fac = alt_info.get("facility") or get_facility_for_material(clean_it)
                tier = alt_info.get("tier", 1)
                recipe_dn = alt_info.get("display_name", clean_it)

                if clean_mat := clean_it:
                    if clean_mat not in alter_plan:
                        alter_plan[clean_mat] = {
                            "item_name": clean_mat,
                            "recipe_name": recipe_dn,
                            "facility": fac,
                            "tier": tier,
                            "ingredients": dict(alt_info.get("ingredients", {})),
                            "works_needed": 0,
                            "yield_per_work": yield_pw,
                            "total_needed": 0,
                            "net_needed": 0,
                        }
                    alter_plan[clean_mat]["total_needed"] += needed
                    intermediate_total_demands[clean_mat] += needed

                if net_needed > 0:
                    works = math.ceil(net_needed / yield_pw)
                    if clean_mat := clean_it:
                        alter_plan[clean_mat]["works_needed"] += works
                        alter_plan[clean_mat]["net_needed"] += net_needed

                    for sub_name, sub_req in alt_info.get("ingredients", {}).items():
                        pending_demands[sub_name] += sub_req * works
                continue

            # Finished Goods (crafted items)
            # t["needed"] is already net deficit (goal - current).
            # Do NOT subtract virtual_stock of finished goods again (double-subtraction bug)!
            # Only subtract pending yields from uncollected altering queue:
            pending_fin = completed_yield_by_item.get(clean_it, 0) + queued_yield_by_item.get(clean_it, 0)
            net_needed = max(0, needed - pending_fin)

            rec = self.get_recipe_ingredients(it_name)
            recipe_details[it_name] = rec

            if net_needed > 0:
                prod_pc = max(1, self.get_produced_per_craft(it_name))
                crafts_needed = math.ceil(net_needed / prod_pc)
                for ing_name, per_craft in rec.items():
                    pending_demands[ing_name] += per_craft * crafts_needed

        # 4. Multi-tier Recursive BOM Resolution

        depth = 0
        MAX_DEPTH = 15

        while pending_demands and depth < MAX_DEPTH:
            depth += 1
            curr_demands = list(pending_demands.items())
            pending_demands.clear()

            for mat_name, demand_qty in curr_demands:
                if demand_qty <= 0:
                    continue
                clean_mat = mat_name.split("(")[0].strip()

                used = consume_v_stock(clean_mat, demand_qty)
                net_def = demand_qty - used

                is_alt, alt_info = self.get_alter_info(clean_mat)

                if is_alt:
                    intermediate_total_demands[clean_mat] += demand_qty
                    yield_pw = alt_info.get("produced", 3)
                    fac = alt_info.get("facility") or get_facility_for_material(clean_mat)
                    tier = alt_info.get("tier", 1)
                    recipe_dn = alt_info.get("display_name", clean_mat)

                    if clean_mat not in alter_plan:
                        alter_plan[clean_mat] = {
                            "item_name": clean_mat,
                            "recipe_name": recipe_dn,
                            "facility": fac,
                            "tier": tier,
                            "ingredients": dict(alt_info.get("ingredients", {})),
                            "works_needed": 0,
                            "yield_per_work": yield_pw,
                            "total_needed": 0,
                            "net_needed": 0,
                        }
                    alter_plan[clean_mat]["total_needed"] += demand_qty

                    if net_def > 0:
                        works = math.ceil(net_def / yield_pw)
                        alter_plan[clean_mat]["works_needed"] += works
                        alter_plan[clean_mat]["net_needed"] += net_def

                        for sub_name, sub_req in alt_info.get("ingredients", {}).items():
                            pending_demands[sub_name] += sub_req * works
                else:
                    raw_total_demands[clean_mat] += demand_qty
                    if net_def > 0:
                        raw_gather_demands[clean_mat] += net_def

        # 5. Build Intermediate Requirements Analysis
        if alter_order == "low_tier":
            sorted_alters = sorted(alter_plan.values(), key=lambda x: (x["tier"], x["item_name"]))
        else:  # "high_tier" (상위 티어부터 / 오래 걸리는 가공 우선)
            sorted_alters = sorted(alter_plan.values(), key=lambda x: (-x["tier"], x["item_name"]))
        intermediate_analysis: List[Dict[str, Any]] = []
        total_works_to_queue = 0
        facility_new_works_demand: Dict[str, int] = defaultdict(int)

        for a in sorted_alters:
            mat_name = a["item_name"]
            bd = get_stock(mat_name)
            queued_cnt = len([w for w in queued_works if (w.get("DisplayName") == mat_name or w.get("DisplayName", "").startswith(mat_name + "(")) and not w.get("IsCompleted")])
            comp_cnt = len([w for w in queued_works if (w.get("DisplayName") == mat_name or w.get("DisplayName", "").startswith(mat_name + "(")) and w.get("IsCompleted")])
            queued_y = queued_yield_by_item.get(mat_name, 0)
            comp_y = completed_yield_by_item.get(mat_name, 0)

            w_needed = a["works_needed"]
            tot_needed = a["total_needed"]
            net_needed = a["net_needed"]
            fac = a["facility"]

            status = "satisfied"
            if w_needed > 0:
                status = "needs_alter"
                total_works_to_queue += w_needed
                facility_new_works_demand[fac] += w_needed
            elif queued_y > 0 and bd["total"] < tot_needed:
                status = "in_queue"
            elif comp_y > 0 and bd["total"] < tot_needed:
                status = "ready_to_collect"

            intermediate_analysis.append({
                "item_name": mat_name,
                "recipe_name": a["recipe_name"],
                "facility": fac,
                "tier": a["tier"],
                "ingredients": a.get("ingredients", {}),
                "total_needed": tot_needed,
                "inventory_count": bd["inventory"],
                "character_storage_count": bd["character_storage"],
                "account_storage_count": bd["account_storage"],
                "storage_count": bd["storage_total"],
                "total_owned": bd["total"],
                "queued_works_count": queued_cnt,
                "completed_works_count": comp_cnt,
                "queued_yield": queued_y,
                "completed_yield": comp_y,
                "net_needed": net_needed,
                "deficit": net_needed,
                "works_needed": w_needed,
                "works_to_queue": w_needed,
                "in_progress_count": queued_cnt,
                "is_ready": (status == "satisfied"),
                "status": status
            })

        # 6. Build Raw Materials Analysis
        raw_materials_analysis: List[Dict[str, Any]] = []
        for raw_name in sorted(raw_total_demands.keys()):
            tot_needed = raw_total_demands[raw_name]
            actual_def = raw_gather_demands.get(raw_name, 0)
            bd = get_stock(raw_name)
            is_gath, tool_ok = self.is_gatherable(raw_name)
            is_special = is_non_gatherable_material(raw_name) or not is_gath

            if is_special:
                status = "satisfied" if actual_def == 0 else "special_obtain"
            elif actual_def == 0:
                status = "satisfied"
            elif tool_ok:
                status = "ready_to_gather"
            else:
                status = "tool_missing"

            raw_materials_analysis.append({
                "item_name": raw_name,
                "total_needed": tot_needed,
                "inventory_count": bd["inventory"],
                "character_storage_count": bd["character_storage"],
                "account_storage_count": bd["account_storage"],
                "storage_count": bd["storage_total"],
                "total_owned": bd["total"],
                "deficit": actual_def,
                "is_gatherable": is_gath and not is_special,
                "is_special_obtain": is_special,
                "tool_ok": tool_ok if not is_special else True,
                "status": status
            })

        # 7. Facility Slot Analysis & Slot Warnings
        facility_analysis: List[Dict[str, Any]] = []
        slot_warnings: List[str] = []
        has_slot_issue = False

        all_facilities = set(facility_slot_usage.keys()) | set(facility_new_works_demand.keys())
        for fac in sorted(all_facilities):
            usage = facility_slot_usage[fac]
            new_demand = facility_new_works_demand.get(fac, 0)
            available = usage["available"]
            available_after_collect = available + usage["completed"]
            can_fit_all = new_demand <= available_after_collect
            overflow = max(0, new_demand - available_after_collect)

            prog_items_str = ", ".join(f"{it} {cnt}회" for it, cnt in usage["in_progress_items"].items())
            comp_items_str = ", ".join(f"{it} {cnt}회" for it, cnt in usage["completed_items"].items())

            fac_info = {
                "facility": fac,
                "max_slots": MAX_SLOTS,
                "in_progress": usage["in_progress"],
                "completed": usage["completed"],
                "in_progress_detail": prog_items_str,
                "completed_detail": comp_items_str,
                "available_now": available,
                "available_after_collect": available_after_collect,
                "new_works_needed": new_demand,
                "overflow": overflow,
                "can_fit": can_fit_all,
            }
            facility_analysis.append(fac_info)

            if new_demand > 0 and not can_fit_all:
                has_slot_issue = True
                slot_warnings.append(
                    f"'{fac}': 가공 {new_demand}회 필요하지만 "
                    f"수령 후에도 가용 슬롯이 {available_after_collect}개뿐입니다 "
                    f"({overflow}회분 초과). 진행 중 {usage['in_progress']}건 완료 후 추가 등록이 필요합니다."
                )
            elif new_demand > 0 and usage["completed"] > 0 and new_demand > available:
                slot_warnings.append(
                    f"'{fac}': 완료된 가공 {usage['completed']}건을 먼저 수령하면 "
                    f"슬롯 {available_after_collect}개 확보 가능 (필요: {new_demand}회)"
                )

        # Count immediately craftable finished goods tasks (needed > 0 and all required ingredients fully owned)
        craftable_tasks_count = 0
        for t in tasks_to_plan:
            it_name = t["item_name"]
            needed = t.get("needed", 0)
            clean_it = it_name.split("(")[0].strip()
            is_gath, _ = self.is_gatherable(it_name)
            is_alt, _ = self.get_alter_info(clean_it)

            if t.get("is_gather_item") or is_gath or clean_it in KNOWN_GATHERABLE_ITEMS:
                continue
            if t.get("is_alter_item") or is_alt:
                continue
            if needed <= 0:
                continue

            rec = recipe_details.get(it_name) or self.get_recipe_ingredients(it_name)
            if not rec:
                continue

            prod_pc = max(1, self.get_produced_per_craft(it_name))
            crafts_needed = math.ceil(needed / prod_pc)
            can_craft_now = True
            for ing_name, per_craft in rec.items():
                total_ing_needed = per_craft * crafts_needed
                if get_stock(ing_name)["total"] < total_ing_needed:
                    can_craft_now = False
                    break
            if can_craft_now:
                craftable_tasks_count += 1

        can_craft_all_immediately = (
            total_works_to_queue == 0 and sum(raw_gather_demands.values()) == 0
        )
        all_completed = len(tasks_to_plan) > 0 and all(t.get("needed", 0) <= 0 for t in tasks_to_plan)

        try:
            wings_count = self.cli.get_wings_count()
        except Exception:
            wings_count = 0

        # 8. Pre-flight Readiness Diagnostics (Tool durability, Locked ingredients, Skill/Facility/Decor level)
        preflight_warnings: List[str] = []
        locked_items_set = set()
        try:
            for it_entry in self.cli.get_items():
                if it_entry.get("IsLocked"):
                    dn = it_entry.get("DisplayName", "")
                    if dn:
                        locked_items_set.add(dn)
        except Exception:
            pass

        alter_reason_map: Dict[str, str] = {}
        try:
            for alt_it in self.cli.get_alterable_items().get("items", []):
                dn = alt_it.get("DisplayName", "").split("(")[0].strip()
                rsn = str(alt_it.get("Reason") or "")
                if dn and rsn and rsn != "not_enough_ingredient":
                    alter_reason_map[dn] = rsn
        except Exception:
            pass

        REASON_LABELS = {
            "insufficient_living_skill_level": "생활 스킬 레벨 부족",
            "insufficient_facility_level": "설비 레벨 부족",
            "insufficient_decor_score": "마이홈 데코 점수 부족",
            "ingredient_locked": "재료 잠금 상태",
            "insufficient_transfer_cost": "재료 전송 비용(골드) 부족",
        }

        for r_mat in raw_materials_analysis:
            r_name = r_mat["item_name"]
            if r_mat.get("status") == "tool_missing" and r_mat.get("deficit", 0) > 0:
                preflight_warnings.append(f"⚠️ [도구 점검] '{r_name}' 채집 도구가 없거나 내구도가 0입니다.")
            if r_name in locked_items_set and r_mat.get("total_owned", 0) > 0:
                r_mat["is_locked"] = True
                preflight_warnings.append(f"🔒 [재료 잠금] '{r_name}' 아이템이 잠금 상태여서 제작/가공 소모가 차단될 수 있습니다.")

        for i_mat in intermediate_analysis:
            i_name = i_mat["item_name"]
            if i_name in locked_items_set and i_mat.get("total_owned", 0) > 0:
                i_mat["is_locked"] = True
                preflight_warnings.append(f"🔒 [재료 잠금] 중간재 '{i_name}' 아이템이 잠금 상태입니다.")
            if i_mat.get("works_needed", 0) > 0 and i_name in alter_reason_map:
                rsn_code = alter_reason_map[i_name]
                rsn_ko = REASON_LABELS.get(rsn_code, rsn_code)
                i_mat["readiness_warning"] = rsn_ko
                preflight_warnings.append(f"⛔ [가공 조건 미충족] '{i_name}': {rsn_ko}")

        # 9. Spirit Wings Estimate & Lightweight ETA Calculation (Zero extra CLI calls)
        gather_trips = sum(math.ceil(r.get("deficit", 0) / 100.0) for r in raw_materials_analysis if r.get("deficit", 0) > 0 and r.get("is_gatherable"))
        craft_calls = 0
        for t in tasks_to_plan:
            if t.get("needed", 0) > 0 and not t.get("is_gather_item") and not t.get("is_alter_item"):
                craft_calls += 1
        estimated_wings = (total_works_to_queue + gather_trips + craft_calls) * 5
        if estimated_wings > 0 and wings_count > 0 and wings_count < estimated_wings:
            preflight_warnings.append(f"🪶 [정령의 날개 부족 주의] 예상 소모량 약 {estimated_wings:,}개 / 현재 보유 {wings_count:,}개")

        total_gather_items = sum(r.get("deficit", 0) for r in raw_materials_analysis if r.get("deficit", 0) > 0 and r.get("is_gatherable"))
        estimated_eta_seconds = int(total_gather_items * 2.2 + total_works_to_queue * 12 + craft_calls * 8)
        if estimated_eta_seconds >= 60:
            estimated_eta_text = f"약 {math.ceil(estimated_eta_seconds / 60)}분"
        elif estimated_eta_seconds > 0:
            estimated_eta_text = f"약 {estimated_eta_seconds}초"
        else:
            estimated_eta_text = "즉시 완료"

        return {
            "plan_source": "custom_tasks" if custom_tasks is not None else "weekly_delivery",
            "tasks": tasks_to_plan,
            "targets": tasks_to_plan,
            "custom_tasks": tasks_to_plan,
            "recipes": recipe_details,
            "intermediate_requirements": intermediate_analysis,
            "raw_materials": raw_materials_analysis,
            "can_craft_immediately": can_craft_all_immediately and not all_completed,
            "craftable_tasks_count": craftable_tasks_count,
            "all_completed": all_completed,
            "total_works_to_queue": total_works_to_queue,
            "facility_slots": facility_analysis,
            "slot_warnings": slot_warnings,
            "preflight_warnings": preflight_warnings,
            "estimated_wings": estimated_wings,
            "estimated_eta_seconds": estimated_eta_seconds,
            "estimated_eta_text": estimated_eta_text,
            "has_slot_issue": has_slot_issue,
            "wings_count": wings_count,
            "alter_order": alter_order
        }

    def calculate_slot_filling_gather_demands(
        self,
        plan: Dict[str, Any],
        max_slots_per_facility: int = 7,
        lookahead_batches: int = 1
    ) -> Dict[str, int]:
        """
        가공 시설의 빈 슬롯(최대 7개)을 꽉 채우는 데 필요한 1차(또는 차기) 배치 분량의 기초 원자재 부족분을 계산합니다.
        전체 마라톤 물량을 한꺼번에 채집하지 않고, 슬롯을 즉시 가동할 만큼만 단계적으로 산정하여 가방 과적을 방지하고
        가공 쿨타임을 조기에 가동시킵니다.
        """
        raw_mats = plan.get("raw_materials", [])
        if not raw_mats:
            return {}

        # 1. Total remaining deficits across all gatherable raw materials
        total_deficits: Dict[str, int] = {}
        for r in raw_mats:
            if r.get("is_gatherable") and r.get("deficit", 0) > 0 and not r.get("is_special_obtain"):
                total_deficits[r["item_name"]] = int(r["deficit"])

        if not total_deficits:
            return {}

        # 2. Check current facility slot usage
        try:
            works_info = self.cli.get_altering_works()
            queued_works = works_info.get("works", [])
        except Exception:
            queued_works = []

        facility_used_slots: Dict[str, int] = defaultdict(int)
        for w in queued_works:
            if not w.get("IsCompleted") and w.get("State") in ("InProgress", "NotStarted"):
                fac = w.get("FacilityName") or get_facility_for_material(w.get("DisplayName", ""))
                facility_used_slots[fac] += 1

        all_facilities = ("금속 가공대", "목재 가공대", "가죽 가공대", "옷감 가공대")
        avail_slots: Dict[str, int] = {}
        for fac in all_facilities:
            used = facility_used_slots.get(fac, 0)
            avail = max(0, max_slots_per_facility - used)
            # If calculating for next batch (lookahead), assume slots will become free
            if lookahead_batches > 1 and avail == 0:
                avail = max_slots_per_facility
            avail_slots[fac] = avail

        # 3. Analyze intermediate requirements that can fill available slots
        intermediate_reqs = plan.get("intermediate_requirements", [])
        demands: Dict[str, int] = defaultdict(int)

        fac_to_raws: Dict[str, List[str]] = {
            "목재 가공대": ["통나무", "상급 통나무", "굵은 나무", "뾰족 나무", "나뭇가지"],
            "금속 가공대": ["철 광석", "은 광석", "금 광석"],
            "가죽 가공대": ["가죽", "상급 가죽"],
            "옷감 가공대": ["양털", "상급 양털", "실크", "거미줄"],
        }

        remaining_slots = dict(avail_slots)
        for req in intermediate_reqs:
            mat_name = req.get("item_name", "")
            fac = req.get("facility") or get_facility_for_material(mat_name)
            w_needed = int(req.get("works_needed", 0))
            if w_needed <= 0:
                continue
            cur_slot = remaining_slots.get(fac, 0)
            if cur_slot <= 0:
                continue

            alloc = min(w_needed, cur_slot)
            remaining_slots[fac] -= alloc

            # Ingredients of this alter
            ings = req.get("ingredients", {})
            for ing_name, ing_per_w in ings.items():
                if ing_name in total_deficits:
                    demands[ing_name] += ing_per_w * alloc
                else:
                    is_alt, alt_inf = self.get_alter_info(ing_name)
                    if is_alt and alt_inf:
                        sub_ings = alt_inf.get("ingredients", {})
                        sub_prod = max(1, alt_inf.get("produced", 3))
                        for sub_sub_name, sub_sub_qty in sub_ings.items():
                            if sub_sub_name in total_deficits:
                                needed_sub_works = math.ceil((ing_per_w * alloc) / sub_prod)
                                demands[sub_sub_name] += sub_sub_qty * needed_sub_works

        # 4. Fallback cap per facility
        for fac, f_raws in fac_to_raws.items():
            f_slots = avail_slots.get(fac, 0)
            if f_slots > 0:
                for r_name in f_raws:
                    for def_name, def_cnt in total_deficits.items():
                        if r_name in def_name or def_name in r_name:
                            slot_cap = f_slots * 4
                            if demands[def_name] == 0:
                                demands[def_name] = min(def_cnt, slot_cap)
                            else:
                                demands[def_name] = min(demands[def_name], slot_cap)

        # 5. Clamp demands by total_deficits
        final_demands: Dict[str, int] = {}
        for k, v in demands.items():
            if k in total_deficits and v > 0:
                final_demands[k] = min(v, total_deficits[k])

        # 6. If no altering demands were derived (e.g. only non-altering targets like eggs/milk remain)
        if not final_demands:
            for def_name, def_cnt in total_deficits.items():
                final_demands[def_name] = min(def_cnt, 30)

        return final_demands

    def execute_batch_deliveries(
        self,
        plan: Dict[str, Any],
        callback: Optional[LogCallback] = None,
        alter_order: Optional[str] = None,
        goal_fulfillment_mode: Optional[bool] = None,
        status_reporter: Optional[Callable[[str], None]] = None
    ) -> Dict[str, Any]:
        """
        Executes unified batch delivery pipeline with slot-aware, resilient scheduling:
        1. Checks character status and handles user abort.
        2. Collects completed altering works across all facilities first (frees slots & gains materials).
        3. Gathers any missing raw materials (both direct craft ingredients and altering materials)
           that are gatherable in the field.
        4. Registers altering works up to available slots per facility according to alter_order preference.
           - If a facility is full (or becomes full during registration), gracefully skips to the
             next material / next facility without crashing or halting the entire pipeline!
           - Synchronizes alarms for queued background altering.
        5. Performs immediate crafting for any tasks whose materials are already 100% prepared!
           - Does NOT halt even if altering was queued; immediately crafts everything possible.
        6. If goal_fulfillment_mode is True, continues in an autonomous marathon loop (PC sleep prevented)
           collecting finished alters, queuing next tiers, and crafting target items until 100% fulfilled!
        7. Summarizes completed crafts, running alters, and deferred tasks.
        """
        if alter_order is None:
            alter_order = plan.get("alter_order") or settings_store.get("alter_order", "high_tier")
        if alter_order not in ("high_tier", "low_tier"):
            alter_order = "high_tier"

        if goal_fulfillment_mode is None:
            goal_fulfillment_mode = bool(plan.get("goal_fulfillment_mode", settings_store.get("goal_fulfillment_mode", True)))

        order_label = "상위 티어 우선 (오래 걸리는 가공부터)" if alter_order != "low_tier" else "하위 티어 우선 (기초 재료부터)"
        mode_label = " [목표 완수 모드]" if goal_fulfillment_mode else ""
        self.reset_abort()
        self.log(f"[통합 일괄 납품 파이프라인 가동{mode_label}] ({order_label}) 전체 목표 최적화 제작을 시작합니다.", "action", callback)

        MAX_SLOTS = 7
        start_time = time.time()
        initial_wings = self._safe_get_wings()
        collected_facilities_all: List[str] = []
        gathered_list: List[Dict[str, Any]] = []
        altered_list: List[Dict[str, Any]] = []
        craft_results: List[Dict[str, Any]] = []
        deferred_works: List[Dict[str, Any]] = []

        if self.abort_requested:
            self.log("[사용자 중지] 시작 전 중지 요청이 확인되었습니다.", "warn", callback)
            final_wings = self._safe_get_wings(initial_wings)
            summary = self.format_and_log_summary(
                summary_title="주간 납품 작업 (사용자 중지)",
                initial_wings=initial_wings,
                final_wings=final_wings,
                gathered_list=gathered_list,
                altered_list=altered_list,
                crafted_list=craft_results,
                collected_facilities=collected_facilities_all,
                deferred_works=deferred_works,
                duration_sec=time.time() - start_time,
                status="aborted",
                callback=callback
            )
            return {"status": "aborted", "message": "사용자에 의해 작업이 중지되었습니다.", "summary": summary}

        # 0. Check activity & normalize character state (stand up from chair/sitting or stop instrument performance)
        self.ensure_character_ready_for_action(callback=callback)

        intermediate_reqs = plan.get("intermediate_requirements", [])
        tasks = plan.get("tasks", [])
        raw_mats = plan.get("raw_materials", [])
        plan_source = plan.get("plan_source") or ("custom_tasks" if tasks else "weekly_delivery")
        session_crafted_items: set = set()

        # Stage 1: Collect any completed altering works across all facilities first!
        self.log("[사전 가공품 수령] 모든 시설의 완료된 가공품을 수령하여 슬롯을 비우고 재료를 확보합니다.", "action", callback)
        collected_facilities = self.collect_completed_altering_works(callback=callback)
        if collected_facilities:
            collected_facilities_all.extend(collected_facilities)
            self.log("[BOM 재계산] 완료 가공품 수령으로 재고가 변동되어 최적 필요 수량을 재계산합니다.", "info", callback)
            plan, tasks = self._reanalyze_execution_plan(plan_source, tasks, alter_order, session_crafted_items)
            intermediate_reqs = plan.get("intermediate_requirements", [])
            raw_mats = plan.get("raw_materials", [])

        if self.abort_requested:
            self.log("[사용자 중지] 작업이 중단되었습니다.", "warn", callback)
            final_wings = self._safe_get_wings(initial_wings)
            summary = self.format_and_log_summary(
                summary_title="주간 납품 작업 (사용자 중지)",
                initial_wings=initial_wings,
                final_wings=final_wings,
                gathered_list=gathered_list,
                altered_list=altered_list,
                crafted_list=craft_results,
                collected_facilities=collected_facilities_all,
                deferred_works=deferred_works,
                duration_sec=time.time() - start_time,
                status="aborted",
                callback=callback
            )
            return {"status": "aborted", "message": "사용자에 의해 작업이 중지되었습니다.", "summary": summary}

        # Stage 1.5: Warn about non-gatherable / shop materials
        special_needed = [r for r in raw_mats if r.get("deficit", 0) > 0 and (r.get("is_special_obtain") or not r.get("is_gatherable"))]
        if special_needed:
            self.log(f"[별도 확보 필요 안내] 상점 구매 또는 드랍으로 획득해야 하는 재료 {len(special_needed)}종이 부족합니다.", "warn", callback)
            for sm in special_needed:
                self.log(f"   • '{sm['item_name']}': 필요 {sm['total_needed']}개 / 보유 {sm['total_owned']}개 ({sm['deficit']}개 부족) -> 잡화점/식료품점 구매 또는 사냥으로 별도 확보 필요", "warn", callback)

        # Stage 2: Slot-Aware Smart Gathering (당장 가공 슬롯을 채울 만큼만 1차 맞춤 채집)
        if raw_mats:
            immediate_demands = self.calculate_slot_filling_gather_demands(plan, max_slots_per_facility=MAX_SLOTS, lookahead_batches=1)
            if immediate_demands:
                self.log(
                    f"[1차 슬롯 충전용 스마트 채집] 가공 슬롯을 즉시 가동하기 위해 당장 필요한 원자재 {len(immediate_demands)}종만 1차 맞춤 채집합니다. "
                    f"(과적 방지 & 가공 쿨타임 조기 시작)",
                    "action", callback
                )
                for g_name, g_deficit in immediate_demands.items():
                    if self.abort_requested:
                        self.log("[사용자 중지] 채집 대기열이 중단되었습니다.", "warn", callback)
                        final_wings = self._safe_get_wings(initial_wings)
                        summary = self.format_and_log_summary(
                            summary_title="주간 납품 작업 (사용자 중지)",
                            initial_wings=initial_wings,
                            final_wings=final_wings,
                            gathered_list=gathered_list,
                            altered_list=altered_list,
                            crafted_list=craft_results,
                            collected_facilities=collected_facilities_all,
                            deferred_works=deferred_works,
                            duration_sec=time.time() - start_time,
                            status="aborted",
                            callback=callback
                        )
                        return {"status": "aborted", "message": "사용자에 의해 작업이 중지되었습니다.", "summary": summary}

                    is_gath, tool_ok = self.is_gatherable(g_name)
                    if not is_gath:
                        self.log(f"'{g_name}'은(는) 필드 채집이 불가능하여 건너뜁니다.", "warn", callback)
                        continue
                    if not tool_ok:
                        self.log(f"'{g_name}' 채집 도구가 없거나 내구도가 0입니다! 채집을 건너뜁니다.", "warn", callback)
                        continue

                    self.log(f"[1차 맞춤 채집 시작] '{g_name}' (가공 슬롯 즉시 충전용: {g_deficit}개) 채집을 진행합니다.", "action", callback)
                    try:
                        g_res = self.auto_gather(g_name, g_deficit, callback=callback)
                        gained = g_res.get("gained", 0) if isinstance(g_res, dict) else 0
                        if gained > 0:
                            self._record_task_progress(tasks, g_name, gained)
                        gathered_list.append({
                            "item": g_name,
                            "gained": gained,
                            "required": g_deficit,
                            "status": g_res.get("status", "completed") if isinstance(g_res, dict) else "completed"
                        })
                        if isinstance(g_res, dict) and g_res.get("status") == "overweight":
                            self.log("[가방 과적 방지] 가방 무게가 기준에 도달하여 1차 채집을 안전 종료하고 즉시 가공으로 이동합니다.", "warn", callback)
                            break
                        if isinstance(g_res, dict) and g_res.get("status") == "aborted":
                            self.log("[사용자 중지] 파이프라인이 중단되었습니다.", "warn", callback)
                            final_wings = self._safe_get_wings(initial_wings)
                            summary = self.format_and_log_summary(
                                summary_title="주간 납품 작업 (사용자 중지)",
                                initial_wings=initial_wings,
                                final_wings=final_wings,
                                gathered_list=gathered_list,
                                altered_list=altered_list,
                                crafted_list=craft_results,
                                collected_facilities=collected_facilities_all,
                                deferred_works=deferred_works,
                                duration_sec=time.time() - start_time,
                                status="aborted",
                                callback=callback
                            )
                            return {"status": "aborted", "message": "사용자에 의해 작업이 중지되었습니다.", "summary": summary}
                    except Exception as g_err:
                        gathered_list.append({
                            "item": g_name,
                            "gained": 0,
                            "required": g_deficit,
                            "status": "failed",
                            "error": str(g_err)
                        })
                        self.log(f"'{g_name}' 채집 중 오류: {g_err}. 다음 작업으로 계속 진행합니다.", "warn", callback)
            else:
                self.log("[원자재 충분] 1차 가공 슬롯을 채우기 위한 원자재가 이미 준비되어 있어 채집을 건너뛰고 가공을 즉시 가동합니다.", "success", callback)

        if self.abort_requested:
            self.log("[사용자 중지] 가공 등록 전 작업이 중단되었습니다.", "warn", callback)
            final_wings = self._safe_get_wings(initial_wings)
            summary = self.format_and_log_summary(
                summary_title="주간 납품 작업 (사용자 중지)",
                initial_wings=initial_wings,
                final_wings=final_wings,
                gathered_list=gathered_list,
                altered_list=altered_list,
                crafted_list=craft_results,
                collected_facilities=collected_facilities_all,
                deferred_works=deferred_works,
                duration_sec=time.time() - start_time,
                status="aborted",
                callback=callback
            )
            return {"status": "aborted", "message": "사용자에 의해 작업이 중지되었습니다.", "summary": summary}

        # Stage 3: Slot-aware Batch Altering (시설 꽉 참/에러 시 스킵하고 다음 가공 및 시설로 진행)
        MAX_SLOTS = 7
        works_info = self.cli.get_altering_works()
        queued_works = works_info.get("works", [])
        facility_used_slots: Dict[str, int] = defaultdict(int)
        for w in queued_works:
            fac = w.get("FacilityName") or get_facility_for_material(w.get("DisplayName", ""))
            if not w.get("IsCompleted"):
                facility_used_slots[fac] += 1

        queued_any_long = False
        full_facilities = set()

        for req in intermediate_reqs:
            if self.abort_requested:
                self.log("[사용자 중지] 가공 대기열 등록이 중단되었습니다.", "warn", callback)
                break

            mat_name = req["item_name"]
            recipe_name = req.get("recipe_name", mat_name)
            works_needed = req["works_needed"]
            if works_needed <= 0:
                continue

            facility = req.get("facility") or get_facility_for_material(mat_name)

            # Check if this facility has already filled up during this batch
            if facility in full_facilities:
                self.log(
                    f"[{facility}] 이미 슬롯이 가득 찼으므로 '{mat_name}' 가공({works_needed}회)을 보류하고 다른 품목/시설로 스킵합니다.",
                    "warn", callback
                )
                deferred_works.append({
                    "item_name": mat_name,
                    "facility": facility,
                    "needed": works_needed,
                    "registered": 0,
                    "deferred": works_needed,
                })
                continue

            # Check if sub-ingredients are available for at least 1 work
            sub_ings = req.get("ingredients", {})
            can_start = True
            missing_sub = []
            for s_name, s_req in sub_ings.items():
                s_owned = self.get_effective_owned(s_name, include_storage=True)
                if s_owned < s_req:
                    can_start = False
                    missing_sub.append(f"{s_name} (보유 {s_owned}/{s_req}개)")
            if not can_start:
                self.log(
                    f"[{facility}] '{mat_name}' 하위 재료({', '.join(missing_sub)}) 가공 진행 대기 중. 현재 가공 완료 후 다음 차례에 등록됩니다.",
                    "info", callback
                )
                deferred_works.append({
                    "item_name": mat_name,
                    "facility": facility,
                    "needed": works_needed,
                    "registered": 0,
                    "deferred": works_needed,
                    "reason": "waiting_sub_ingredients"
                })
                continue

            used = facility_used_slots.get(facility, 0)
            available = max(0, MAX_SLOTS - used)

            if available <= 0:
                full_facilities.add(facility)
                self.log(
                    f"[{facility}] 슬롯 한도 도달(사용 중: {used}/{MAX_SLOTS}). '{mat_name}' 가공({works_needed}회)을 보류하고 다른 가공으로 스킵합니다.",
                    "warn", callback
                )
                deferred_works.append({
                    "item_name": mat_name,
                    "facility": facility,
                    "needed": works_needed,
                    "registered": 0,
                    "deferred": works_needed,
                })
                continue

            # Calculate how many works can actually be supported by currently owned sub-ingredients
            max_works_by_sub = works_needed
            if sub_ings:
                for s_name, s_req in sub_ings.items():
                    if s_req > 0:
                        s_owned = self.get_effective_owned(s_name, include_storage=True)
                        possible_for_this = s_owned // s_req
                        if possible_for_this < max_works_by_sub:
                            max_works_by_sub = possible_for_this

            works_to_register = min(works_needed, available, max_works_by_sub)
            deferred_count = works_needed - works_to_register

            if deferred_count > 0:
                if works_to_register == 0:
                    self.log(
                        f"[{facility}] '{mat_name}' 하위 재료 가공 대기 중 (필요: {works_needed}회). "
                        f"하위 재료가 준비된 후 다음 차례에 등록됩니다.",
                        "info", callback
                    )
                else:
                    self.log(
                        f"[{facility}] '{mat_name}' 가공 {works_needed}회 필요하지만 현재 재료/슬롯상 {works_to_register}회만 등록 가능합니다. "
                        f"{works_to_register}회 등록 후 {deferred_count}회는 보류합니다.",
                        "warn", callback
                    )
                deferred_works.append({
                    "item_name": mat_name,
                    "facility": facility,
                    "needed": works_needed,
                    "registered": works_to_register,
                    "deferred": deferred_count,
                })

            if works_to_register > 0:
                self.log(
                    f"[{facility}] '{recipe_name}' {works_to_register}회 가공 등록 시도 "
                    f"(필요 총량: {req['total_needed']}개, 시설 가용 슬롯: {available}/{MAX_SLOTS})",
                    "action", callback
                )
                try:
                    alter_res = self.auto_alter(recipe_name, works_to_register, callback=callback, is_delivery=True, skip_pre_collect=True)
                    reg_count = alter_res.get("registered", works_to_register) if isinstance(alter_res, dict) else works_to_register
                    facility_used_slots[facility] += reg_count

                    if reg_count > 0:
                        alt_inf = self.get_alter_info(mat_name)[1] or {}
                        produced_pw = alt_inf.get("produced", 3)
                        altered_list.append({
                            "item": mat_name,
                            "recipe": recipe_name,
                            "facility": facility,
                            "registered": reg_count,
                            "yield_est": reg_count * produced_pw
                        })

                    if reg_count < works_to_register:
                        status = alter_res.get("status") if isinstance(alter_res, dict) else ""
                        failed_count = works_to_register - reg_count
                        deferred_works.append({
                            "item_name": mat_name,
                            "facility": facility,
                            "needed": works_needed,
                            "registered": reg_count,
                            "deferred": failed_count,
                        })
                        # 실제 슬롯 부족(slot_full_skipped)일 때만 시설 전체를 차단하고,
                        # 재료 부족(ingredient_missing_skipped)인 경우는 남은 슬롯이 있으므로 동일 시설의 다른 품목 가공을 허용!
                        if status == "slot_full_skipped":
                            full_facilities.add(facility)
                            facility_used_slots[facility] = MAX_SLOTS

                    if isinstance(alter_res, dict) and alter_res.get("status") in ("waiting_in_background", "slot_full_skipped"):
                        queued_any_long = True
                except Exception as ex:
                    self.log(f"[{facility}] '{recipe_name}' 가공 등록 실패 ({ex}). 다음 항목으로 스킵합니다.", "warn", callback)
                    deferred_works.append({
                        "item_name": mat_name,
                        "facility": facility,
                        "needed": works_needed,
                        "registered": 0,
                        "deferred": works_needed,
                    })
                    ex_str = str(ex).lower()
                    if "slot" in ex_str or "full" in ex_str or "슬롯" in ex_str:
                        full_facilities.add(facility)
                        facility_used_slots[facility] = MAX_SLOTS

        if deferred_works:
            self.log(
                f"[가공 슬롯/의존성 보류 요약] 총 {len(deferred_works)}건의 가공이 슬롯 또는 하위 재료 대기로 보류되었습니다. "
                f"현재 진행 중인 가공이 완료된 후 다시 '일괄 제작'을 실행하면 보류된 가공이 자동으로 등록됩니다.",
                "warn", callback
            )

        if queued_any_long:
            alarm_store.sync_facility_alarms(self.cli, default_source="주간 납품")
            self.log("[가공 알람 동기화] 등록된 가공 작업들의 완료 시각에 맞춰 알람이 동기화되었습니다.", "info", callback)

        if altered_list:
            self.notify_altering_updated()

        # Stage 3.5: Collect any newly finished altering works before crafting
        new_collected = self.collect_completed_altering_works(callback=callback)
        if new_collected:
            collected_facilities_all.extend(new_collected)
            plan, tasks = self._reanalyze_execution_plan(plan_source, tasks, alter_order, session_crafted_items)
            raw_mats = plan.get("raw_materials", raw_mats)

        # Stage 4: Immediate Batch Crafting for any ready tasks!
        self.log("[납품 완제품 제작 단계] 현재 가방/창고 상태에서 즉시 제작 가능한 납품 아이템을 확인합니다.", "action", callback)
        craftable_tasks = []
        waiting_tasks = []
        gather_ready_tasks = []

        for t in tasks:
            item_name = t["item_name"]
            needed = t["needed"]
            clean_t = item_name.split("(")[0].strip()

            # Skip gather-only items (우유, 달걀, 작물, 통나무 등)
            is_gath, _ = self.is_gatherable(item_name)
            if t.get("is_gather_item") or is_gath or clean_t in KNOWN_GATHERABLE_ITEMS:
                gather_ready_tasks.append(t)
                continue

            # Skip alter-only items (이미 가공 단계에서 대기열 등록 처리됨)
            is_alt, _ = self.get_alter_info(item_name)
            if t.get("is_alter_item") or is_alt:
                continue

            if needed <= 0:
                continue

            recipe = self.get_recipe_ingredients(item_name)
            if not recipe:
                self.log(f"'{item_name}'은(는) 제작 레시피가 없거나 완제품 제작 대상이 아니므로 제작 단계에서 건너뜁니다.", "info", callback)
                continue

            can_craft_this = True
            missing_for_this = []

            prod_pc = max(1, self.get_produced_per_craft(item_name))
            crafts_needed = math.ceil(needed / prod_pc)

            for ing_name, per_craft in recipe.items():
                total_ing_needed = per_craft * crafts_needed
                owned_ing = self.get_effective_owned(ing_name, include_storage=True)
                if owned_ing < total_ing_needed:
                    can_craft_this = False
                    missing_for_this.append(f"{ing_name} (보유 {owned_ing}/{total_ing_needed}개)")

            if can_craft_this:
                craftable_tasks.append(t)
            else:
                waiting_tasks.append((t, missing_for_this))

        if gather_ready_tasks:
            ready_names = []
            short_names = []
            for gt in gather_ready_tasks:
                gn = gt["item_name"]
                g_goal = int(gt.get("goal", 1))
                g_cur = self.get_effective_owned(gn, include_storage=False)
                if g_cur >= g_goal:
                    ready_names.append(f"{gn} ({g_cur}/{g_goal}개)")
                else:
                    short_names.append(f"{gn} ({g_cur}/{g_goal}개, {g_goal - g_cur}개 부족)")
            if ready_names:
                self.log(f"[채집 납품 품목 가방 준비 완료] {len(ready_names)}건({', '.join(ready_names)})은 가방에 준비 완료되었습니다.", "success", callback)
            if short_names:
                self.log(f"[채집 납품 품목 가방 부족] {len(short_names)}건({', '.join(short_names)})은 가방 수량이 부족합니다. 추가 채집/확보가 필요합니다.", "warn", callback)

        if craftable_tasks:
            self.log(f"[즉시 제작 가능 목표] 총 {len(craftable_tasks)}개 품목은 재료가 모두 준비되어 있어 즉시 제작을 진행합니다!", "action", callback)
            for t in craftable_tasks:
                if self.abort_requested:
                    self.log("[사용자 중지] 완제품 제작이 중단되었습니다.", "warn", callback)
                    break
                item_name = t["item_name"]
                goal = int(t.get("goal", 1))
                # Weekly deliveries require items to be physically in inventory (가방) to turn in to NPC:
                is_custom_task = bool(t.get("quest_title") == "개별 지정 제작")
                has_override = (t.get("override_current") is not None)
                cur_owned = self.get_effective_owned(item_name, include_storage=is_custom_task)

                if is_custom_task or has_override:
                    needed = int(t.get("needed", 0))
                else:
                    needed = t.get("needed") if (t.get("needed") is not None and t.get("needed") > 0) else max(0, goal - cur_owned)

                if needed <= 0 or (not is_custom_task and not has_override and cur_owned >= goal):
                    loc_text = "전체 보유" if is_custom_task else "가방"
                    self.log(f"'{item_name}'은(는) 이미 {loc_text}에 목표 수량({cur_owned}/{goal}개)을 보유 중이므로 제작을 건너뜁니다.", "success", callback)
                    craft_results.append({"item_name": item_name, "crafted": 0, "status": "already_completed"})
                    continue

                storage_info = " (창고 포함)" if is_custom_task else ""
                self.log(f"[완제품 제작 시작] '{item_name}' {needed}개 제작 진행... (현재 보유: {cur_owned}개{storage_info}, 목표: {goal}개)", "action", callback)
                try:
                    crafted_count = self.auto_craft(item_name, needed, callback=callback)
                    if crafted_count > 0:
                        session_crafted_items.add(item_name)
                        self._record_task_progress(tasks, item_name, crafted_count)
                    craft_results.append({"item_name": item_name, "crafted": crafted_count, "status": "success"})
                    self.log(f"'{item_name}' {crafted_count}개 제작 완료!", "success", callback)
                except Exception as e:
                    self.log(f"'{item_name}' 제작 중 오류: {str(e)}", "error", callback)
                    craft_results.append({"item_name": item_name, "error": str(e), "status": "failed", "crafted": 0})
        else:
            if waiting_tasks:
                self.log(f"[제작 대기] 아직 가공 완료 또는 재료 준비가 필요한 납품 품목 {len(waiting_tasks)}건은 가공 완료 후 제작됩니다.", "info", callback)
                for t, missing_list in waiting_tasks[:5]:
                    self.log(f"   • '{t['item_name']}': {', '.join(missing_list)} 대기 중", "info", callback)
                if len(waiting_tasks) > 5:
                    self.log(f"   • 외 {len(waiting_tasks) - 5}건 대기 중", "info", callback)

        # Helper to check if all tasks are fulfilled
        def _are_all_tasks_fulfilled() -> bool:
            for t in tasks:
                t_name = t["item_name"]
                t_goal = int(t.get("goal", 1))
                is_custom_t = bool(t.get("quest_title") == "개별 지정 제작")
                has_ovr = (t.get("override_current") is not None)
                if is_custom_t or has_ovr:
                    if int(t.get("needed", 0) or 0) > 0:
                        return False
                else:
                    if t.get("needed") is not None and int(t.get("needed", 0) or 0) <= 0:
                        continue
                    cur_o = self.get_effective_owned(t_name, include_storage=False)
                    if max(0, t_goal - cur_o) > 0 and cur_o < t_goal:
                        return False
            return True

        # Goal Fulfillment Mode Loop (목표 완수 모드: 전 단계 수령 -> 다음 가공 -> 완제품까지 자율 완주)
        if goal_fulfillment_mode and not _are_all_tasks_fulfilled() and not self.abort_requested:
            enable_prevent_sleep()
            self.log("[목표 완수 모드 가동] PC 절전 방지 활성화. 가공 완료 수령 -> 다음 티어 가공 -> 완제품 제작까지 연속 진행합니다.", "action", callback)
            cycle = 1
            pending_craft_after_alter = False
            try:
                while not self.abort_requested:
                    if _are_all_tasks_fulfilled():
                        self.log("[목표 완수 성공!] 모든 목표 아이템 수량이 100% 달성되었습니다!", "success", callback)
                        break

                    # 1. Safety check: game process running
                    if not self.cli.check_game_running():
                        self.log("게임 클라이언트가 일시적으로 감지되지 않습니다. 10초 후 재확인합니다...", "warn", callback)
                        time.sleep(10)
                        if not self.cli.check_game_running():
                            self.log("게임 연결이 끊겨 목표 완수 모드를 안전하게 종료합니다.", "warn", callback)
                            break

                    # 2. Safety check: bag weight (98% limit)
                    try:
                        inv = self.cli.get_inventory()
                        cur_w = float(inv.get("CurrentInventoryWeightAsDecimal", 0) or inv.get("CurrentInventoryWeight", 0) or 0)
                        max_w = float(inv.get("MaxInventoryWeightAsDecimal", 100) or inv.get("MaxInventoryWeight", 100) or 100)
                        limit_ratio = get_weight_limit_ratio()
                        if max_w > 0 and (cur_w / max_w) >= limit_ratio:
                            pct = (cur_w / max_w) * 100
                            limit_pct = limit_ratio * 100
                            self.log(f"[가방 과적 경고] 가방 무게({cur_w:.1f}/{max_w:.1f}, {pct:.1f}%)가 {limit_pct:.0f}% 기준에 도달하여 안전을 위해 가공 수령 및 연속 작업을 멈춥니다.", "warn", callback)
                            break
                    except Exception:
                        pass

                    # 3. Safety check: wings
                    curr_wings = self._safe_get_wings(100)
                    if curr_wings < 5:
                        self.log(f"[정령의 날개 부족] 남은 날개({curr_wings}개)가 부족하여 작업을 안전하게 마무리합니다.", "warn", callback)
                        break

                    # 4. Check altering works in progress / completed
                    try:
                        works_info = self.cli.get_altering_works()
                        self.notify_altering_updated(works_info)
                    except Exception as ex:
                        self.log(f"가공 대기열 확인 중 일시 오류: {ex}. 5초 후 재시도합니다.", "warn", callback)
                        time.sleep(5)
                        continue

                    completed_count = int(works_info.get("completedCount", 0) or 0)
                    new_colls: List[str] = []
                    collect_failed_this_cycle = False
                    if completed_count > 0:
                        self.log(f"[완료 가공품 감지] 총 {completed_count}건의 완료 가공품을 일괄 수령합니다.", "action", callback)
                        new_colls = self.collect_completed_altering_works(callback=callback)
                        if not new_colls:
                            collect_failed_this_cycle = True
                    if new_colls or pending_craft_after_alter:
                        pending_craft_after_alter = False
                        if new_colls:
                            collected_facilities_all.extend(new_colls)
                        if not self.abort_requested:
                            self.cli.invalidate_cache()

                            # Try crafting any newly ready tasks with fresh materials
                            self.log("[완제품 추가 제작 확인] 새로 수령한 재료로 완제품 제작 가능 여부를 확인합니다.", "action", callback)
                            for t in tasks:
                                if self.abort_requested:
                                    break
                                item_name = t["item_name"]
                                goal = int(t.get("goal", 1))
                                is_custom_t = bool(t.get("quest_title") == "개별 지정 제작")
                                has_ovr = (t.get("override_current") is not None)
                                cur_o = self.get_effective_owned(item_name, include_storage=is_custom_t)
                                needed_cnt = int(t.get("needed", 0)) if (is_custom_t or has_ovr) else max(0, goal - cur_o)
                                if not is_custom_t and not has_ovr and t.get("needed") is not None:
                                    needed_cnt = min(needed_cnt, int(t.get("needed", needed_cnt) or 0))
                                if needed_cnt <= 0 or (not is_custom_t and not has_ovr and cur_o >= goal):
                                    continue

                                recipe = self.get_recipe_ingredients(item_name)
                                if not recipe:
                                    continue
                                prod_pc = max(1, self.get_produced_per_craft(item_name))
                                crafts_needed = math.ceil(needed_cnt / prod_pc)
                                can_craft = True
                                for ing_n, per_c in recipe.items():
                                    tot_req = per_c * crafts_needed
                                    if self.get_effective_owned(ing_n, include_storage=True) < tot_req:
                                        can_craft = False
                                        break
                                if can_craft:
                                    try:
                                        c_cnt = self.auto_craft(item_name, needed_cnt, callback=callback)
                                        if c_cnt > 0:
                                            session_crafted_items.add(item_name)
                                            self._record_task_progress(tasks, item_name, c_cnt)
                                        craft_results.append({"item_name": item_name, "crafted": c_cnt, "status": "success"})
                                        self.log(f"'{item_name}' {c_cnt}개 추가 제작 완료!", "success", callback)
                                    except Exception as ce:
                                        self.log(f"'{item_name}' 추가 제작 중 오류: {ce}", "error", callback)

                            if _are_all_tasks_fulfilled():
                                self.log("[목표 완수 성공!] 모든 목표가 달성되었습니다!", "success", callback)
                                break

                    if collect_failed_this_cycle and getattr(self, "_last_collect_blocked_reason", None) == "continuous_run_limit":
                        self.log("[연속 실행 팝업 대기] 게임 내 확인 팝업을 누를 때까지 15초 대기 후 수령을 재시도합니다.", "warn", callback)
                        self._sleep_with_abort(
                            15,
                            status_reporter=status_reporter,
                            status_text=f"[목표 완수 모드 {cycle}회차] 게임 내 '연속 실행 확인' 팝업 확인 대기 중..."
                        )
                        cycle += 1
                        continue

                    if _are_all_tasks_fulfilled():
                        self.log("[목표 완수 성공!] 모든 목표 아이템 수량이 100% 달성되었습니다!", "success", callback)
                        break

                    # 5. Try queuing next-tier alters into free slots (refresh live targets & inventory first)
                    plan_fresh, tasks = self._reanalyze_execution_plan(plan_source, tasks, alter_order, session_crafted_items)
                    raw_mats = plan_fresh.get("raw_materials", raw_mats)
                    if plan_fresh.get("all_completed"):
                        self.log("[목표 완수 성공!] 모든 목표 아이템 수량이 100% 달성되었습니다!", "success", callback)
                        break
                    fresh_inter = plan_fresh.get("intermediate_requirements", [])
                    works_info = self.cli.get_altering_works()
                    current_q_works = works_info.get("works", [])
                    fac_used = defaultdict(int)
                    for w in current_q_works:
                        if not w.get("IsCompleted"):
                            fac = w.get("FacilityName") or get_facility_for_material(w.get("DisplayName", ""))
                            fac_used[fac] += 1

                    registered_any_new = False
                    for req in fresh_inter:
                        if self.abort_requested:
                            break
                        mat_name = req["item_name"]
                        rec_name = req.get("recipe_name", mat_name)
                        w_needed = req["works_needed"]
                        if w_needed <= 0:
                            continue
                        fac = req.get("facility") or get_facility_for_material(mat_name)
                        avail_slots = max(0, MAX_SLOTS - fac_used[fac])
                        if avail_slots <= 0:
                            continue

                        sub_ings = req.get("ingredients", {})
                        max_possible = w_needed
                        if sub_ings:
                            for s_n, s_r in sub_ings.items():
                                if s_r > 0:
                                    s_own = self.get_effective_owned(s_n, include_storage=True)
                                    possible_here = s_own // s_r
                                    if possible_here < max_possible:
                                        max_possible = possible_here

                        to_reg = min(w_needed, avail_slots, max_possible)
                        if to_reg > 0:
                            self.log(f"[연계 가공 등록] [{fac}] '{rec_name}' {to_reg}회 추가 가공 등록합니다.", "action", callback)
                            try:
                                alt_res = self.auto_alter(rec_name, to_reg, callback=callback, is_delivery=True, skip_pre_collect=True)
                                reg_cnt = alt_res.get("registered", to_reg) if isinstance(alt_res, dict) else to_reg
                                fac_used[fac] += reg_cnt
                                if reg_cnt > 0:
                                    registered_any_new = True
                                    alt_status = alt_res.get("status") if isinstance(alt_res, dict) else ""
                                    if alt_status not in ("waiting_in_background", "slot_full_skipped", "ingredient_missing_skipped"):
                                        # Short <=45s work was collected synchronously by auto_alter; trigger craft check!
                                        pending_craft_after_alter = True
                                        self.cli.invalidate_cache()
                                    altered_list.append({
                                        "item": mat_name,
                                        "recipe": rec_name,
                                        "facility": fac,
                                        "registered": reg_cnt,
                                        "yield_est": reg_cnt * 3
                                    })
                            except Exception as al_err:
                                self.log(f"[{fac}] '{rec_name}' 가공 등록 실패: {al_err}", "warn", callback)

                    # 6. Check active pending works
                    works_info = self.cli.get_altering_works()
                    self.notify_altering_updated(works_info)
                    all_works = works_info.get("works", [])
                    pending_works = [w for w in all_works if not w.get("IsCompleted") and w.get("State") in ("InProgress", "NotStarted")]

                    if not pending_works:
                        if pending_craft_after_alter:
                            cycle += 1
                            continue
                        if not registered_any_new:
                            self.log("[목표 완수 종료] 더 이상 진행 가능한 가공 작업이 없습니다 (원자재 소진 또는 대기열 없음).", "info", callback)
                            break
                        else:
                            time.sleep(3)
                            continue

                    # 7. Find the FIRST completing work across all facilities so finished slots are collected immediately
                    in_progress_works = [w for w in pending_works if w.get("State") == "InProgress" and int(w.get("RemainingSeconds", 0) or 0) > 0]
                    cand_works = in_progress_works if in_progress_works else [w for w in pending_works if int(w.get("RemainingSeconds", 0) or 0) > 0]
                    if cand_works:
                        soonest_w = min(cand_works, key=lambda w: int(w.get("RemainingSeconds", 0) or 0))
                        min_fac_remaining = int(soonest_w.get("RemainingSeconds", 30) or 30)
                        fac_label = soonest_w.get("FacilityName") or get_facility_for_material(soonest_w.get("DisplayName", "")) or "가공 시설"
                    else:
                        min_fac_remaining = 15
                        fac_label = "가공 시설"

                    wait_seconds = max(8, min_fac_remaining + 2)
                    wait_minutes = math.ceil(wait_seconds / 60)

                    # --- [스마트 파이프라인] 가공 대기 시간 중 차기 배치 예측 선채집 ---
                    plan_current, tasks = self._reanalyze_execution_plan(plan_source, tasks, alter_order, session_crafted_items)
                    raw_mats = plan_current.get("raw_materials", raw_mats)
                    next_demands = self.calculate_slot_filling_gather_demands(plan_current, max_slots_per_facility=MAX_SLOTS, lookahead_batches=2)

                    if next_demands and min_fac_remaining >= 25 and not self.abort_requested:
                        self.log(
                            f"[가공 진행 중 차기 선채집] '{fac_label}' 가공 완료 대기 시간({wait_minutes}분) 동안, "
                            f"다음 턴 가공 슬롯을 채울 원자재 {len(next_demands)}종을 필드에서 미리 채집합니다.",
                            "action", callback
                        )
                        for n_name, n_qty in next_demands.items():
                            if self.abort_requested or not self.cli.check_game_running():
                                break

                            # Bag weight check
                            try:
                                inv_check = self.cli.get_inventory()
                                c_w = float(inv_check.get("CurrentInventoryWeightAsDecimal", 0) or 0)
                                m_w = float(inv_check.get("MaxInventoryWeightAsDecimal", 100) or 100)
                                if m_w > 0 and (c_w / m_w) >= get_weight_limit_ratio():
                                    self.log("[가방 무게 관리] 선채집 중 가방 무게 기준에 도달하여 선채집을 중단하고 수령을 대기합니다.", "warn", callback)
                                    break
                            except Exception:
                                pass

                            is_gath, tool_ok = self.is_gatherable(n_name)
                            if not is_gath or not tool_ok:
                                continue

                            self.log(f"[차기 선채집 시작] '{n_name}' {n_qty}개 사전 채집 (다음 가공 수령 즉시 재등록 대비)", "action", callback)
                            try:
                                g_res = self.auto_gather(n_name, n_qty, callback=callback)
                                gained = g_res.get("gained", 0) if isinstance(g_res, dict) else 0
                                if gained > 0:
                                    self._record_task_progress(tasks, n_name, gained)
                                gathered_list.append({
                                    "item": n_name,
                                    "gained": gained,
                                    "required": n_qty,
                                    "status": g_res.get("status", "completed") if isinstance(g_res, dict) else "completed"
                                })
                            except Exception as nge:
                                self.log(f"차기 선채집 중 오류: {nge}", "warn", callback)

                            # Check if altering works finished while gathering
                            try:
                                check_w = self.cli.get_altering_works()
                                if int(check_w.get("completedCount", 0) or 0) > 0:
                                    self.log("[가공 완료 감지] 선채집 중 가공품이 완성되었습니다. 즉시 수령하러 이동합니다!", "success", callback)
                                    break
                            except Exception:
                                pass

                    # Recalculate remaining wait after pre-gathering
                    try:
                        recheck_works = self.cli.get_altering_works()
                        self.notify_altering_updated(recheck_works)
                        if not collect_failed_this_cycle and int(recheck_works.get("completedCount", 0) or 0) > 0:
                            # Already has completed works, proceed immediately to collect
                            cycle += 1
                            continue
                        all_pend = [w for w in recheck_works.get("works", []) if not w.get("IsCompleted") and w.get("State") in ("InProgress", "NotStarted")]
                        in_p = [w for w in all_pend if w.get("State") == "InProgress" and int(w.get("RemainingSeconds", 0) or 0) > 0]
                        cands = in_p if in_p else all_pend
                        if cands:
                            min_fac_remaining = min(int(w.get("RemainingSeconds", 15) or 15) for w in cands)
                        else:
                            min_fac_remaining = 10
                    except Exception:
                        min_fac_remaining = 10

                    wait_seconds = max(5, min_fac_remaining + 2)
                    wait_minutes = math.ceil(wait_seconds / 60)

                    self.log(
                        f"[목표 완수 스마트 대기 {cycle}회차] '{fac_label}' 다음 슬롯 완료까지 약 {wait_minutes}분({wait_seconds}초) 대기합니다. (PC 절전 방지 유지 중)",
                        "info", callback
                    )

                    wake_time = time.time() + wait_seconds
                    while time.time() < wake_time:
                        if self.abort_requested:
                            break
                        rem_sec = int(wake_time - time.time())
                        if status_reporter and rem_sec > 0:
                            rem_m = rem_sec // 60
                            rem_s = rem_sec % 60
                            status_reporter(f"[목표 완수 모드 {cycle}회차] '{fac_label}' 가공 중 (다음 수령까지 약 {rem_m}분 {rem_s}초 남음)")
                        time.sleep(min(2.0, max(0.5, rem_sec if rem_sec > 0 else 1)))

                    cycle += 1
            finally:
                disable_prevent_sleep()

        duration = time.time() - start_time
        final_wings = self._safe_get_wings(initial_wings)
        
        if goal_fulfillment_mode:
            all_done = _are_all_tasks_fulfilled()
            status_str = "aborted" if self.abort_requested else ("completed" if all_done else "waiting_in_background")
            title_str = "목표 완수 모드 작업 리포트"
        else:
            status_str = "waiting_in_background" if queued_any_long else "completed"
            title_str = "주간 납품 작업 완료"

        special_needed = [r for r in raw_mats if r.get("deficit", 0) > 0 and (r.get("is_special_obtain") or not r.get("is_gatherable"))]

        summary = self.format_and_log_summary(
            summary_title=title_str,
            initial_wings=initial_wings,
            final_wings=final_wings,
            gathered_list=gathered_list,
            altered_list=altered_list,
            crafted_list=craft_results,
            collected_facilities=collected_facilities_all,
            deferred_works=deferred_works,
            duration_sec=duration,
            status=status_str,
            special_materials=special_needed,
            callback=callback
        )

        if not goal_fulfillment_mode and queued_any_long:
            self.log("[가공 백그라운드 진행 안내] 대기열 가공이 진행 중입니다. 알람이 울리면 웹 대시보드에서 '일괄 제작'을 다시 실행하세요!", "success", callback)
            self.try_auto_play_music(callback=callback)
            return {
                "status": "waiting_in_background",
                "message": "가공 대기열 등록 완료 및 즉시 제작 가능한 아이템 제작 완료",
                "results": craft_results,
                "deferred_works": deferred_works,
                "summary": summary
            }

        self.log("[통합 납품 파이프라인 처리 완료] 일괄 처리가 마무리되었습니다!", "success", callback)
        self.try_auto_play_music(callback=callback)
        return {"status": "completed", "results": craft_results, "deferred_works": deferred_works, "summary": summary}

    def resolve_and_produce(self, item_name: str, target_count: int, callback: Optional[LogCallback] = None) -> Dict[str, Any]:
        """Single-item production pipeline with recursive BOM and upfront gathering support."""
        self.reset_abort()
        self.log(f"[개별 제작 파이프라인 시작] 목표: '{item_name}' {target_count}개", "info", callback)

        start_time = time.time()
        initial_wings = self._safe_get_wings()
        collected_facilities_all: List[str] = []
        gathered_list: List[Dict[str, Any]] = []
        altered_list: List[Dict[str, Any]] = []
        craft_results: List[Dict[str, Any]] = []

        activity = self.cli.get_activity()
        if activity.get("IsDead") or activity.get("IsReviving"):
            msg = "캐릭터가 행동 불능(사망 또는 부활 대기) 상태입니다. 게임 화면에서 부활을 먼저 완료해주세요."
            self.log(f"[작업 차단] {msg}", "error", callback)
            raise MabinogiCLIError(msg, response_data={"error": "blocked", "kind": "dead"})

        owned = self.get_effective_owned(item_name)
        needed = target_count
        self.log(f"현재 보유량: {owned}개 (가방+창고) / 제작 요청 수량: {needed}개 진행 시작", "info", callback)

        # 1. Collect completed altering works first to free slots and update stock
        cf = self.collect_completed_altering_works(callback=callback)
        if cf:
            collected_facilities_all.extend(cf)

        # 2. Analyze recursive BOM for this single target
        single_task = [{"item_name": item_name, "needed": needed, "goal": target_count, "current": owned}]
        plan = self.analyze_batch_plan(custom_tasks=single_task)

        # 3. Upfront gather all missing raw materials in a single trip (saves Spirit Wings!)
        raw_mats = plan.get("raw_materials", [])
        if raw_mats:
            needed_gather = [r for r in raw_mats if r.get("deficit", 0) > 0 and r.get("is_gatherable")]
            if needed_gather:
                self.log(f"[부족 원자재 맞춤 채집] '{item_name}' 제작에 필요한 원자재 {len(needed_gather)}종을 필드에서 1회 맞춤 채집합니다 (정령의 날개 낭비 방지).", "action", callback)
                for rm in needed_gather:
                    if self.abort_requested:
                        self.log("[사용자 중지] 채집 대기열이 중단되었습니다.", "warn", callback)
                        final_wings = self._safe_get_wings(initial_wings)
                        summary = self.format_and_log_summary(
                            summary_title=f"'{item_name}' 제작 (사용자 중지)",
                            initial_wings=initial_wings,
                            final_wings=final_wings,
                            gathered_list=gathered_list,
                            altered_list=altered_list,
                            crafted_list=craft_results,
                            collected_facilities=collected_facilities_all,
                            duration_sec=time.time() - start_time,
                            status="aborted",
                            callback=callback
                        )
                        return {"status": "aborted", "message": "사용자에 의해 작업이 중지되었습니다.", "summary": summary}
                    g_name = rm["item_name"]
                    g_deficit = rm["deficit"]
                    is_gath, tool_ok = self.is_gatherable(g_name)
                    if not is_gath:
                        self.log(f"'{g_name}'은(는) 필드 채집이 불가능하여 건너뜁니다.", "warn", callback)
                        continue
                    if not tool_ok:
                        self.log(f"'{g_name}' 채집 도구가 없거나 내구도가 0입니다! 채집을 건너뜁니다.", "warn", callback)
                        continue
                    self.log(f"[맞춤 채집] '{g_name}' {g_deficit}개 채집 시작...", "action", callback)
                    try:
                        g_res = self.auto_gather(g_name, g_deficit, callback=callback)
                        gained = g_res.get("gained", 0) if isinstance(g_res, dict) else 0
                        gathered_list.append({
                            "item": g_name,
                            "gained": gained,
                            "required": g_deficit,
                            "status": g_res.get("status", "completed") if isinstance(g_res, dict) else "completed"
                        })
                        if isinstance(g_res, dict) and g_res.get("status") == "overweight":
                            self.log("[가방 과적 방지] 가방 무게가 98% 기준에 도달하여 남은 채집을 안전 중단하고 다음 단계로 진행합니다.", "warn", callback)
                            break
                    except Exception as g_err:
                        gathered_list.append({
                            "item": g_name,
                            "gained": 0,
                            "required": g_deficit,
                            "status": "failed",
                            "error": str(g_err)
                        })
                        self.log(f"'{g_name}' 채집 중 오류: {g_err}. 계속 진행합니다.", "warn", callback)

        if self.abort_requested:
            final_wings = self._safe_get_wings(initial_wings)
            summary = self.format_and_log_summary(
                summary_title=f"'{item_name}' 제작 (사용자 중지)",
                initial_wings=initial_wings,
                final_wings=final_wings,
                gathered_list=gathered_list,
                altered_list=altered_list,
                crafted_list=craft_results,
                collected_facilities=collected_facilities_all,
                duration_sec=time.time() - start_time,
                status="aborted",
                callback=callback
            )
            return {"status": "aborted", "message": "사용자에 의해 작업이 중지되었습니다.", "summary": summary}

        # 4. Process intermediate alters in dependency order (tier 1 -> tier 2 -> tier 3)
        intermediate_reqs = plan.get("intermediate_requirements", [])
        queued_any_long = False
        for req in intermediate_reqs:
            if self.abort_requested:
                break
            mat_name = req["item_name"]
            recipe_name = req.get("recipe_name", mat_name)
            works_needed = req["works_needed"]
            if works_needed <= 0:
                continue

            sub_ings = req.get("ingredients", {})
            can_start = True
            for s_name, s_req in sub_ings.items():
                if self.get_effective_owned(s_name, include_storage=True) < s_req:
                    can_start = False
                    self.log(f"'{mat_name}' 하위 재료('{s_name}') 가공 대기 중입니다.", "info", callback)
                    break
            if not can_start:
                continue

            self.log(f"'{recipe_name}' {works_needed}회 가공 등록 진행...", "action", callback)
            alter_res = self.auto_alter(recipe_name, works_needed, callback=callback, is_delivery=True)
            reg_count = alter_res.get("registered", works_needed) if isinstance(alter_res, dict) else works_needed
            if reg_count > 0:
                alt_inf = self.get_alter_info(mat_name)[1] or {}
                produced_pw = alt_inf.get("produced", 3)
                altered_list.append({
                    "item": mat_name,
                    "recipe": recipe_name,
                    "facility": get_facility_for_material(mat_name),
                    "registered": reg_count,
                    "yield_est": reg_count * produced_pw
                })
            if isinstance(alter_res, dict) and alter_res.get("status") in ("waiting_in_background", "slot_full_skipped"):
                queued_any_long = True

        if queued_any_long:
            self.log(f"'{item_name}' 가공 대기열이 등록되어 백그라운드 진행 중입니다. 완료 알람이 울리면 다시 실행하세요.", "info", callback)
            final_wings = self._safe_get_wings(initial_wings)
            summary = self.format_and_log_summary(
                summary_title=f"'{item_name}' 가공 대기열 등록 완료",
                initial_wings=initial_wings,
                final_wings=final_wings,
                gathered_list=gathered_list,
                altered_list=altered_list,
                crafted_list=craft_results,
                collected_facilities=collected_facilities_all,
                duration_sec=time.time() - start_time,
                status="waiting_in_background",
                callback=callback
            )
            return {"status": "waiting_in_background", "item": item_name, "message": "가공 대기열 등록 완료", "summary": summary}

        # 5. Collect any newly finished alters
        cf = self.collect_completed_altering_works(callback=callback)
        if cf:
            collected_facilities_all.extend(cf)

        # 6. Craft finished product
        clean_target = item_name.split("(")[0].strip()
        is_gath, _ = self.is_gatherable(item_name)
        if is_gath or clean_target in KNOWN_GATHERABLE_ITEMS:
            self.log(f"[채집 완료] '{item_name}'은(는) 필드 채집 아이템이므로 채집 완료로 모든 준비가 끝났습니다!", "success", callback)
            final_wings = self._safe_get_wings(initial_wings)
            summary = self.format_and_log_summary(
                summary_title=f"'{item_name}' 채집 완료 리포트",
                initial_wings=initial_wings,
                final_wings=final_wings,
                gathered_list=gathered_list,
                altered_list=altered_list,
                crafted_list=craft_results,
                collected_facilities=collected_facilities_all,
                duration_sec=time.time() - start_time,
                status="completed",
                callback=callback
            )
            return {"status": "completed", "item": item_name, "gathered": needed, "summary": summary}

        # Check if target item itself is an alterable good (가공품인 경우 가공 대기열 등록으로 완결)
        is_alt, alt_info = self.get_alter_info(item_name)
        if is_alt and alt_info:
            owned = self.get_effective_owned(item_name)
            self.log(f"'{item_name}' 가공 준비 및 대기열 등록이 완료되었습니다! (현재 총 보유량: {owned}/{target_count}개)", "success", callback)
            final_wings = self._safe_get_wings(initial_wings)
            status_str = "waiting_in_background" if queued_any_long else "completed"
            summary = self.format_and_log_summary(
                summary_title=f"'{item_name}' 가공 등록 완료",
                initial_wings=initial_wings,
                final_wings=final_wings,
                gathered_list=gathered_list,
                altered_list=altered_list,
                crafted_list=craft_results,
                collected_facilities=collected_facilities_all,
                duration_sec=time.time() - start_time,
                status=status_str,
                callback=callback
            )
            return {"status": status_str, "item": item_name, "altered": needed, "summary": summary}

        craft_recipe = self.find_craft_recipe(item_name)
        if not craft_recipe:
            raise MabinogiCLIError(f"'{item_name}'에 대한 제작 레시피를 찾을 수 없습니다.")

        # Ensure needed count reflects user's requested production quantity
        needed = max(1, target_count)
        prod_pc = max(1, self.get_produced_per_craft(item_name))
        crafts_needed = math.ceil(needed / prod_pc)

        ingredients = self.get_recipe_ingredients(item_name)
        for ing_name, per_craft in ingredients.items():
            tot_needed_for_craft = per_craft * crafts_needed
            owned_ing = self.get_effective_owned(ing_name, include_storage=True)
            if owned_ing < tot_needed_for_craft:
                missing = tot_needed_for_craft - owned_ing
                self.log(f"'{item_name}' 제작 재료 '{ing_name}' {missing}개 부족 (필요: {tot_needed_for_craft}개, 전체 보유: {owned_ing}개)", "warn", callback)
                final_wings = self._safe_get_wings(initial_wings)
                summary = self.format_and_log_summary(
                    summary_title=f"'{item_name}' 재료 부족으로 제작 보류",
                    initial_wings=initial_wings,
                    final_wings=final_wings,
                    gathered_list=gathered_list,
                    altered_list=altered_list,
                    crafted_list=craft_results,
                    collected_facilities=collected_facilities_all,
                    duration_sec=time.time() - start_time,
                    status="material_missing",
                    callback=callback
                )
                return {"status": "material_missing", "item": item_name, "missing_ingredient": ing_name, "missing_count": missing, "summary": summary}

        crafted = self.auto_craft(item_name, needed, callback)
        craft_results.append({"item_name": item_name, "crafted": crafted, "status": "success"})
        new_count = self.get_effective_owned(item_name)
        self.log(f"'{item_name}' {crafted}개 제작 완료! (총 보유량: {new_count}/{target_count}개)", "success", callback)

        final_wings = self._safe_get_wings(initial_wings)
        summary = self.format_and_log_summary(
            summary_title=f"'{item_name}' {crafted}개 제작 완료",
            initial_wings=initial_wings,
            final_wings=final_wings,
            gathered_list=gathered_list,
            altered_list=altered_list,
            crafted_list=craft_results,
            collected_facilities=collected_facilities_all,
            duration_sec=time.time() - start_time,
            status="completed",
            callback=callback
        )
        return {"status": "success", "item": item_name, "crafted": crafted, "total_owned": new_count, "summary": summary}

    # =========================================================================
    # 7-SLOT FACILITY QUICK ALTER ROUTINE (최고 레벨 가공대 7슬롯 빠른 실행)
    # =========================================================================

    CATEGORY_7_TIERS: Dict[str, Dict[str, Any]] = {
        "금속": {
            "facility": "금속 가공 시설",
            "tiers": ["백금강괴", "운철괴", "은합금괴", "특수강괴", "합금강괴", "강철괴", "철괴"]
        },
        "목재": {
            "facility": "목재 가공 시설",
            "tiers": ["특급 목재", "최상급 목재+", "최상급 목재", "상급 목재+", "상급 목재", "목재+", "목재"]
        },
        "가죽": {
            "facility": "가죽 가공 시설",
            "tiers": ["특급 가죽", "최상급 가죽+", "최상급 가죽", "상급 가죽+", "상급 가죽", "가죽+", "가죽"]
        },
        "옷감": {
            "facility": "옷감 가공 시설",
            "tiers": ["특급 옷감", "최상급 옷감+", "최상급 옷감", "상급 옷감+", "상급 옷감", "옷감+", "옷감"]
        }
    }

    def analyze_quick_alter(self, category_key: Optional[str] = None, alter_order: Optional[str] = None) -> Dict[str, Any]:
        """
        Analyzes 7-slot alteration bench state for specified category (or all 4 categories):
        1. Checks completed works to collect.
        2. Checks in-progress works and calculates available slots (out of 7).
        3. Evaluates tiers according to alter_order:
           - high_tier: High Tier (Tier 7) down to Low Tier (Tier 1) [default: 오래 걸리는 고티어 우선]
           - low_tier: Low Tier (Tier 1) up to High Tier (Tier 7) [기초 재료 우선]
           - If non-gatherable intermediate goods are lacking -> Skips tier ("가공으로만 얻을 수 있는 재료가 부족하면 그냥 넘어가고").
           - If all missing ingredients are gatherable -> Marks viable and adds to gather plan.
           - If already alterable -> Marks ready immediately.
        4. Compiles aggregated raw material gathering requirements.
        """
        if alter_order is None:
            alter_order = settings_store.get("alter_order", "high_tier")
        if alter_order not in ("high_tier", "low_tier"):
            alter_order = "high_tier"

        target_cats = {}
        if category_key and category_key in self.CATEGORY_7_TIERS:
            target_cats[category_key] = self.CATEGORY_7_TIERS[category_key]
        else:
            target_cats = dict(self.CATEGORY_7_TIERS)

        alter_data = self.cli.get_alterable_items()
        alter_items = self.filter_standard_alter_recipes(alter_data.get("items", []))

        gather_data = self.cli.get_gatherable_items()
        gather_dict = {g["DisplayName"]: g for g in gather_data.get("items", [])}

        works_data = self.cli.get_altering_works()
        works = works_data.get("works", [])

        # Map of ProducedPerWork
        produced_map = {it.get("DisplayName"): it.get("ProducedPerWork", 3) for it in alter_items}

        # Calculate pending yields from completed works waiting on the bench to be collected
        pending_yields = defaultdict(int)
        for w in works:
            if w.get("IsCompleted"):
                w_item = w.get("DisplayName")
                pending_yields[w_item] += produced_map.get(w_item, 3)

        # Single-call cached inventory map (가방 소지품: 실제 가공시설에서 즉시 소모 가능한 수량)
        owned_inventory_map = defaultdict(int)
        owned_storage_map = defaultdict(int)
        try:
            all_locs = self.cli.get_all_item_locations_map()
            for dn, bd in all_locs.items():
                if bd.get("inventory", 0) > 0:
                    owned_inventory_map[dn] += bd["inventory"]
                if bd.get("storage_total", 0) > 0:
                    owned_storage_map[dn] += bd["storage_total"]
        except Exception:
            pass

        categories_plan: Dict[str, Any] = {}
        total_raw_needed: Dict[str, int] = defaultdict(int)

        for cat_name, cat_info in target_cats.items():
            facility = cat_info["facility"]
            base_tiers = list(cat_info["tiers"])
            tiers_list = list(reversed(base_tiers)) if alter_order == "low_tier" else base_tiers

            completed_works = [w for w in works if w.get("FacilityName") == facility and w.get("IsCompleted")]
            in_progress_works = [w for w in works if w.get("FacilityName") == facility and not w.get("IsCompleted")]
            available_slots = max(0, 7 - len(in_progress_works))

            planned_tiers = []
            skipped_tiers = []
            category_raw_needed = defaultdict(int)
            allocated_materials = defaultdict(int)

            for tier_idx, item_name in enumerate(tiers_list, 1):
                clean_name = item_name.split("(")[0].strip()
                std_info = STANDARD_ALTER_RECIPES.get(clean_name)
                tier_num = std_info.get("tier", 1) if std_info else (8 - tier_idx if alter_order != "low_tier" else tier_idx)
                matching_recipes = [
                    it for it in alter_items 
                    if it["DisplayName"] == item_name or it["DisplayName"].startswith(item_name + "(")
                ]

                viable_choice = None
                skip_reasons = []

                for r in matching_recipes:
                    r_name = r["DisplayName"]
                    # Determine full required ingredients:
                    if std_info:
                        req_ingredients = dict(std_info["ingredients"])
                    else:
                        req_ingredients = {}
                        for m in (r.get("MissingIngredients") or []):
                            req_ingredients[m["DisplayName"]] = int(m.get("Required", 1))

                    cannot_gather = []
                    can_gather = {}
                    tier_allocated = {}

                    for m_name, req in req_ingredients.items():
                        inv_owned = owned_inventory_map.get(m_name, 0)
                        clean_m = m_name.split("(")[0].strip()
                        if inv_owned == 0 and clean_m in owned_inventory_map:
                            inv_owned = owned_inventory_map[clean_m]

                        stor_owned = owned_storage_map.get(m_name, 0)
                        if stor_owned == 0 and clean_m in owned_storage_map:
                            stor_owned = owned_storage_map[clean_m]

                        incoming = pending_yields.get(m_name, 0)
                        total_owned = inv_owned + stor_owned
                        already_used = allocated_materials.get(m_name, 0)
                        if already_used == 0 and clean_m in allocated_materials:
                            already_used = allocated_materials[clean_m]
                        effective_owned = max(0, total_owned + incoming - already_used)

                        deficit = max(0, req - effective_owned)
                        if deficit > 0:
                            is_gath = (m_name in gather_dict or clean_m in gather_dict or m_name in KNOWN_GATHERABLE_ITEMS or clean_m in KNOWN_GATHERABLE_ITEMS)
                            if is_gath:
                                can_gather[m_name] = deficit
                                tier_allocated[m_name] = effective_owned + deficit
                            else:
                                cannot_gather.append(f"{m_name}(보유 부족 {deficit}개, 전체보유:{effective_owned}/{req})")
                        else:
                            tier_allocated[m_name] = req

                    if cannot_gather:
                        skip_reasons.append(f"가공 전용/비채집 재료 부족: {', '.join(cannot_gather)}")
                    else:
                        viable_choice = {
                            "recipe_name": r_name,
                            "needs_gathering": len(can_gather) > 0,
                            "gather_reqs": can_gather,
                            "allocated": tier_allocated
                        }
                        break

                if viable_choice:
                    if len(planned_tiers) < available_slots:
                        planned_tiers.append({
                            "tier": tier_num,
                            "item_name": item_name,
                            "recipe_name": viable_choice["recipe_name"],
                            "needs_gathering": viable_choice["needs_gathering"],
                            "gather_reqs": viable_choice["gather_reqs"],
                            "status": "needs_gathering" if viable_choice["needs_gathering"] else "ready_to_alter"
                        })
                        for g_name, g_qty in viable_choice["gather_reqs"].items():
                            category_raw_needed[g_name] += g_qty
                            total_raw_needed[g_name] += g_qty
                        for mat_k, mat_qty in viable_choice["allocated"].items():
                            allocated_materials[mat_k] += mat_qty
                    else:
                        skipped_tiers.append({
                            "tier": tier_num,
                            "item_name": item_name,
                            "reason": f"가공 슬롯 부족 (가용 {available_slots}개 모두 소진)"
                        })
                else:
                    skipped_tiers.append({
                        "tier": tier_num,
                        "item_name": item_name,
                        "reason": " / ".join(skip_reasons) if skip_reasons else "가공 레시피 없음 또는 재료 부족"
                    })

            categories_plan[cat_name] = {
                "category": cat_name,
                "facility": facility,
                "completed_count": len(completed_works),
                "completed_items": [w.get("DisplayName") for w in completed_works],
                "in_progress_count": len(in_progress_works),
                "in_progress_items": [w.get("DisplayName") for w in in_progress_works],
                "available_slots": available_slots,
                "planned_tiers": planned_tiers,
                "skipped_tiers": skipped_tiers,
                "raw_needed": dict(category_raw_needed)
            }

        return {
            "selected_category": category_key or "all",
            "alter_order": alter_order,
            "categories": categories_plan,
            "total_raw_needed": dict(total_raw_needed),
            "can_start": any(len(c["planned_tiers"]) > 0 for c in categories_plan.values()) or any(c["completed_count"] > 0 for c in categories_plan.values())
        }

    def execute_quick_alter(self, plan_or_category: Any = "all", callback: Optional[LogCallback] = None, alter_order: Optional[str] = None) -> Dict[str, Any]:
        """
        Executes the 7-slot facility quick alter routine:
        1. Collects all completed works at target facilities FIRST (가공품을 먼저 수령하여 재료 확보).
        2. Evaluates ingredient shortages & determines plan AFTER collection (수령 후 최신 인벤토리 기준으로 재료 판단).
        3. Gathers all required gatherable raw materials in bulk.
        4. Starts altering according to alter_order preference (high_tier: T7->T1, low_tier: T1->T7).
        5. Registers alarms for background completion.
        """
        if alter_order is None:
            if isinstance(plan_or_category, dict):
                alter_order = plan_or_category.get("alter_order") or settings_store.get("alter_order", "high_tier")
            else:
                alter_order = settings_store.get("alter_order", "high_tier")
        if alter_order not in ("high_tier", "low_tier"):
            alter_order = "high_tier"

        order_label = "상위 티어 우선 (오래 걸리는 가공부터)" if alter_order != "low_tier" else "하위 티어 우선 (기초 재료부터)"
        self.log(f"[7슬롯 최고 레벨 가공대 빠른 실행 가동] ({order_label}) 루틴을 시작합니다.", "action", callback)
        self.reset_abort()

        start_time = time.time()
        initial_wings = self._safe_get_wings()
        gathered_list: List[Dict[str, Any]] = []
        altered_list: List[Dict[str, Any]] = []

        activity = self.cli.get_activity()
        if activity.get("IsDead") or activity.get("IsReviving"):
            raise MabinogiCLIError("캐릭터가 행동 불능 상태입니다. 인게임에서 부활을 먼저 완료해주세요.")

        # Determine target categories
        cat_key = "all"
        if isinstance(plan_or_category, dict) and "selected_category" in plan_or_category:
            cat_key = plan_or_category["selected_category"]
        elif isinstance(plan_or_category, str):
            cat_key = plan_or_category

        target_cats = {}
        if cat_key and cat_key in self.CATEGORY_7_TIERS:
            target_cats[cat_key] = self.CATEGORY_7_TIERS[cat_key]
        else:
            target_cats = dict(self.CATEGORY_7_TIERS)

        if self.abort_requested:
            self.log("[사용자 중지] 시작 전 중지 요청이 감지되었습니다.", "warn", callback)
            final_wings = self._safe_get_wings(initial_wings)
            summary = self.format_and_log_summary(
                summary_title="7슬롯 빠른 가공 (사용자 중지)",
                initial_wings=initial_wings,
                final_wings=final_wings,
                gathered_list=gathered_list,
                altered_list=altered_list,
                crafted_list=[],
                collected_facilities=[],
                duration_sec=time.time() - start_time,
                status="aborted",
                callback=callback
            )
            return {"status": "aborted", "message": "사용자에 의해 작업이 중지되었습니다.", "summary": summary}

        # -------------------------------------------------------------
        # STEP 1: Collect ALL completed works at facilities FIRST!
        # (완료된 가공품을 먼저 모두 수령하여 가방에 재료를 확보하고 슬롯을 비움)
        # -------------------------------------------------------------
        collected_facilities = self.collect_completed_altering_works(callback=callback)

        if self.abort_requested:
            self.log("[사용자 중지] 작업이 중단되었습니다.", "warn", callback)
            final_wings = self._safe_get_wings(initial_wings)
            summary = self.format_and_log_summary(
                summary_title="7슬롯 빠른 가공 (사용자 중지)",
                initial_wings=initial_wings,
                final_wings=final_wings,
                gathered_list=gathered_list,
                altered_list=altered_list,
                crafted_list=[],
                collected_facilities=collected_facilities,
                duration_sec=time.time() - start_time,
                status="aborted",
                callback=callback
            )
            return {"status": "aborted", "message": "사용자에 의해 작업이 중지되었습니다.", "summary": summary}

        # -------------------------------------------------------------
        # STEP 2: Evaluate ingredient shortages & determine plan AFTER collection!
        # (수령 완료 후 최신화된 가방 보유량 기준으로 부족 여부를 판단)
        # -------------------------------------------------------------
        self.log(f"[가공 재료 판단] 가공품 수령 완료 후 최신 가방 상태를 기준으로 {order_label} 가공 가능 여부를 판단합니다.", "action", callback)
        plan = self.analyze_quick_alter(cat_key if cat_key != "all" else None, alter_order=alter_order)
        categories = plan.get("categories", {})
        total_raw_needed = plan.get("total_raw_needed", {})

        if self.abort_requested:
            self.log("[사용자 중지] 채집 전 작업이 중단되었습니다.", "warn", callback)
            final_wings = self._safe_get_wings(initial_wings)
            summary = self.format_and_log_summary(
                summary_title="7슬롯 빠른 가공 (사용자 중지)",
                initial_wings=initial_wings,
                final_wings=final_wings,
                gathered_list=gathered_list,
                altered_list=altered_list,
                crafted_list=[],
                collected_facilities=collected_facilities,
                duration_sec=time.time() - start_time,
                status="aborted",
                callback=callback
            )
            return {"status": "aborted", "message": "사용자에 의해 작업이 중지되었습니다.", "summary": summary}

        # -------------------------------------------------------------
        # STEP 3: Bulk gathering of all needed raw materials
        # (가공에 필요한 채집 원자재를 사전에 부족한 수량만큼만 채집)
        # -------------------------------------------------------------
        if total_raw_needed:
            self.log(f"[사전 원자재 맞춤 채집] 가공에 부족한 원자재 총 {len(total_raw_needed)}종을 필요한 수량만큼만 채집합니다.", "action", callback)
            for mat_name, needed_qty in total_raw_needed.items():
                if self.abort_requested:
                    self.log("[사용자 중지] 채집 대기열이 중단되었습니다.", "warn", callback)
                    final_wings = self._safe_get_wings(initial_wings)
                    summary = self.format_and_log_summary(
                        summary_title="7슬롯 빠른 가공 (사용자 중지)",
                        initial_wings=initial_wings,
                        final_wings=final_wings,
                        gathered_list=gathered_list,
                        altered_list=altered_list,
                        crafted_list=[],
                        collected_facilities=collected_facilities,
                        duration_sec=time.time() - start_time,
                        status="aborted",
                        callback=callback
                    )
                    return {"status": "aborted", "message": "사용자에 의해 작업이 중지되었습니다.", "summary": summary}

                if needed_qty <= 0:
                    continue
                is_gath, tool_ok = self.is_gatherable(mat_name)
                if not is_gath:
                    self.log(f"'{mat_name}'은(는) 필드 채집 목록에 없어 채집을 건너뜁니다.", "warn", callback)
                    continue
                if not tool_ok:
                    self.log(f"'{mat_name}' 채집 도구가 없거나 내구도가 0입니다!", "warn", callback)

                self.log(f"[맞춤 채집 시작] '{mat_name}' (부족 수량: {needed_qty}개) 목표 수량만큼만 채집을 진행합니다.", "action", callback)
                try:
                    g_res = self.auto_gather(mat_name, needed_qty, callback=callback)
                    gained = g_res.get("gained", 0) if isinstance(g_res, dict) else 0
                    gathered_list.append({
                        "item": mat_name,
                        "gained": gained,
                        "required": needed_qty,
                        "status": g_res.get("status", "completed") if isinstance(g_res, dict) else "completed"
                    })
                    if isinstance(g_res, dict) and g_res.get("status") == "overweight":
                        self.log("[가방 과적 방지] 가방 무게가 98% 기준에 도달하여 남은 맞춤 채집을 안전 중단하고 다음 단계로 진행합니다.", "warn", callback)
                        break
                    if isinstance(g_res, dict) and g_res.get("status") == "aborted":
                        self.log("[사용자 중지] 빠른 실행 파이프라인이 중단되었습니다.", "warn", callback)
                        final_wings = self._safe_get_wings(initial_wings)
                        summary = self.format_and_log_summary(
                            summary_title="7슬롯 빠른 가공 (사용자 중지)",
                            initial_wings=initial_wings,
                            final_wings=final_wings,
                            gathered_list=gathered_list,
                            altered_list=altered_list,
                            crafted_list=[],
                            collected_facilities=collected_facilities,
                            duration_sec=time.time() - start_time,
                            status="aborted",
                            callback=callback
                        )
                        return {"status": "aborted", "message": "사용자에 의해 작업이 중지되었습니다.", "summary": summary}
                except Exception as ex:
                    gathered_list.append({
                        "item": mat_name,
                        "gained": 0,
                        "required": needed_qty,
                        "status": "failed",
                        "error": str(ex)
                    })
                    self.log(f"'{mat_name}' 채집 중 오류 발생: {ex}", "warn", callback)

        if self.abort_requested:
            self.log("[사용자 중지] 가공 등록 전 작업이 중단되었습니다.", "warn", callback)
            final_wings = self._safe_get_wings(initial_wings)
            summary = self.format_and_log_summary(
                summary_title="7슬롯 빠른 가공 (사용자 중지)",
                initial_wings=initial_wings,
                final_wings=final_wings,
                gathered_list=gathered_list,
                altered_list=altered_list,
                crafted_list=[],
                collected_facilities=collected_facilities,
                duration_sec=time.time() - start_time,
                status="aborted",
                callback=callback
            )
            return {"status": "aborted", "message": "사용자에 의해 작업이 중지되었습니다.", "summary": summary}

        # -------------------------------------------------------------
        # STEP 4: Start altering from High Tier down to Low Tier at once
        # (고티어부터 순서대로 가공 시작)
        # -------------------------------------------------------------
        queued_results = []
        for c_name, c_info in categories.items():
            if self.abort_requested:
                self.log("[사용자 중지] 가공 슬롯 등록 중 작업이 중단되었습니다.", "warn", callback)
                break

            facility = c_info["facility"]
            planned = c_info.get("planned_tiers", [])
            if not planned:
                self.log(f"[{c_name} ({facility})] 등록 가능한 고티어 가공이 없어 건너뜁니다.", "info", callback)
                continue

            self.log(f"[{c_name} ({facility})] {order_label} 순차 가공 등록 시작 (총 {len(planned)}건)", "action", callback)
            for item in planned:
                if self.abort_requested:
                    self.log("[사용자 중지] 가공 등록이 중단되었습니다.", "warn", callback)
                    break

                tier_num = item["tier"]
                r_name = item["recipe_name"]
                self.log(f"   ▶ Tier {tier_num} '{r_name}' 가공 대기열 등록...", "action", callback)
                try:
                    res = self.cli.execute_altering(r_name)
                    self.log("     등록 성공!", "success", callback)
                    queued_results.append({"category": c_name, "tier": tier_num, "recipe": r_name, "status": "success"})
                    altered_list.append({
                        "facility": facility,
                        "recipe": r_name,
                        "item": item["item_name"],
                        "registered": 1,
                        "yield_est": 3
                    })
                except Exception as ex:
                    self.log(f"     등록 실패: {ex}", "error", callback)
                    queued_results.append({"category": c_name, "tier": tier_num, "recipe": r_name, "status": "failed", "error": str(ex)})
                time.sleep(0.35)

        if altered_list:
            self.notify_altering_updated()

        # -------------------------------------------------------------
        # STEP 5: Register facility-wide alarms for last work completion
        # -------------------------------------------------------------
        synced = alarm_store.sync_facility_alarms(self.cli, default_source="빠른 가공")
        if synced:
            self.log(f"[가공시설 알람 동기화] 총 {len(synced)}개 시설의 마지막 가공 완료 시각에 맞춰 알람이 등록되었습니다.", "info", callback)

        self.log("[가공대 7슬롯 빠른 실행 완료] 수령 -> 가공 판단 -> 일괄 채집 -> 고티어 가공 등록이 완료되었습니다!", "success", callback)
        self.log("각 가공시설별 마지막 가공이 끝나면 알람이 울립니다.", "info", callback)
        self.try_auto_play_music(callback=callback)

        duration = time.time() - start_time
        final_wings = self._safe_get_wings(initial_wings)
        status_str = "waiting_in_background" if len(queued_results) > 0 else "completed"

        summary = self.format_and_log_summary(
            summary_title="7슬롯 최고 레벨 가공대 빠른 실행 완료",
            initial_wings=initial_wings,
            final_wings=final_wings,
            gathered_list=gathered_list,
            altered_list=altered_list,
            crafted_list=[],
            collected_facilities=collected_facilities,
            duration_sec=duration,
            status=status_str,
            callback=callback
        )

        return {
            "status": "completed",
            "collected_facilities": collected_facilities,
            "queued": queued_results,
            "total_queued": len(queued_results),
            "summary": summary
        }

    # =========================================================================
    # AI CONNECTOR EXTENDED AUTOMATION & CONVENIENCE FEATURES
    # =========================================================================

    def ensure_character_ready_for_action(self, callback: Optional[LogCallback] = None) -> None:
        """
        Checks character state via get_activity() before starting a pipeline:
        - Raises if dead/reviving or in dungeon/battlefield.
        - Automatically stands up if sitting on a chair or ground (/앉기), or stops instrument performance.
        """
        try:
            activity = self.cli.get_activity()
        except Exception:
            return

        combat = activity.get("combatState", {}) if isinstance(activity.get("combatState"), dict) else {}
        if activity.get("IsDead") or activity.get("IsReviving") or combat.get("IsDead") or combat.get("IsReviving"):
            raise MabinogiCLIError("캐릭터가 행동 불능 상태입니다. 인게임에서 부활을 먼저 완료해주세요.")

        dungeon = activity.get("Dungeon", {}) if isinstance(activity.get("Dungeon"), dict) else {}
        if dungeon.get("State") and dungeon.get("State") != "NotInDungeon":
            raise MabinogiCLIError(f"캐릭터가 현재 던전({dungeon.get('State')}) 내부에 있어 생활 이동을 시작할 수 없습니다.")

        perf = activity.get("Performance", {}) if isinstance(activity.get("Performance"), dict) else {}
        mode = activity.get("Mode", {}) if isinstance(activity.get("Mode"), dict) else {}
        if perf.get("IsPlaying") or mode.get("SitState") == "Sitting":
            self.log("[상태 자동 보정] 의자 착석 또는 악기 연주 상태를 해제하고 출발합니다.", "info", callback)
            try:
                self.cli.stop_action()
                time.sleep(0.4)
            except Exception:
                pass

        # Also attempt stand_up quietly in case character used /앉기 emote
        try:
            self.cli.stand_up()
        except Exception:
            pass

    def try_auto_play_music(self, callback: Optional[LogCallback] = None) -> bool:
        """
        If 'auto_play_music_enabled' setting is ON, equips an instrument (if none equipped)
        and starts playing the user's selected music score (or the first available score) when idle.
        """
        if not settings_store.get("auto_play_music_enabled", False) or self.abort_requested:
            return False
        try:
            instruments = self.cli.get_instruments()
            if not instruments:
                return False
            equipped = [inst for inst in instruments if inst.get("IsEquipped")]
            if not equipped:
                first_inst = instruments[0].get("Name")
                if first_inst:
                    self.cli.change_instrument(first_inst)
                    time.sleep(0.3)

            scores = self.cli.get_music_scores()
            if not scores:
                return False
            pref_title = (settings_store.get("auto_play_music_title") or "").strip()
            target_title = ""
            if pref_title:
                for sc in scores:
                    if sc.get("DisplayTitle") == pref_title or pref_title in sc.get("DisplayTitle", ""):
                        target_title = sc.get("DisplayTitle", "")
                        break
            if not target_title:
                target_title = scores[0].get("DisplayTitle", "")
            if target_title:
                self.cli.play_music_score(target_title)
                self.log(f"🎵 [휴식 자동 연주] 작업 대기를 시작하며 '{target_title}' 악보 연주를 시작합니다.", "info", callback)
                return True
        except Exception:
            pass
        return False

    def recommend_zero_gather_crafts(self) -> List[Dict[str, Any]]:
        """
        [#8 창고 털이 추천기]
        Scans current bag + personal/account storage inventory once and finds all craftable / alterable
        recipes that can be produced 100% IMMEDIATELY with ZERO additional gathering.
        Returns sorted list by total materials consumed (best storage cleanup effect first).
        """
        locs_map = self.cli.get_all_item_locations_map()
        recommendations: List[Dict[str, Any]] = []

        # 1. Check all craft recipes
        for r_name, ings in (self.recipes or {}).items():
            if not ings or not isinstance(ings, dict):
                continue
            max_crafts = 999999
            per_craft_sum = 0
            ing_details = []
            for ing_k, ing_req in ings.items():
                req = max(1, int(ing_req))
                per_craft_sum += req
                clean_k = ing_k.split("(")[0].strip()
                owned = locs_map.get(clean_k, {}).get("total", 0)
                if owned < req:
                    max_crafts = 0
                    break
                max_crafts = min(max_crafts, owned // req)
                ing_details.append({"name": clean_k, "req": req, "owned": owned})

            if 0 < max_crafts < 999999:
                prod_pc = max(1, self.get_produced_per_craft(r_name))
                total_consumed = max_crafts * per_craft_sum
                owned_self = locs_map.get(r_name, {}).get("total", 0)
                recommendations.append({
                    "item_name": r_name,
                    "category": "완제품 제작",
                    "max_crafts": max_crafts,
                    "total_output": max_crafts * prod_pc,
                    "owned_now": owned_self,
                    "materials_consumed": total_consumed,
                    "net_slots_freed": max(0, total_consumed - (max_crafts * prod_pc)),
                    "ingredients": ing_details
                })

        # 2. Check all standard alter recipes
        for a_name, a_info in STANDARD_ALTER_RECIPES.items():
            ings = a_info.get("ingredients", {})
            if not ings:
                continue
            max_works = 999999
            per_work_sum = 0
            ing_details = []
            for ing_k, ing_req in ings.items():
                req = max(1, int(ing_req))
                per_work_sum += req
                clean_k = ing_k.split("(")[0].strip()
                owned = locs_map.get(clean_k, {}).get("total", 0)
                if owned < req:
                    max_works = 0
                    break
                max_works = min(max_works, owned // req)
                ing_details.append({"name": clean_k, "req": req, "owned": owned})

            if 0 < max_works < 999999:
                prod_pw = max(1, int(a_info.get("produced", 3)))
                total_consumed = max_works * per_work_sum
                owned_self = locs_map.get(a_name, {}).get("total", 0)
                recommendations.append({
                    "item_name": a_name,
                    "category": a_info.get("facility", "시설 가공"),
                    "max_crafts": max_works,
                    "total_output": max_works * prod_pw,
                    "owned_now": owned_self,
                    "materials_consumed": total_consumed,
                    "net_slots_freed": max(0, total_consumed - (max_works * prod_pw)),
                    "ingredients": ing_details
                })

        recommendations.sort(key=lambda x: (-x["materials_consumed"], -x["max_crafts"], x["item_name"]))
        return recommendations[:40]

    def get_safety_stocks_status(self) -> List[Dict[str, Any]]:
        """
        [#9 상비약·요리 최소 재고 자동 보충]
        Returns current inventory+storage counts vs configured minimum safety stock levels.
        """
        stocks_cfg = settings_store.get("safety_stocks", {}) or {}
        if not isinstance(stocks_cfg, dict):
            stocks_cfg = {}
        try:
            locs_map = self.cli.get_all_item_locations_map()
        except Exception:
            locs_map = {}

        result = []
        for item_name, min_qty in stocks_cfg.items():
            goal = max(1, int(min_qty or 1))
            bd = locs_map.get(item_name, {"inventory": 0, "storage_total": 0, "total": 0})
            owned = bd.get("total", 0)
            deficit = max(0, goal - owned)
            result.append({
                "item_name": item_name,
                "min_stock": goal,
                "owned_bag": bd.get("inventory", 0),
                "owned_storage": bd.get("storage_total", 0),
                "owned_total": owned,
                "deficit": deficit,
                "is_satisfied": deficit == 0
            })
        return result

    def apply_safety_stocks_to_custom(self) -> List[Dict[str, Any]]:
        """
        Adds all deficient safety stock items to custom_target_store so the user can produce them in one click.
        """
        statuses = self.get_safety_stocks_status()
        added = []
        for st in statuses:
            if st["deficit"] > 0:
                entry = custom_target_store.add_or_update(
                    item_name=st["item_name"],
                    goal=st["min_stock"],
                    current=st["owned_total"]
                )
                added.append(entry)
        return added

    def scan_all_selectable_quests(self) -> List[Dict[str, Any]]:
        """
        [#11 퀘스트 선택 가져오기 모달용 스캔]
        Scans both weekly delivery quests AND general/sub/guide/event quests in the Quest Tracker,
        returning a structured list so the user can selectively check which items to import.
        """
        selectable: List[Dict[str, Any]] = []
        seen_keys = set()

        # 1. Weekly Delivery Quests
        for dq in self.detect_delivery_quests():
            key = f"delivery:{dq.item_name}"
            seen_keys.add(key)
            selectable.append({
                "id": key,
                "quest_title": dq.quest_title,
                "category": "주간 납품",
                "target_mode": "delivery",
                "item_name": dq.item_name,
                "current": dq.current,
                "goal": dq.goal,
                "needed": dq.needed,
                "is_completed": dq.is_completed,
                "already_registered": dq.item_name in delivery_target_store.targets
            })

        # 2. General / Guide / Sub / Event / Goddess Missions in Quest Tracker
        try:
            raw_quests = self.cli.get_quests()
        except Exception:
            raw_quests = []

        all_known_items = (
            set((self.recipes or {}).keys())
            | set(STANDARD_ALTER_RECIPES.keys())
            | set(KNOWN_GATHERABLE_ITEMS)
        )
        # Sort known items by length descending so longer specific names match first
        sorted_known = sorted(all_known_items, key=len, reverse=True)

        for q in raw_quests:
            q_title = self.clean_text(q.get("QuestTitle", ""))
            src_disp = self.clean_text(q.get("SourceDisplayName", "") or q.get("Source", "일반 퀘스트"))
            for obj in q.get("Objectives", []):
                desc = self.clean_text(obj.get("Description", ""))
                cur = int(obj.get("Count", 0) or 0)
                goal = int(obj.get("Goal", 1) or 1)
                is_comp = bool(obj.get("IsCompleted", False))

                # Try matching any known craft/alter/gather item inside the objective description
                matched_item = ""
                for cand in sorted_known:
                    if len(cand) >= 2 and cand in desc:
                        matched_item = cand
                        break
                if not matched_item:
                    continue

                if f"delivery:{matched_item}" in seen_keys:
                    continue
                key = f"quest:{q_title}:{matched_item}"
                if key in seen_keys:
                    continue
                seen_keys.add(key)

                selectable.append({
                    "id": key,
                    "quest_title": q_title,
                    "category": src_disp or "일반/가이드",
                    "target_mode": "custom",
                    "item_name": matched_item,
                    "current": cur,
                    "goal": goal,
                    "needed": max(0, goal - cur),
                    "is_completed": is_comp,
                    "objective_text": desc,
                    "already_registered": matched_item in custom_target_store.targets or matched_item in delivery_target_store.targets
                })

        return selectable

    def execute_smart_fishing(
        self,
        fish_name: str = "고등어",
        target_count: int = 0,
        max_minutes: int = 30,
        callback: Optional[LogCallback] = None,
        status_reporter: Optional[Callable[[str], None]] = None
    ) -> Dict[str, Any]:
        """
        [#5 스마트 자동 낚시 매니저]
        Starts auto-fishing for `fish_name` via execute_gathering (which returns 'started' immediately
        and runs indefinitely in-game), then monitors bag weight, gained fish count, and timer,
        calling stop_action() automatically when any safety/goal condition is reached.
        """
        self.reset_abort()
        self.ensure_character_ready_for_action(callback=callback)

        fish_name = (fish_name or "고등어").strip()
        max_sec = max(1, int(max_minutes or 30)) * 60
        start_time = time.time()
        initial_wings = self._safe_get_wings()

        try:
            init_locs = self.cli.get_all_item_locations_map(bypass_cache=True)
            init_inv = {k: v.get("inventory", 0) for k, v in init_locs.items()}
        except Exception:
            init_inv = {}
        init_fish_cnt = init_inv.get(fish_name, 0)

        self.log(
            f"🎣 [스마트 자동 낚시 시작] 대상: '{fish_name}' | 목표: {f'{target_count}마리' if target_count > 0 else '제한 없음'} | 최대 시간: {max_minutes}분",
            "action",
            callback
        )
        try:
            res = self.cli.execute_gathering(fish_name)
            self.log(f"   낚시터 이동 및 자동 낚시 시작 응답: {res}", "info", callback)
        except Exception as e:
            self.log(f"   낚시 시작 오류: {e}", "error", callback)
            raise

        stop_reason = "목표 시간 도달"
        round_start_time = time.time()
        while not self.abort_requested:
            elapsed = time.time() - start_time
            if elapsed >= max_sec:
                stop_reason = f"설정 시간({max_minutes}분) 도달"
                break

            # 55-minute single-action cycle refresh (prevents 1-hour continuous action popup)
            if (time.time() - round_start_time) >= 3300 and (max_sec - elapsed) > 60:
                self.log(
                    f"🎣 [55분 주기 갱신] '{fish_name}' 연속 낚시 55분이 경과하여 1시간 확인창 방지를 위해 잠시 멈추고 즉시 다시 낚시를 시작합니다.",
                    "info",
                    callback
                )
                try:
                    self.cli.stop_action()
                except Exception:
                    pass
                time.sleep(2.0)
                if self.abort_requested:
                    break
                try:
                    self.cli.execute_gathering(fish_name)
                    round_start_time = time.time()
                except Exception as e:
                    self.log(f"   낚시 재시작 오류: {e}", "warn", callback)
                    break

            # Check inventory weight & fish count every 3 seconds
            try:
                inv = self.cli.get_inventory()
                cur_w = float(inv.get("CurrentInventoryWeightAsDecimal", 0) or inv.get("CurrentInventoryWeight", 0) or 0)
                max_w = float(inv.get("MaxInventoryWeightAsDecimal", 100) or inv.get("MaxInventoryWeight", 100) or 100)
                limit_ratio = get_weight_limit_ratio()
                if max_w > 0 and (cur_w / max_w) >= limit_ratio:
                    stop_reason = f"가방 안전 무게({limit_ratio*100:.0f}%) 도달 ({cur_w:.0f}/{max_w:.0f})"
                    break
            except Exception:
                pass

            try:
                curr_locs = self.cli.get_all_item_locations_map(bypass_cache=True)
                curr_fish = curr_locs.get(fish_name, {}).get("inventory", 0)
                gained_target = max(0, curr_fish - init_fish_cnt)
                total_all_gained = sum(
                    max(0, v.get("inventory", 0) - init_inv.get(k, 0))
                    for k, v in curr_locs.items()
                )
                rem_min = max(0, int((max_sec - elapsed) // 60))
                if status_reporter:
                    status_reporter(
                        f"🎣 스마트 낚시 중: '{fish_name}' +{gained_target}마리 (전체 어획 +{total_all_gained}개 | 남은 시간 {rem_min}분)"
                    )
                if target_count > 0 and gained_target >= target_count:
                    stop_reason = f"목표 수량({target_count}마리) 달성"
                    break
            except Exception:
                pass

            time.sleep(3.0)

        if self.abort_requested:
            stop_reason = "사용자 중지 요청"

        try:
            self.cli.stop_action()
        except Exception:
            pass

        # Summarize all caught items
        caught_items = []
        try:
            final_locs = self.cli.get_all_item_locations_map(bypass_cache=True)
            for k, v in final_locs.items():
                diff = v.get("inventory", 0) - init_inv.get(k, 0)
                if diff > 0:
                    caught_items.append({"item": k, "gained": diff, "required": target_count if k == fish_name else diff, "status": "completed"})
        except Exception:
            pass

        self.log(f"🎣 [스마트 자동 낚시 종료] 사유: {stop_reason} (총 {len(caught_items)}종 어획 완료)", "success", callback)
        self.try_auto_play_music(callback=callback)

        final_wings = self._safe_get_wings(initial_wings)
        summary = self.format_and_log_summary(
            summary_title=f"스마트 자동 낚시 완료 ({stop_reason})",
            initial_wings=initial_wings,
            final_wings=final_wings,
            gathered_list=caught_items,
            altered_list=[],
            crafted_list=[],
            collected_facilities=[],
            duration_sec=time.time() - start_time,
            status="aborted" if self.abort_requested else "completed",
            callback=callback
        )
        return {
            "status": "aborted" if self.abort_requested else "completed",
            "reason": stop_reason,
            "caught": caught_items,
            "summary": summary
        }


