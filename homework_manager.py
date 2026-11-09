# -*- coding: utf-8 -*-
"""
Homework Management & In-Game Telemetry/Event Auto-Detection Engine for Mobi Supporter (모비노기 지원도구).
Integrates the core features of '모비 숙제 오버레이' directly into the web dashboard backend,
and synchronizes multi-character profiles with RegisteredDeliveryStore.
"""

import copy
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from mabi_cli import MabinogiCLI, get_user_data_path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
KST_TZ = timezone(timedelta(hours=9))

HOMEWORK_CONFIG_FILE = get_user_data_path("homework_config.json", SCRIPT_DIR)
HOMEWORK_DATA_FILE = get_user_data_path("homework_data.json", SCRIPT_DIR)

# Legacy overlay paths for automatic one-time migration if present on the user's PC
LEGACY_OVERLAY_DIRS = [
    os.path.expanduser(r"~\Documents\모비 숙제 오버레이\모비 숙제 오버레이(제작)"),
    os.path.expanduser(r"~\Documents\모비 숙제 오버레이"),
]

DEFAULT_CHARACTERS: List[Dict[str, Any]] = [
    {"name": "캐릭터 1", "class": "", "combat_power": 0, "title": ""},
]

DEFAULT_DAILY_TASKS: List[str] = [
    "정기 의뢰 [J]",
    "검은 구멍 [M]",
    "요일 던전 [J]",
    "성수·럭키 찬스 [J]",
    "일일 미션 [N]",
    "레드닷 확인 [ESC]",
]

DEFAULT_FOCUS_TASKS: List[str] = [
    "던전 토벌 [/]",
    "심층 던전 토벌 [/]",
    "소모품 정리 [I]",
    "일반패션 럭키박스 [U]",
    "보석 연금술 [K]",
    "장비 감정 [I]",
    "성능 개선 [K]",
    "원석 변환 [K]",
    "장비 분해 [I]",
    "룬 승급 [K]",
    "패션 장비 승급 [K]",
    "성수 물물교환 [M]",
    "보관함 [X or C]",
    "대장간 [.]",
]

DEFAULT_WEEKLY_TASKS: List[str] = [
    "소환의 결계",
    "검은 구멍",
    "필드 보스",
    "레이드: 적월의 스카스",
    "레이드: 카브락",
    "레이드: 에이렐",
    "레이드: 화이트 서큐버스",
    "어비스: 허상의 정박지",
    "어비스: 광기의 동굴",
    "어비스: 흩어진 물길",
    "뱅가드 브리치",
]


# --- In-game auto-detection rules (verified against live MabinogiMobile_CLI output) ---

# get_daily_missions: number of claimed rewards that counts as '일일 미션' done
DAILY_MISSION_REWARD_THRESHOLD = 6

# get_weekly_missions is account-wide in Mabinogi Mobile.
# Account-wide missions must NEVER auto-mark per-character weekly homework
# (e.g. '검은 구멍 1회 입장' should not complete a 7~14 run weekly homework,
# and completing it on one character should never auto-check homework on alt characters).
# Per-character raids/bosses are safely detected via actual in-game combat clears in WEEKLY_BOSS_RULES.
WEEKLY_MISSION_RULES: Tuple[Tuple[str, str], ...] = ()

# 요일 던전: 월/목 빛나는 동굴, 화/금 눈부신 저택, 수/토 찬란한 유적, 일 택1
DAY_DUNGEON_KEYWORDS: Tuple[str, ...] = ("빛나는 동굴", "눈부신 저택", "찬란한 유적", "요일")
ABYSS_KEYWORDS: Tuple[str, ...] = ("허상의 정박지", "광기의 동굴", "흩어진 물길", "어비스")
ABYSS_TASKS: Tuple[str, ...] = ("어비스: 허상의 정박지", "어비스: 광기의 동굴", "어비스: 흩어진 물길")
WEEKLY_BOSS_RULES: Tuple[Tuple[Tuple[str, ...], str], ...] = (
    (("페리", "크라브바흐", "크라마", "드로흐에넴", "토르모그", "앙그르바한", "필드 보스", "필드보스"), "필드 보스"),
    (("스카스", "적월"), "레이드: 적월의 스카스"),
    (("카브락",), "레이드: 카브락"),
    (("에이렐",), "레이드: 에이렐"),
    (("서큐버스",), "레이드: 화이트 서큐버스"),
)

# A dungeon-clear counter increase within this many seconds after leaving a dungeon
# is attributed to that run (covers clears missed by a throttled poller).
DUNGEON_EVIDENCE_WINDOW_SEC = 90.0

CURRENCY_KEYWORDS: Tuple[str, ...] = ("은동전", "마족 공물", "골드", "데카", "날개", "주화")

# Maps that are towns or non-dungeon fields (must NEVER be classified as dungeons/abyss)
TOWN_MAP_KEYWORDS: Tuple[str, ...] = (
    "티르코네일", "던바튼", "이멘마하", "반호르", "켈라", "카브",
    "콜헨", "필리아", "발레스", "거주지", "하우징", "마을", "농장"
)


def is_town_map(map_name: str) -> bool:
    clean = (map_name or "").strip()
    return bool(clean and any(t in clean for t in TOWN_MAP_KEYWORDS))


def get_logical_now_kst() -> datetime:
    """Returns current datetime in KST shifted back by 6 hours (06:00 AM reset boundary)."""
    return datetime.now(KST_TZ) - timedelta(hours=6)


def get_logical_date_str() -> str:
    """Returns 'YYYY-MM-DD' for the current logical game day (06:00 AM KST boundary)."""
    return get_logical_now_kst().strftime("%Y-%m-%d")


def get_monday_str() -> str:
    """Returns 'YYYY-MM-DD' for the Monday of the current logical game week (Monday 06:00 AM KST boundary)."""
    logical_now = get_logical_now_kst()
    monday = logical_now - timedelta(days=logical_now.weekday())
    return monday.strftime("%Y-%m-%d")


def extract_base_char_name(raw_name: str) -> Tuple[str, str]:
    """
    Splits a label like '캐릭터 1 (전사)' into ('캐릭터 1', '전사').
    If no parenthetical job exists, returns (raw_name.strip(), '').
    """
    s = (raw_name or "").strip()
    m = re.match(r"^(.+?)\s*\(([^()]+)\)\s*$", s)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return s, ""


class HomeworkManager:
    """
    Manages character homework checklists (Daily / Focus / Weekly), character memos,
    real-time in-game telemetry/mission/quest collection, auto character detection,
    auto task checking, Omen Barrier alerts, and Discord webhook notifications.
    """

    def __init__(
        self,
        cli: MabinogiCLI,
        delivery_store: Any,
        settings_store: Any,
        log_callback: Optional[Callable[[str, str], None]] = None,
    ):
        self.cli = cli
        self.delivery_store = delivery_store
        self.settings_store = settings_store
        self.log_callback = log_callback
        self._lock = threading.RLock()

        self.config_file = HOMEWORK_CONFIG_FILE
        self.data_file = HOMEWORK_DATA_FILE

        self.config: Dict[str, Any] = {}
        self.data: Dict[str, Any] = {}
        self.event_logs: List[Dict[str, str]] = []

        # Step rollback history stack: list of (char_name, category, task_name)
        self._rollback_stack: List[Tuple[str, str, str]] = []

        # Live telemetry cache & state tracking for event transitions
        self._last_extended_poll_ts: float = 0.0
        self._data_dirty: bool = False
        self._live_telemetry: Dict[str, Any] = {
            "currencies": [],
            "daily_missions": [],
            "weekly_missions": [],
            "quests": [],
            "growth_guide": {},
            "altering_summary": {"ready": 0, "total": 0, "works": []},
            "omen_barrier": {},
            "updated_at": 0,
        }
        self._prev_state: Dict[str, Any] = {
            "char_name": None,          # last auto-switched character
            "telemetry_char": None,     # character the transient state below belongs to
            "dungeon_state": "",
            "dungeon_run": None,        # {active, handled, name, ended_at}
            "abyss_result_playing": False,
            "last_abyss_at": 0.0,
            "goddess_loot_ready": set(),
            "growth_guide_daily_rem": {},
            "last_interaction": "",
            "altering_ready": None,
        }

        self.load()
        self.sync_with_delivery_store(initial=True)
        self.check_auto_reset()

    def _log(self, level: str, message: str):
        ts = datetime.now(KST_TZ).strftime("%H:%M:%S")
        with self._lock:
            self.event_logs.append({"time": ts, "level": level, "message": message})
            if len(self.event_logs) > 150:
                self.event_logs.pop(0)
        if callable(self.log_callback):
            try:
                self.log_callback(level, f"[숙제 지원] {message}")
            except Exception:
                pass

    # =========================================================================
    # 1. Load / Save & Legacy Overlay Migration
    # =========================================================================

    def _find_legacy_overlay_files(self) -> Tuple[Optional[str], Optional[str]]:
        for d in LEGACY_OVERLAY_DIRS:
            cfg_p = os.path.join(d, "mobi_homework_config.json")
            dat_p = os.path.join(d, "mobi_homework_data.json")
            if os.path.isfile(cfg_p) or os.path.isfile(dat_p):
                return (
                    cfg_p if os.path.isfile(cfg_p) else None,
                    dat_p if os.path.isfile(dat_p) else None,
                )
        return None, None

    def _build_default_config(self) -> Dict[str, Any]:
        return {
            "characters": copy.deepcopy(DEFAULT_CHARACTERS),
            "daily_tasks": list(DEFAULT_DAILY_TASKS),
            "focus_tasks": list(DEFAULT_FOCUS_TASKS),
            "weekly_tasks": list(DEFAULT_WEEKLY_TASKS),
            "task_character_Mapping": {
                "daily": {},
                "focus": {},
                "weekly": {},
            },
            "auto_reset_enabled": True,
            "focus_mode_enabled": False,
            "omen_barrier_alert": True,
            "sound_alert_enabled": True,
            "ai_auto_char_detect": True,
            "ai_auto_task_check": True,
            "discord_webhook_url": "",
            "webhook_notify_gathering": False,
            "webhook_notify_crafting": True,
            "webhook_notify_Abyss": True,
            "webhook_notify_abyss": True,
            "webhook_notify_dungeon": True,
            "webhook_notify_hunting": True,
        }

    def load(self):
        with self._lock:
            cfg_exists = os.path.isfile(self.config_file)
            dat_exists = os.path.isfile(self.data_file)
            legacy_cfg, legacy_dat = self._find_legacy_overlay_files()

            base_cfg = self._build_default_config()
            loaded_cfg = {}

            if cfg_exists:
                try:
                    with open(self.config_file, "r", encoding="utf-8") as f:
                        loaded_cfg = json.load(f) or {}
                except Exception:
                    loaded_cfg = {}
            elif legacy_cfg:
                try:
                    with open(legacy_cfg, "r", encoding="utf-8") as f:
                        loaded_cfg = json.load(f) or {}
                except Exception:
                    loaded_cfg = {}

            if isinstance(loaded_cfg, dict):
                for k, v in loaded_cfg.items():
                    base_cfg[k] = v

            # Normalize character list
            raw_chars = base_cfg.get("characters", [])
            norm_chars: List[Dict[str, Any]] = []
            seen_names = set()
            if isinstance(raw_chars, list):
                for c in raw_chars:
                    if isinstance(c, str):
                        bname, bcls = extract_base_char_name(c)
                        if bname and bname not in seen_names:
                            seen_names.add(bname)
                            norm_chars.append({
                                "name": bname,
                                "class": bcls or "미지정",
                                "combat_power": 0,
                                "title": "",
                            })
                    elif isinstance(c, dict):
                        bname, bcls = extract_base_char_name(str(c.get("name") or ""))
                        job_cls = str(c.get("class") or bcls or "미지정").strip()
                        if bname and bname not in seen_names:
                            seen_names.add(bname)
                            norm_chars.append({
                                "name": bname,
                                "class": job_cls,
                                "combat_power": int(c.get("combat_power") or 0),
                                "title": str(c.get("title") or "").strip(),
                                "living_score": int(c.get("living_score") or 0),
                                "attractiveness_score": int(c.get("attractiveness_score") or 0),
                                "decor_score": int(c.get("decor_score") or 0),
                            })
            if not norm_chars:
                norm_chars = copy.deepcopy(DEFAULT_CHARACTERS)
            base_cfg["characters"] = norm_chars

            # Ensure task lists & mapping exist
            for tkey, def_list in (
                ("daily_tasks", DEFAULT_DAILY_TASKS),
                ("focus_tasks", DEFAULT_FOCUS_TASKS),
                ("weekly_tasks", DEFAULT_WEEKLY_TASKS),
            ):
                if not isinstance(base_cfg.get(tkey), list) or not base_cfg[tkey]:
                    base_cfg[tkey] = list(def_list)

            mapping = base_cfg.get("task_character_Mapping")
            if not isinstance(mapping, dict):
                mapping = {}
            for cat in ("daily", "focus", "weekly"):
                if not isinstance(mapping.get(cat), dict):
                    mapping[cat] = {}
            base_cfg["task_character_Mapping"] = mapping

            # Sync webhook URL with settings_store if one of them has a URL
            shared_url = str(self.settings_store.get("discord_webhook_url") or "").strip()
            hw_url = str(base_cfg.get("discord_webhook_url") or "").strip()
            if hw_url and not shared_url:
                self.settings_store.set("discord_webhook_url", hw_url)
            elif shared_url and not hw_url:
                base_cfg["discord_webhook_url"] = shared_url

            self.config = base_cfg

            # Load homework data
            loaded_dat = {}
            if dat_exists:
                try:
                    with open(self.data_file, "r", encoding="utf-8") as f:
                        loaded_dat = json.load(f) or {}
                except Exception:
                    loaded_dat = {}
            elif legacy_dat:
                try:
                    with open(legacy_dat, "r", encoding="utf-8") as f:
                        loaded_dat = json.load(f) or {}
                except Exception:
                    loaded_dat = {}

            if not isinstance(loaded_dat, dict):
                loaded_dat = {}

            self.data = {
                "last_daily_reset": loaded_dat.get("last_daily_reset") or get_logical_date_str(),
                "last_weekly_reset": loaded_dat.get("last_weekly_reset") or get_monday_str(),
                "current_char_index": int(loaded_dat.get("current_char_index") or 0),
                "characters": loaded_dat.get("characters") if isinstance(loaded_dat.get("characters"), dict) else {},
            }

            self._ensure_character_data_locked()
            if not cfg_exists or not dat_exists:
                self.save_config()
                self.save_data()

    def _ensure_character_data_locked(self):
        char_map = self.data.setdefault("characters", {})
        for c in self.config.get("characters", []):
            cname = c["name"]
            c_entry = char_map.setdefault(cname, {})
            c_entry.setdefault("daily", {})
            c_entry.setdefault("focus", {})
            c_entry.setdefault("weekly", {})
            c_entry.setdefault("memo", "")
            c_entry.setdefault("last_focus_completed", "")
            if "weekly_focus_done" not in c_entry:
                last_fc = str(c_entry.get("last_focus_completed") or "")[:10]
                c_entry["weekly_focus_done"] = bool(last_fc and last_fc >= get_monday_str())
            c_entry.setdefault("combat_power", int(c.get("combat_power") or 0))
            c_entry.setdefault("living_score", int(c.get("living_score") or 0))
            c_entry.setdefault("attractiveness_score", int(c.get("attractiveness_score") or 0))
            c_entry.setdefault("decor_score", int(c.get("decor_score") or 0))
            c_entry.setdefault("title", str(c.get("title") or ""))
            c_entry.setdefault("level", 0)
            c_entry.setdefault("bag_weight_pct", 0)
            c_entry.setdefault("bag_weight_str", "")
            c_entry.setdefault("currencies", {})
            c_entry.setdefault("altering_ready", 0)
            c_entry.setdefault("altering_total", 0)

            for cat, t_list in (
                ("daily", self.config.get("daily_tasks", [])),
                ("focus", self.config.get("focus_tasks", [])),
                ("weekly", self.config.get("weekly_tasks", [])),
            ):
                cat_dict = c_entry[cat]
                for t in t_list:
                    if t not in cat_dict:
                        cat_dict[t] = False

            # Keep config combat_power / scores synced with data if data has higher values
            if int(c_entry.get("combat_power") or 0) > int(c.get("combat_power") or 0):
                c["combat_power"] = int(c_entry["combat_power"])
            elif int(c.get("combat_power") or 0) > int(c_entry.get("combat_power") or 0):
                c_entry["combat_power"] = int(c["combat_power"])

            for skey in ("living_score", "attractiveness_score", "decor_score"):
                if int(c_entry.get(skey) or 0) > 0 and not int(c.get(skey) or 0):
                    c[skey] = int(c_entry[skey])
            if c_entry.get("title") and not c.get("title"):
                c["title"] = str(c_entry["title"])

        chars = self.config.get("characters", [])
        if chars:
            idx = int(self.data.get("current_char_index") or 0)
            if idx < 0 or idx >= len(chars):
                self.data["current_char_index"] = 0

    def _atomic_write_json(self, path: str, payload: Dict[str, Any]) -> None:
        """Writes JSON via temp file + os.replace so a crash never leaves a truncated file."""
        tmp_path = f"{path}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except Exception as e:
            print(f"[숙제 지원] 저장 실패 ({os.path.basename(path)}): {e}")

    def save_config(self):
        with self._lock:
            self._atomic_write_json(self.config_file, self.config)

    def save_data(self):
        with self._lock:
            self._atomic_write_json(self.data_file, self.data)

    # =========================================================================
    # 2. Unification with RegisteredDeliveryStore (Weekly Delivery Profiles)
    # =========================================================================

    def _format_delivery_profile_name(self, name: str, job_class: str) -> str:
        clean_name, _ = extract_base_char_name(name)
        return clean_name

    def sync_with_delivery_store(self, initial: bool = False):
        """
        Ensures RegisteredDeliveryStore and HomeworkManager share the same character list
        and active character selection.
        """
        with self._lock:
            with self.delivery_store._lock:
                self.delivery_store._ensure_active_character_locked()
                hw_chars = self.config.get("characters", [])
                deliv_chars = self.delivery_store.characters

                # Check if delivery_store only has the unconfigured default "캐릭터 1"
                if len(deliv_chars) == 1:
                    only_cid = next(iter(deliv_chars.keys()))
                    only_cinfo = deliv_chars[only_cid]
                    only_base, only_cls = extract_base_char_name(only_cinfo.get("name", ""))
                    if only_base.startswith("캐릭터 ") and hw_chars:
                        # Map the first character (or matching class character) to only_cid so existing target progress is kept!
                        sig = only_cinfo.get("signature") or {}
                        sig_job = str(sig.get("job") or only_cls or "").strip()
                        matched_hw_idx = 0
                        if sig_job:
                            for idx, hc in enumerate(hw_chars):
                                if hc.get("class") == sig_job:
                                    matched_hw_idx = idx
                                    break
                        first_hc = hw_chars[matched_hw_idx]
                        only_cinfo["name"] = first_hc["name"]

                # Build lookup of existing delivery characters by base name
                deliv_by_base: Dict[str, str] = {}
                for cid, cinfo in list(deliv_chars.items()):
                    bname, bcls = extract_base_char_name(cinfo.get("name", cid))
                    if bname:
                        deliv_by_base[bname] = cid

                # Ensure every homework character exists in delivery_store in the same order
                ordered_deliv_chars: Dict[str, Dict[str, Any]] = {}
                for hc in hw_chars:
                    bname = hc["name"]
                    jcls = hc.get("class", "")
                    disp_name = bname
                    if bname in deliv_by_base:
                        cid = deliv_by_base[bname]
                        cinfo = deliv_chars[cid]
                        cinfo["name"] = disp_name
                        sig = cinfo.get("signature")
                        if not isinstance(sig, dict):
                            sig = {
                                "job": jcls if jcls != "미지정" else "",
                                "level": int(self.data.get("characters", {}).get(bname, {}).get("level") or 0),
                                "living_score": int(hc.get("living_score") or 0),
                                "attractiveness_score": int(hc.get("attractiveness_score") or 0),
                                "combat_score": int(hc.get("combat_power") or 0),
                                "realm": "",
                            }
                            cinfo["signature"] = sig
                        else:
                            if jcls and jcls != "미지정":
                                sig["job"] = jcls
                        ordered_deliv_chars[cid] = cinfo
                    else:
                        # Create new profile in delivery_store with default 18 targets
                        idx = len(deliv_chars) + len(ordered_deliv_chars) + 1
                        while f"char_{idx}" in deliv_chars or f"char_{idx}" in ordered_deliv_chars:
                            idx += 1
                        new_cid = f"char_{idx}"
                        new_targets = self.delivery_store._build_default_18_targets()
                        for t in new_targets.values():
                            t["override_current"] = 0
                        sig = None
                        if hc.get("living_score") or (jcls and jcls != "미지정"):
                            sig = {
                                "job": jcls if jcls != "미지정" else "",
                                "level": int(self.data.get("characters", {}).get(bname, {}).get("level") or 0),
                                "living_score": int(hc.get("living_score") or 0),
                                "attractiveness_score": int(hc.get("attractiveness_score") or 0),
                                "combat_score": int(hc.get("combat_power") or 0),
                                "realm": "",
                            }
                        ordered_deliv_chars[new_cid] = {
                            "id": new_cid,
                            "name": disp_name,
                            "signature": sig,
                            "weekly_delivered": False,
                            "delivered_week_key": None,
                            "delivered_at": None,
                            "targets": new_targets,
                            "created_at": int(time.time() * 1000),
                        }

                # Also if delivery_store had any extra user-created characters not in hw_chars, import them into hw_chars
                for cid, cinfo in deliv_chars.items():
                    if cid not in ordered_deliv_chars:
                        bname, bcls = extract_base_char_name(cinfo.get("name", cid))
                        if bname and not bname.startswith("캐릭터 "):
                            sig = cinfo.get("signature") or {}
                            cls_str = bcls or str(sig.get("job") or "미지정")
                            hw_chars.append({
                                "name": bname,
                                "class": cls_str,
                                "combat_power": int(sig.get("combat_score") or 0),
                                "title": "",
                                "living_score": int(sig.get("living_score") or 0),
                                "attractiveness_score": int(sig.get("attractiveness_score") or 0),
                                "decor_score": 0,
                            })
                            ordered_deliv_chars[cid] = cinfo

                if ordered_deliv_chars:
                    self.delivery_store.characters = ordered_deliv_chars

                self._ensure_character_data_locked()

                # Sync active character selection
                if initial:
                    # Align delivery_store active character to homework's current_char_index (or vice versa if delivery was active)
                    active_hw_name = self.get_active_char_name()
                    matched_cid = self._find_delivery_char_id_by_name(active_hw_name)
                    if matched_cid:
                        self.delivery_store.active_character_id = matched_cid
                self.delivery_store.save()
                self.save_config()
                self.save_data()

    def _find_delivery_char_id_by_name(self, char_name: str) -> Optional[str]:
        target_base, _ = extract_base_char_name(char_name)
        for cid, cinfo in self.delivery_store.characters.items():
            bname, _ = extract_base_char_name(cinfo.get("name", cid))
            if bname == target_base or cinfo.get("name") == char_name:
                return cid
        return None

    def on_delivery_character_selected(self, char_id: str):
        """Called when the user switches character in Weekly Delivery mode."""
        with self._lock:
            cinfo = self.delivery_store.characters.get(char_id)
            if not cinfo:
                return
            bname, _ = extract_base_char_name(cinfo.get("name", ""))
            for idx, hc in enumerate(self.config.get("characters", [])):
                if hc["name"] == bname:
                    if self.data.get("current_char_index") != idx:
                        self.data["current_char_index"] = idx
                        self.save_data()
                    break

    def on_delivery_character_added(self, cinfo: Dict[str, Any]):
        """Called when a character is added in Weekly Delivery mode."""
        with self._lock:
            bname, bcls = extract_base_char_name(cinfo.get("name", ""))
            if not bname:
                return
            sig = cinfo.get("signature") or {}
            jcls = bcls or str(sig.get("job") or "미지정")
            for idx, hc in enumerate(self.config.get("characters", [])):
                if hc["name"] == bname:
                    self.data["current_char_index"] = idx
                    self.save_data()
                    return
            self.config["characters"].append({
                "name": bname,
                "class": jcls,
                "combat_power": int(sig.get("combat_score") or 0),
                "title": "",
                "living_score": int(sig.get("living_score") or 0),
                "attractiveness_score": int(sig.get("attractiveness_score") or 0),
                "decor_score": 0,
            })
            self._ensure_character_data_locked()
            self.data["current_char_index"] = len(self.config["characters"]) - 1
            self.save_config()
            self.save_data()

    def on_delivery_character_renamed(self, old_name: str, new_name: str):
        """Called when a character is renamed in Weekly Delivery mode."""
        with self._lock:
            old_base, _ = extract_base_char_name(old_name)
            new_base, new_cls = extract_base_char_name(new_name)
            if not new_base:
                return
            for hc in self.config.get("characters", []):
                if hc["name"] == old_base:
                    hc["name"] = new_base
                    if new_cls:
                        hc["class"] = new_cls
                    break
            if old_base != new_base and old_base in self.data.get("characters", {}):
                self.data["characters"][new_base] = self.data["characters"].pop(old_base)
            # Update task_character_Mapping references
            for cat in ("daily", "focus", "weekly"):
                cmap = self.config.get("task_character_Mapping", {}).get(cat, {})
                for tname, clist in cmap.items():
                    if isinstance(clist, list) and old_base in clist:
                        cmap[tname] = [new_base if x == old_base else x for x in clist]
            self._ensure_character_data_locked()
            self.save_config()
            self.save_data()

    def on_delivery_character_deleted(self, deleted_name: str):
        """Called when a character is deleted in Weekly Delivery mode."""
        with self._lock:
            del_base, _ = extract_base_char_name(deleted_name)
            chars = self.config.get("characters", [])
            if len(chars) <= 1:
                return
            self.config["characters"] = [hc for hc in chars if hc["name"] != del_base]
            self.data.get("characters", {}).pop(del_base, None)
            self._ensure_character_data_locked()
            # Sync active index to delivery_store's new active character
            act_cid = self.delivery_store.active_character_id
            self.on_delivery_character_selected(act_cid)
            self.save_config()
            self.save_data()

    def on_delivery_character_linked(self, char_info: Optional[Dict[str, Any]]):
        """Called when the active character is explicitly linked to in-game character."""
        if not char_info or not isinstance(char_info, dict):
            return
        with self._lock:
            active_name = self.get_active_char_name()
            self._live_telemetry["matched_char_name"] = active_name
            self._prev_state["char_name"] = active_name
            cdata = self.data.setdefault("characters", {}).setdefault(active_name, {})
            cp = self._extract_stat_int(char_info.get("CombatScore") or char_info.get("CombatPower"))
            ls = self._extract_stat_int(char_info.get("LivingScore"))
            cs = self._extract_stat_int(char_info.get("AttractivenessScore"))
            ds = self._extract_stat_int(char_info.get("DecorScore"))
            lvl = self._extract_stat_int(char_info.get("Level"))
            title = str(char_info.get("Title") or char_info.get("EquippedTitleName") or "").strip()
            job_disp = str(char_info.get("EnabledCombatJobDisplayName") or "").strip()
            for hc in self.config.get("characters", []):
                if hc["name"] == active_name:
                    if cp > 0:
                        hc["combat_power"] = cp
                        cdata["combat_power"] = cp
                    if ls > 0:
                        hc["living_score"] = ls
                        cdata["living_score"] = ls
                    if cs > 0:
                        hc["attractiveness_score"] = cs
                        cdata["attractiveness_score"] = cs
                    if ds > 0:
                        hc["decor_score"] = ds
                        cdata["decor_score"] = ds
                    if title:
                        hc["title"] = title
                        cdata["title"] = title
                    if job_disp and hc.get("class") in ("", "미지정"):
                        hc["class"] = job_disp
                    break
            if lvl > 0:
                cdata["level"] = lvl
            cdata["updated_at"] = datetime.now(KST_TZ).strftime("%H:%M:%S")
            self.save_config()
            self.save_data()

    # =========================================================================
    # 3. Auto-Reset (Daily 06:00 AM KST & Monday 06:00 AM KST)
    # =========================================================================

    def check_auto_reset(self) -> Dict[str, bool]:
        with self._lock:
            if not self.config.get("auto_reset_enabled", True):
                return {"daily_reset": False, "weekly_reset": False}

            today_logical = get_logical_date_str()
            monday_logical = get_monday_str()
            daily_reset_done = False
            weekly_reset_done = False

            if self.data.get("last_daily_reset") != today_logical:
                for cname, cdata in self.data.get("characters", {}).items():
                    for k in list(cdata.get("daily", {}).keys()):
                        cdata["daily"][k] = False
                    for k in list(cdata.get("focus", {}).keys()):
                        cdata["focus"][k] = False
                self.data["last_daily_reset"] = today_logical
                daily_reset_done = True

            if self.data.get("last_weekly_reset") != monday_logical:
                for cname, cdata in self.data.get("characters", {}).items():
                    for k in list(cdata.get("weekly", {}).keys()):
                        cdata["weekly"][k] = False
                    cdata["weekly_focus_done"] = False
                self.data["last_weekly_reset"] = monday_logical
                weekly_reset_done = True

            if daily_reset_done or weekly_reset_done:
                self.save_data()
                if daily_reset_done and weekly_reset_done:
                    self._log("info", f"오전 6시 기준 일일·집중·주간 숙제가 자동 초기화되었습니다 ({today_logical}).")
                elif daily_reset_done:
                    self._log("info", f"오전 6시 기준 일일·집중 숙제가 자동 초기화되었습니다 ({today_logical}).")
                elif weekly_reset_done:
                    self._log("info", f"월요일 오전 6시 기준 주간 숙제가 자동 초기화되었습니다 ({monday_logical}).")

            return {"daily_reset": daily_reset_done, "weekly_reset": weekly_reset_done}

    # =========================================================================
    # 4. Character & Task Helpers
    # =========================================================================

    def get_active_char_index(self) -> int:
        with self._lock:
            chars = self.config.get("characters", [])
            if not chars:
                return 0
            idx = int(self.data.get("current_char_index") or 0)
            if idx < 0 or idx >= len(chars):
                idx = 0
                self.data["current_char_index"] = 0
            return idx

    def get_active_char_info(self) -> Dict[str, Any]:
        with self._lock:
            chars = self.config.get("characters", [])
            if not chars:
                return {"name": "캐릭터 1", "class": "미지정", "combat_power": 0, "title": ""}
            return chars[self.get_active_char_index()]

    def get_active_char_name(self) -> str:
        return self.get_active_char_info().get("name", "캐릭터 1")

    def get_char_tasks(self, char_name: str, category: str) -> List[str]:
        """Returns the list of tasks in `category` ('daily', 'focus', 'weekly') assigned to `char_name`."""
        with self._lock:
            all_tasks = self.config.get(f"{category}_tasks", [])
            mapping = self.config.get("task_character_Mapping", {}).get(category, {})
            result = []
            for t in all_tasks:
                allowed = mapping.get(t, [])
                if not allowed or char_name in allowed:
                    result.append(t)
            return result

    def get_recommended_focus_character(self) -> Optional[str]:
        """
        Determines the best character to run Focus Homework next (`★ 추천`):
        Picks among characters who still have incomplete Focus tasks (prioritizing those
        who haven't completed weekly focus yet this week), sorted by oldest `last_focus_completed`.
        """
        with self._lock:
            candidates: List[Tuple[int, str, str, int]] = []
            for idx, hc in enumerate(self.config.get("characters", [])):
                cname = hc["name"]
                ftasks = self.get_char_tasks(cname, "focus")
                if not ftasks:
                    continue
                cdata = self.data.get("characters", {}).get(cname, {})
                fdict = cdata.get("focus", {})
                done_cnt = sum(1 for t in ftasks if fdict.get(t, False))
                if done_cnt < len(ftasks):
                    wk_done = 1 if bool(cdata.get("weekly_focus_done", False)) else 0
                    last_dt = str(cdata.get("last_focus_completed") or "")
                    candidates.append((wk_done, last_dt, cname, idx))
            if not candidates:
                return None
            candidates.sort(key=lambda x: (x[0], x[1], x[3]))
            return candidates[0][2]

    # =========================================================================
    # 5. Character Resolution (Smart Multi-Signal Matching)
    # =========================================================================

    @staticmethod
    def _extract_stat_int(val: Any) -> int:
        if isinstance(val, dict):
            val = val.get("Value", 0)
        try:
            return int(float(val or 0))
        except Exception:
            return 0

    def resolve_character(self, my_info: Optional[Dict[str, Any]]) -> Optional[str]:
        """
        Matches in-game `my_info` to a configured character name using:
        1. Direct Name / CharacterName match
        2. Relative Stat Similarity (LivingScore 40% + AttractivenessScore 40% + CombatScore 20% within 8% error)
        3. Title match
        4. Combat Job (EnabledCombatJobDisplayName) match
        """
        if not my_info or not isinstance(my_info, dict):
            return None

        with self._lock:
            chars = self.config.get("characters", [])
            if not chars:
                return None

            # 1. Direct name match
            direct_name = str(
                my_info.get("Name")
                or my_info.get("CharacterName")
                or my_info.get("UserName")
                or ""
            ).strip()
            if direct_name:
                for c in chars:
                    if c.get("name") == direct_name:
                        return direct_name

            job_name = str(
                my_info.get("EnabledCombatJobDisplayName")
                or my_info.get("CombatJobDisplayName")
                or my_info.get("JobDisplayName")
                or ""
            ).strip()
            title_name = str(
                my_info.get("Title")
                or my_info.get("EquippedTitleName")
                or my_info.get("TitleName")
                or ""
            ).strip()
            combat_score = self._extract_stat_int(my_info.get("CombatScore") or my_info.get("CombatPower"))
            living_score = self._extract_stat_int(my_info.get("LivingScore"))
            charm_score = self._extract_stat_int(my_info.get("AttractivenessScore"))

            # 2. Relative stat similarity (works even when the user changed combat job on the same character)
            if living_score > 0 and charm_score > 0:
                best_stat_match = None
                min_rel_diff = 0.08  # 8% threshold
                for c in chars:
                    cname = c.get("name")
                    cdata = self.data.get("characters", {}).get(cname, {})
                    saved_ls = int(cdata.get("living_score") or c.get("living_score") or 0)
                    saved_cs = int(cdata.get("attractiveness_score") or c.get("attractiveness_score") or 0)
                    saved_cp = int(cdata.get("combat_power") or c.get("combat_power") or 0)
                    if saved_ls > 0 and saved_cs > 0:
                        ls_diff = abs(saved_ls - living_score) / max(living_score, saved_ls, 1)
                        cs_diff = abs(saved_cs - charm_score) / max(charm_score, saved_cs, 1)
                        if ls_diff < 0.05 and cs_diff < 0.05:
                            cp_diff = (
                                abs(saved_cp - combat_score) / max(combat_score, saved_cp, 1)
                                if (saved_cp > 0 and combat_score > 0)
                                else 0.0
                            )
                            total_diff = (ls_diff * 0.4) + (cs_diff * 0.4) + (cp_diff * 0.2)
                            if total_diff < min_rel_diff:
                                min_rel_diff = total_diff
                                best_stat_match = cname
                if best_stat_match:
                    return best_stat_match

            # 3. Title match
            if title_name:
                title_matches = [
                    c for c in chars
                    if str(c.get("title") or "").strip() == title_name
                    or str(self.data.get("characters", {}).get(c["name"], {}).get("title") or "").strip() == title_name
                ]
                if len(title_matches) == 1:
                    return title_matches[0].get("name")
                elif len(title_matches) > 1 and combat_score > 0:
                    best_c = min(
                        title_matches,
                        key=lambda c: abs(int(c.get("combat_power") or 0) - combat_score),
                    )
                    return best_c.get("name")

            # 4. Job (class) match
            if job_name:
                job_matches = [c for c in chars if str(c.get("class") or "").strip() == job_name]
                if len(job_matches) == 1:
                    return job_matches[0].get("name")
                elif len(job_matches) > 1:
                    if title_name:
                        for c in job_matches:
                            if str(c.get("title") or "").strip() == title_name:
                                return c.get("name")
                    if combat_score > 0:
                        best_c = min(
                            job_matches,
                            key=lambda c: abs(int(c.get("combat_power") or 0) - combat_score),
                        )
                        return best_c.get("name")
                    return job_matches[0].get("name")

            return None

    # =========================================================================
    # 6.Hourly Omen Barrier (불길한 소환의 결계) Status
    # =========================================================================

    def get_omen_barrier_status(self) -> Dict[str, Any]:
        now = datetime.now(KST_TZ)
        minute = now.minute
        second = now.second
        # Omen barrier spawns every top of the hour (XX:00)
        is_active_window = (0 <= minute < 10)
        is_peak_window = (0 <= minute < 5)
        seconds_until_next = ((59 - minute) * 60) + (60 - second)
        minutes_until_next = (seconds_until_next + 59) // 60
        next_hour = (now.hour + 1) % 24

        if is_peak_window:
            label = f"결계 진행 중! ({now.hour:02d}시 정각 스폰 · {minute}분 경과)"
            short_label = f"결계 진행중 ({minute}분)"
            level = "active"
        elif minute >= 55:
            rem_m = 59 - minute
            rem_s = 60 - second
            label = f"{next_hour:02d}시 결계 임박! ({rem_m}분 {rem_s:02d}초 전)"
            short_label = f"결계 임박 {rem_m}:{rem_s:02d}"
            level = "imminent"
        else:
            label = f"결계 대기 ({next_hour:02d}:00까지 {minutes_until_next}분 남음)"
            short_label = f"결계 {minutes_until_next}분 전"
            level = "normal"

        return {
            "enabled": bool(self.config.get("omen_barrier_alert", True)),
            "hour": now.hour,
            "minute": minute,
            "second": second,
            "next_hour": next_hour,
            "hour_key": now.strftime("%Y-%m-%d_%H"),
            "is_active_window": is_active_window,
            "is_peak_window": is_peak_window,
            "is_pre_alert": (minute >= 55),
            "seconds_until_next": seconds_until_next,
            "minutes_until_next": minutes_until_next,
            "label": label,
            "short_label": short_label,
            "level": level,
        }

    # =========================================================================
    # 7. Real-Time In-Game Polling & Event Auto-Detection Engine
    # =========================================================================

    def send_homework_discord_webhook(self, title: str, description: str, color: int = 0x5865F2) -> Tuple[bool, str]:
        url = str(
            self.config.get("discord_webhook_url")
            or self.settings_store.get("discord_webhook_url")
            or ""
        ).strip()
        if not url or not url.startswith("http"):
            return False, "웹훅 URL이 설정되지 않았습니다."

        payload = {
            "username": "모비노기 지원도구 알림",
            "embeds": [
                {
                    "title": title,
                    "description": description,
                    "color": color,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "footer": {
                        "text": f"모비노기 지원도구 | {datetime.now(KST_TZ).strftime('%Y-%m-%d %H:%M:%S')}"
                    },
                }
            ],
        }
        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "MobiSupporter/2.0",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                if resp.status in (200, 204):
                    return True, "전송 성공"
                return False, f"HTTP {resp.status}"
        except Exception as e:
            return False, str(e)

    def _auto_mark_task_locked(
        self,
        char_name: str,
        category: str,
        keyword_or_exact: str,
        value: bool = True,
        reason: str = "",
        exact: bool = False,
    ) -> List[str]:
        """Marks matching task(s) in `category` for `char_name` and logs if newly changed."""
        cdata = self.data.setdefault("characters", {}).setdefault(char_name, {})
        cat_dict = cdata.setdefault(category, {})
        char_tasks = self.get_char_tasks(char_name, category)
        changed_tasks: List[str] = []

        kw_compact = self._compact(keyword_or_exact)
        for t_name in char_tasks:
            t_compact = self._compact(t_name)
            if exact:
                matched = (t_name == keyword_or_exact)
            elif kw_compact == "던전토벌":
                matched = (kw_compact in t_compact) and ("심층" not in t_compact)
            else:
                matched = (keyword_or_exact in t_name) or (bool(kw_compact) and kw_compact in t_compact)
            if matched:
                prev_val = bool(cat_dict.get(t_name, False))
                if prev_val != value:
                    cat_dict[t_name] = value
                    changed_tasks.append(t_name)

        if changed_tasks:
            if category == "focus" and value:
                if all(cat_dict.get(t, False) for t in char_tasks):
                    cdata["last_focus_completed"] = get_logical_date_str()
                    cdata["weekly_focus_done"] = True
            self.save_data()
            for t in changed_tasks:
                cat_ko = {"daily": "일일", "focus": "집중", "weekly": "주간"}.get(category, category)
                state_ko = "완료 체크" if value else "체크 해제"
                msg = f"[{char_name}] {cat_ko} 숙제 '{t}' 자동 {state_ko}"
                if reason:
                    msg += f" ({reason})"
                self._log("success" if value else "info", msg)
        return changed_tasks

    # --- Telemetry text helpers ---------------------------------------------------

    @staticmethod
    def _clean_text(text: Any) -> str:
        """Strips rich-text tags (e.g. '<color=orange>') and collapses whitespace."""
        s = re.sub(r"<[^>]+>", "", str(text or ""))
        return re.sub(r"\s+", " ", s).strip()

    @classmethod
    def _compact(cls, text: Any) -> str:
        """Cleaned text with all spaces removed, for robust keyword matching."""
        return cls._clean_text(text).replace(" ", "")

    @classmethod
    def _objective_key(cls, desc: Any) -> str:
        """
        Stable identity for a quest objective. The game embeds live progress in the
        description (e.g. '사냥터 클리어 2/5'), so the trailing 'n/m' must be stripped,
        otherwise the key changes on every clear and progress can never be compared.
        """
        s = re.sub(r"\s*\d+\s*/\s*\d+\s*$", "", cls._clean_text(desc))
        return s.replace(" ", "")

    @staticmethod
    def _to_int(val: Any) -> int:
        try:
            return int(val or 0)
        except (TypeError, ValueError):
            return 0

    # --- Auto-mark primitives --------------------------------------------------------

    def _mark_once_locked(self, char_name: str, category: str, keyword: str, reason: str) -> None:
        """
        Auto-marks a task from a *state* signal (e.g. a mission that stays completed) only
        once per reset period, so a manual un-check by the user is not overwritten on
        every poll. Daily/focus periods roll at 06:00, weekly at Monday 06:00.
        """
        cdata = self.data.setdefault("characters", {}).setdefault(char_name, {})
        period = get_monday_str() if category == "weekly" else get_logical_date_str()
        reg_key = "auto_marked_weekly" if category == "weekly" else "auto_marked_daily"
        reg = cdata.get(reg_key)
        if not isinstance(reg, dict) or reg.get("period") != period:
            reg = {"period": period, "keys": []}
            cdata[reg_key] = reg
        mark_key = f"{category}:{keyword}"
        if mark_key in reg["keys"]:
            return
        reg["keys"].append(mark_key)
        self._data_dirty = True
        self._auto_mark_task_locked(char_name, category, keyword, True, reason)

    def _counter_increased_locked(self, cdata: Dict[str, Any], key: str, count: int) -> bool:
        """
        Per-character progress snapshot for cumulative counters (e.g. weekly 정기 의뢰
        objectives). Returns True when `count` grew since it was last observed during the
        current logical day. The snapshot is persisted so character switches and app
        restarts within the same day do not lose the baseline.
        """
        today = get_logical_date_str()
        snap = cdata.get("progress_snapshot")
        if not isinstance(snap, dict) or snap.get("date") != today:
            snap = {"date": today, "counts": {}}
            cdata["progress_snapshot"] = snap
            self._data_dirty = True
        counts = snap.setdefault("counts", {})
        prev = counts.get(key)
        if prev != count:
            counts[key] = count
            self._data_dirty = True
        return prev is not None and count > prev

    def _notify_async(self, enabled: bool, title: str, description: str, color: int) -> None:
        if not enabled:
            return
        threading.Thread(
            target=self.send_homework_discord_webhook,
            args=(title, description, color),
            daemon=True,
        ).start()

    # --- Main entry point ------------------------------------------------------------

    def update_from_ingame_state(
        self,
        my_info: Optional[Dict[str, Any]],
        activity: Optional[Dict[str, Any]],
        inventory: Optional[Dict[str, Any]],
        environment: Optional[Dict[str, Any]],
        is_busy: bool = False,
    ):
        """
        Called whenever live CLI status is fetched (dashboard polling and the server's
        background telemetry loop). Updates character stats, performs character
        auto-detection/switching, and auto-checks homework from missions, the quest
        tracker and dungeon activity transitions.
        """
        self.check_auto_reset()
        if not my_info:
            return

        with self._lock:
            prev_char = self._prev_state.get("telemetry_char")
            target_char_name = self._resolve_and_switch_character_locked(my_info, is_busy)
            if not target_char_name:
                return

            char_changed = bool(prev_char and target_char_name != prev_char)
            if char_changed:
                self._reset_transient_state(target_char_name)

        telemetry = self._refresh_extended_telemetry(is_busy)

        with self._lock:
            cdata = self.data.setdefault("characters", {}).setdefault(target_char_name, {})
            cdata["updated_at"] = datetime.now(KST_TZ).strftime("%H:%M:%S")

            if self._update_character_stats_locked(target_char_name, cdata, my_info, inventory, telemetry):
                self.save_config()
                self.save_data()

            if not self.config.get("ai_auto_task_check", True):
                return

            # Safety Guard: Skip auto task-check on the very frame character switched
            # to guarantee old character's transient events never contaminate the new character
            if char_changed:
                return

            self._data_dirty = False
            act = activity if isinstance(activity, dict) else {}
            env = environment if isinstance(environment, dict) else {}

            dungeon_evidence = self._detect_from_daily_missions_locked(target_char_name, cdata, telemetry["daily_missions"])
            abyss_evidence = self._detect_from_weekly_missions_locked(target_char_name, cdata, telemetry["weekly_missions"])
            dungeon_evidence |= self._detect_from_quests_locked(target_char_name, cdata, telemetry["quests"])
            self._detect_dungeon_events_locked(target_char_name, act, env, dungeon_evidence, abyss_evidence)
            self._detect_from_growth_guide_locked(target_char_name, cdata, telemetry.get("growth_guide", {}))
            self._detect_gathering_locked(target_char_name, act)
            self._detect_altering_locked(target_char_name, telemetry["altering_summary"])

            if self._data_dirty:
                self.save_data()
                self._data_dirty = False

    # --- Telemetry collection --------------------------------------------------------

    def _refresh_extended_telemetry(self, is_busy: bool) -> Dict[str, Any]:
        """Polls currencies / missions / quests / altering at most every 8s (never while busy)."""
        now = time.time()
        tel = self._live_telemetry
        if is_busy or (now - self._last_extended_poll_ts) < 8.0:
            return tel
        self._last_extended_poll_ts = now

        def _safe(fn: Callable[[], Any], fallback: Any) -> Any:
            try:
                return fn()
            except Exception:
                return fallback

        currencies = _safe(self.cli.get_currencies, tel.get("currencies", []))
        daily_missions = _safe(self.cli.get_daily_missions, tel.get("daily_missions", []))
        weekly_missions = _safe(self.cli.get_weekly_missions, tel.get("weekly_missions", []))
        quests = _safe(self.cli.get_quests, tel.get("quests", []))
        growth_guide = _safe(self.cli.get_growth_guide, tel.get("growth_guide", {}))
        altering_summary = tel.get("altering_summary", {"ready": 0, "total": 0, "works": []})
        alt_raw = _safe(self.cli.get_altering_works, None)
        if isinstance(alt_raw, dict):
            works = alt_raw.get("works", []) or []
            altering_summary = {
                "ready": sum(1 for w in works if w.get("IsCompleted") or self._to_int(w.get("RemainingSeconds")) <= 0),
                "total": len(works),
                "works": works,
            }

        with self._lock:
            self._live_telemetry = {
                "matched_char_name": tel.get("matched_char_name") or "",
                "currencies": currencies if isinstance(currencies, list) else [],
                "daily_missions": daily_missions if isinstance(daily_missions, list) else [],
                "weekly_missions": weekly_missions if isinstance(weekly_missions, list) else [],
                "quests": quests if isinstance(quests, list) else [],
                "growth_guide": growth_guide if isinstance(growth_guide, dict) else {},
                "altering_summary": altering_summary,
                "omen_barrier": self.get_omen_barrier_status(),
                "updated_at": int(now * 1000),
            }
            return self._live_telemetry

    def _resolve_and_switch_character_locked(self, my_info: Dict[str, Any], is_busy: bool) -> str:
        resolved_name = self.resolve_character(my_info)
        if not resolved_name and self.delivery_store.matched_ingame_character_id:
            dc_matched = self.delivery_store.characters.get(self.delivery_store.matched_ingame_character_id)
            if dc_matched:
                resolved_name, _ = extract_base_char_name(dc_matched.get("name", ""))

        self._live_telemetry["matched_char_name"] = resolved_name or ""
        if resolved_name:
            deliv_cid = self._find_delivery_char_id_by_name(resolved_name)
            if deliv_cid:
                with self.delivery_store._lock:
                    self.delivery_store.matched_ingame_character_id = deliv_cid

        active_name = self.get_active_char_name()
        if (
            resolved_name
            and self.config.get("ai_auto_char_detect", True)
            and not is_busy
            and resolved_name != self._prev_state.get("char_name")
        ):
            self._prev_state["char_name"] = resolved_name
            for idx, hc in enumerate(self.config.get("characters", [])):
                if hc["name"] != resolved_name:
                    continue
                if self.data.get("current_char_index") != idx:
                    self.data["current_char_index"] = idx
                    self.save_data()
                    self._log("info", f"인게임 접속 캐릭터 감지 -> '{resolved_name}({hc.get('class', '')})' 프로필로 자동 전환되었습니다.")
                deliv_cid = self._find_delivery_char_id_by_name(resolved_name)
                if deliv_cid and self.delivery_store.active_character_id != deliv_cid:
                    self.delivery_store.select_character(deliv_cid)
                active_name = resolved_name
                break

        target = resolved_name or active_name
        # Transient edge-detection state belongs to one in-game character; reset it on change
        # so the previous character's dungeon/quest state never triggers marks for the new one.
        if target and target != self._prev_state.get("telemetry_char"):
            self._reset_transient_state(target)
        return target

    def _reset_transient_state(self, char_name: str) -> None:
        self._prev_state.update({
            "telemetry_char": char_name,
            "dungeon_state": "",
            "dungeon_run": None,
            "abyss_result_playing": False,
            "goddess_loot_ready": set(),
            "last_interaction": "",
            "altering_ready": None,
        })
        self._prev_state.setdefault("hunting_spoils", {})[char_name] = False
        self._prev_state.setdefault("growth_guide_daily_rem", {})[char_name] = None
        self._last_extended_poll_ts = 0.0
        if hasattr(self, "_live_telemetry"):
            self._live_telemetry["daily_missions"] = []
            self._live_telemetry["weekly_missions"] = []
            self._live_telemetry["quests"] = []
            self._live_telemetry["growth_guide"] = {}

    def _update_character_stats_locked(
        self,
        char_name: str,
        cdata: Dict[str, Any],
        my_info: Dict[str, Any],
        inventory: Optional[Dict[str, Any]],
        telemetry: Dict[str, Any],
    ) -> bool:
        """Syncs combat power / scores / title / level / bag weight / currencies. Returns True if changed."""
        dirty = False
        stat_values = {
            "combat_power": self._extract_stat_int(my_info.get("CombatScore") or my_info.get("CombatPower")),
            "living_score": self._extract_stat_int(my_info.get("LivingScore")),
            "attractiveness_score": self._extract_stat_int(my_info.get("AttractivenessScore")),
            "decor_score": self._extract_stat_int(my_info.get("DecorScore")),
        }
        title = str(my_info.get("Title") or my_info.get("EquippedTitleName") or "").strip()
        job_disp = str(my_info.get("EnabledCombatJobDisplayName") or "").strip()

        for hc in self.config.get("characters", []):
            if hc["name"] != char_name:
                continue
            for key, val in stat_values.items():
                if val > 0 and hc.get(key) != val:
                    hc[key] = val
                    cdata[key] = val
                    dirty = True
            if title and hc.get("title") != title:
                hc["title"] = title
                cdata["title"] = title
                dirty = True
            if job_disp and hc.get("class") in ("", "미지정"):
                hc["class"] = job_disp
                dirty = True
            break

        lvl = self._extract_stat_int(my_info.get("Level"))
        if lvl > 0 and cdata.get("level") != lvl:
            cdata["level"] = lvl
            dirty = True

        cur_w = max_w = 0.0
        if isinstance(inventory, dict):
            cur_w = float(inventory.get("CurrentInventoryWeightAsDecimal") or inventory.get("CurrentInventoryWeight") or 0)
            max_w = float(inventory.get("MaxInventoryWeightAsDecimal") or inventory.get("MaxInventoryWeight") or 0)
        vitals = my_info.get("Vitals") if isinstance(my_info.get("Vitals"), dict) else {}
        if max_w <= 0 and vitals:
            cur_w = float(vitals.get("InventoryWeightCurrent") or 0)
            max_w = float(vitals.get("InventoryWeightMax") or 0)
        if max_w > 0:
            pct = int(round((cur_w / max_w) * 100))
            w_str = f"{cur_w:.0f}/{max_w:.0f}"
            if cdata.get("bag_weight_pct") != pct or cdata.get("bag_weight_str") != w_str:
                cdata["bag_weight_pct"] = pct
                cdata["bag_weight_str"] = w_str
                dirty = True

        curr_map: Dict[str, Any] = {}
        for item in telemetry.get("currencies") or []:
            if not isinstance(item, dict):
                continue
            cname_cur = str(item.get("Name") or item.get("DisplayName") or "").strip()
            if any(k in cname_cur for k in CURRENCY_KEYWORDS):
                amt = self._to_int(item.get("Amount") or item.get("Count"))
                max_amt = self._to_int(item.get("MaxAmount"))
                curr_map[cname_cur] = f"{amt}/{max_amt}" if max_amt > 0 else amt
        if curr_map and cdata.get("currencies") != curr_map:
            cdata["currencies"] = curr_map
            dirty = True

        alt = telemetry.get("altering_summary") or {}
        ar, at = self._to_int(alt.get("ready")), self._to_int(alt.get("total"))
        if cdata.get("altering_ready") != ar or cdata.get("altering_total") != at:
            cdata["altering_ready"] = ar
            cdata["altering_total"] = at
            dirty = True
        return dirty

    # --- Detectors -------------------------------------------------------------------

    def _detect_from_daily_missions_locked(self, char_name: str, cdata: Dict[str, Any], missions: List[Any]) -> bool:
        """
        Daily missions reset at 06:00 together with daily homework, so their state is
        authoritative for the current day. Mission titles are flavour text
        ('구석구석 깨끗하게'); the actual objective lives in `Description`
        ('검은 구멍 1회 입장'), so matching must use Title + Description.
        Returns True if the daily dungeon-clear counter increased (dungeon clear evidence).
        """
        claimed = 0
        dungeon_count = 0
        for m in missions or []:
            if not isinstance(m, dict):
                continue
            text = self._compact(f"{m.get('Title', '')} {m.get('Description', '')}")
            cur = self._to_int(m.get("CurrentCount"))
            done = bool(m.get("IsCompleted")) or bool(m.get("IsRewardReceived"))
            if m.get("IsRewardReceived"):
                claimed += 1
            if "검은구멍" in text and (done or cur > 0):
                self._mark_once_locked(char_name, "daily", "검은 구멍", "일일 미션: 검은 구멍 입장 확인")
            if "던전" in text and "토벌" in text and "심층" not in text:
                dungeon_count = max(dungeon_count, cur)

        if claimed >= DAILY_MISSION_REWARD_THRESHOLD:
            self._mark_once_locked(char_name, "daily", "일일 미션", f"일일 미션 보상 {claimed}개 수령")
        return self._counter_increased_locked(cdata, "daily_mission:dungeon", dungeon_count)

    def _detect_from_weekly_missions_locked(self, char_name: str, cdata: Dict[str, Any], missions: List[Any]) -> bool:
        """
        Weekly missions in Mabinogi Mobile are account-wide.
        To prevent cross-character contamination where clearing on the main character
        auto-marks weekly homework on all alt characters, text-based auto marking is disabled.
        Only character-specific in-game dungeon/boss clears are used.
        """
        return False

    def _detect_from_growth_guide_locked(
        self,
        char_name: str,
        cdata: Dict[str, Any],
        guide: Dict[str, Any],
    ) -> None:
        """
        Processes character-specific growth guide contents:
          * Daily '검은 구멍': Auto-mark daily '검은 구멍' if remaining count decreased during session or IsCompleted
          * Daily '요일 던전': Auto-mark daily '요일 던전' if IsCompleted
          * Weekly tasks ('검은 구멍', '소환의 결계', '필드 보스', '뱅가드 브리치'):
            Auto-mark if IsCompleted or (RemainingCount is not None and RemainingCount <= 0)
          * Excludes Weekly '어비스' as requested (each abyss dungeon is individual)
        """
        if not isinstance(guide, dict):
            return
        contents = guide.get("Contents")
        if not isinstance(contents, list) or not contents:
            return

        daily_rem_tracker = self._prev_state.setdefault("growth_guide_daily_rem", {})
        prev_bh_daily_rem = daily_rem_tracker.get(char_name)
        guide_summary = cdata.setdefault("growth_guide", {})

        for item in contents:
            if not isinstance(item, dict):
                continue
            target = str(item.get("Target") or "").strip()
            day_type = str(item.get("DayType") or "").strip().lower()
            is_completed = bool(item.get("IsCompleted", False))
            rem = item.get("RemainingCount")
            rem_count = self._to_int(rem) if rem is not None else None

            summary_key = f"{day_type}:{target}"
            guide_summary[summary_key] = {
                "day_type": day_type,
                "target": target,
                "is_completed": is_completed,
                "remaining_count": rem_count,
            }

            # 1. 일일 숙제 반영
            if day_type == "daily":
                if target == "검은 구멍":
                    if rem_count is not None:
                        # 접속 중 남은 횟수가 하나라도 차감된 경우 (소탕권 포함)
                        if prev_bh_daily_rem is not None and rem_count < prev_bh_daily_rem:
                            self._auto_mark_task_locked(
                                char_name, "daily", "검은 구멍", True,
                                f"성장 가이드: 일일 검은 구멍 횟수 차감 ({prev_bh_daily_rem} -> {rem_count})"
                            )
                        daily_rem_tracker[char_name] = rem_count

                    if is_completed:
                        self._auto_mark_task_locked(
                            char_name, "daily", "검은 구멍", True,
                            "성장 가이드: 일일 검은 구멍 완료"
                        )
                elif target == "요일 던전":
                    if is_completed:
                        self._auto_mark_task_locked(
                            char_name, "daily", "요일 던전", True,
                            "성장 가이드: 일일 요일 던전 완료"
                        )

            # 2. 주간 숙제 반영
            elif day_type == "weekly":
                # 어비스는 각 어비스 던전마다 한 번씩 돌아야 하므로 주간 남은 횟수로 카운트하는 것 제외
                if target == "어비스":
                    continue

                is_done = is_completed or (rem_count is not None and rem_count <= 0)
                if is_done:
                    reason = f"성장 가이드: 주간 {target} 완료"
                    if rem_count is not None and rem_count <= 0:
                        reason += " (남은 0회)"
                    if target in ("검은 구멍", "소환의 결계", "필드 보스", "뱅가드 브리치"):
                        self._auto_mark_task_locked(char_name, "weekly", target, True, reason)

    def _detect_from_quests_locked(self, char_name: str, cdata: Dict[str, Any], quests: List[Any]) -> bool:
        """
        Quest tracker detection:
          * '[주간 목표] 모험가 길드의 정기 의뢰' objective counts (던전/심층 던전/사냥터 클리어)
            -> any increase today marks '정기 의뢰'.
          * 지역 임무(goddess_mission) reaching the '전리품' step -> hunting ground cleared -> marks '정기 의뢰'.
        Returns True if a dungeon-clear objective increased (dungeon clear evidence).
        """
        regular_progress: List[str] = []
        dungeon_progress = False
        hunting_progress = False
        has_active_spoils_quest = False
        active_spoils_title = ""

        for q in quests or []:
            if not isinstance(q, dict):
                continue
            title = self._clean_text(q.get("QuestTitle"))
            src = str(q.get("Source") or "").lower()
            src_disp = str(q.get("SourceDisplayName") or "")
            objs = [o for o in (q.get("Objectives") or []) if isinstance(o, dict)]

            # 1. 모험가 길드의 정기 의뢰 퀘스트 추적
            is_guild_regular = (
                ("모험가 길드" in title and "정기 의뢰" in title)
                or ("[주간 목표]" in title and "정기 의뢰" in title)
                or ("정기의뢰" in title.replace(" ", ""))
            )
            if is_guild_regular:
                for obj in objs:
                    desc_clean = self._clean_text(obj.get("Description"))
                    okey = self._objective_key(desc_clean)
                    if not okey:
                        continue
                    cnt = self._to_int(obj.get("Count"))
                    if self._counter_increased_locked(cdata, f"regular:{okey}", cnt):
                        regular_progress.append(desc_clean)
                        if "사냥터" in okey:
                            hunting_progress = True
                        elif "던전" in okey or "심층" in okey:
                            dungeon_progress = True

            # 2. 사냥터 전리품 수령 대기(여신상에서 전리품 받기) 퀘스트 활성화 여부 확인
            is_hunting_mission = (src == "goddess_mission" or "지역 임무" in src_disp or "지역임무" in src_disp or "사냥" in title)
            if is_hunting_mission:
                for obj in objs:
                    desc = self._clean_text(obj.get("Description"))
                    if "전리품 받기" in desc or ("여신상" in desc and "전리품" in desc):
                        has_active_spoils_quest = True
                        active_spoils_title = title
                        break

        # A) 정기 의뢰 진행 카운트 증가 시 일일 숙제 자동 완료
        if regular_progress or hunting_progress or dungeon_progress:
            label = ", ".join(regular_progress) if regular_progress else "정기 의뢰 진행"
            self._auto_mark_task_locked(
                char_name, "daily", "정기 의뢰", True, f"정기 의뢰 진행: {label}"
            )

        # B) 사냥터 여신상 전리품 수령 대기 감지 (사냥 종료 즉시 정기 의뢰 완료)
        spoils_tracker = self._prev_state.setdefault("hunting_spoils", {})
        char_prev_spoils = spoils_tracker.get(char_name, False)
        if has_active_spoils_quest and not char_prev_spoils:
            spoils_tracker[char_name] = True
            label = active_spoils_title or "사냥터"
            self._auto_mark_task_locked(char_name, "daily", "정기 의뢰", True, f"사냥터 클리어 완료 ({label})")
            self._notify_async(
                bool(self.config.get("webhook_notify_hunting", True)),
                "사냥터 클리어 알림",
                f"**[{char_name}]** **{label}** 클리어! 여신상에서 전리품을 수령하세요.",
                0x2ECC71,
            )
        elif not has_active_spoils_quest:
            spoils_tracker[char_name] = False

        return dungeon_progress

    def _detect_dungeon_events_locked(
        self,
        char_name: str,
        act: Dict[str, Any],
        env: Dict[str, Any],
        dungeon_evidence: bool,
        abyss_evidence: bool,
    ) -> None:
        """
        `get_activity().Dungeon` only exposes `State` (NotInDungeon / Entering / InProgress /
        Cleared) — it carries no dungeon name. The name is taken from the environment's
        GameSpaceDisplayName while inside the dungeon and remembered per run, so the
        clear can still be classified (요일 던전 / 레이드 / 어비스) after the player exits.
        A run counts as cleared on the State->Cleared edge, or when a dungeon-clear counter
        (daily mission / 정기 의뢰) increases shortly after the run, which covers clears the
        poller missed because the browser tab was throttled.
        """
        dungeon = act.get("Dungeon") if isinstance(act.get("Dungeon"), dict) else {}
        state = str(dungeon.get("State") or "")
        prev_state = str(self._prev_state.get("dungeon_state") or "")
        map_name = self._clean_text(env.get("GameSpaceDisplayName") or env.get("ChannelDisplayName"))
        now = time.time()
        run = self._prev_state.get("dungeon_run")

        in_dungeon = state in ("Entering", "InProgress", "Cleared")
        if in_dungeon:
            if not run or not run.get("active"):
                # A run seen first in 'Cleared' right after startup was already finished
                # before we were watching; don't re-announce it.
                run = {"active": True, "handled": (state == "Cleared" and not prev_state), "name": "", "ended_at": 0.0}
            if map_name and not is_town_map(map_name):
                # Only record as dungeon name if map_name is not a town map
                if not run.get("name") or any(k in map_name for k in ("던전", "동굴", "유적", "저택", "정박지", "물길", "어비스", "심층", "고분", "레이드", "보스")):
                    run["name"] = map_name
        elif run and run.get("active"):
            run["active"] = False
            run["ended_at"] = now

        cleared_edge = (state == "Cleared" and prev_state != "Cleared")
        recent_run = bool(run) and (run.get("active") or (now - run.get("ended_at", 0.0)) < DUNGEON_EVIDENCE_WINDOW_SEC)
        if run and not run.get("handled") and (cleared_edge or (dungeon_evidence and recent_run)):
            run["handled"] = True
            run_name = run.get("name") or "던전"
            if not is_town_map(run_name):
                self._on_dungeon_cleared_locked(char_name, run_name)

        # Abyss result sequence (top-level field in get_activity; older builds nested it in Dungeon)
        abyss_seq = bool(act.get("IsAbyssResultSequencePlaying") or dungeon.get("IsAbyssResultSequencePlaying"))
        if abyss_seq and not self._prev_state.get("abyss_result_playing"):
            # Result sequence is definitively playing: identify abyss dungeon name safely
            candidate = (run or {}).get("name") or map_name
            abyss_label = candidate if any(k in candidate for k in ABYSS_KEYWORDS) else "어비스 던전"
            self._on_abyss_cleared_locked(char_name, abyss_label)
        elif abyss_evidence and recent_run and run and run.get("name") and any(k in run.get("name") for k in ABYSS_KEYWORDS):
            # Only attribute weekly abyss counter increase if the player actually ran an abyss dungeon recently
            run["handled"] = True
            self._on_abyss_cleared_locked(char_name, run.get("name"))

        self._prev_state["abyss_result_playing"] = abyss_seq
        self._prev_state["dungeon_state"] = state
        self._prev_state["dungeon_run"] = run

    def _on_dungeon_cleared_locked(self, char_name: str, label: str) -> None:
        if is_town_map(label):
            return
        reason = f"{label} 클리어"

        # 1) 정기 의뢰 [J]: 해당 캐릭터로 아무 던전(일반, 요일, 심층, 보스 등) 클리어 시 즉시 완료!
        self._auto_mark_task_locked(char_name, "daily", "정기 의뢰", True, reason)

        # 2) 요일 던전
        if any(k in label for k in DAY_DUNGEON_KEYWORDS):
            self._auto_mark_task_locked(char_name, "daily", "요일 던전", True, reason)

        # 4) 주간 보스/레이드
        for keywords, task in WEEKLY_BOSS_RULES:
            if any(k in label for k in keywords):
                self._auto_mark_task_locked(char_name, "weekly", task, True, reason)

        self._notify_async(
            bool(self.config.get("webhook_notify_dungeon", True)),
            "던전 클리어 알림",
            f"**[{char_name}]** 캐릭터가 **{label}** 클리어를 완료했습니다!",
            0x3498DB,
        )

    def _on_abyss_cleared_locked(self, char_name: str, label: str) -> None:
        # The result sequence, the Cleared edge and the weekly mission counter can all
        # report the same abyss run; only the first signal within the window counts.
        now = time.time()
        if (now - float(self._prev_state.get("last_abyss_at") or 0.0)) < DUNGEON_EVIDENCE_WINDOW_SEC:
            return
        self._prev_state["last_abyss_at"] = now

        # Strict safety check: Never allow a town/field name to be treated as an abyss
        clean_label = str(label or "").strip()
        weekly_map = self.data.get("characters", {}).get(char_name, {}).get("weekly", {})
        target_task = next((t for t in ABYSS_TASKS if t.split(": ", 1)[-1] in clean_label), None)
        if not target_task:
            target_task = next((t for t in ABYSS_TASKS if not weekly_map.get(t, False)), ABYSS_TASKS[0])

        # If label is not a verified abyss dungeon name, extract clean name from the task
        display_name = clean_label if any(k in clean_label for k in ABYSS_KEYWORDS) else target_task.replace("어비스: ", "")

        # 어비스 던전 역시 던전 클리어이므로 정기 의뢰 완료 연동
        self._auto_mark_task_locked(char_name, "daily", "정기 의뢰", True, f"어비스 던전 클리어 ({display_name})")
        self._auto_mark_task_locked(char_name, "weekly", target_task, True, f"{display_name} 클리어")
        self._notify_async(
            bool(self.config.get("webhook_notify_abyss", True)),
            "어비스 클리어 알림",
            f"**[{char_name}]** 캐릭터가 **{display_name}** 클리어를 완료했습니다!",
            0x9B59B6,
        )

    def _detect_gathering_locked(self, char_name: str, act: Dict[str, Any]) -> None:
        """Tracks gathering interaction state (auto-mark disabled: user manages gathering manually)."""
        inter = act.get("Interaction") if isinstance(act.get("Interaction"), dict) else {}
        self._prev_state["last_interaction"] = str(inter.get("LastRunningInteractionType") or "")

    def _detect_altering_locked(self, char_name: str, altering_summary: Dict[str, Any]) -> None:
        """Tracks altering state (auto-mark disabled: user manages processing manually)."""
        self._prev_state["altering_ready"] = self._to_int((altering_summary or {}).get("ready"))

    # =========================================================================
    # 8. Dashboard State & Summary Builders
    # =========================================================================

    def get_dashboard_state(self) -> Dict[str, Any]:
        """Returns the complete Homework Support state for the web dashboard."""
        with self._lock:
            self.check_auto_reset()
            self._ensure_character_data_locked()

            chars_cfg = self.config.get("characters", [])
            active_idx = self.get_active_char_index()
            active_char = chars_cfg[active_idx] if chars_cfg else {"name": "캐릭터 1", "class": "미지정"}
            active_name = active_char["name"]
            recommended_focus_char = self.get_recommended_focus_character()

            # Weekly delivery summary lookup by base character name
            deliv_summary = self.delivery_store.get_characters_summary()
            deliv_by_base: Dict[str, Dict[str, Any]] = {}
            for dc in deliv_summary.get("characters", []):
                bname, _ = extract_base_char_name(dc.get("name", ""))
                deliv_by_base[bname] = dc

            # Build per-character full summary
            char_summaries: List[Dict[str, Any]] = []
            overall_daily_done = 0
            overall_daily_total = 0
            overall_focus_done = 0
            overall_focus_total = 0
            overall_weekly_done = 0
            overall_weekly_total = 0

            for idx, hc in enumerate(chars_cfg):
                cname = hc["name"]
                cdata = self.data.get("characters", {}).get(cname, {})

                d_tasks = self.get_char_tasks(cname, "daily")
                f_tasks = self.get_char_tasks(cname, "focus")
                w_tasks = self.get_char_tasks(cname, "weekly")

                d_map = cdata.get("daily", {})
                f_map = cdata.get("focus", {})
                w_map = cdata.get("weekly", {})

                d_done = sum(1 for t in d_tasks if d_map.get(t, False))
                f_done = sum(1 for t in f_tasks if f_map.get(t, False))
                w_done = sum(1 for t in w_tasks if w_map.get(t, False))

                overall_daily_done += d_done
                overall_daily_total += len(d_tasks)
                overall_focus_done += f_done
                overall_focus_total += len(f_tasks)
                overall_weekly_done += w_done
                overall_weekly_total += len(w_tasks)

                deliv_info = deliv_by_base.get(cname, {})
                d_pct = int(round((d_done / len(d_tasks)) * 100)) if d_tasks else 100
                f_pct = int(round((f_done / len(f_tasks)) * 100)) if f_tasks else 100
                w_pct = int(round((w_done / len(w_tasks)) * 100)) if w_tasks else 100
                d_items = [{"name": t, "task": t, "done": bool(d_map.get(t, False))} for t in d_tasks]
                f_items = [{"name": t, "task": t, "done": bool(f_map.get(t, False))} for t in f_tasks]
                w_items = [{"name": t, "task": t, "done": bool(w_map.get(t, False))} for t in w_tasks]

                cur_dict = cdata.get("currencies") or {}
                bw_str = str(cdata.get("bag_weight_str") or "")
                bw_cur, bw_max = 0, 0
                if "/" in bw_str:
                    parts = bw_str.split("/", 1)
                    try:
                        bw_cur = float(parts[0].strip())
                        bw_max = float(parts[1].strip())
                    except Exception:
                        pass

                char_summaries.append({
                    "index": idx,
                    "name": cname,
                    "class": hc.get("class", "미지정"),
                    "title": cdata.get("title") or hc.get("title", ""),
                    "level": int(cdata.get("level") or 0),
                    "combat_power": int(cdata.get("combat_power") or hc.get("combat_power") or 0),
                    "living_score": int(cdata.get("living_score") or hc.get("living_score") or 0),
                    "attractiveness_score": int(cdata.get("attractiveness_score") or hc.get("attractiveness_score") or 0),
                    "decor_score": int(cdata.get("decor_score") or hc.get("decor_score") or 0),
                    "bag_weight_pct": int(cdata.get("bag_weight_pct") or 0),
                    "bag_weight_str": bw_str,
                    "currencies": cur_dict,
                    "altering_ready": int(cdata.get("altering_ready") or 0),
                    "altering_total": int(cdata.get("altering_total") or 0),
                    "memo": str(cdata.get("memo") or ""),
                    "last_focus_completed": str(cdata.get("last_focus_completed") or ""),
                    "weekly_focus_done": bool(cdata.get("weekly_focus_done", False)),
                    "is_active": (idx == active_idx),
                    "is_recommended_focus": (cname == recommended_focus_char),
                    "daily_done": d_done,
                    "daily_total": len(d_tasks),
                    "daily_pct": d_pct,
                    "focus_done": f_done,
                    "focus_total": len(f_tasks),
                    "focus_pct": f_pct,
                    "weekly_done": w_done,
                    "weekly_total": len(w_tasks),
                    "weekly_pct": w_pct,
                    "delivery_char_id": deliv_info.get("id", ""),
                    "delivery_completed_count": int(deliv_info.get("completed_count") or 0),
                    "delivery_total_count": int(deliv_info.get("total_count") or 18),
                    "delivery_target_count": int(deliv_info.get("target_count") or deliv_info.get("total_count") or 18),
                    "weekly_delivered": bool(deliv_info.get("weekly_delivered", False)),
                    "daily_Checklist": d_items,
                    "focus_Checklist": f_items,
                    "weekly_Checklist": w_items,
                    "progress": {
                        "daily": {"done": d_done, "total": len(d_tasks), "pct": d_pct, "items": d_items},
                        "focus": {"done": f_done, "total": len(f_tasks), "pct": f_pct, "items": f_items},
                        "weekly": {"done": w_done, "total": len(w_tasks), "pct": w_pct, "items": w_items},
                    },
                    "stats": {
                        "level": int(cdata.get("level") or 0),
                        "title": cdata.get("title") or hc.get("title", ""),
                        "combat_power": int(cdata.get("combat_power") or hc.get("combat_power") or 0),
                        "living_score": int(cdata.get("living_score") or hc.get("living_score") or 0),
                        "attractiveness_score": int(cdata.get("attractiveness_score") or hc.get("attractiveness_score") or 0),
                        "decor_score": int(cdata.get("decor_score") or hc.get("decor_score") or 0),
                        "bag_weight": bw_cur,
                        "max_weight": bw_max,
                        "silver_coin": cur_dict.get("은동전", "-"),
                        "tribute": cur_dict.get("마족 공물", "-"),
                        "gold": cur_dict.get("골드", 0),
                        "wings": cur_dict.get("정령의 날개", "-"),
                        "updated_at": str(cdata.get("updated_at") or ""),
                    },
                })

            active_summary = char_summaries[active_idx] if char_summaries else {}

            # Determine active sequential step for Master Command Bar & Step Guide Card
            step_info = self._compute_active_step_locked(active_name)
            active_step_compat = None
            if not step_info.get("all_completed"):
                cat_k = step_info.get("category", "daily")
                cat_p = active_summary.get("progress", {}).get(cat_k, {})
                active_step_compat = {
                    "cat_key": cat_k,
                    "cat_title": step_info.get("category_label", ""),
                    "task_name": step_info.get("task", ""),
                    "cat_done": cat_p.get("done", 0),
                    "cat_total": cat_p.get("total", 0),
                }

            cfg_out = {
                "daily_tasks": self.config.get("daily_tasks", []),
                "focus_tasks": self.config.get("focus_tasks", []),
                "weekly_tasks": self.config.get("weekly_tasks", []),
                "task_character_Mapping": self.config.get("task_character_Mapping", {}),
                "auto_reset_enabled": bool(self.config.get("auto_reset_enabled", True)),
                "focus_mode_enabled": bool(self.config.get("focus_mode_enabled", False)),
                "omen_barrier_alert": bool(self.config.get("omen_barrier_alert", True)),
                "sound_alert_enabled": bool(self.config.get("sound_alert_enabled", True)),
                "ai_auto_char_detect": bool(self.config.get("ai_auto_char_detect", True)),
                "ai_auto_task_check": bool(self.config.get("ai_auto_task_check", True)),
                "auto_detect_character": bool(self.config.get("ai_auto_char_detect", True)),
                "auto_check_missions": bool(self.config.get("ai_auto_task_check", True)),
                "discord_webhook_url": str(self.config.get("discord_webhook_url") or self.settings_store.get("discord_webhook_url") or ""),
                "webhook_notify_dungeon": bool(self.config.get("webhook_notify_dungeon", True)),
                "webhook_notify_abyss": bool(self.config.get("webhook_notify_abyss", self.config.get("webhook_notify_Abyss", True))),
                "webhook_notify_hunting": bool(self.config.get("webhook_notify_hunting", True)),
                "webhook_notify_gathering": bool(self.config.get("webhook_notify_gathering", False)),
                "webhook_notify_crafting": bool(self.config.get("webhook_notify_crafting", True)),
                "webhook_events": {
                    "dungeon": bool(self.config.get("webhook_notify_dungeon", True)),
                    "abyss": bool(self.config.get("webhook_notify_abyss", self.config.get("webhook_notify_Abyss", True))),
                    "hunting_ground": bool(self.config.get("webhook_notify_hunting", True)),
                    "gathering": bool(self.config.get("webhook_notify_gathering", False)),
                    "crafting": bool(self.config.get("webhook_notify_crafting", True)),
                },
            }

            focus_mode = bool(self.config.get("focus_mode_enabled", False))
            if focus_mode:
                can_rb = bool(active_summary.get("focus_done", 0) > 0) or any(
                    item[1] == "focus" for item in self._rollback_stack
                )
            else:
                can_rb = bool(active_summary.get("daily_done", 0) > 0) or any(
                    item[1] != "focus" for item in self._rollback_stack
                )

            return {
                "active_char_index": active_idx,
                "active_char_name": active_name,
                "active_character": active_name,
                "active_character_detail": active_summary,
                "recommended_focus_char": recommended_focus_char,
                "recommended_focus_character": recommended_focus_char,
                "matched_ingame_character": self._live_telemetry.get("matched_char_name") or "",
                "focus_mode_enabled": focus_mode,
                "step_guide": step_info,
                "active_step": active_step_compat,
                "can_rollback": can_rb,
                "characters": char_summaries,
                "categories": {
                    "daily": {"title": "일일 숙제", "tasks": list(self.config.get("daily_tasks", []))},
                    "focus": {"title": "집중 숙제", "tasks": list(self.config.get("focus_tasks", []))},
                    "weekly": {"title": "주간 숙제", "tasks": list(self.config.get("weekly_tasks", []))},
                },
                "task_character_mapping": self.config.get("task_character_Mapping", {}),
                "overall": {
                    "daily_done": overall_daily_done,
                    "daily_total": overall_daily_total,
                    "daily_pct": int(round((overall_daily_done / overall_daily_total) * 100)) if overall_daily_total else 100,
                    "focus_done": overall_focus_done,
                    "focus_total": overall_focus_total,
                    "focus_pct": int(round((overall_focus_done / overall_focus_total) * 100)) if overall_focus_total else 100,
                    "weekly_done": overall_weekly_done,
                    "weekly_total": overall_weekly_total,
                    "weekly_pct": int(round((overall_weekly_done / overall_weekly_total) * 100)) if overall_weekly_total else 100,
                    "last_daily_reset": self.data.get("last_daily_reset", ""),
                    "last_weekly_reset": self.data.get("last_weekly_reset", ""),
                },
                "config": cfg_out,
                "settings": cfg_out,
                "omen_barrier": self.get_omen_barrier_status(),
                "live_telemetry": self._live_telemetry,
                "event_logs": list(self.event_logs[-60:]),
            }

    def _compute_active_step_locked(self, char_name: str) -> Dict[str, Any]:
        cdata = self.data.get("characters", {}).get(char_name, {})
        focus_mode = bool(self.config.get("focus_mode_enabled", False))
        cat_pairs = (("focus", "집중 숙제"),) if focus_mode else (("daily", "일일 숙제"), ("weekly", "주간 숙제"))
        for cat, cat_label in cat_pairs:
            tasks = self.get_char_tasks(char_name, cat)
            cmap = cdata.get(cat, {})
            for idx, t in enumerate(tasks):
                if not cmap.get(t, False):
                    next_task = tasks[idx + 1] if (idx + 1) < len(tasks) else None
                    return {
                        "all_completed": False,
                        "category": cat,
                        "category_label": cat_label,
                        "task": t,
                        "step_index": idx + 1,
                        "step_total": len(tasks),
                        "next_task": next_task,
                    }
        return {
            "all_completed": True,
            "category": "done",
            "category_label": "집중 숙제 완료" if focus_mode else "일일·주간 숙제 완료",
            "task": "현재 캐릭터의 집중 숙제를 모두 마쳤습니다!" if focus_mode else "현재 캐릭터의 일일·주간 숙제를 모두 마쳤습니다!",
            "step_index": 0,
            "step_total": 0,
            "next_task": None,
        }

    # =========================================================================
    # 9. User Actions (Check/Uncheck, Step Advance, Rollback, Memo, Config CRUD)
    # =========================================================================

    def select_character_by_name_or_index(self, char_name: Optional[str] = None, index: Optional[int] = None) -> Tuple[bool, str]:
        with self._lock:
            chars = self.config.get("characters", [])
            if not chars:
                return False, "등록된 캐릭터가 없습니다."

            target_idx = None
            if char_name:
                bname, _ = extract_base_char_name(char_name)
                for idx, hc in enumerate(chars):
                    if hc["name"] == bname or hc["name"] == char_name:
                        target_idx = idx
                        break
            if target_idx is None and index is not None:
                if 0 <= int(index) < len(chars):
                    target_idx = int(index)

            if target_idx is None:
                return False, "해당 캐릭터를 찾을 수 없습니다."

            self.data["current_char_index"] = target_idx
            self.save_data()
            selected_name = chars[target_idx]["name"]

            # Sync with delivery_store
            deliv_cid = self._find_delivery_char_id_by_name(selected_name)
            if deliv_cid:
                self.delivery_store.select_character(deliv_cid)

            return True, f"'{selected_name}' 캐릭터로 전환되었습니다."

    def select_character(self, char_name: str) -> Dict[str, Any]:
        self.select_character_by_name_or_index(char_name=char_name)
        return self.get_dashboard_state()

    def toggle_task(
        self,
        char_name: Optional[str],
        category: str,
        task_name: str,
        done: Optional[bool] = None,
        force_value: Optional[bool] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            if category not in ("daily", "focus", "weekly"):
                return self.get_dashboard_state()
            target_name = (char_name or self.get_active_char_name()).strip()
            cdata = self.data.setdefault("characters", {}).setdefault(target_name, {})
            cat_dict = cdata.setdefault(category, {})
            cur_val = bool(cat_dict.get(task_name, False))
            explicit_val = force_value if force_value is not None else done
            new_val = (not cur_val) if explicit_val is None else bool(explicit_val)
            cat_dict[task_name] = new_val

            if new_val:
                self._rollback_stack.append((target_name, category, task_name))
                if len(self._rollback_stack) > 50:
                    self._rollback_stack.pop(0)

            # Update last_focus_completed & weekly_focus_done if all focus tasks for target_name are now done
            if category == "focus":
                ftasks = self.get_char_tasks(target_name, "focus")
                if ftasks and all(cat_dict.get(t, False) for t in ftasks):
                    cdata["last_focus_completed"] = get_logical_date_str()
                    cdata["weekly_focus_done"] = True

            self.save_data()
            return self.get_dashboard_state()

    def set_weekly_focus_done(
        self,
        char_name: Optional[str] = None,
        done: Optional[bool] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            cname = (char_name or self.get_active_char_name()).strip()
            cdata = self.data.setdefault("characters", {}).setdefault(cname, {})
            cur_val = bool(cdata.get("weekly_focus_done", False))
            new_val = (not cur_val) if done is None else bool(done)
            cdata["weekly_focus_done"] = new_val
            if new_val and not cdata.get("last_focus_completed"):
                cdata["last_focus_completed"] = get_logical_date_str()
            self.save_data()
            return {"ok": True, "weekly_focus_done": new_val, "state": self.get_dashboard_state()}

    def complete_current_step(
        self,
        category_filter: Optional[str] = None,
        char_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            cname = (char_name or self.get_active_char_name()).strip()
            cdata = self.data.setdefault("characters", {}).setdefault(cname, {})
            focus_mode = bool(self.config.get("focus_mode_enabled", False))
            if category_filter in ("daily", "focus", "weekly"):
                cats = [category_filter]
            else:
                cats = ["focus"] if focus_mode else ["daily", "weekly"]
            for cat in cats:
                tasks = self.get_char_tasks(cname, cat)
                cmap = cdata.setdefault(cat, {})
                for t in tasks:
                    if not cmap.get(t, False):
                        cmap[t] = True
                        self._rollback_stack.append((cname, cat, t))
                        if cat == "focus" and all(cmap.get(ft, False) for ft in tasks):
                            cdata["last_focus_completed"] = get_logical_date_str()
                            cdata["weekly_focus_done"] = True
                        self.save_data()
                        return {"ok": True, "message": f"[{cname}] '{t}' 완료 처리되었습니다.", "state": self.get_dashboard_state()}
            return {"ok": False, "message": f"[{cname}] 이미 모든 숙제가 완료되었습니다!", "state": self.get_dashboard_state()}

    def rollback_last_step(self, char_name: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            focus_mode = bool(self.config.get("focus_mode_enabled", False))
            active_cname = (char_name or self.get_active_char_name()).strip()

            # 1. Try popping from _rollback_stack matching current mode (focus vs daily/weekly)
            for idx in range(len(self._rollback_stack) - 1, -1, -1):
                cname, cat, tname = self._rollback_stack[idx]
                if focus_mode and cat != "focus":
                    continue
                if (not focus_mode) and cat == "focus":
                    continue
                cdata = self.data.get("characters", {}).get(cname, {})
                cmap = cdata.get(cat, {})
                if cmap.get(tname, False):
                    self._rollback_stack.pop(idx)
                    cmap[tname] = False
                    if cat == "focus":
                        cdata["weekly_focus_done"] = False
                    self.save_data()
                    return {"ok": True, "message": f"[{cname}] '{tname}' 완료 체크를 되돌렸습니다.", "state": self.get_dashboard_state()}

            # 2. Fallback: uncheck the last completed task in the active category for active_cname
            fallback_cats = ["focus"] if focus_mode else ["weekly", "daily"]
            cdata = self.data.get("characters", {}).get(active_cname, {})
            for cat in fallback_cats:
                tasks = self.get_char_tasks(active_cname, cat)
                cmap = cdata.get(cat, {})
                for tname in reversed(tasks):
                    if cmap.get(tname, False):
                        cmap[tname] = False
                        if cat == "focus":
                            cdata["weekly_focus_done"] = False
                        self.save_data()
                        return {"ok": True, "message": f"[{active_cname}] '{tname}' 완료 체크를 되돌렸습니다.", "state": self.get_dashboard_state()}

            return {"ok": False, "message": "되돌릴 완료 내역이 없습니다.", "state": self.get_dashboard_state()}

    def batch_set_category(
        self,
        char_name_or_category: str,
        category_or_done: Any = None,
        completed: Optional[bool] = None,
        char_name: Optional[str] = None,
        all_characters: bool = False,
        task_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Batch complete or reset a category for one or all characters."""
        with self._lock:
            if char_name_or_category in ("daily", "focus", "weekly", "all"):
                category = char_name_or_category
                done = bool(category_or_done if completed is None else completed)
                target_char = char_name
            else:
                target_char = char_name_or_category
                category = str(category_or_done or "daily")
                done = bool(True if completed is None else completed)

            if category not in ("daily", "focus", "weekly", "all"):
                return self.get_dashboard_state()
            cats = ["daily", "focus", "weekly"] if category == "all" else [category]
            target_chars = (
                [c["name"] for c in self.config.get("characters", [])]
                if all_characters
                else [(target_char or self.get_active_char_name()).strip()]
            )

            for cname in target_chars:
                cdata = self.data.setdefault("characters", {}).setdefault(cname, {})
                for cat in cats:
                    cmap = cdata.setdefault(cat, {})
                    tasks = [task_name] if task_name else self.get_char_tasks(cname, cat)
                    for t in tasks:
                        if cmap.get(t) != done:
                            cmap[t] = done
                    if cat == "focus":
                        if done:
                            ftasks = self.get_char_tasks(cname, "focus")
                            if ftasks and all(cmap.get(ft, False) for ft in ftasks):
                                cdata["last_focus_completed"] = get_logical_date_str()
                                cdata["weekly_focus_done"] = True
                        else:
                            cdata["weekly_focus_done"] = False

            self.save_data()
            return self.get_dashboard_state()

    def batch_set_weekly_task_all_chars(self, task_name: str, completed: bool = True) -> Dict[str, Any]:
        return self.batch_set_category("weekly", completed, all_characters=True, task_name=task_name)

    def save_memo(self, char_name: Optional[str], memo_text: str) -> Dict[str, Any]:
        with self._lock:
            target_name = (char_name or self.get_active_char_name()).strip()
            cdata = self.data.setdefault("characters", {}).setdefault(target_name, {})
            cdata["memo"] = str(memo_text or "")
            self.save_data()
            return self.get_dashboard_state()

    def update_character_profile(
        self,
        index: int,
        name: str,
        job_class: str,
        combat_power: int = 0,
        title: str = "",
    ) -> Tuple[bool, str]:
        with self._lock:
            chars = self.config.get("characters", [])
            if index < 0 or index >= len(chars):
                return False, "수정할 캐릭터 인덱스가 올바르지 않습니다."
            clean_name, parsed_cls = extract_base_char_name(name)
            clean_cls = (job_class or parsed_cls or "미지정").strip()
            if not clean_name:
                return False, "캐릭터 이름을 입력해주세요."

            old_name = chars[index]["name"]
            if clean_name != old_name and any(c["name"] == clean_name for i, c in enumerate(chars) if i != index):
                return False, f"이미 '{clean_name}' 캐릭터가 존재합니다."

            chars[index]["name"] = clean_name
            chars[index]["class"] = clean_cls
            chars[index]["combat_power"] = max(0, int(combat_power or 0))
            chars[index]["title"] = (title or "").strip()

            if old_name != clean_name and old_name in self.data.get("characters", {}):
                self.data["characters"][clean_name] = self.data["characters"].pop(old_name)

            cdata = self.data.setdefault("characters", {}).setdefault(clean_name, {})
            cdata["combat_power"] = max(0, int(combat_power or 0))
            cdata["title"] = (title or "").strip()

            # Update task_character_Mapping if renamed
            if old_name != clean_name:
                for cat in ("daily", "focus", "weekly"):
                    cmap = self.config.get("task_character_Mapping", {}).get(cat, {})
                    for t, clist in cmap.items():
                        if isinstance(clist, list) and old_name in clist:
                            cmap[t] = [clean_name if x == old_name else x for x in clist]

            # Update delivery_store profile name & class
            deliv_cid = self._find_delivery_char_id_by_name(old_name)
            if deliv_cid:
                self.delivery_store.rename_character(deliv_cid, clean_name)
                with self.delivery_store._lock:
                    dc_info = self.delivery_store.characters.get(deliv_cid)
                    if dc_info:
                        if not isinstance(dc_info.get("signature"), dict):
                            dc_info["signature"] = {"job": clean_cls if clean_cls != "미지정" else ""}
                        else:
                            dc_info["signature"]["job"] = clean_cls if clean_cls != "미지정" else ""
                        self.delivery_store.save()

            self._ensure_character_data_locked()
            self.save_config()
            self.save_data()
            return True, f"'{clean_name} ({clean_cls})' 캐릭터 프로필이 저장되었습니다."

    def add_character_profile(self, name: str, job_class: str, combat_power: int = 0, title: str = "") -> Tuple[bool, str]:
        with self._lock:
            clean_name, parsed_cls = extract_base_char_name(name)
            clean_cls = (job_class or parsed_cls or "미지정").strip()
            if not clean_name:
                return False, "추가할 캐릭터 이름을 입력해주세요."
            if any(c["name"] == clean_name for c in self.config.get("characters", [])):
                return False, f"이미 '{clean_name}' 캐릭터가 등록되어 있습니다."

            self.config["characters"].append({
                "name": clean_name,
                "class": clean_cls,
                "combat_power": max(0, int(combat_power or 0)),
                "title": (title or "").strip(),
                "living_score": 0,
                "attractiveness_score": 0,
                "decor_score": 0,
            })
            self._ensure_character_data_locked()
            self.data["current_char_index"] = len(self.config["characters"]) - 1

            # Also add to delivery_store
            created_dc = self.delivery_store.add_character(name=clean_name, init_mode="default_18")
            if created_dc and clean_cls and clean_cls != "미지정":
                with self.delivery_store._lock:
                    created_dc["signature"] = {"job": clean_cls, "level": 0, "living_score": 0, "attractiveness_score": 0, "combat_score": max(0, int(combat_power or 0)), "realm": ""}
                    self.delivery_store.save()

            self.save_config()
            self.save_data()
            return True, f"'{clean_name} ({clean_cls})' 캐릭터가 추가되었습니다."

    def delete_character_profile(self, index: int) -> Tuple[bool, str]:
        with self._lock:
            chars = self.config.get("characters", [])
            if len(chars) <= 1:
                return False, "최소 1명의 캐릭터는 유지되어야 합니다."
            if index < 0 or index >= len(chars):
                return False, "삭제할 캐릭터를 찾을 수 없습니다."
            removed = chars.pop(index)
            rname = removed["name"]
            self.data.get("characters", {}).pop(rname, None)

            deliv_cid = self._find_delivery_char_id_by_name(rname)
            if deliv_cid:
                self.delivery_store.delete_character(deliv_cid)

            self._ensure_character_data_locked()
            self.save_config()
            self.save_data()
            return True, f"'{rname}' 캐릭터 프로필이 삭제되었습니다."

    def move_character_profile(self, index: int, direction: int) -> Tuple[bool, str]:
        with self._lock:
            chars = self.config.get("characters", [])
            new_idx = index + direction
            if index < 0 or index >= len(chars) or new_idx < 0 or new_idx >= len(chars):
                return False, "이동할 수 없는 위치입니다."
            active_name = self.get_active_char_name()
            chars[index], chars[new_idx] = chars[new_idx], chars[index]
            for i, c in enumerate(chars):
                if c["name"] == active_name:
                    self.data["current_char_index"] = i
                    break
            self.sync_with_delivery_store(initial=False)
            self.save_config()
            self.save_data()
            return True, "캐릭터 순서가 변경되었습니다."

    # --- Task List & Per-Character Filter Management ---

    def manage_task_item(
        self,
        action: str,
        category: str,
        task_name: str = "",
        new_name: str = "",
        index: int = -1,
        direction: int = 0,
        target_characters: Optional[List[str]] = None,
    ) -> Tuple[bool, str]:
        with self._lock:
            if category not in ("daily", "focus", "weekly"):
                return False, "유효하지 않은 카테고리입니다."
            t_key = f"{category}_tasks"
            t_list: List[str] = self.config.setdefault(t_key, [])
            cmap: Dict[str, List[str]] = self.config.setdefault("task_character_Mapping", {}).setdefault(category, {})

            if action == "add":
                clean = (task_name or "").strip()
                if not clean:
                    return False, "숙제 항목 이름을 입력해주세요."
                if clean in t_list:
                    return False, f"이미 '{clean}' 항목이 존재합니다."
                t_list.append(clean)
                if target_characters is not None:
                    cmap[clean] = [c for c in target_characters if c]
                self._ensure_character_data_locked()
                self.save_config()
                self.save_data()
                return True, f"'{clean}' 숙제 항목이 추가되었습니다."

            elif action == "edit":
                old_t = t_list[index] if (0 <= index < len(t_list)) else (task_name or "").strip()
                clean_new = (new_name or task_name or "").strip()
                if not old_t or old_t not in t_list or not clean_new:
                    return False, "수정할 숙제 항목을 찾을 수 없습니다."
                idx = t_list.index(old_t)
                t_list[idx] = clean_new
                if old_t != clean_new:
                    if old_t in cmap:
                        cmap[clean_new] = cmap.pop(old_t)
                    for cdata in self.data.get("characters", {}).values():
                        cdict = cdata.get(category, {})
                        if old_t in cdict:
                            cdict[clean_new] = cdict.pop(old_t)
                if target_characters is not None:
                    cmap[clean_new] = [c for c in target_characters if c]
                self._ensure_character_data_locked()
                self.save_config()
                self.save_data()
                return True, f"'{clean_new}' 숙제 설정이 저장되었습니다."

            elif action == "delete":
                target_t = t_list[index] if (0 <= index < len(t_list)) else (task_name or "").strip()
                if not target_t or target_t not in t_list:
                    return False, "삭제할 숙제 항목을 찾을 수 없습니다."
                t_list.remove(target_t)
                cmap.pop(target_t, None)
                for cdata in self.data.get("characters", {}).values():
                    cdata.get(category, {}).pop(target_t, None)
                self.save_config()
                self.save_data()
                return True, f"'{target_t}' 숙제 항목이 삭제되었습니다."

            elif action == "move":
                idx = index if (0 <= index < len(t_list)) else (t_list.index(task_name) if task_name in t_list else -1)
                new_idx = idx + direction
                if idx < 0 or new_idx < 0 or new_idx >= len(t_list):
                    return False, "이동할 수 없는 위치입니다."
                t_list[idx], t_list[new_idx] = t_list[new_idx], t_list[idx]
                self.save_config()
                return True, "숙제 항목 순서가 변경되었습니다."

            elif action == "set_targets":
                target_t = (task_name or "").strip()
                if not target_t or target_t not in t_list:
                    return False, "대상 숙제 항목을 찾을 수 없습니다."
                cmap[target_t] = [c for c in (target_characters or []) if c]
                self.save_config()
                return True, f"'{target_t}' 캐릭터별 적용 필터가 저장되었습니다."

            elif action == "reset_defaults":
                defaults = {
                    "daily": DEFAULT_DAILY_TASKS,
                    "focus": DEFAULT_FOCUS_TASKS,
                    "weekly": DEFAULT_WEEKLY_TASKS,
                }[category]
                self.config[t_key] = list(defaults)
                self.config["task_character_Mapping"][category] = {}
                self._ensure_character_data_locked()
                self.save_config()
                self.save_data()
                return True, "기본 숙제 목록으로 복원되었습니다."

            return False, "지원하지 않는 작업입니다."

    def update_homework_settings(self, new_settings: Dict[str, Any]) -> Tuple[bool, str]:
        with self._lock:
            bool_keys = (
                "auto_reset_enabled",
                "focus_mode_enabled",
                "omen_barrier_alert",
                "sound_alert_enabled",
                "ai_auto_char_detect",
                "ai_auto_task_check",
                "webhook_notify_dungeon",
                "webhook_notify_abyss",
                "webhook_notify_hunting",
                "webhook_notify_gathering",
                "webhook_notify_crafting",
            )
            for k in bool_keys:
                if k in new_settings:
                    self.config[k] = bool(new_settings[k])
            if "webhook_notify_abyss" in new_settings:
                self.config["webhook_notify_Abyss"] = bool(new_settings["webhook_notify_abyss"])

            if "auto_detect_character" in new_settings:
                sw = bool(new_settings["auto_detect_character"])
                self.config["ai_auto_char_detect"] = sw
                self.delivery_store.set_auto_switch(sw)
            if "auto_check_missions" in new_settings:
                self.config["ai_auto_task_check"] = bool(new_settings["auto_check_missions"])

            if "webhook_events" in new_settings and isinstance(new_settings["webhook_events"], dict):
                ev = new_settings["webhook_events"]
                if "dungeon" in ev:
                    self.config["webhook_notify_dungeon"] = bool(ev["dungeon"])
                if "abyss" in ev:
                    self.config["webhook_notify_abyss"] = bool(ev["abyss"])
                    self.config["webhook_notify_Abyss"] = bool(ev["abyss"])
                if "hunting_ground" in ev:
                    self.config["webhook_notify_hunting"] = bool(ev["hunting_ground"])
                if "gathering" in ev:
                    self.config["webhook_notify_gathering"] = bool(ev["gathering"])
                if "crafting" in ev:
                    self.config["webhook_notify_crafting"] = bool(ev["crafting"])

            if "discord_webhook_url" in new_settings:
                url = str(new_settings.get("discord_webhook_url") or "").strip()
                self.config["discord_webhook_url"] = url
                self.settings_store.set("discord_webhook_url", url)

            if "auto_switch_character" in new_settings:
                sw = bool(new_settings["auto_switch_character"])
                self.config["ai_auto_char_detect"] = sw
                self.delivery_store.set_auto_switch(sw)

            self.save_config()
            return True, "숙제 및 알림 설정이 저장되었습니다."

    def update_settings(self, new_settings: Dict[str, Any]) -> Dict[str, Any]:
        ok, msg = self.update_homework_settings(new_settings)
        return {"ok": ok, "message": msg, "state": self.get_dashboard_state()}

    # --- Convenience Web API Wrappers ---

    def _find_char_idx(self, name: str) -> int:
        bname, _ = extract_base_char_name(name)
        for idx, c in enumerate(self.config.get("characters", [])):
            if c.get("name") == bname or c.get("name") == name:
                return idx
        return -1

    def add_character(self, name: str, char_class: str = "", combat_power: Any = 0, title: str = "") -> Dict[str, Any]:
        ok, msg = self.add_character_profile(name, char_class, int(combat_power or 0), title)
        return {"ok": ok, "message": msg, "state": self.get_dashboard_state()}

    def update_character(self, old_name: str, new_name: str, char_class: Optional[str] = None, combat_power: Any = None, title: Optional[str] = None) -> Dict[str, Any]:
        idx = self._find_char_idx(old_name)
        if idx < 0:
            return {"ok": False, "message": "수정할 캐릭터를 찾을 수 없습니다.", "state": self.get_dashboard_state()}
        cur = self.config["characters"][idx]
        ok, msg = self.update_character_profile(
            idx,
            new_name or old_name,
            char_class if char_class is not None else cur.get("class", ""),
            int(combat_power if combat_power is not None else cur.get("combat_power", 0)),
            title if title is not None else cur.get("title", ""),
        )
        return {"ok": ok, "message": msg, "state": self.get_dashboard_state()}

    def delete_character(self, name: str) -> Dict[str, Any]:
        idx = self._find_char_idx(name)
        ok, msg = self.delete_character_profile(idx)
        return {"ok": ok, "message": msg, "state": self.get_dashboard_state()}

    def move_character(self, name: str, direction: int) -> Dict[str, Any]:
        idx = self._find_char_idx(name)
        ok, msg = self.move_character_profile(idx, direction)
        return {"ok": ok, "message": msg, "state": self.get_dashboard_state()}

    def add_task(self, category: str, task_name: str) -> Dict[str, Any]:
        ok, msg = self.manage_task_item("add", category, task_name=task_name)
        return {"ok": ok, "message": msg, "state": self.get_dashboard_state()}

    def rename_task(self, category: str, old_name: str, new_name: str) -> Dict[str, Any]:
        ok, msg = self.manage_task_item("edit", category, task_name=old_name, new_name=new_name)
        return {"ok": ok, "message": msg, "state": self.get_dashboard_state()}

    def delete_task(self, category: str, task_name: str) -> Dict[str, Any]:
        ok, msg = self.manage_task_item("delete", category, task_name=task_name)
        return {"ok": ok, "message": msg, "state": self.get_dashboard_state()}

    def move_task(self, category: str, task_name: str, direction: int) -> Dict[str, Any]:
        ok, msg = self.manage_task_item("move", category, task_name=task_name, direction=direction)
        return {"ok": ok, "message": msg, "state": self.get_dashboard_state()}

    def set_task_target_characters(self, category: str, task_name: str, target_chars: Optional[List[str]]) -> Dict[str, Any]:
        ok, msg = self.manage_task_item("set_targets", category, task_name=task_name, target_characters=target_chars or [])
        return {"ok": ok, "message": msg, "state": self.get_dashboard_state()}

    def reset_category_default_tasks(self, category: str) -> Dict[str, Any]:
        ok, msg = self.manage_task_item("reset_defaults", category)
        return {"ok": ok, "message": msg, "state": self.get_dashboard_state()}

    def export_backup_bundle(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "exported_at": datetime.now(KST_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                "homework_config": copy.deepcopy(self.config),
                "homework_data": copy.deepcopy(self.data),
            }

    def import_backup_bundle(self, bundle: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            if not isinstance(bundle, dict):
                return {"ok": False, "message": "올바른 백업 JSON 형식이 아닙니다.", "state": self.get_dashboard_state()}
            if "homework_config" in bundle and isinstance(bundle["homework_config"], dict):
                self.config.update(bundle["homework_config"])
            elif "characters" in bundle and "daily_tasks" in bundle:
                self.config.update(bundle)

            if "homework_data" in bundle and isinstance(bundle["homework_data"], dict):
                self.data.update(bundle["homework_data"])

            self._ensure_character_data_locked()
            self.sync_with_delivery_store(initial=False)
            self.save_config()
            self.save_data()
            return {"ok": True, "message": "숙제 설정 및 진행 데이터가 성공적으로 복원되었습니다.", "state": self.get_dashboard_state()}

    def reimport_from_legacy_overlay(self) -> Dict[str, Any]:
        with self._lock:
            legacy_cfg, legacy_dat = self._find_legacy_overlay_files()
            if not legacy_cfg and not legacy_dat:
                return {"ok": False, "message": "내 문서에서 기존 '모비 숙제 오버레이' 설정/데이터 파일을 찾을 수 없습니다.", "state": self.get_dashboard_state()}
            try:
                if legacy_cfg:
                    with open(legacy_cfg, "r", encoding="utf-8") as f:
                        c = json.load(f)
                        if isinstance(c, dict):
                            self.config.update(c)
                if legacy_dat:
                    with open(legacy_dat, "r", encoding="utf-8") as f:
                        d = json.load(f)
                        if isinstance(d, dict):
                            self.data.update(d)
                self._ensure_character_data_locked()
                self.sync_with_delivery_store(initial=True)
                self.save_config()
                self.save_data()
                return {"ok": True, "message": "기존 '모비 숙제 오버레이'의 캐릭터 및 숙제 데이터를 성공적으로 가져왔습니다!", "state": self.get_dashboard_state()}
            except Exception as e:
                return {"ok": False, "message": f"가져오기 중 오류 발생: {e}", "state": self.get_dashboard_state()}
