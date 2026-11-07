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

from mabi_cli import MabinogiCLI, MabinogiCLIError, get_user_data_path

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
    {"name": "카르브", "class": "수도사", "combat_power": 0, "title": ""},
    {"name": "티세", "class": "전사", "combat_power": 0, "title": ""},
    {"name": "베닐", "class": "빙결술사", "combat_power": 0, "title": ""},
    {"name": "라으", "class": "대검전사", "combat_power": 0, "title": ""},
    {"name": "일리브", "class": "격투가", "combat_power": 0, "title": ""},
    {"name": "데즈", "class": "사제", "combat_power": 0, "title": ""},
]

DEFAULT_DAILY_TASKS: List[str] = [
    "정기 의뢰 [J]",
    "검은 구멍 [M]",
    "요일 던전 [J]",
    "성수·럭키 찬스 [J]",
    "재료 채집 [K]",
    "재료 가공 [K]",
    "도구 수리 [.]",
    "보관함 [C]",
    "일일 미션 [N]",
    "레드닷 확인 [ESC]",
]

DEFAULT_FOCUS_TASKS: List[str] = [
    "던전 토벌 [/]",
    "심층 던전 토벌 [/]",
    "소모품 정리 [I]",
    "일반패션 럭키박스 [Y]",
    "보석 연금술",
    "성능 개선",
    "원석 변환",
    "장비 분해",
    "룬 승급",
    "패션 장비 승급",
    "성수 물물교환",
    "공방",
    "보관함",
    "대장간",
]

DEFAULT_WEEKLY_TASKS: List[str] = [
    "소환의 결계",
    "검은 구멍",
    "필드 보스",
    "레이드: 카브락",
    "레이드: 에이렐",
    "레이드: 화이트 서큐버스",
    "어비스: 허상의 정박지",
    "어비스: 광기의 동굴",
    "어비스: 흩어진 물길",
    "뱅가드 브리치",
]


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
    Splits a label like '카르브 (수도사)' into ('카르브', '수도사').
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
        self._live_telemetry: Dict[str, Any] = {
            "currencies": [],
            "daily_missions": [],
            "weekly_missions": [],
            "quests": [],
            "altering_summary": {"ready": 0, "total": 0, "works": []},
            "omen_barrier": {},
            "updated_at": 0,
        }
        self._prev_state: Dict[str, Any] = {
            "char_name": None,
            "is_gathering": False,
            "gather_target": "",
            "is_crafting": False,
            "craft_action": "",
            "dungeon_state": "",
            "dungeon_name": "",
            "abyss_result_playing": False,
            "abyss_mission_counts": {},
            "goddess_loot_ready": set(),
            "regular_quest_counts": {},
            "altering_ready": 0,
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

    def save_config(self):
        with self._lock:
            try:
                with open(self.config_file, "w", encoding="utf-8") as f:
                    json.dump(self.config, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

    def save_data(self):
        with self._lock:
            try:
                with open(self.data_file, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

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
        Picks among characters who still have incomplete Focus tasks, prioritizing
        those with no `last_focus_completed` or the oldest `last_focus_completed` date.
        """
        with self._lock:
            candidates: List[Tuple[str, str, int]] = []
            for idx, hc in enumerate(self.config.get("characters", [])):
                cname = hc["name"]
                ftasks = self.get_char_tasks(cname, "focus")
                if not ftasks:
                    continue
                fdict = self.data.get("characters", {}).get(cname, {}).get("focus", {})
                done_cnt = sum(1 for t in ftasks if fdict.get(t, False))
                if done_cnt < len(ftasks):
                    last_dt = str(self.data.get("characters", {}).get(cname, {}).get("last_focus_completed") or "")
                    # Empty string sorts before any 'YYYY-MM-DD' date
                    candidates.append((last_dt, cname, idx))
            if not candidates:
                return None
            candidates.sort(key=lambda x: (x[0], x[2]))
            return candidates[0][1]

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
            label = f"🔥 결계 진행 중! ({now.hour:02d}시 정각 스폰 · {minute}분 경과)"
            short_label = f"🔥 결계 진행중 ({minute}분)"
            level = "active"
        elif minute >= 55:
            rem_m = 59 - minute
            rem_s = 60 - second
            label = f"⚡ {next_hour:02d}시 불길한 소환의 결계 임박! ({rem_m}분 {rem_s:02d}초 전)"
            short_label = f"⚡ 결계 임박 {rem_m}:{rem_s:02d}"
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

        for t_name in char_tasks:
            matched = (t_name == keyword_or_exact) if exact else (keyword_or_exact in t_name)
            if matched:
                prev_val = bool(cat_dict.get(t_name, False))
                if prev_val != value:
                    cat_dict[t_name] = value
                    changed_tasks.append(t_name)

        if changed_tasks:
            if category == "focus" and value:
                if all(cat_dict.get(t, False) for t in char_tasks):
                    cdata["last_focus_completed"] = get_logical_date_str()
            self.save_data()
            for t in changed_tasks:
                cat_ko = {"daily": "일일", "focus": "집중", "weekly": "주간"}.get(category, category)
                state_ko = "완료 체크" if value else "체크 해제"
                msg = f"[{char_name}] {cat_ko} 숙제 '{t}' 자동 {state_ko}"
                if reason:
                    msg += f" ({reason})"
                self._log("success" if value else "info", msg)
        return changed_tasks

    def update_from_ingame_state(
        self,
        my_info: Optional[Dict[str, Any]],
        activity: Optional[Dict[str, Any]],
        inventory: Optional[Dict[str, Any]],
        environment: Optional[Dict[str, Any]],
        is_busy: bool = False,
    ):
        """
        Called by `get_cached_status()` when live CLI status is fetched.
        Updates character stats, performs smart character auto-detection/switching,
        and polls missions/quests/currencies/altering works on a lightweight 8s cadence
        to auto-check homework tasks and trigger Discord notifications.
        """
        self.check_auto_reset()
        now = time.time()

        # Poll extended telemetry (currencies, missions, quests, altering) at most every 8 seconds when not busy
        need_extended = (not is_busy) and ((now - self._last_extended_poll_ts) >= 8.0)
        currencies = self._live_telemetry.get("currencies", [])
        daily_missions = self._live_telemetry.get("daily_missions", [])
        weekly_missions = self._live_telemetry.get("weekly_missions", [])
        quests = self._live_telemetry.get("quests", [])
        altering_summary = self._live_telemetry.get("altering_summary", {"ready": 0, "total": 0, "works": []})

        if need_extended:
            self._last_extended_poll_ts = now
            try:
                currencies = self.cli.get_currencies()
            except Exception:
                pass
            try:
                daily_missions = self.cli.get_daily_missions()
            except Exception:
                pass
            try:
                weekly_missions = self.cli.get_weekly_missions()
            except Exception:
                pass
            try:
                quests = self.cli.get_quests()
            except Exception:
                pass
            try:
                alt_raw = self.cli.get_altering_works()
                works_list = alt_raw.get("works", []) if isinstance(alt_raw, dict) else []
                ready_cnt = sum(1 for w in works_list if w.get("IsCompleted") or int(w.get("RemainingSeconds", 0) or 0) <= 0)
                altering_summary = {
                    "ready": ready_cnt,
                    "total": len(works_list),
                    "works": works_list,
                }
            except Exception:
                pass

            with self._lock:
                prev_matched = self._live_telemetry.get("matched_char_name") or ""
                self._live_telemetry = {
                    "matched_char_name": prev_matched,
                    "currencies": currencies,
                    "daily_missions": daily_missions,
                    "weekly_missions": weekly_missions,
                    "quests": quests,
                    "altering_summary": altering_summary,
                    "omen_barrier": self.get_omen_barrier_status(),
                    "updated_at": int(now * 1000),
                }

        if not my_info:
            return

        with self._lock:
            resolved_name = self.resolve_character(my_info)
            if not resolved_name and self.delivery_store.matched_ingame_character_id:
                dc_matched = self.delivery_store.characters.get(self.delivery_store.matched_ingame_character_id)
                if dc_matched:
                    bname, _ = extract_base_char_name(dc_matched.get("name", ""))
                    if bname:
                        resolved_name = bname

            self._live_telemetry["matched_char_name"] = resolved_name or ""

            if resolved_name:
                deliv_cid = self._find_delivery_char_id_by_name(resolved_name)
                if deliv_cid:
                    with self.delivery_store._lock:
                        self.delivery_store.matched_ingame_character_id = deliv_cid

            active_name = self.get_active_char_name()

            # Auto character switch if enabled
            if (
                resolved_name
                and self.config.get("ai_auto_char_detect", True)
                and not is_busy
                and resolved_name != self._prev_state.get("char_name")
            ):
                self._prev_state["char_name"] = resolved_name
                for idx, hc in enumerate(self.config.get("characters", [])):
                    if hc["name"] == resolved_name:
                        if self.data.get("current_char_index") != idx:
                            self.data["current_char_index"] = idx
                            self.save_data()
                            self._log("info", f"인게임 접속 캐릭터 감지 -> '{resolved_name}({hc.get('class', '')})' 프로필로 자동 전환되었습니다.")
                        # Also sync delivery_store active character!
                        deliv_cid = self._find_delivery_char_id_by_name(resolved_name)
                        if deliv_cid and self.delivery_store.active_character_id != deliv_cid:
                            self.delivery_store.select_character(deliv_cid)
                        active_name = resolved_name
                        break

            target_char_name = resolved_name or active_name
            cdata = self.data.setdefault("characters", {}).setdefault(target_char_name, {})
            cdata["updated_at"] = datetime.now(KST_TZ).strftime("%H:%M:%S")

            # Update character stats from my_info & inventory & currencies
            cp = self._extract_stat_int(my_info.get("CombatScore") or my_info.get("CombatPower"))
            ls = self._extract_stat_int(my_info.get("LivingScore"))
            cs = self._extract_stat_int(my_info.get("AttractivenessScore"))
            ds = self._extract_stat_int(my_info.get("DecorScore"))
            lvl = self._extract_stat_int(my_info.get("Level"))
            title = str(my_info.get("Title") or my_info.get("EquippedTitleName") or "").strip()
            job_disp = str(my_info.get("EnabledCombatJobDisplayName") or "").strip()

            stats_dirty = False
            for hc in self.config.get("characters", []):
                if hc["name"] == target_char_name:
                    if cp > 0 and hc.get("combat_power") != cp:
                        hc["combat_power"] = cp
                        cdata["combat_power"] = cp
                        stats_dirty = True
                    if ls > 0 and hc.get("living_score") != ls:
                        hc["living_score"] = ls
                        cdata["living_score"] = ls
                        stats_dirty = True
                    if cs > 0 and hc.get("attractiveness_score") != cs:
                        hc["attractiveness_score"] = cs
                        cdata["attractiveness_score"] = cs
                        stats_dirty = True
                    if ds > 0 and hc.get("decor_score") != ds:
                        hc["decor_score"] = ds
                        cdata["decor_score"] = ds
                        stats_dirty = True
                    if title and hc.get("title") != title:
                        hc["title"] = title
                        cdata["title"] = title
                        stats_dirty = True
                    if job_disp and hc.get("class") in ("", "미지정"):
                        hc["class"] = job_disp
                        stats_dirty = True
                    break

            if lvl > 0 and cdata.get("level") != lvl:
                cdata["level"] = lvl
                stats_dirty = True

            vitals = my_info.get("Vitals") if isinstance(my_info.get("Vitals"), dict) else {}
            cur_w = 0.0
            max_w = 0.0
            if inventory and isinstance(inventory, dict):
                cur_w = float(inventory.get("CurrentInventoryWeightAsDecimal", 0) or inventory.get("CurrentInventoryWeight", 0) or 0)
                max_w = float(inventory.get("MaxInventoryWeightAsDecimal", 0) or inventory.get("MaxInventoryWeight", 0) or 0)
            if max_w <= 0 and vitals:
                cur_w = float(vitals.get("InventoryWeightCurrent", 0) or 0)
                max_w = float(vitals.get("InventoryWeightMax", 0) or 0)
            if max_w > 0:
                pct = int(round((cur_w / max_w) * 100))
                w_str = f"{cur_w:.0f}/{max_w:.0f}"
                if cdata.get("bag_weight_pct") != pct or cdata.get("bag_weight_str") != w_str:
                    cdata["bag_weight_pct"] = pct
                    cdata["bag_weight_str"] = w_str
                    stats_dirty = True

            if currencies and isinstance(currencies, list):
                curr_map = {}
                for item in currencies:
                    if not isinstance(item, dict):
                        continue
                    cname_cur = str(item.get("Name") or item.get("DisplayName") or "").strip()
                    amt = int(item.get("Amount") or item.get("Count") or 0)
                    max_amt = int(item.get("MaxAmount") or 0)
                    if any(k in cname_cur for k in ("은동전", "마족 공물", "골드", "데카", "날개", "주화")):
                        curr_map[cname_cur] = f"{amt}/{max_amt}" if max_amt > 0 else amt
                if curr_map and cdata.get("currencies") != curr_map:
                    cdata["currencies"] = curr_map
                    stats_dirty = True

            if altering_summary:
                ar = int(altering_summary.get("ready") or 0)
                at = int(altering_summary.get("total") or 0)
                if cdata.get("altering_ready") != ar or cdata.get("altering_total") != at:
                    cdata["altering_ready"] = ar
                    cdata["altering_total"] = at
                    stats_dirty = True

            if stats_dirty:
                self.save_config()
                self.save_data()

            # Auto-task check & event triggers
            if not self.config.get("ai_auto_task_check", True):
                return

            # 1) Daily Missions Bidirectional Sync ('검은 구멍 [M]', '일일 미션 [N]')
            if daily_missions and isinstance(daily_missions, list):
                claimed_rewards = sum(1 for m in daily_missions if isinstance(m, dict) and m.get("IsRewardReceived"))
                for m in daily_missions:
                    if not isinstance(m, dict):
                        continue
                    m_title = str(m.get("Title") or "").replace(" ", "")
                    is_comp = bool(m.get("IsCompleted")) or bool(m.get("IsRewardReceived"))
                    if "검은구멍" in m_title and is_comp:
                        self._auto_mark_task_locked(target_char_name, "daily", "검은 구멍", True, "인게임 일일 미션 달성")
                if claimed_rewards >= 6:
                    self._auto_mark_task_locked(target_char_name, "daily", "일일 미션", True, f"일일 미션 보상 {claimed_rewards}개 수령")

            # 2) Activity Transitions (Dungeon Clear, Abyss Clear, Gathering, Crafting)
            act = activity if isinstance(activity, dict) else {}
            dungeon_info = act.get("Dungeon") if isinstance(act.get("Dungeon"), dict) else {}
            cur_d_state = str(dungeon_info.get("State") or "")
            cur_d_name = str(dungeon_info.get("Name") or "")
            cur_abyss_seq = bool(dungeon_info.get("IsAbyssResultSequencePlaying", False))

            prev_d_state = str(self._prev_state.get("dungeon_state") or "")
            prev_abyss_seq = bool(self._prev_state.get("abyss_result_playing", False))

            # General Dungeon / Raid / Field Boss Clear
            if cur_d_state == "Cleared" and prev_d_state and prev_d_state != "Cleared":
                d_label = cur_d_name or "던전"
                self._auto_mark_task_locked(target_char_name, "daily", "정기 의뢰", True, f"{d_label} 클리어")
                if any(k in d_label for k in ("찬란한 유적", "빛나는 동굴", "눈부신 저택", "요일")):
                    self._auto_mark_task_locked(target_char_name, "daily", "요일 던전", True, f"{d_label} 클리어")
                if any(k in d_label for k in ("페리", "크라브바흐", "크라마", "드로흐에넴", "토르모그", "앙그르바한", "필드 보스", "필드보스")):
                    self._auto_mark_task_locked(target_char_name, "weekly", "필드 보스", True, f"{d_label} 처치")
                if "카브락" in d_label:
                    self._auto_mark_task_locked(target_char_name, "weekly", "레이드: 카브락", True, f"{d_label} 클리어")
                if "에이렐" in d_label:
                    self._auto_mark_task_locked(target_char_name, "weekly", "레이드: 에이렐", True, f"{d_label} 클리어")
                if "서큐버스" in d_label:
                    self._auto_mark_task_locked(target_char_name, "weekly", "레이드: 화이트 서큐버스", True, f"{d_label} 클리어")

                if self.config.get("webhook_notify_dungeon", True):
                    threading.Thread(
                        target=self.send_homework_discord_webhook,
                        args=(
                            "⚔️ 던전 클리어 알림",
                            f"**[{target_char_name}]** 캐릭터가 **{d_label}** 클리어를 완료했습니다!",
                            0x3498DB,
                        ),
                        daemon=True,
                    ).start()

            # Abyss Clear Detection (Sequence or Dungeon Cleared on Abyss map)
            abyss_cleared_name = ""
            if cur_abyss_seq and not prev_abyss_seq:
                abyss_cleared_name = cur_d_name or "어비스 던전"
            elif cur_d_state == "Cleared" and prev_d_state != "Cleared" and any(
                k in cur_d_name for k in ("허상의 정박지", "광기의 동굴", "흩어진 물길", "어비스")
            ):
                abyss_cleared_name = cur_d_name

            # Also check weekly_missions abyss count increase
            if weekly_missions and isinstance(weekly_missions, list):
                prev_ab_counts = self._prev_state.get("abyss_mission_counts", {})
                new_ab_counts = {}
                for wm in weekly_missions:
                    if not isinstance(wm, dict):
                        continue
                    wtitle = str(wm.get("Title") or "")
                    if "어비스" in wtitle:
                        mid = wm.get("MissionId") or wtitle
                        prog = int(wm.get("Progress") or 0)
                        new_ab_counts[mid] = prog
                        if mid in prev_ab_counts and prog > prev_ab_counts[mid] and not abyss_cleared_name:
                            abyss_cleared_name = cur_d_name or wtitle
                self._prev_state["abyss_mission_counts"] = new_ab_counts

            if abyss_cleared_name:
                if "허상의 정박지" in abyss_cleared_name:
                    self._auto_mark_task_locked(target_char_name, "weekly", "어비스: 허상의 정박지", True, "어비스 클리어")
                elif "광기의 동굴" in abyss_cleared_name:
                    self._auto_mark_task_locked(target_char_name, "weekly", "어비스: 광기의 동굴", True, "어비스 클리어")
                elif "흩어진 물길" in abyss_cleared_name:
                    self._auto_mark_task_locked(target_char_name, "weekly", "어비스: 흩어진 물길", True, "어비스 클리어")
                else:
                    # Mark first unchecked abyss task
                    for ab_task in ("어비스: 허상의 정박지", "어비스: 광기의 동굴", "어비스: 흩어진 물길"):
                        if not cdata.get("weekly", {}).get(ab_task, False):
                            self._auto_mark_task_locked(target_char_name, "weekly", ab_task, True, f"{abyss_cleared_name} 클리어")
                            break

                if self.config.get("webhook_notify_abyss", True) or self.config.get("webhook_notify_Abyss", True):
                    threading.Thread(
                        target=self.send_homework_discord_webhook,
                        args=(
                            "🟣 어비스 클리어 알림",
                            f"**[{target_char_name}]** 캐릭터가 **{abyss_cleared_name}** 클리어를 완료했습니다!",
                            0x9B59B6,
                        ),
                        daemon=True,
                    ).start()

            self._prev_state["dungeon_state"] = cur_d_state
            self._prev_state["dungeon_name"] = cur_d_name
            self._prev_state["abyss_result_playing"] = cur_abyss_seq

            # 3) Hunting Ground / Goddess Mission / Guild Regular Quest Detection
            if quests and isinstance(quests, list):
                cur_goddess_ready = set()
                cur_reg_counts = {}
                for q in quests:
                    if not isinstance(q, dict):
                        continue
                    q_src = str(q.get("Source") or "")
                    q_src_disp = str(q.get("SourceDisplayName") or "")
                    q_title = str(q.get("QuestTitle") or "")
                    objs = q.get("Objectives") if isinstance(q.get("Objectives"), list) else []

                    if q_src == "goddess_mission" or "지역 임무" in q_src_disp:
                        for obj in objs:
                            if not isinstance(obj, dict):
                                continue
                            desc = str(obj.get("Description") or "")
                            if "여신상에서 전리품 받기" in desc and obj.get("IsActive") and not obj.get("IsCompleted"):
                                cur_goddess_ready.add(q_title)

                    if q_src == "guild" or "길드" in q_src_disp or "정기" in q_title:
                        for obj in objs:
                            if not isinstance(obj, dict):
                                continue
                            desc = str(obj.get("Description") or "")
                            if "던전 클리어" in desc or "사냥터" in desc:
                                key = f"{q_title}:{desc}"
                                cnt = int(obj.get("Count") or 0)
                                cur_reg_counts[key] = cnt

                prev_goddess = self._prev_state.get("goddess_loot_ready", set())
                newly_ready_goddess = cur_goddess_ready - prev_goddess
                if newly_ready_goddess:
                    q_label = ", ".join(sorted(newly_ready_goddess))
                    self._auto_mark_task_locked(target_char_name, "daily", "정기 의뢰", True, f"사냥터 임무 달성: {q_label}")
                    if self.config.get("webhook_notify_hunting", True):
                        threading.Thread(
                            target=self.send_homework_discord_webhook,
                            args=(
                                "🏹 사냥터 지역 임무 완료 알림",
                                f"**[{target_char_name}]** **{q_label}** 목표 달성! 여신상에서 전리품을 수령하세요.",
                                0x2ECC71,
                            ),
                            daemon=True,
                        ).start()

                prev_reg = self._prev_state.get("regular_quest_counts", {})
                for k, cnt in cur_reg_counts.items():
                    if k in prev_reg and cnt > prev_reg[k]:
                        self._auto_mark_task_locked(target_char_name, "daily", "정기 의뢰", True, f"정기 의뢰 진행 ({k})")
                self._prev_state["goddess_loot_ready"] = cur_goddess_ready
                self._prev_state["regular_quest_counts"] = cur_reg_counts

            # 4) Gathering & Altering Completion Detection
            cur_gathering = bool(act.get("IsGathering", False))
            prev_gathering = bool(self._prev_state.get("is_gathering", False))
            if cur_gathering:
                self._prev_state["gather_target"] = str(act.get("TargetName") or act.get("ActionName") or "자원")
            elif prev_gathering and not cur_gathering:
                g_target = self._prev_state.get("gather_target") or "자원"
                self._auto_mark_task_locked(target_char_name, "daily", "재료 채집", True, f"{g_target} 채집 진행")
                if self.config.get("webhook_notify_gathering", False) and not is_busy:
                    threading.Thread(
                        target=self.send_homework_discord_webhook,
                        args=(
                            "🌿 채집 활동 종료 알림",
                            f"**[{target_char_name}]** 캐릭터의 **{g_target}** 채집 활동이 완료(또는 중단)되었습니다.",
                            0x2ECC71,
                        ),
                        daemon=True,
                    ).start()
            self._prev_state["is_gathering"] = cur_gathering

            cur_alt_ready = int(altering_summary.get("ready") or 0)
            prev_alt_ready = int(self._prev_state.get("altering_ready") or 0)
            if cur_alt_ready > prev_alt_ready:
                self._auto_mark_task_locked(target_char_name, "daily", "재료 가공", True, f"가공 완료 {cur_alt_ready}건")
            self._prev_state["altering_ready"] = cur_alt_ready

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

            return {
                "active_char_index": active_idx,
                "active_char_name": active_name,
                "active_character": active_name,
                "active_character_detail": active_summary,
                "recommended_focus_char": recommended_focus_char,
                "recommended_focus_character": recommended_focus_char,
                "matched_ingame_character": self._live_telemetry.get("matched_char_name") or "",
                "step_guide": step_info,
                "active_step": active_step_compat,
                "can_rollback": len(self._rollback_stack) > 0,
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
        for cat, cat_label in (("daily", "일일 숙제"), ("focus", "집중 숙제"), ("weekly", "주간 숙제")):
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
            "category_label": "모든 숙제 완료",
            "task": "현재 캐릭터의 일일·집중·주간 숙제를 모두 마쳤습니다!",
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

            # Update last_focus_completed if all focus tasks for target_name are now done
            if category == "focus":
                ftasks = self.get_char_tasks(target_name, "focus")
                if ftasks and all(cat_dict.get(t, False) for t in ftasks):
                    cdata["last_focus_completed"] = get_logical_date_str()

            self.save_data()
            return self.get_dashboard_state()

    def complete_current_step(
        self,
        category_filter: Optional[str] = None,
        char_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            cname = (char_name or self.get_active_char_name()).strip()
            cdata = self.data.setdefault("characters", {}).setdefault(cname, {})
            cats = [category_filter] if category_filter in ("daily", "focus", "weekly") else ["daily", "focus", "weekly"]
            for cat in cats:
                tasks = self.get_char_tasks(cname, cat)
                cmap = cdata.setdefault(cat, {})
                for t in tasks:
                    if not cmap.get(t, False):
                        cmap[t] = True
                        self._rollback_stack.append((cname, cat, t))
                        if cat == "focus" and all(cmap.get(ft, False) for ft in tasks):
                            cdata["last_focus_completed"] = get_logical_date_str()
                        self.save_data()
                        return {"ok": True, "message": f"[{cname}] '{t}' 완료 처리되었습니다.", "state": self.get_dashboard_state()}
            return {"ok": False, "message": f"[{cname}] 이미 모든 숙제가 완료되었습니다!", "state": self.get_dashboard_state()}

    def rollback_last_step(self, char_name: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            while self._rollback_stack:
                cname, cat, tname = self._rollback_stack.pop()
                cdata = self.data.get("characters", {}).get(cname, {})
                cmap = cdata.get(cat, {})
                if cmap.get(tname, False):
                    cmap[tname] = False
                    self.save_data()
                    return {"ok": True, "message": f"[{cname}] '{tname}' 완료 체크를 되돌렸습니다.", "state": self.get_dashboard_state()}
            return {"ok": False, "message": "되돌릴 최근 완료 내역이 없습니다.", "state": self.get_dashboard_state()}

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
                    if cat == "focus" and done:
                        ftasks = self.get_char_tasks(cname, "focus")
                        if ftasks and all(cmap.get(ft, False) for ft in ftasks):
                            cdata["last_focus_completed"] = get_logical_date_str()

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
