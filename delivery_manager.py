import os
import sys
import re
import math
import time
import json
import threading
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional, Tuple, Set

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from mabi_cli import MabinogiCLI, MabinogiCLIError

LogCallback = Callable[[str, str], None]  # (level, message)
AlarmCallback = Callable[[Dict[str, Any]], None]

if getattr(sys, "frozen", False):
    SCRIPT_DIR = os.path.dirname(sys.executable)
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RECIPE_CACHE_FILE = os.path.join(SCRIPT_DIR, "recipes_cache.json")
ALARMS_FILE = os.path.join(SCRIPT_DIR, "delivery_alarms.json")
DELIVERY_TARGETS_FILE = os.path.join(SCRIPT_DIR, "registered_deliveries.json")

# Default known recipes for common weekly delivery items
DEFAULT_RECIPES: Dict[str, Dict[str, int]] = {
    "론 엣지소드S": {"합금강괴": 3, "가죽+": 3},
    "론 엣지소드": {"철괴": 3, "가죽": 3},
    "비늘 갑옷 신발S": {"합금강괴": 3, "가죽+": 2},
    "비늘 갑옷 투구S": {"합금강괴": 3, "가죽+": 2},
    "비늘 갑옷 장갑S": {"합금강괴": 3, "가죽+": 2},
    "비늘 갑옷 상의S": {"합금강괴": 5, "가죽+": 4},
    "비늘 갑옷 하의S": {"합금강괴": 4, "가죽+": 3},
    "로터스 힐링 완드S": {"상급 목재": 3, "합금강괴": 3},
    "로터스 힐링 완드": {"목재": 3, "철괴": 3},
    "린넨 로브S": {"상급 옷감": 3, "옷감+": 2},
    "린넨 로브": {"옷감": 3, "가죽": 2},
    "마블 힐링 완드S": {"상급 목재": 3, "강철괴": 3},
    "달걀프라이": {"달걀": 1},
    "라이트 크로스보우S": {"상급 목재": 3, "강철괴": 3},
    "가죽 갑옷 신발S": {"상급 가죽": 3, "강철괴": 2},
    "두꺼운 가죽 갑옷 신발": {"가죽+": 2, "강철괴": 1},
    "두꺼운 전투복 신발": {"옷감+": 2, "강철괴": 1},
    "사슬 갑옷 신발": {"강철괴": 2, "가죽+": 1},
    "그랜드 크로스보우": {"목재+": 2, "강철괴": 2},
    "전투복 신발S": {"상급 옷감": 3, "강철괴": 2},
    "크레센트 엣지소드": {"강철괴": 2, "가죽+": 2},
    "마법 유탄 부품": {"마나 허브": 2},
    "캠프파이어 키트": {"통나무": 5},
    "숙련 캠프파이어 키트": {"상급 통나무": 5},
    "전문 캠프파이어 키트": {"상급 통나무+": 4},
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
    # 🪙 금속 가공 시설 (Metal)
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
        "ingredients": {"합금강괴": 4, "석탄": 12},
        "tier": 4,
        "display_name": "특수강괴"
    },
    "은합금괴": {
        "facility": "금속 가공 시설",
        "produced": 3,
        "ingredients": {"특수강괴": 5, "석탄": 16},
        "tier": 5,
        "display_name": "은합금괴"
    },
    "운철괴": {
        "facility": "금속 가공 시설",
        "produced": 3,
        "ingredients": {"은합금괴": 5, "석탄": 20},
        "tier": 6,
        "display_name": "운철괴"
    },
    "백금강괴": {
        "facility": "금속 가공 시설",
        "produced": 3,
        "ingredients": {"운철괴": 5, "석탄": 20},
        "tier": 7,
        "display_name": "백금강괴"
    },

    # 🪵 목재 가공 시설 (Lumber)
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
        "ingredients": {"목재": 3, "단단한 통나무": 4},
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
        "ingredients": {"상급 목재+": 5, "나무 진액": 16},
        "tier": 5,
        "display_name": "최상급 목재"
    },
    "최상급 목재+": {
        "facility": "목재 가공 시설",
        "produced": 3,
        "ingredients": {"최상급 목재": 5, "나무 진액": 20},
        "tier": 6,
        "display_name": "최상급 목재+"
    },
    "특급 목재": {
        "facility": "목재 가공 시설",
        "produced": 3,
        "ingredients": {"최상급 목재+": 5, "나무 진액": 20},
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

    # 🦊 가죽 가공 시설 (Leather)
    "가죽": {
        "facility": "가죽 가공 시설",
        "produced": 3,
        "ingredients": {"거친 가죽": 10},
        "tier": 1,
        "display_name": "가죽"
    },
    "가죽+": {
        "facility": "가죽 가공 시설",
        "produced": 3,
        "ingredients": {"가죽": 3, "두꺼운 가죽": 4},
        "tier": 2,
        "display_name": "가죽+"
    },
    "상급 가죽": {
        "facility": "가죽 가공 시설",
        "produced": 3,
        "ingredients": {"가죽+": 3},
        "tier": 3,
        "display_name": "상급 가죽"
    },
    "상급 가죽+": {
        "facility": "가죽 가공 시설",
        "produced": 3,
        "ingredients": {"상급 가죽": 4, "타닌 가루": 12},
        "tier": 4,
        "display_name": "상급 가죽+"
    },
    "최상급 가죽": {
        "facility": "가죽 가공 시설",
        "produced": 3,
        "ingredients": {"상급 가죽+": 5, "타닌 가루": 16},
        "tier": 5,
        "display_name": "최상급 가죽"
    },
    "최상급 가죽+": {
        "facility": "가죽 가공 시설",
        "produced": 3,
        "ingredients": {"최상급 가죽": 5, "타닌 가루": 20},
        "tier": 6,
        "display_name": "최상급 가죽+"
    },
    "특급 가죽": {
        "facility": "가죽 가공 시설",
        "produced": 3,
        "ingredients": {"최상급 가죽+": 5, "타닌 가루": 20},
        "tier": 7,
        "display_name": "특급 가죽"
    },

    # 🧶 옷감 가공 시설 (Cloth)
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
        "ingredients": {"옷감+": 3, "양털": 8},
        "tier": 3,
        "display_name": "상급 옷감"
    },
    "상급 옷감+": {
        "facility": "옷감 가공 시설",
        "produced": 3,
        "ingredients": {"상급 옷감": 4, "양털": 12},
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
        "ingredients": {"두꺼운 양털": 50, "밀랍": 2},
        "tier": 3,
        "display_name": "두꺼운 옷감"
    },

    # 🧵 실크 (Silk)
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

    # 🌿 요리 및 기타 가공
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

    # ⚗️ 약품 / 결정
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
    "거친 가죽", "가죽", "두꺼운 가죽",
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
    "완드", "소드", "검", "활", "크로스보우", "갑옷", "투구", "신발", "장갑", "로브", "방패",
    "의복", "전투복", "모자", "옷", "너클", "둔기", "도끼", "스태프", "랜스", "체인"
]

def is_equipment_item(item_name: str) -> bool:
    """Returns True if item_name is an equipment/weapon/armor piece."""
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
            # If the new work completes later than existing target, extend targetTimestamp
            if target_ms > existing.get("targetTimestamp", 0) or existing.get("notified", False):
                existing["targetTimestamp"] = target_ms
                existing["notified"] = False
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
        for fac, f_works in facility_works.items():
            incomplete = [w for w in f_works if not w.get("IsCompleted") and int(w.get("RemainingSeconds", 0)) > 0]
            if not incomplete:
                # All slots at this facility are finished and waiting to be collected
                if fac in self.alarms:
                    self.alarms[fac]["total_remaining_seconds"] = 0
                    self.alarms[fac]["targetTimestamp"] = min(self.alarms[fac].get("targetTimestamp", int(time.time() * 1000)), int(time.time() * 1000))
                    self.save()
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
                self.delete_alarm(k)

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

class RegisteredDeliveryStore:
    """Manages persistent weekly delivery target items registered by the user."""
    def __init__(self, filepath: str = DELIVERY_TARGETS_FILE):
        self.filepath = filepath
        self.targets: Dict[str, Dict[str, Any]] = {}
        self.load()

    def load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    self.targets = json.load(f)
            except Exception:
                self.targets = {}

    def save(self):
        try:
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(self.targets, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def add_or_update(self, item_name: str, goal: int, current: int = 0, quest_title: str = "주간 납품") -> Dict[str, Any]:
        needed = max(0, int(goal) - int(current))
        data = {
            "quest_title": quest_title,
            "item_name": item_name,
            "goal": int(goal),
            "current": int(current),
            "needed": needed,
            "is_completed": (int(current) >= int(goal)),
            "updated_at": int(time.time() * 1000)
        }
        self.targets[item_name] = data
        self.save()
        return data

    def update_current(self, item_name: str, current: int) -> Optional[Dict[str, Any]]:
        """Directly sets the current owned count for item_name and recalculates completion status."""
        if item_name in self.targets:
            t = self.targets[item_name]
            goal = int(t.get("goal", 1))
            cur = max(0, int(current))
            t["current"] = cur
            t["needed"] = max(0, goal - cur)
            t["is_completed"] = (cur >= goal)
            t["updated_at"] = int(time.time() * 1000)
            self.targets[item_name] = t
            self.save()
            return t
        return None

    def delete(self, item_name: str):
        if item_name in self.targets:
            del self.targets[item_name]
            self.save()

    def clear(self):
        self.targets = {}
        self.save()

    def get_all(self) -> List[Dict[str, Any]]:
        return list(self.targets.values())

delivery_target_store = RegisteredDeliveryStore()

PRESETS_FILE = os.path.join(SCRIPT_DIR, "delivery_presets.json")

class DeliveryPresetStore:
    """Manages saved weekly delivery quest presets for reuse across weeks."""
    def __init__(self, filepath: str = PRESETS_FILE):
        self.filepath = filepath
        self.presets: Dict[str, Dict[str, Any]] = {}
        self.load()

    def load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    self.presets = json.load(f)
            except Exception:
                self.presets = {}

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

class DeliveryManager:
    def __init__(self, cli: Optional[MabinogiCLI] = None):
        self.cli = cli or MabinogiCLI()
        self.recipes: Dict[str, Dict[str, int]] = dict(DEFAULT_RECIPES)
        self.abort_requested: bool = False
        self.load_recipe_cache()

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
        if os.path.exists(RECIPE_CACHE_FILE):
            try:
                with open(RECIPE_CACHE_FILE, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                    self.recipes.update(cached)
            except Exception:
                pass

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
    ) -> Dict[str, Any]:
        """
        Builds a comprehensive, aggregated execution report detailing:
        - Total Spirit Wings consumed (initial -> final)
        - Raw materials gathered (item, quantity, goal)
        - Altering works queued (facility, recipe, slots, estimated yield)
        - Finished goods crafted (item, quantity)
        - Pre-collected completed alters & deferred tasks
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
            f"📊 [{summary_title}]",
            "======================================================================",
        ]

        # 1. Wings
        if wings_used > 0:
            lines.append(f"🪽 [정령의 날개 소모] 총 {wings_used:,}개 사용 (시작: {initial_wings:,}개 ➔ 현재: {final_wings:,}개)")
        else:
            lines.append(f"🪽 [정령의 날개 소모] 0개 사용 (날개 차감 없음 | 현재 보유: {final_wings:,}개)")

        # 2. Gather
        lines.append("")
        if gathered_aggregated:
            lines.append(f"🌿 [원자재 맞춤 채집] 총 {len(gathered_aggregated)}종 / {total_gathered_qty:,}개 채집 완료:")
            for g in gathered_aggregated:
                req_info = f" (목표: {g['required']:,}개)" if g.get("required") else ""
                lines.append(f"   • {g['item']}: +{g['gained']:,}개 획득{req_info}")
        else:
            lines.append("🌿 [원자재 맞춤 채집] 채집 작업 없음 (필요 원자재가 가방/창고에 충분함)")

        # 3. Alter
        lines.append("")
        if altered_aggregated:
            lines.append(f"⚙️ [시설 가공 등록] 총 {len(altered_aggregated)}개 품목 / {total_altered_slots}슬롯 등록 완료:")
            for a in altered_aggregated:
                yield_info = f" (예상 산출량: 약 {a['yield_est']:,}개)" if a.get("yield_est") else ""
                lines.append(f"   • [{a['facility']}] '{a['recipe']}': {a['registered']}슬롯{yield_info}")
        else:
            lines.append("⚙️ [시설 가공 등록] 신규 가공 등록 없음")

        # 4. Craft
        lines.append("")
        if crafted_aggregated:
            lines.append(f"🔨 [완제품 납품 제작] 총 {len(crafted_aggregated)}개 품목 / {total_crafted_qty:,}개 제작 완료:")
            for c in crafted_aggregated:
                lines.append(f"   • '{c['item']}': {c['crafted']:,}개 제작 완료 ✅")
        else:
            lines.append("🔨 [완제품 납품 제작] 완제품 제작 없음 (현재 가공 대기열 진행 중)")

        # 5. Pre-collection
        if unique_collected:
            lines.append("")
            lines.append(f"📦 [사전 가공품 수령] {', '.join(unique_collected)} 완료 가공품 수령 완료 (슬롯 확보 및 재료 입고)")

        # 6. Deferred
        if deferred_works:
            lines.append("")
            lines.append(f"⏳ [가공 보류/대기] 총 {len(deferred_works)}건의 가공이 슬롯 또는 하위 재료 대기로 보류되었습니다.")
            for d in deferred_works[:3]:
                fac = d.get("facility", "")
                it = d.get("item_name", "")
                cnt = d.get("deferred", d.get("needed", 0))
                lines.append(f"   • [{fac}] '{it}': {cnt}회 보류")
            if len(deferred_works) > 3:
                lines.append(f"   • 외 {len(deferred_works) - 3}건 보류")

        if status == "waiting_in_background":
            lines.append("")
            lines.append("🔔 [알람 동기화] 등록된 가공이 백그라운드에서 진행 중입니다. 완료 알람 후 '일괄 제작'을 다시 실행하세요!")
        elif status == "aborted":
            lines.append("")
            lines.append("🛑 [사용자 중지] 사용자의 요청으로 작업이 중간에 중단되었습니다. 중단 시점까지의 결과가 요약되었습니다.")

        lines.append("======================================================================")

        for l in lines:
            self.log(l, "success" if status in ("completed", "waiting_in_background") else "warn", callback)

        m, s = divmod(int(duration_sec), 60)
        dur_str = f"{m}분 {s}초" if m > 0 else f"{s}초"

        status_text = {
            "completed": "완료 ✅",
            "waiting_in_background": "완료 ✅",
            "aborted": "사용자 중지 🛑",
            "error": "오류 중단 ❌"
        }.get(status, "완료 ✅")

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
        Scans active quest tracker entries for delivery objectives like:
        '<color=orange>아이템명</color> 보유 3/6'
        """
        quests = self.cli.get_quests()
        tasks: List[DeliveryTask] = []
        pattern = re.compile(r"(.+?)\s*보유\s*(\d+)/(\d+)")

        for q in quests:
            quest_title = self.clean_text(q.get("QuestTitle", ""))
            objectives = q.get("Objectives", [])
            for obj in objectives:
                desc = self.clean_text(obj.get("Description", ""))
                match = pattern.search(desc)
                if match:
                    item_name = match.group(1).strip()
                    current = int(match.group(2))
                    goal = int(match.group(3))
                    is_completed = obj.get("IsCompleted", current >= goal)
                    tasks.append(DeliveryTask(quest_title, item_name, current, goal, is_completed))

        return tasks

    def get_effective_owned(self, item_name: str, include_storage: bool = True) -> int:
        """
        Returns how many of item_name the player owns (in bag, character storage, and account storage).
        For equipment (which get_items omits), checks active quest objective count if available,
        or falls back to persistent registered target count if present.
        """
        total_count = self.cli.count_item(item_name, include_storage=include_storage)
        if total_count > 0:
            return total_count

        for task in self.detect_delivery_quests():
            if task.item_name == item_name:
                return task.current

        # Fallback to persistent registered target count if recorded
        if item_name in delivery_target_store.targets:
            return int(delivery_target_store.targets[item_name].get("current", 0))

        return 0

    def get_registered_deliveries_with_status(self) -> List[Dict[str, Any]]:
        """
        Returns all registered weekly delivery targets, with real-time current counts
        (including inventory, character storage, and account storage) queried from the game client.
        Preserves existing counts for equipment items when quest is unpinned in-game.
        """
        targets = delivery_target_store.get_all()
        updated_list = []
        active_delivery_tasks = {t.item_name: t for t in self.detect_delivery_quests()}

        for t in targets:
            item_name = t["item_name"]
            goal = int(t.get("goal", 1))
            prev_current = int(t.get("current", 0))

            breakdown = self.cli.get_item_location_breakdown(item_name)
            detected_current = breakdown["total"]

            # If not detected by get_items (equipment items), check active quest tracker
            if detected_current == 0 and item_name in active_delivery_tasks:
                detected_current = active_delivery_tasks[item_name].current

            # For equipment items (get_items cannot query equipment):
            # If quest is unpinned (detected_current == 0), DO NOT overwrite a known positive count with 0!
            if is_equipment_item(item_name):
                if detected_current > 0:
                    current = detected_current
                else:
                    current = prev_current
            else:
                current = detected_current

            needed = max(0, goal - current)
            is_completed = (current >= goal)

            t["current"] = current
            t["needed"] = needed
            t["is_completed"] = is_completed
            t["inventory_count"] = max(breakdown["inventory"], current if is_equipment_item(item_name) else 0)
            t["character_storage_count"] = breakdown["character_storage"]
            t["account_storage_count"] = breakdown["account_storage"]
            t["storage_count"] = breakdown["storage_total"]
            delivery_target_store.targets[item_name] = t
            updated_list.append(t)
        delivery_target_store.save()
        return updated_list

    def add_detected_quests_to_targets(self) -> List[Dict[str, Any]]:
        """
        Scans currently active quests via detect_delivery_quests() and adds or updates
        them in the persistent registered delivery targets store.
        """
        detected = self.detect_delivery_quests()
        added = []
        for q in detected:
            cur = self.get_effective_owned(q.item_name)
            entry = delivery_target_store.add_or_update(
                item_name=q.item_name,
                goal=q.goal,
                current=cur,
                quest_title=q.quest_title
            )
            added.append(entry)
        return added

    def find_craft_recipe(self, item_name: str) -> Optional[Dict[str, Any]]:
        """Finds craft recipe for the exact item_name."""
        res = self.cli.get_craftable_items(item_name)
        for item in res.get("items", []):
            if item.get("DisplayName") == item_name:
                # Dynamically update recipe cache if MissingIngredients present (merge, don't overwrite)
                missing = item.get("MissingIngredients", [])
                if missing:
                    existing = self.recipes.get(item_name, {})
                    updated = dict(existing)
                    for ing in missing:
                        updated[ing["DisplayName"]] = int(ing.get("Required", 1))
                    self.recipes[item_name] = updated
                    self.save_recipe_cache()
                return item
        return None

    def get_produced_per_craft(self, item_name: str) -> int:
        """Returns how many pieces 1 craft produces (e.g. 마법 유탄 부품 = 5)."""
        if item_name in PRODUCED_PER_CRAFT:
            return PRODUCED_PER_CRAFT[item_name]
        return 1

    def get_recipe_ingredients(self, item_name: str) -> Dict[str, int]:
        """Returns ingredient dict {ing_name: qty_per_craft} for item_name."""
        if item_name in self.recipes:
            return self.recipes[item_name]

        # Try querying CLI
        recipe_data = self.find_craft_recipe(item_name)
        if recipe_data and recipe_data.get("MissingIngredients"):
            rec_dict = {ing["DisplayName"]: int(ing.get("Required", 1)) for ing in recipe_data["MissingIngredients"]}
            existing = self.recipes.get(item_name, {})
            updated = dict(existing)
            updated.update(rec_dict)
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
        try:
            res = self.cli.get_gatherable_items(item_name)
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
        2. Starts execute_gathering in a worker thread.
        3. Polls inventory count periodically (every ~1.0-1.2s).
        4. When target_count is reached (or abort requested), stops the worker immediately.
        5. If execute_gathering finishes before target_count is reached (e.g. 100-item cap per call
           or ore/tree node depleted), automatically re-launches execute_gathering in a loop
           until the full required_count is collected!
        """
        if self.abort_requested:
            self.log("🛑 [사용자 중지] 채집 시작 전 중지 요청이 확인되었습니다.", "warn", callback)
            return {"status": "aborted", "item": item_name, "gathered": 0}

        if required_count <= 0:
            self.log(f"🌿 '{item_name}'은(는) 이미 충분하여 채집을 건너뜁니다.", "info", callback)
            return {"status": "satisfied", "item": item_name, "gathered": 0}

        is_gath, tool_ok = self.is_gatherable(item_name)
        if not is_gath:
            raise MabinogiCLIError(f"'{item_name}'은(는) 필드 채집 목록에 없습니다.")
        if not tool_ok:
            raise MabinogiCLIError(f"'{item_name}' 채집 도구가 없거나 내구도가 0입니다.")

        initial_count = self.cli.count_item(item_name, include_storage=False)
        target_count = initial_count + required_count

        self.log(
            f"🌿 [부족 수량 맞춤 채집 시작] '{item_name}' 목표: {required_count}개 추가 채집 "
            f"(현재 가방: {initial_count}개 ➔ 목표: {target_count}개)", 
            "action", 
            callback
        )

        stopped_by_target = False
        poll_interval = 1.0
        last_logged_gained = -1
        consecutive_zero_gains = 0

        while not self.abort_requested:
            curr_count = self.cli.count_item(item_name, include_storage=False)
            gained = max(0, curr_count - initial_count)
            if curr_count >= target_count or gained >= required_count:
                stopped_by_target = True
                break

            # Check tool status before launching or re-launching
            is_gath, tool_ok = self.is_gatherable(item_name)
            if not tool_ok:
                self.log(f"⚠️ '{item_name}' 채집 도구가 소모되었거나 내구도가 0입니다. 채집을 중단합니다.", "warn", callback)
                break

            worker_error = []
            worker_result = []

            def gather_worker():
                try:
                    res = self.cli.execute_gathering(item_name)
                    worker_result.append(res)
                except Exception as e:
                    worker_error.append(e)

            worker_thread = threading.Thread(target=gather_worker, daemon=True)
            worker_thread.start()

            round_initial_count = curr_count
            idle_counter = 0

            # Monitoring loop for current execute_gathering call
            while True:
                time.sleep(poll_interval)

                # Check abort request
                if self.abort_requested:
                    self.log(f"🛑 [사용자 중지 요청] '{item_name}' 채집을 즉시 중단합니다.", "warn", callback)
                    self._force_stop_worker(worker_thread)
                    return {
                        "status": "aborted",
                        "item": item_name,
                        "required": required_count,
                        "gained": max(0, self.cli.count_item(item_name, include_storage=False) - initial_count)
                    }

                # 1. Safety check
                activity = {}
                try:
                    activity = self.cli.get_activity()
                    if activity.get("IsDead") or activity.get("IsReviving"):
                        self.log(f"⚠️ 캐릭터가 행동 불능 상태입니다. 채집을 중단합니다.", "warn", callback)
                        self._force_stop_worker(worker_thread)
                        return {
                            "status": "dead",
                            "item": item_name,
                            "required": required_count,
                            "gained": max(0, self.cli.count_item(item_name, include_storage=False) - initial_count)
                        }
                except Exception:
                    pass

                # 1-1. Weight check to prevent overweight (95% limit)
                try:
                    inv_info = self.cli.get_inventory()
                    cur_w = float(inv_info.get("CurrentInventoryWeight", 0))
                    max_w = float(inv_info.get("MaxInventoryWeight", 1))
                    if max_w > 0 and (cur_w / max_w) >= 0.95:
                        pct = (cur_w / max_w) * 100
                        self.log(
                            f"⚠️ [가방 무게 경고] 가방 무게가 {pct:.1f}%({cur_w:.1f}/{max_w:.1f})에 도달하여 과적 방지를 위해 채집을 즉시 안전 중단합니다.",
                            "warn",
                            callback
                        )
                        self._force_stop_worker(worker_thread)
                        break
                except Exception:
                    pass

                # 2. Check current count in inventory
                try:
                    curr_count = self.cli.count_item(item_name, include_storage=False)
                except Exception:
                    continue
                gained = max(0, curr_count - initial_count)

                if gained != last_logged_gained and gained > 0:
                    last_logged_gained = gained
                    self.log(f"🌾 '{item_name}' 채집 진행 중... ({gained}/{required_count}개 획득, 현재 가방: {curr_count}개)", "info", callback)

                # 3. Check if target count is reached
                if curr_count >= target_count or gained >= required_count:
                    self.log(
                        f"🎯 [목표 달성] '{item_name}' 부족 수량 {required_count}개 채집 완료! "
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
                            self.log(f"ℹ️ 필드에 더 이상 상호작용 가능한 '{item_name}' 노드가 없어 채집 동작을 종료합니다.", "info", callback)
                            self._force_stop_worker(worker_thread)
                            break
                    else:
                        idle_counter = 0
                except Exception:
                    pass

                # 5. Check if worker thread finished
                if not worker_thread.is_alive():
                    # Auto-fishing returns result: started immediately while character is still fishing
                    if worker_result and isinstance(worker_result[0], dict) and worker_result[0].get("result") == "started":
                        continue
                    else:
                        break

            # Ensure current worker is stopped
            self._force_stop_worker(worker_thread, max_attempts=4)

            if stopped_by_target or self.abort_requested:
                break

            # Check if an error occurred during gathering
            if worker_error:
                err = worker_error[0]
                err_msg = str(err)
                self.log(f"⚠️ '{item_name}' 채집 중 오류 발생: {err_msg}", "warn", callback)
                if any(k in err_msg for k in ["tool", "overweight", "blocked", "not_found", "not_in_field"]):
                    raise err
                break

            # Check if this round gained any items
            curr_count = self.cli.count_item(item_name, include_storage=False)
            round_gained = curr_count - round_initial_count
            if round_gained <= 0:
                consecutive_zero_gains += 1
                if consecutive_zero_gains >= 3:
                    self.log(f"⚠️ '{item_name}' 채집 시도에서 더 이상 아이템을 획득하지 못하여 채집을 종료합니다.", "warn", callback)
                    break
            else:
                consecutive_zero_gains = 0

            # If still deficient, notify and continue next round (e.g. 100-cap or node depleted)
            remaining_deficit = target_count - curr_count
            if remaining_deficit > 0:
                self.log(
                    f"🔄 '{item_name}' 1회 채집 구간 종료 (현재 가방: {curr_count}개, 총 획득: {curr_count - initial_count}/{required_count}개). "
                    f"남은 부족분 {remaining_deficit}개를 마저 채집하기 위해 연속 채집을 계속 진행합니다 (100개 제한/노드 재탐색)...", 
                    "action", 
                    callback
                )
                time.sleep(1.0)

        # Final recount
        final_count = self.cli.count_item(item_name, include_storage=False)
        total_gained = max(0, final_count - initial_count)

        if total_gained >= required_count or final_count >= target_count:
            self.log(
                f"✅ [맞춤 채집 완료] '{item_name}' 총 {total_gained}개 획득 (가방: {final_count}개 / 필요: {required_count}개)", 
                "success", 
                callback
            )
        else:
            self.log(
                f"⚠️ [채집 조기 종료] '{item_name}' {total_gained}/{required_count}개 획득 (가방: {final_count}개 / 목표: {target_count}개)", 
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
            t["needed"] = max(0, goal - new_cur)
            t["is_completed"] = (new_cur >= goal)
            t["updated_at"] = int(time.time() * 1000)
            delivery_target_store.targets[item_name] = t
            delivery_target_store.save()
            self.log(f"📊 [납품 목표 갱신] '{item_name}' 채집 완료({total_gained}개 획득) 반영 ➔ 현재 {new_cur}/{goal}개", "info", callback)

        return {
            "status": "completed" if (total_gained >= required_count or final_count >= target_count) else "partial",
            "item": item_name,
            "required": required_count,
            "gained": total_gained,
            "current_owned": final_count,
            "stopped_by_target": stopped_by_target
        }


    def collect_completed_altering_works(self, preferred_facility: Optional[str] = None, callback: Optional[LogCallback] = None) -> List[str]:
        """
        Checks current altering queue across all facilities. If any completed altering works exist,
        collects them facility by facility so slots are freed and materials are gathered
        into inventory BEFORE new altering works are submitted.
        If preferred_facility is provided, processes that facility first.
        """
        try:
            works_info = self.cli.get_altering_works()
        except Exception as e:
            self.log(f"⚠️ 가공 현황 조회 중 오류 (수령 건너뜀): {e}", "warn", callback)
            return []

        works = works_info.get("works", [])
        completed_works = [
            w for w in works 
            if w.get("IsCompleted") or (w.get("RemainingSeconds", 1) <= 0 and w.get("State") == "Completed")
        ]
        if not completed_works:
            return []

        # Group completed works by facility
        facility_to_items: Dict[str, List[str]] = defaultdict(list)
        for w in completed_works:
            fac = w.get("FacilityName") or get_facility_for_material(w.get("DisplayName", ""))
            facility_to_items[fac].append(w.get("DisplayName"))

        collected_facilities = []
        facilities = list(facility_to_items.keys())
        if preferred_facility and preferred_facility in facilities:
            facilities.remove(preferred_facility)
            facilities.insert(0, preferred_facility)

        self.log(f"🎁 [완료 가공품 감지] 총 {len(completed_works)}건의 완료된 가공 슬롯이 확인되었습니다. 새 가공 전 우선 수령합니다.", "action", callback)

        for fac in facilities:
            if self.abort_requested:
                break
            items = facility_to_items[fac]
            rep_item = items[0]
            self.log(f"   ▶ '{fac}' 완료 가공품 {len(items)}건 수령 진행 ('{rep_item}' 등)...", "action", callback)
            try:
                res = self.cli.complete_altering_work(rep_item)
                collected_facilities.append(fac)
                self.log(f"     ✅ '{fac}' 완료 가공품 수령 완료! (슬롯 확보 완료)", "success", callback)
                # Clean up alarms for items at this facility
                for it in items:
                    key = f"{fac}_{it}"
                    alarm_store.delete_alarm(key)
                time.sleep(1)
            except Exception as ex:
                self.log(f"     ⚠️ '{fac}' 수령 중 알림/오류: {ex}", "warn", callback)

        return collected_facilities

    def auto_alter(self, item_name: str, works_needed: int, callback: Optional[LogCallback] = None, is_delivery: bool = True):
        """Queues works_needed altering tasks, sets delivery alarm, and manages waiting."""
        recipe = self.find_alter_recipe(item_name)
        if not recipe:
            raise MabinogiCLIError(f"'{item_name}' 가공 레시피를 찾을 수 없습니다.")

        # Use the recipe's actual DisplayName for execute_altering
        # e.g. "철괴" -> "철괴(철 광석)" to ensure the correct recipe variant is used
        alter_display_name = recipe.get("DisplayName", item_name)

        target_facility = get_facility_for_material(item_name)

        # 1. Check and collect any completed altering slots across facilities before submitting new work!
        self.collect_completed_altering_works(preferred_facility=target_facility, callback=callback)

        successful_works = 0
        for i in range(works_needed):
            if self.abort_requested:
                self.log("🛑 [사용자 중지] 가공 등록이 중단되었습니다.", "warn", callback)
                break
            self.log(f"⚙️ [가공 등록 {i+1}/{works_needed}] '{alter_display_name}' 가공 대기열에 등록합니다.", "action", callback)
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
                                        f"💡 [전송기 보충 채집] 가방에 '{ing_name}'이(가) {inv_cnt}개 남아 1회분({ing_req}개) 합산 결제가 지연되었습니다. "
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
                            self.log(f"   ✅ [전송기 보충 후 재시도 성공] {res}", "success", callback)
                            successful_works += 1
                            time.sleep(1)
                            continue
                        except MabinogiCLIError as retry_err:
                            self.log(f"   ⚠️ 보충 후 가공 재시도 실패 ({retry_err}). 등록을 보류하고 다음 작업으로 진행합니다.", "warn", callback)
                            break
                    else:
                        self.log(f"   ⚠️ '{alter_display_name}' 가공 재료 부족 ({err}). 남은 {works_needed - successful_works}회 등록을 보류하고 다음 작업으로 진행합니다.", "warn", callback)
                        break

                self.log(f"   ⚠️ 가공 등록 중 오류: {err}. 완료 가공 슬롯 재확인 후 1회 재시도합니다.", "warn", callback)
                self.collect_completed_altering_works(preferred_facility=target_facility, callback=callback)
                try:
                    res = self.cli.execute_altering(alter_display_name)
                    self.log(f"   재시도 등록 결과: {res}", "success", callback)
                    successful_works += 1
                except MabinogiCLIError as retry_err:
                    self.log(f"   ⚠️ '{alter_display_name}' 가공 슬롯 부족/오류 ({retry_err}). 남은 {works_needed - successful_works}회 등록을 보류하고 다음 작업으로 진행합니다.", "warn", callback)
                    break
            time.sleep(1)

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
            self.log(f"🔔 [가공시설 알람 등록] '{facility_name}'의 마지막 가공 완료 시각(약 {rem_m}분 뒤)에 맞춰 알람이 등록되었습니다.", "info", callback)

        # Non-blocking transition if remaining time > 45s
        if total_facility_remaining > 45:
            remaining_min = math.ceil(total_facility_remaining / 60)
            self.log(f"☕ [비동기 백그라운드 전환] '{facility_name}' 가공 완료까지 약 {remaining_min}분({total_facility_remaining}초) 소요됩니다.", "info", callback)
            self.log(f"🎮 캐릭터 조작이 즉시 자유화되었습니다! 편하게 사냥, 던전 등 다른 콘텐츠를 플레이하세요.", "success", callback)
            self.log(f"🔔 마지막 가공이 끝나면 알람이 울리며 웹 대시보드에서 즉시 수령 후 제작을 진행할 수 있습니다.", "info", callback)
            return {"status": "waiting_in_background", "item": item_name, "remaining_seconds": total_facility_remaining, "registered": successful_works, "facility": facility_name}

        # Short wait
        self.log(f"⏳ [가공 대기] '{item_name}' 가공 완료를 잠시 기다립니다 (약 {max_remaining}초)...", "info", callback)
        while True:
            works_info = self.cli.get_altering_works()
            pending_works = [w for w in works_info.get("works", []) if w.get("DisplayName") == item_name and not w.get("IsCompleted")]

            if not pending_works:
                self.log(f"✨ [가공 완료 확인] 모든 '{item_name}' 작업이 완료되었습니다.", "success", callback)
                break

            remaining_sec = max([w.get("RemainingSeconds", 0) for w in pending_works], default=5)
            time.sleep(min(5, max(1, remaining_sec)))

        # Collect
        self.log(f"📦 [가공품 수령] '{item_name}' 가공 결과물을 수령합니다.", "action", callback)
        coll_res = self.cli.complete_altering_work(item_name)
        self.log(f"   수령 결과: {coll_res}", "success", callback)
        return coll_res

    def auto_craft(self, item_name: str, craft_count: int, callback: Optional[LogCallback] = None) -> int:
        """Crafts item_name with batching support and fallback."""
        remaining = craft_count
        batch = remaining
        total_produced = 0

        while remaining > 0:
            self.log(f"🔨 [제작 시작] '{item_name}' {batch}회 제작 시도...", "action", callback)
            try:
                res = self.cli.execute_crafting(item_name, craft_count=batch)
                produced_now = 0
                for r in res.get("rewards", []):
                    if r.get("Name") == item_name:
                        produced_now += r.get("Amount", 0)
                for r in res.get("criticalRewards", []):
                    if r.get("Name") == item_name:
                        produced_now += r.get("Amount", 0)

                items_made = max(produced_now, batch)
                total_produced += items_made
                self.log(f"🔨 [제작 성공] {batch}회 제작 완료 (산출: {items_made}개)", "success", callback)

                remaining -= batch
                batch = remaining
            except MabinogiCLIError as err:
                data = err.response_data or {}
                err_code = data.get("error") if isinstance(data, dict) else ""

                if err_code == "invalid_count":
                    max_count = data.get("maxCount", 1)
                    batch = min(remaining, max_count)
                    self.log(f"⚠️ 1회 최대 제작량 제한({max_count}개)으로 분할 제작합니다.", "warn", callback)
                    continue
                elif err_code == "not_enough_ingredient":
                    if batch > 1:
                        batch = max(1, batch // 2)
                        self.log(f"⚠️ 재료 부족으로 제작 수량을 {batch}개로 낮추어 재시도합니다.", "warn", callback)
                        continue
                    else:
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
                                            f"💡 [전송기 보충 채집] 가방에 '{ing_name}'이(가) {inv_cnt}개 남아 1회분({per_craft}개) 합산 결제가 지연되었습니다. "
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
                                items_made = max(produced_now, 1)
                                total_produced += items_made
                                self.log(f"🔨 [전송기 보충 후 제작 성공] 1회 제작 완료 (산출: {items_made}개)", "success", callback)
                                remaining -= 1
                                batch = remaining
                                continue
                            except MabinogiCLIError as retry_err:
                                self.log(f"⚠️ 보충 후 제작 재시도 실패 ({retry_err})", "warn", callback)
                                raise retry_err
                        else:
                            raise err
                else:
                    raise err

        # Update delivery_target_store if this item is in registered targets
        if item_name in delivery_target_store.targets and total_produced > 0:
            t = delivery_target_store.targets[item_name]
            prev = int(t.get("current", 0))
            new_cur = prev + total_produced
            goal = int(t.get("goal", 1))
            t["current"] = new_cur
            t["needed"] = max(0, goal - new_cur)
            t["is_completed"] = (new_cur >= goal)
            t["updated_at"] = int(time.time() * 1000)
            delivery_target_store.targets[item_name] = t
            delivery_target_store.save()
            self.log(f"📊 [납품 목표 갱신] '{item_name}' 제작 완료({total_produced}개 산출) 반영 ➔ 현재 {new_cur}/{goal}개", "info", callback)

        return total_produced

    # =========================================================================
    # UNIFIED BATCH PRODUCTION PLANNER (통합 일괄 납품 플래너)
    # =========================================================================

    def analyze_batch_plan(self, custom_tasks: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """
        Full Recursive Bill-of-Materials (BOM) Batch Planner:
        1. Determines active/registered tasks to produce.
        2. Queries inventory, character storage, account storage, and altering works once.
        3. Recursively expands intermediate materials down to field gatherables:
           - Correctly traverses multi-tier dependencies (e.g. 합금강괴 -> 강철괴 -> 철괴 -> 철 광석).
           - Accounts for items already owned or queued in altering.
           - Calculates exact deficit for each field gatherable item.
        4. Sorts altering works in strict dependency/tier order (Tier 1 -> Tier 2 -> Tier 3).
        5. Computes per-facility slot usage (out of 7) and slot warnings.
        """
        tasks_to_plan: List[Dict[str, Any]] = []

        if custom_tasks is not None:
            tasks_to_plan = custom_tasks
        else:
            # 1. First check registered targets
            registered = self.get_registered_deliveries_with_status()
            if registered:
                for t in registered:
                    tasks_to_plan.append({
                        "quest_title": t.get("quest_title", "주간 납품"),
                        "item_name": t["item_name"],
                        "current": t["current"],
                        "goal": t["goal"],
                        "needed": t["needed"],
                        "is_completed": t.get("is_completed", False)
                    })
            else:
                # 2. Fallback to active quests in Quest Tracker
                active_quests = self.detect_delivery_quests()
                for q in active_quests:
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
                "tasks": [],
                "intermediate_requirements": [],
                "raw_materials": [],
                "can_craft_immediately": False,
                "all_completed": False,
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
                completed_yield_by_item[clean_w] += yield_pw
            else:
                facility_slot_usage[fac]["in_progress"] += 1
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

        # 3. Resolve Finished Goods & Direct Gatherable Delivery Items
        recipe_details: Dict[str, Dict[str, int]] = {}
        pending_demands: Dict[str, int] = defaultdict(int)
        raw_gather_demands: Dict[str, int] = defaultdict(int)
        raw_total_demands: Dict[str, int] = defaultdict(int)
        craftable_tasks_count = 0

        for t in tasks_to_plan:
            it_name = t["item_name"]
            needed = t.get("needed", 0)
            clean_it = it_name.split("(")[0].strip()
            is_gath, _ = self.is_gatherable(it_name)

            if needed <= 0:
                # If this completed task is a raw gatherable item (e.g. 달걀, 우유),
                # reserve its required stock so other recipes won't consume it:
                if is_gath or clean_it in KNOWN_GATHERABLE_ITEMS:
                    goal_qty = t.get("goal") or t.get("current", 0)
                    if goal_qty > 0:
                        consume_v_stock(clean_it, goal_qty)
                continue

            # Check if this delivery item is a raw gatherable item (e.g. 우유, 달걀, 사과, 감자 등)
            if is_gath or clean_it in KNOWN_GATHERABLE_ITEMS:
                t["is_gather_item"] = True
                recipe_details[it_name] = {}
                target_qty = t.get("goal") or (t.get("current", 0) + needed)
                raw_total_demands[clean_it] += target_qty
                used = consume_v_stock(clean_it, target_qty)
                net_def = max(0, target_qty - used)
                if net_def > 0:
                    raw_gather_demands[clean_it] += net_def
                else:
                    craftable_tasks_count += 1
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
                prod_pc = self.get_produced_per_craft(it_name)
                crafts_needed = math.ceil(net_needed / prod_pc)
                for ing_name, per_craft in rec.items():
                    pending_demands[ing_name] += per_craft * crafts_needed
            else:
                craftable_tasks_count += 1

        # 4. Multi-tier Recursive BOM Resolution
        alter_plan: Dict[str, Dict[str, Any]] = {}
        intermediate_total_demands: Dict[str, int] = defaultdict(int)

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
                    if net_def > 0:
                        yield_pw = alt_info.get("produced", 3)
                        works = math.ceil(net_def / yield_pw)
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
                        alter_plan[clean_mat]["works_needed"] += works
                        alter_plan[clean_mat]["total_needed"] += demand_qty
                        alter_plan[clean_mat]["net_needed"] += net_def

                        for sub_name, sub_req in alt_info.get("ingredients", {}).items():
                            pending_demands[sub_name] += sub_req * works
                else:
                    raw_total_demands[clean_mat] += demand_qty
                    if net_def > 0:
                        raw_gather_demands[clean_mat] += net_def

        # 5. Build Intermediate Requirements Analysis
        sorted_alters = sorted(alter_plan.values(), key=lambda x: (x["tier"], x["item_name"]))
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
                "works_needed": w_needed,
                "status": status
            })

        # 6. Build Raw Materials Analysis
        raw_materials_analysis: List[Dict[str, Any]] = []
        for raw_name in sorted(raw_total_demands.keys()):
            tot_needed = raw_total_demands[raw_name]
            actual_def = raw_gather_demands.get(raw_name, 0)
            bd = get_stock(raw_name)
            is_gath, tool_ok = self.is_gatherable(raw_name)

            raw_materials_analysis.append({
                "item_name": raw_name,
                "total_needed": tot_needed,
                "inventory_count": bd["inventory"],
                "character_storage_count": bd["character_storage"],
                "account_storage_count": bd["account_storage"],
                "storage_count": bd["storage_total"],
                "total_owned": bd["total"],
                "deficit": actual_def,
                "is_gatherable": is_gath,
                "tool_ok": tool_ok,
                "status": "satisfied" if actual_def == 0 else ("ready_to_gather" if tool_ok else "tool_missing")
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

            fac_info = {
                "facility": fac,
                "max_slots": MAX_SLOTS,
                "in_progress": usage["in_progress"],
                "completed": usage["completed"],
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
                    f"⚠️ '{fac}': 가공 {new_demand}회 필요하지만 "
                    f"수령 후에도 가용 슬롯이 {available_after_collect}개뿐입니다 "
                    f"({overflow}회분 초과). 진행 중 {usage['in_progress']}건 완료 후 추가 등록이 필요합니다."
                )
            elif new_demand > 0 and usage["completed"] > 0 and new_demand > available:
                slot_warnings.append(
                    f"ℹ️ '{fac}': 완료된 가공 {usage['completed']}건을 먼저 수령하면 "
                    f"슬롯 {available_after_collect}개 확보 가능 (필요: {new_demand}회)"
                )

        can_craft_all_immediately = (
            total_works_to_queue == 0 and sum(raw_gather_demands.values()) == 0
        )
        all_completed = len(tasks_to_plan) > 0 and all(t.get("needed", 0) <= 0 for t in tasks_to_plan)

        try:
            wings_count = self.cli.get_wings_count()
        except Exception:
            wings_count = 0

        return {
            "tasks": tasks_to_plan,
            "recipes": recipe_details,
            "intermediate_requirements": intermediate_analysis,
            "raw_materials": raw_materials_analysis,
            "can_craft_immediately": can_craft_all_immediately and not all_completed,
            "craftable_tasks_count": craftable_tasks_count,
            "all_completed": all_completed,
            "total_works_to_queue": total_works_to_queue,
            "facility_slots": facility_analysis,
            "slot_warnings": slot_warnings,
            "has_slot_issue": has_slot_issue,
            "wings_count": wings_count
        }

    def execute_batch_deliveries(self, plan: Dict[str, Any], callback: Optional[LogCallback] = None) -> Dict[str, Any]:
        """
        Executes unified batch delivery pipeline with slot-aware, resilient scheduling:
        1. Checks character status and handles user abort.
        2. Collects completed altering works across all facilities first (frees slots & gains materials).
        3. Gathers any missing raw materials (both direct craft ingredients and altering materials)
           that are gatherable in the field.
        4. Registers altering works up to available slots per facility.
           - If a facility is full (or becomes full during registration), gracefully skips to the
             next material / next facility without crashing or halting the entire pipeline!
           - Synchronizes alarms for queued background altering.
        5. Performs immediate crafting for any tasks whose materials are already 100% prepared!
           - Does NOT halt even if altering was queued; immediately crafts everything possible.
        6. Summarizes completed crafts, running alters, and deferred tasks.
        """
        self.log("🚀 [통합 일괄 납품 파이프라인 가동] 전체 주간 납품 목표 최적화 제작을 시작합니다.", "action", callback)

        start_time = time.time()
        initial_wings = self._safe_get_wings()
        collected_facilities_all: List[str] = []
        gathered_list: List[Dict[str, Any]] = []
        altered_list: List[Dict[str, Any]] = []
        craft_results: List[Dict[str, Any]] = []
        deferred_works: List[Dict[str, Any]] = []

        if self.abort_requested:
            self.log("🛑 [사용자 중지] 시작 전 중지 요청이 확인되었습니다.", "warn", callback)
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

        # 0. Check activity
        activity = self.cli.get_activity()
        if activity.get("IsDead") or activity.get("IsReviving"):
            raise MabinogiCLIError("캐릭터가 행동 불능 상태입니다. 인게임에서 부활을 먼저 완료해주세요.")

        intermediate_reqs = plan.get("intermediate_requirements", [])
        tasks = plan.get("tasks", [])
        raw_mats = plan.get("raw_materials", [])

        # Stage 1: Collect any completed altering works across all facilities first!
        self.log("📦 [사전 가공품 수령] 모든 시설의 완료된 가공품을 수령하여 슬롯을 비우고 재료를 확보합니다.", "action", callback)
        collected_facilities = self.collect_completed_altering_works(callback=callback)
        if collected_facilities:
            collected_facilities_all.extend(collected_facilities)
            self.log("🔄 [BOM 재계산] 완료 가공품 수령으로 재고가 변동되어 최적 필요 수량을 재계산합니다.", "info", callback)
            plan = self.analyze_batch_plan(custom_tasks=tasks)
            intermediate_reqs = plan.get("intermediate_requirements", [])
            tasks = plan.get("tasks", [])
            raw_mats = plan.get("raw_materials", [])

        if self.abort_requested:
            self.log("🛑 [사용자 중지] 작업이 중단되었습니다.", "warn", callback)
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

        # Stage 2: Gather missing raw materials (부족한 원자재 사전 자동 채집)
        if raw_mats:
            needed_gather = [r for r in raw_mats if r.get("deficit", 0) > 0 and r.get("is_gatherable")]
            if needed_gather:
                self.log(f"🌿 [부족 원자재 일괄 맞춤 채집] 주간 납품/가공에 부족한 원자재 총 {len(needed_gather)}종을 필드에서 1회 맞춤 채집합니다 (정령의 날개 낭비 방지).", "action", callback)
                for rm in needed_gather:
                    if self.abort_requested:
                        self.log("🛑 [사용자 중지] 채집 대기열이 중단되었습니다.", "warn", callback)
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
                    g_name = rm["item_name"]
                    g_deficit = rm["deficit"]
                    is_gath, tool_ok = self.is_gatherable(g_name)
                    if not is_gath:
                        self.log(f"⚠️ '{g_name}'은(는) 필드 채집이 불가능하여 건너뜁니다.", "warn", callback)
                        continue
                    if not tool_ok:
                        self.log(f"⚠️ '{g_name}' 채집 도구가 없거나 내구도가 0입니다! 채집을 건너뜁니다.", "warn", callback)
                        continue

                    stor_cnt = rm.get("storage_count", 0)
                    stor_msg = f" | 창고 {stor_cnt}개 보관 중 (인게임 가공 소모를 위해 가방 기준 채집)" if stor_cnt > 0 else ""
                    self.log(f"🌿 [맞춤 채집 시작] '{g_name}' (가방 부족 수량: {g_deficit}개{stor_msg}) 채집을 진행합니다.", "action", callback)
                    try:
                        g_res = self.auto_gather(g_name, g_deficit, callback=callback)
                        gained = g_res.get("gained", 0) if isinstance(g_res, dict) else 0
                        gathered_list.append({
                            "item": g_name,
                            "gained": gained,
                            "required": g_deficit,
                            "status": g_res.get("status", "completed") if isinstance(g_res, dict) else "completed"
                        })
                        if isinstance(g_res, dict) and g_res.get("status") == "aborted":
                            self.log("🛑 [사용자 중지] 파이프라인이 중단되었습니다.", "warn", callback)
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
                        self.log(f"⚠️ '{g_name}' 채집 중 오류: {g_err}. 다음 작업으로 계속 진행합니다.", "warn", callback)

        if self.abort_requested:
            self.log("🛑 [사용자 중지] 가공 등록 전 작업이 중단되었습니다.", "warn", callback)
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
                self.log("🛑 [사용자 중지] 가공 대기열 등록이 중단되었습니다.", "warn", callback)
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
                    f"⚠️ [{facility}] 이미 슬롯이 가득 찼으므로 '{mat_name}' 가공({works_needed}회)을 보류하고 다른 품목/시설로 스킵합니다.",
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
                    f"⏳ [{facility}] '{mat_name}' 하위 재료({', '.join(missing_sub)}) 가공 진행 대기 중. 현재 가공 완료 후 다음 차례에 등록됩니다.",
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
                    f"⚠️ [{facility}] 슬롯 한도 도달(사용 중: {used}/{MAX_SLOTS}). '{mat_name}' 가공({works_needed}회)을 보류하고 다른 가공으로 스킵합니다.",
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

            works_to_register = min(works_needed, available)
            deferred_count = works_needed - works_to_register

            if deferred_count > 0:
                self.log(
                    f"⚠️ [{facility}] '{mat_name}' 가공 {works_needed}회 필요하지만 가용 슬롯이 {available}개뿐입니다. "
                    f"{works_to_register}회만 등록하고 {deferred_count}회는 보류합니다.",
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
                    f"⚙️ [{facility}] '{recipe_name}' {works_to_register}회 가공 등록 시도 "
                    f"(필요 총량: {req['total_needed']}개, 시설 가용 슬롯: {available}/{MAX_SLOTS})",
                    "action", callback
                )
                try:
                    alter_res = self.auto_alter(recipe_name, works_to_register, callback=callback, is_delivery=True)
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
                        # Queue filled up earlier than expected
                        full_facilities.add(facility)
                        facility_used_slots[facility] = MAX_SLOTS
                        failed_count = works_to_register - reg_count
                        deferred_works.append({
                            "item_name": mat_name,
                            "facility": facility,
                            "needed": works_needed,
                            "registered": reg_count,
                            "deferred": failed_count,
                        })

                    if isinstance(alter_res, dict) and alter_res.get("status") in ("waiting_in_background", "slot_full_skipped"):
                        queued_any_long = True
                except Exception as ex:
                    self.log(f"⚠️ [{facility}] '{recipe_name}' 가공 등록 실패 ({ex}). 다음 항목으로 스킵합니다.", "warn", callback)
                    full_facilities.add(facility)
                    facility_used_slots[facility] = MAX_SLOTS
                    deferred_works.append({
                        "item_name": mat_name,
                        "facility": facility,
                        "needed": works_needed,
                        "registered": 0,
                        "deferred": works_needed,
                    })

        if deferred_works:
            self.log(
                f"📋 [가공 슬롯/의존성 보류 요약] 총 {len(deferred_works)}건의 가공이 슬롯 또는 하위 재료 대기로 보류되었습니다. "
                f"현재 진행 중인 가공이 완료된 후 다시 '일괄 제작'을 실행하면 보류된 가공이 자동으로 등록됩니다.",
                "warn", callback
            )

        if queued_any_long:
            alarm_store.sync_facility_alarms(self.cli, default_source="주간 납품")
            self.log("🔔 [가공 알람 동기화] 등록된 가공 작업들의 완료 시각에 맞춰 알람이 동기화되었습니다.", "info", callback)

        # Stage 3.5: Collect any newly finished altering works before crafting
        new_collected = self.collect_completed_altering_works(callback=callback)
        if new_collected:
            collected_facilities_all.extend(new_collected)

        # Stage 4: Immediate Batch Crafting for any ready tasks!
        self.log("🔨 [납품 완제품 제작 단계] 현재 가방/창고 상태에서 즉시 제작 가능한 납품 아이템을 확인합니다.", "action", callback)
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

            if needed <= 0:
                continue

            recipe = self.get_recipe_ingredients(item_name)
            if not recipe:
                self.log(f"ℹ️ '{item_name}'은(는) 제작 레시피가 없거나 완제품 제작 대상이 아니므로 제작 단계에서 건너뜁니다.", "info", callback)
                continue

            can_craft_this = True
            missing_for_this = []

            for ing_name, per_craft in recipe.items():
                total_ing_needed = per_craft * needed
                owned_ing = self.get_effective_owned(ing_name, include_storage=True)
                if owned_ing < total_ing_needed:
                    can_craft_this = False
                    missing_for_this.append(f"{ing_name} (보유 {owned_ing}/{total_ing_needed}개)")

            if can_craft_this:
                craftable_tasks.append(t)
            else:
                waiting_tasks.append((t, missing_for_this))

        if gather_ready_tasks:
            g_names = [gt["item_name"] for gt in gather_ready_tasks]
            self.log(f"🌿 [채집 납품 품목 준비 완료] {len(gather_ready_tasks)}건의 채집 납품 품목({', '.join(g_names)})은 필드 채집으로 가방에 준비 완료되었습니다.", "success", callback)

        if craftable_tasks:
            self.log(f"🎯 [즉시 제작 가능 목표] 총 {len(craftable_tasks)}개 품목은 재료가 모두 준비되어 있어 즉시 제작을 진행합니다!", "action", callback)
            for t in craftable_tasks:
                if self.abort_requested:
                    self.log("🛑 [사용자 중지] 완제품 제작이 중단되었습니다.", "warn", callback)
                    break
                item_name = t["item_name"]
                needed = t["needed"]
                self.log(f"🔨 [완제품 제작 시작] '{item_name}' {needed}개 제작 진행...", "action", callback)
                try:
                    crafted_count = self.auto_craft(item_name, needed, callback=callback)
                    craft_results.append({"item_name": item_name, "crafted": crafted_count, "status": "success"})
                    self.log(f"✅ '{item_name}' {crafted_count}개 제작 완료!", "success", callback)
                except Exception as e:
                    self.log(f"❌ '{item_name}' 제작 중 오류: {str(e)}", "error", callback)
                    craft_results.append({"item_name": item_name, "error": str(e), "status": "failed", "crafted": 0})
        else:
            if waiting_tasks:
                self.log(f"ℹ️ [제작 대기] 아직 가공 완료 또는 재료 준비가 필요한 납품 품목 {len(waiting_tasks)}건은 가공 완료 후 제작됩니다.", "info", callback)
                for t, missing_list in waiting_tasks[:5]:
                    self.log(f"   • '{t['item_name']}': {', '.join(missing_list)} 대기 중", "info", callback)
                if len(waiting_tasks) > 5:
                    self.log(f"   • 외 {len(waiting_tasks) - 5}건 대기 중", "info", callback)

        duration = time.time() - start_time
        final_wings = self._safe_get_wings(initial_wings)
        status_str = "waiting_in_background" if queued_any_long else "completed"

        title_str = "주간 납품 작업 완료"

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
            callback=callback
        )

        if queued_any_long:
            self.log("☕ [가공 백그라운드 진행 안내] 대기열 가공이 진행 중입니다. 알람이 울리면 웹 대시보드에서 '일괄 제작'을 다시 실행하세요!", "success", callback)
            return {
                "status": "waiting_in_background",
                "message": "가공 대기열 등록 완료 및 즉시 제작 가능한 아이템 제작 완료",
                "results": craft_results,
                "deferred_works": deferred_works,
                "summary": summary
            }

        self.log("🎉 [통합 납품 파이프라인 처리 완료] 일괄 처리가 마무리되었습니다!", "success", callback)
        return {"status": "completed", "results": craft_results, "deferred_works": deferred_works, "summary": summary}

    def resolve_and_produce(self, item_name: str, target_count: int, callback: Optional[LogCallback] = None) -> Dict[str, Any]:
        """Single-item production pipeline with recursive BOM and upfront gathering support."""
        self.log(f"🚀 [개별 제작 파이프라인 시작] 목표: '{item_name}' {target_count}개", "info", callback)

        start_time = time.time()
        initial_wings = self._safe_get_wings()
        collected_facilities_all: List[str] = []
        gathered_list: List[Dict[str, Any]] = []
        altered_list: List[Dict[str, Any]] = []
        craft_results: List[Dict[str, Any]] = []

        activity = self.cli.get_activity()
        if activity.get("IsDead") or activity.get("IsReviving"):
            msg = "캐릭터가 행동 불능(사망 또는 부활 대기) 상태입니다. 게임 화면에서 부활을 먼저 완료해주세요."
            self.log(f"❌ [작업 차단] {msg}", "error", callback)
            raise MabinogiCLIError(msg, response_data={"error": "blocked", "kind": "dead"})

        owned = self.get_effective_owned(item_name)
        self.log(f"📊 현재 보유량: {owned}개 / 목표 수량: {target_count}개", "info", callback)

        if owned >= target_count:
            msg = f"'{item_name}'을(를) 이미 {owned}개 보유하고 있어 추가 제작이 필요 없습니다!"
            self.log(f"🎉 {msg}", "success", callback)
            final_wings = self._safe_get_wings(initial_wings)
            summary = self.format_and_log_summary(
                summary_title=f"'{item_name}' 제작 불필요 (이미 목표 수량 보유)",
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
            return {"status": "already_satisfied", "owned": owned, "target": target_count, "summary": summary}

        needed = target_count - owned
        self.log(f"🎯 추가 제작 필요 수량: {needed}개", "info", callback)

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
                self.log(f"🌿 [부족 원자재 맞춤 채집] '{item_name}' 제작에 필요한 원자재 {len(needed_gather)}종을 필드에서 1회 맞춤 채집합니다 (정령의 날개 낭비 방지).", "action", callback)
                for rm in needed_gather:
                    if self.abort_requested:
                        self.log("🛑 [사용자 중지] 채집 대기열이 중단되었습니다.", "warn", callback)
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
                        self.log(f"⚠️ '{g_name}'은(는) 필드 채집이 불가능하여 건너뜁니다.", "warn", callback)
                        continue
                    if not tool_ok:
                        self.log(f"⚠️ '{g_name}' 채집 도구가 없거나 내구도가 0입니다! 채집을 건너뜁니다.", "warn", callback)
                        continue
                    self.log(f"🌿 [맞춤 채집] '{g_name}' {g_deficit}개 채집 시작...", "action", callback)
                    try:
                        g_res = self.auto_gather(g_name, g_deficit, callback=callback)
                        gained = g_res.get("gained", 0) if isinstance(g_res, dict) else 0
                        gathered_list.append({
                            "item": g_name,
                            "gained": gained,
                            "required": g_deficit,
                            "status": g_res.get("status", "completed") if isinstance(g_res, dict) else "completed"
                        })
                    except Exception as g_err:
                        gathered_list.append({
                            "item": g_name,
                            "gained": 0,
                            "required": g_deficit,
                            "status": "failed",
                            "error": str(g_err)
                        })
                        self.log(f"⚠️ '{g_name}' 채집 중 오류: {g_err}. 계속 진행합니다.", "warn", callback)

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
                    self.log(f"⏳ '{mat_name}' 하위 재료('{s_name}') 가공 대기 중입니다.", "info", callback)
                    break
            if not can_start:
                continue

            self.log(f"⚙️ '{recipe_name}' {works_needed}회 가공 등록 진행...", "action", callback)
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
            self.log(f"☕ '{item_name}' 가공 대기열이 등록되어 백그라운드 진행 중입니다. 완료 알람이 울리면 다시 실행하세요.", "info", callback)
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
            self.log(f"🌿 [채집 완료] '{item_name}'은(는) 필드 채집 아이템이므로 채집 완료로 모든 준비가 끝났습니다!", "success", callback)
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

        craft_recipe = self.find_craft_recipe(item_name)
        if not craft_recipe:
            raise MabinogiCLIError(f"'{item_name}'에 대한 제작 레시피를 찾을 수 없습니다.")

        ingredients = self.get_recipe_ingredients(item_name)
        for ing_name, per_craft in ingredients.items():
            tot_needed_for_craft = per_craft * needed
            owned_ing = self.get_effective_owned(ing_name, include_storage=True)
            if owned_ing < tot_needed_for_craft:
                missing = tot_needed_for_craft - owned_ing
                self.log(f"⚠️ '{item_name}' 제작 재료 '{ing_name}' {missing}개 부족 (필요: {tot_needed_for_craft}개, 전체 보유: {owned_ing}개)", "warn", callback)
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
        self.log(f"🎉 '{item_name}' {crafted}개 제작 완료! (총 보유량: {new_count}/{target_count}개)", "success", callback)

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

    def analyze_quick_alter(self, category_key: Optional[str] = None) -> Dict[str, Any]:
        """
        Analyzes 7-slot alteration bench state for specified category (or all 4 categories):
        1. Checks completed works to collect.
        2. Checks in-progress works and calculates available slots (out of 7).
        3. Evaluates tiers from High Tier (Tier 7) down to Low Tier (Tier 1):
           - If non-gatherable intermediate goods are lacking -> Skips tier ("가공으로만 얻을 수 있는 재료가 부족하면 그냥 넘어가고").
           - If all missing ingredients are gatherable -> Marks viable and adds to gather plan.
           - If already alterable -> Marks ready immediately.
        4. Compiles aggregated raw material gathering requirements.
        """
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

        # Single-call inventory map (가방 소지품: 실제 가공시설에서 즉시 소모 가능한 수량)
        owned_inventory_map = defaultdict(int)
        owned_storage_map = defaultdict(int)
        try:
            for it in self.cli.get_items():
                loc = it.get("Location")
                cnt = int(it.get("Count", 0))
                dn = it.get("DisplayName", "")
                if loc == "inventory":
                    owned_inventory_map[dn] += cnt
                elif loc in ("character_storage", "account_storage"):
                    owned_storage_map[dn] += cnt
        except Exception:
            pass

        categories_plan: Dict[str, Any] = {}
        total_raw_needed: Dict[str, int] = defaultdict(int)

        for cat_name, cat_info in target_cats.items():
            facility = cat_info["facility"]
            tiers_list = cat_info["tiers"]

            completed_works = [w for w in works if w.get("FacilityName") == facility and w.get("IsCompleted")]
            in_progress_works = [w for w in works if w.get("FacilityName") == facility and not w.get("IsCompleted")]
            available_slots = max(0, 7 - len(in_progress_works))

            planned_tiers = []
            skipped_tiers = []
            category_raw_needed = defaultdict(int)
            allocated_materials = defaultdict(int)

            for tier_idx, item_name in enumerate(tiers_list, 1):
                tier_num = 8 - tier_idx
                matching_recipes = [
                    it for it in alter_items 
                    if it["DisplayName"] == item_name or it["DisplayName"].startswith(item_name + "(")
                ]

                clean_name = item_name.split("(")[0].strip()
                std_info = STANDARD_ALTER_RECIPES.get(clean_name)

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
            "categories": categories_plan,
            "total_raw_needed": dict(total_raw_needed),
            "can_start": any(len(c["planned_tiers"]) > 0 for c in categories_plan.values()) or any(c["completed_count"] > 0 for c in categories_plan.values())
        }

    def execute_quick_alter(self, plan_or_category: Any = "all", callback: Optional[LogCallback] = None) -> Dict[str, Any]:
        """
        Executes the 7-slot facility quick alter routine:
        1. Collects all completed works at target facilities FIRST (가공품을 먼저 수령하여 재료 확보).
        2. Evaluates ingredient shortages & determines plan AFTER collection (수령 후 최신 인벤토리 기준으로 재료 판단).
        3. Gathers all required gatherable raw materials in bulk.
        4. Starts altering from High Tier down to Low Tier at once.
        5. Registers alarms for background completion.
        """
        self.log("🚀 [7슬롯 최고 레벨 가공대 빠른 실행 가동] 루틴을 시작합니다.", "action", callback)
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
            self.log("🛑 [사용자 중지] 시작 전 중지 요청이 감지되었습니다.", "warn", callback)
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
            self.log("🛑 [사용자 중지] 작업이 중단되었습니다.", "warn", callback)
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
        self.log("🔍 [가공 재료 판단] 가공품 수령 완료 후 최신 가방 상태를 기준으로 고티어 가공 가능 여부를 판단합니다.", "action", callback)
        plan = self.analyze_quick_alter(cat_key if cat_key != "all" else None)
        categories = plan.get("categories", {})
        total_raw_needed = plan.get("total_raw_needed", {})

        if self.abort_requested:
            self.log("🛑 [사용자 중지] 채집 전 작업이 중단되었습니다.", "warn", callback)
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
            self.log(f"🌿 [사전 원자재 맞춤 채집] 가공에 부족한 원자재 총 {len(total_raw_needed)}종을 필요한 수량만큼만 채집합니다.", "action", callback)
            for mat_name, needed_qty in total_raw_needed.items():
                if self.abort_requested:
                    self.log("🛑 [사용자 중지] 채집 대기열이 중단되었습니다.", "warn", callback)
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
                    self.log(f"⚠️ '{mat_name}'은(는) 필드 채집 목록에 없어 채집을 건너뜁니다.", "warn", callback)
                    continue
                if not tool_ok:
                    self.log(f"⚠️ '{mat_name}' 채집 도구가 없거나 내구도가 0입니다!", "warn", callback)

                self.log(f"🌿 [맞춤 채집 시작] '{mat_name}' (부족 수량: {needed_qty}개) 목표 수량만큼만 채집을 진행합니다.", "action", callback)
                try:
                    g_res = self.auto_gather(mat_name, needed_qty, callback=callback)
                    gained = g_res.get("gained", 0) if isinstance(g_res, dict) else 0
                    gathered_list.append({
                        "item": mat_name,
                        "gained": gained,
                        "required": needed_qty,
                        "status": g_res.get("status", "completed") if isinstance(g_res, dict) else "completed"
                    })
                    if isinstance(g_res, dict) and g_res.get("status") == "aborted":
                        self.log("🛑 [사용자 중지] 빠른 실행 파이프라인이 중단되었습니다.", "warn", callback)
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
                    self.log(f"⚠️ '{mat_name}' 채집 중 오류 발생: {ex}", "warn", callback)

        if self.abort_requested:
            self.log("🛑 [사용자 중지] 가공 등록 전 작업이 중단되었습니다.", "warn", callback)
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
                self.log("🛑 [사용자 중지] 가공 슬롯 등록 중 작업이 중단되었습니다.", "warn", callback)
                break

            facility = c_info["facility"]
            planned = c_info.get("planned_tiers", [])
            if not planned:
                self.log(f"ℹ️ [{c_name} ({facility})] 등록 가능한 고티어 가공이 없어 건너뜁니다.", "info", callback)
                continue

            self.log(f"⚙️ [{c_name} ({facility})] 고티어 우선 순차 가공 등록 시작 (총 {len(planned)}건)", "action", callback)
            for item in planned:
                if self.abort_requested:
                    self.log("🛑 [사용자 중지] 가공 등록이 중단되었습니다.", "warn", callback)
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
                time.sleep(1)

        # -------------------------------------------------------------
        # STEP 5: Register facility-wide alarms for last work completion
        # -------------------------------------------------------------
        synced = alarm_store.sync_facility_alarms(self.cli, default_source="빠른 가공")
        if synced:
            self.log(f"🔔 [가공시설 알람 동기화] 총 {len(synced)}개 시설의 마지막 가공 완료 시각에 맞춰 알람이 등록되었습니다.", "info", callback)

        self.log("🎉 [가공대 7슬롯 빠른 실행 완료] 수령 ➔ 가공 판단 ➔ 일괄 채집 ➔ 고티어 가공 등록이 완료되었습니다!", "success", callback)
        self.log("🔔 각 가공시설별 마지막 가공이 끝나면 알람이 울립니다.", "info", callback)

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

