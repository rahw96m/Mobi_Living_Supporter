import os
import sys
import json
import threading
import time
import copy
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from typing import Dict, Any, List, Optional

# pythonw execution support: Ensure sys.stdout and sys.stderr are valid streams
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
else:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")
else:
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


import webbrowser
import subprocess
from mabi_cli import MabinogiCLI, MabinogiCLIError, get_saved_cli_path, get_user_data_dir
from delivery_manager import (
    DeliveryManager, alarm_store, delivery_target_store,
    delivery_preset_store, custom_target_store, settings_store, get_facility_for_material,
    send_discord_test_message, notify_discord_goal_completion,
    STANDARD_ALTER_RECIPES, KNOWN_GATHERABLE_ITEMS, KNOWN_NON_GATHERABLE_MATERIALS
)
from homework_manager import HomeworkManager

BASE_DIR = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
EXE_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))

_ICON_BYTES_CACHE: Dict[str, Any] = {}
_HTML_PAGE_BYTES: Any = None

def get_icon_bytes(format_type="png"):
    if format_type in _ICON_BYTES_CACHE:
        return _ICON_BYTES_CACHE[format_type]
    filename = "app_icon.png" if format_type == "png" else "app_icon.ico"
    candidates = [
        os.path.join(BASE_DIR, filename),
        os.path.join(EXE_DIR, filename),
        os.path.join(os.getcwd(), filename),
    ]
    for c in candidates:
        if os.path.exists(c):
            try:
                with open(c, "rb") as f:
                    data = f.read()
                    _ICON_BYTES_CACHE[format_type] = data
                    return data
            except Exception:
                pass
    _ICON_BYTES_CACHE[format_type] = None
    return None

cli_instance = MabinogiCLI()
manager_instance = DeliveryManager(cli=cli_instance)

# Server State
console_logs: List[Dict[str, str]] = []
log_lock = threading.Lock()
is_busy = False
current_task_info = ""

# Auto-Shutdown & Heartbeat State
server_instance = None
server_start_time = time.time()
has_client_connected = False
last_heartbeat_time = time.time()
shutdown_timer = None
shutdown_timer_lock = threading.Lock()
is_shutting_down = False

def record_heartbeat():
    global has_client_connected, last_heartbeat_time, shutdown_timer
    has_client_connected = True
    last_heartbeat_time = time.time()
    with shutdown_timer_lock:
        if shutdown_timer is not None:
            shutdown_timer.cancel()
            shutdown_timer = None

def schedule_shutdown(delay: float = 1800.0, reason: str = "웹 대시보드가 닫혔습니다."):
    global shutdown_timer
    # Ignore spurious pagehide/disconnect within first 10 seconds of server start
    if (time.time() - server_start_time) < 10.0:
        return
    # NEVER schedule shutdown if Mabinogi Mobile is running!
    try:
        if cli_instance.check_game_running():
            return
    except Exception:
        pass
    with shutdown_timer_lock:
        if shutdown_timer is not None:
            shutdown_timer.cancel()
        def _trigger():
            now = time.time()
            # If dashboard reconnected or sent heartbeat recently, cancel shutdown
            if (now - last_heartbeat_time) < 60.0:
                return
            # If a background task is busy running, do not shut down
            if is_busy:
                record_heartbeat()
                return
            # If Mabinogi Mobile is running, do not shut down!
            try:
                if cli_instance.check_game_running():
                    record_heartbeat()
                    return
            except Exception:
                pass
            print(f"\n[자동 종료] {reason} 마비노기 모바일도 실행 중이 아니므로 서버를 안전하게 종료합니다.")
            shutdown_server()
        shutdown_timer = threading.Timer(delay, _trigger)
        shutdown_timer.daemon = True
        shutdown_timer.start()

def shutdown_server():
    global is_shutting_down
    if is_shutting_down:
        return
    is_shutting_down = True
    print("\n=========================================================")
    print("[서버 자동 종료] 웹 대시보드가 종료되어 서버와 프로세스를 안전하게 정리합니다.")
    print("=========================================================\n")
    def _do_shutdown():
        try:
            manager_instance.request_abort()
        except Exception:
            pass
        try:
            cli_instance.stop_action()
        except Exception:
            pass
        time.sleep(0.3)
        # Kill any orphaned MabinogiMobile_CLI.exe child processes
        if sys.platform == "win32":
            try:
                subprocess.run(["taskkill", "/F", "/T", "/IM", "MabinogiMobile_CLI.exe"], capture_output=True, timeout=2)
            except Exception:
                pass
        if server_instance:
            try:
                server_instance.shutdown()
            except Exception:
                pass
        os._exit(0)
    threading.Thread(target=_do_shutdown, daemon=True).start()

def watchdog_loop():
    while not is_shutting_down:
        time.sleep(5)
        now = time.time()
        # Give 30 seconds grace period after server start before requiring heartbeats
        if (now - server_start_time) < 30.0:
            continue
        # CRITICAL 1: If a task is busy running, NEVER shut down and refresh heartbeat!
        if is_busy:
            record_heartbeat()
            continue
        # CRITICAL 2: If Mabinogi Mobile is running, NEVER shut down and refresh heartbeat!
        try:
            if cli_instance.check_game_running():
                record_heartbeat()
                continue
        except Exception:
            pass
        # CRITICAL 3: Only shut down if no heartbeat for more than 4 hours (14400s) AND game is closed
        if has_client_connected and (now - last_heartbeat_time) > 14400.0:
            print("\n[장시간 미사용 감지] 마비노기 모바일이 종료되었고 4시간 이상 대시보드 신호가 없어 안전하게 종료합니다...")
            shutdown_server()
            break


def homework_telemetry_loop():
    """
    Keeps homework auto-detection alive even when the dashboard tab is hidden/minimized
    (Chrome throttles background timers to ~1/min, which made dungeon clears and quest
    progress go unnoticed). get_cached_status() reuses its 5s cache, so this adds no
    extra CLI load while the dashboard is actively polling.
    """
    interval = HOMEWORK_POLL_INTERVAL_SEC
    while not is_shutting_down:
        time.sleep(interval)
        if is_busy:
            continue
        try:
            get_cached_status()
            interval = HOMEWORK_POLL_INTERVAL_SEC
        except Exception:
            # Game closed / CLI disconnected: back off to avoid spawning CLI processes needlessly
            interval = HOMEWORK_POLL_BACKOFF_SEC


cached_status_data = None
cached_status_time = 0
last_homework_error_log = 0.0
HOMEWORK_POLL_INTERVAL_SEC = 6.0
HOMEWORK_POLL_BACKOFF_SEC = 30.0
cached_character_info = None
cached_character_time = 0
cached_altering_works = None
cached_delivery_targets = None
cached_custom_targets = None
cached_quests = None
cached_batch_plan = None
cached_batch_plan_time = 0.0
cached_batch_plan_order = None
cached_custom_plan = None
cached_custom_plan_time = 0.0
cached_custom_plan_order = None
cached_quick_plan = None
cached_quick_plan_time = 0.0
cached_quick_plan_key = None
status_lock = threading.Lock()

_CHOSUNG_LIST = [
    'ㄱ', 'ㄲ', 'ㄴ', 'ㄷ', 'ㄸ', 'ㄹ', 'ㅁ', 'ㅂ', 'ㅃ', 'ㅅ',
    'ㅆ', 'ㅇ', 'ㅈ', 'ㅉ', 'ㅊ', 'ㅋ', 'ㅌ', 'ㅍ', 'ㅎ'
]

def _extract_chosung_py(text: str) -> str:
    out = []
    for ch in (text or ""):
        code = ord(ch) - 0xAC00
        if 0 <= code <= 11171:
            out.append(_CHOSUNG_LIST[code // 588])
        else:
            out.append(ch)
    return "".join(out)

def _match_query_py(target: str, q_lower: str, q_nospace: str) -> bool:
    t_lower = target.lower()
    if q_lower in t_lower:
        return True
    t_nospace = t_lower.replace(" ", "")
    if q_nospace in t_nospace:
        return True
    if q_nospace in _extract_chosung_py(t_nospace):
        return True
    return False

def invalidate_plan_caches():
    global cached_batch_plan, cached_batch_plan_time, cached_custom_plan, cached_custom_plan_time, cached_quick_plan, cached_quick_plan_time
    with status_lock:
        cached_batch_plan = None
        cached_batch_plan_time = 0.0
        cached_custom_plan = None
        cached_custom_plan_time = 0.0
        cached_quick_plan = None
        cached_quick_plan_time = 0.0

def _on_altering_works_updated_handler(works_info: Dict[str, Any]):
    global cached_altering_works
    with status_lock:
        cached_altering_works = works_info

manager_instance.on_altering_updated = _on_altering_works_updated_handler
last_execution_summary: Optional[Dict[str, Any]] = None
last_summary_id: int = 0

def add_log(level: str, message: str):
    global current_task_info
    timestamp = time.strftime("%H:%M:%S")
    with log_lock:
        console_logs.append({"time": timestamp, "level": level, "message": message})
        if len(console_logs) > 300:
            console_logs.pop(0)
    if is_busy and (level in ("action", "busy") or "채집 진행 중" in message or "가공 중" in message or "제작 진행" in message or message.startswith("[목표") or message.startswith("[맞춤 채집") or message.startswith("[부족 수량")):
        current_task_info = message

homework_instance = HomeworkManager(
    cli=cli_instance,
    delivery_store=delivery_target_store,
    settings_store=settings_store,
    log_callback=add_log,
)

def get_cached_status() -> Dict[str, Any]:
    global cached_status_data, cached_status_time, cached_character_info, cached_character_time
    now = time.time()
    with status_lock:
        # While a task is busy running, NEVER invoke CLI commands to prevent pipe contention and HTTP socket starvation!
        if is_busy:
            if cached_status_data:
                cached_status_data["is_busy"] = True
                cached_status_data["current_task"] = current_task_info
                cached_status_data["abort_requested"] = manager_instance.abort_requested
                cached_status_data["last_summary"] = last_execution_summary
                cached_status_data["last_summary_id"] = last_summary_id
                cached_status_data["delivery_character"] = delivery_target_store.get_characters_summary()
                cached_status_data["homework"] = homework_instance.get_dashboard_state()
                cached_status_data["omen_barrier"] = homework_instance.get_omen_barrier_status()
                return copy.deepcopy(cached_status_data)
            return {
                "connected": True,
                "activity": {},
                "character": copy.deepcopy(cached_character_info or {}),
                "delivery_character": delivery_target_store.get_characters_summary(),
                "homework": homework_instance.get_dashboard_state(),
                "omen_barrier": homework_instance.get_omen_barrier_status(),
                "wings": 0,
                "inventory": {},
                "is_busy": True,
                "current_task": current_task_info,
                "abort_requested": manager_instance.abort_requested,
                "last_summary": last_execution_summary,
                "last_summary_id": last_summary_id,
                "cli_path": cli_instance.cli_path or "",
                "discovery_source": getattr(cli_instance, "discovery_source", "")
            }

        # If cache is fresh (< 5.0s), immediately return cached data without invoking CLI
        if cached_status_data and (now - cached_status_time) < 5.0:
            cached_status_data["is_busy"] = is_busy
            cached_status_data["current_task"] = current_task_info
            cached_status_data["abort_requested"] = manager_instance.abort_requested
            cached_status_data["last_summary"] = last_execution_summary
            cached_status_data["last_summary_id"] = last_summary_id
            cached_status_data["delivery_character"] = delivery_target_store.get_characters_summary()
            cached_status_data["homework"] = homework_instance.get_dashboard_state()
            cached_status_data["omen_barrier"] = homework_instance.get_omen_barrier_status()
            return copy.deepcopy(cached_status_data)

        need_char_refresh = (not cached_character_info) or ((now - cached_character_time) > 5.0)
        prev_char_info = copy.deepcopy(cached_character_info) if cached_character_info else {}

    # Invoke CLI outside status_lock to avoid blocking other requests
    try:
        new_char_info = None
        if need_char_refresh:
            try:
                new_char_info = cli_instance.get_my_info()
            except Exception:
                new_char_info = None

        char_info_now = new_char_info or prev_char_info
        if char_info_now:
            switched_char_id = delivery_target_store.on_ingame_character_seen(
                char_info_now, allow_auto_switch=(not is_busy)
            )
            if switched_char_id:
                homework_instance.on_delivery_character_selected(switched_char_id)
                invalidate_plan_caches()
                with status_lock:
                    global cached_delivery_targets
                    cached_delivery_targets = None
                char_summary = delivery_target_store.get_characters_summary()
                switched_name = char_summary.get("active_character_name", switched_char_id)
                switched_label = char_summary.get("last_ingame_label", "")
                add_log("info", f"[캐릭터 자동 전환] 인게임 캐릭터 감지({switched_label}) -> '{switched_name}' 프로필로 자동 전환되었습니다.")

        activity = cli_instance.get_activity()
        wings = cli_instance.get_wings_count()
        inv = cli_instance.get_inventory()
        try:
            env_info = cli_instance.get_current_environment()
        except Exception:
            env_info = {}

        try:
            homework_instance.update_from_ingame_state(
                char_info_now or {},
                activity or {},
                inv or {},
                env_info or {},
                is_busy=is_busy,
            )
        except Exception as hw_err:
            # Never break status polling, but surface the error (throttled) instead of hiding it
            global last_homework_error_log
            if (time.time() - last_homework_error_log) > 60.0:
                last_homework_error_log = time.time()
                add_log("warn", f"[숙제 지원] 인게임 자동 체크 처리 중 오류: {hw_err}")

        data = {
            "connected": True,
            "activity": activity,
            "environment": env_info,
            "character": char_info_now,
            "delivery_character": delivery_target_store.get_characters_summary(),
            "homework": homework_instance.get_dashboard_state(),
            "omen_barrier": homework_instance.get_omen_barrier_status(),
            "wings": wings,
            "inventory": inv,
            "is_busy": is_busy,
            "current_task": current_task_info,
            "abort_requested": manager_instance.abort_requested,
            "last_summary": last_execution_summary,
            "last_summary_id": last_summary_id,
            "cli_path": cli_instance.cli_path or "",
            "discovery_source": getattr(cli_instance, "discovery_source", "")
        }
        with status_lock:
            if new_char_info:
                cached_character_info = new_char_info
                cached_character_time = time.time()
            cached_status_data = data
            cached_status_time = time.time()
        return copy.deepcopy(data)
    except MabinogiCLIError as e:
        err_data = {
            "connected": False,
            "error": str(e),
            "error_code": e.error_code or "CLI_ERROR",
            "cli_path": cli_instance.cli_path or "",
            "discovery_source": getattr(cli_instance, "discovery_source", ""),
            "delivery_character": delivery_target_store.get_characters_summary(),
            "homework": homework_instance.get_dashboard_state(),
            "omen_barrier": homework_instance.get_omen_barrier_status(),
            "is_busy": is_busy,
            "current_task": current_task_info,
            "last_summary": last_execution_summary,
            "last_summary_id": last_summary_id
        }
        with status_lock:
            cached_status_data = err_data
            cached_status_time = time.time()
        return copy.deepcopy(err_data)
    except Exception as e:
        err_data = {
            "connected": False,
            "error": str(e),
            "error_code": "UNKNOWN",
            "cli_path": cli_instance.cli_path or "",
            "discovery_source": getattr(cli_instance, "discovery_source", ""),
            "delivery_character": delivery_target_store.get_characters_summary(),
            "homework": homework_instance.get_dashboard_state(),
            "omen_barrier": homework_instance.get_omen_barrier_status(),
            "is_busy": is_busy,
            "current_task": current_task_info,
            "last_summary": last_execution_summary,
            "last_summary_id": last_summary_id
        }
        with status_lock:
            cached_status_data = err_data
            cached_status_time = time.time()
        return copy.deepcopy(err_data)

class RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        # Override BaseHTTPRequestHandler default logging to prevent crash in pythonw
        pass

    def _send_data(self, status=200, data_bytes=b"", content_type="application/json; charset=utf-8"):
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data_bytes)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data_bytes)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
            pass
        finally:
            self.close_connection = True

    def _send_json(self, obj, status=200):
        body_bytes = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send_data(status, body_bytes, "application/json; charset=utf-8")

    def _send_html(self, html_text, status=200):
        global _HTML_PAGE_BYTES
        if html_text is HTML_PAGE:
            if _HTML_PAGE_BYTES is None:
                _HTML_PAGE_BYTES = HTML_PAGE.encode("utf-8")
            body_bytes = _HTML_PAGE_BYTES
        else:
            body_bytes = html_text.encode("utf-8")
        self._send_data(status, body_bytes, "text/html; charset=utf-8")

    def _send_error(self, message, status=500):
        self._send_json({"error": str(message)}, status)

    def do_OPTIONS(self):
        self._send_data(200, b"")

    def do_GET(self):
        record_heartbeat()
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/heartbeat":
            self._send_json({"status": "alive"})
            return

        if path == "/api/network_info":
            local_ip = get_local_ip()
            self._send_json({
                "local_ip": local_ip,
                "port": active_server_port,
                "mobile_url": f"http://{local_ip}:{active_server_port}",
                "is_lan": local_ip not in ("127.0.0.1", "localhost")
            })
            return

        if path == "/favicon.ico":
            icon_data = get_icon_bytes("ico") or get_icon_bytes("png")
            if icon_data:
                self._send_data(200, icon_data, "image/x-icon")
            else:
                self._send_data(404, b"")
            return

        if path in ("/api/icon", "/app_icon.png"):
            icon_data = get_icon_bytes("png") or get_icon_bytes("ico")
            if icon_data:
                self._send_data(200, icon_data, "image/png")
            else:
                self._send_data(404, b"")
            return

        if path in ("/", "/index.html"):
            self._send_html(HTML_PAGE)
            return

        if path == "/api/status":
            resp = get_cached_status()
            self._send_json(resp)
            return

        if path == "/api/last_summary":
            with status_lock:
                self._send_json({"summary": last_execution_summary, "id": last_summary_id})
            return

        if path == "/api/logs":
            with log_lock:
                logs_copy = list(console_logs)
            self._send_json({"logs": logs_copy})
            return

        if path == "/api/delivery_targets":
            global cached_delivery_targets
            if is_busy:
                with status_lock:
                    targets = cached_delivery_targets if cached_delivery_targets is not None else delivery_target_store.get_all()
                    self._send_json({
                        "targets": targets,
                        "characters_info": delivery_target_store.get_characters_summary(),
                        "is_busy": True
                    })
                    return
            try:
                targets = manager_instance.get_registered_deliveries_with_status()
                with status_lock:
                    cached_delivery_targets = targets
                self._send_json({
                    "targets": targets,
                    "characters_info": delivery_target_store.get_characters_summary()
                })
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/delivery_presets":
            try:
                presets = delivery_preset_store.get_all()
                self._send_json({"presets": presets})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/quests":
            global cached_quests
            if is_busy:
                with status_lock:
                    tasks = cached_quests if cached_quests is not None else []
                    self._send_json({"tasks": tasks, "is_busy": True})
                    return
            try:
                tasks = manager_instance.detect_delivery_quests()
                resp = [t.to_dict() for t in tasks]
                with status_lock:
                    cached_quests = resp
                self._send_json({"tasks": resp})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/settings":
            try:
                self._send_json(settings_store.get_all())
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/user_data_info":
            try:
                data_dir = get_user_data_dir()
                files = []
                if os.path.isdir(data_dir):
                    for f in sorted(os.listdir(data_dir)):
                        fp = os.path.join(data_dir, f)
                        if os.path.isfile(fp):
                            files.append({"name": f, "size": os.path.getsize(fp)})
                self._send_json({"data_dir": data_dir, "files": files})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/batch_plan":
            global cached_batch_plan, cached_batch_plan_time, cached_batch_plan_order
            if is_busy:
                self._send_json({"is_busy": True, "can_start": False, "items": [], "steps": []})
                return
            query_params = parse_qs(parsed.query)
            alter_order = query_params.get("alter_order", [None])[0]
            try:
                plan = manager_instance.analyze_batch_plan(alter_order=alter_order)
                with status_lock:
                    cached_batch_plan = plan
                    cached_batch_plan_time = time.time()
                    cached_batch_plan_order = alter_order
                self._send_json(plan)
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/custom_targets":
            global cached_custom_targets
            if is_busy:
                with status_lock:
                    targets = cached_custom_targets if cached_custom_targets is not None else []
                    self._send_json({"targets": targets, "is_busy": True})
                    return
            try:
                targets = manager_instance.get_custom_targets_with_status()
                with status_lock:
                    cached_custom_targets = targets
                self._send_json({"targets": targets})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/custom_plan":
            global cached_custom_plan, cached_custom_plan_time, cached_custom_plan_order
            if is_busy:
                self._send_json({"is_busy": True, "can_start": False, "items": [], "steps": []})
                return
            query_params = parse_qs(parsed.query)
            alter_order = query_params.get("alter_order", [None])[0]
            try:
                plan = manager_instance.analyze_custom_plan(alter_order=alter_order)
                with status_lock:
                    cached_custom_plan = plan
                    cached_custom_plan_time = time.time()
                    cached_custom_plan_order = alter_order
                self._send_json(plan)
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/alarms":
            try:
                if not is_busy:
                    alarm_store.sync_facility_alarms(cli_instance)
                alarms = alarm_store.get_all()
                self._send_json({"alarms": alarms})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/search_recipe":
            query_params = parse_qs(parsed.query)
            item_name = query_params.get("name", [""])[0].strip()
            if not item_name:
                self._send_error("검색할 이름을 입력해주세요.", 400)
                return
            try:
                # 1. Local Recipe / Alter / Gather / Reverse-Usage Database Lookup (Always fast & available)
                q_lower = item_name.lower()
                q_nospace = q_lower.replace(" ", "")
                local_crafts = []
                used_in = []

                for r_name, ings in (manager_instance.recipes or {}).items():
                    if _match_query_py(r_name, q_lower, q_nospace):
                        local_crafts.append({
                            "name": r_name,
                            "type": "제작",
                            "ingredients": ings
                        })
                    if isinstance(ings, dict):
                        for ing_k, ing_v in ings.items():
                            if _match_query_py(ing_k, q_lower, q_nospace):
                                used_in.append({
                                    "target": r_name,
                                    "category": "제작",
                                    "ingredient": ing_k,
                                    "qty": ing_v
                                })

                local_alters = []
                for a_name, a_info in STANDARD_ALTER_RECIPES.items():
                    ings = a_info.get("ingredients", {})
                    fac = a_info.get("facility", "가공 시설")
                    if _match_query_py(a_name, q_lower, q_nospace):
                        local_alters.append({
                            "name": a_name,
                            "facility": fac,
                            "produced": a_info.get("produced", 1),
                            "tier": a_info.get("tier", 1),
                            "ingredients": ings
                        })
                    if isinstance(ings, dict):
                        for ing_k, ing_v in ings.items():
                            if _match_query_py(ing_k, q_lower, q_nospace):
                                used_in.append({
                                    "target": a_name,
                                    "category": fac.replace(" 가공 시설", "").replace(" 시설", "") + " 가공",
                                    "ingredient": ing_k,
                                    "qty": ing_v
                                })

                local_gathers = [g for g in sorted(KNOWN_GATHERABLE_ITEMS) if _match_query_py(g, q_lower, q_nospace)]
                local_specials = [s for s in sorted(KNOWN_NON_GATHERABLE_MATERIALS) if _match_query_py(s, q_lower, q_nospace)]

                # Sort exact matches first, then prefix matches, then by length
                def _rank_name(nm: str) -> tuple:
                    nl = nm.lower()
                    if nl == q_lower:
                        return (0, len(nm), nm)
                    if nl.startswith(q_lower):
                        return (1, len(nm), nm)
                    return (2, len(nm), nm)

                local_crafts.sort(key=lambda x: _rank_name(x["name"]))
                local_alters.sort(key=lambda x: (0 if x["name"].lower() == q_lower else 1, x.get("tier", 1), x["name"]))
                local_gathers.sort(key=_rank_name)
                local_specials.sort(key=_rank_name)
                used_in.sort(key=lambda x: (0 if x["ingredient"].lower() == q_lower else 1, x["target"]))

                # Resolve best canonical item name for inventory stock check if user searched a partial/chosung query
                stock_target_name = item_name
                exact_candidates = (
                    [c["name"] for c in local_crafts]
                    + [a["name"] for a in local_alters]
                    + local_gathers
                    + local_specials
                )
                if exact_candidates and item_name not in exact_candidates:
                    stock_target_name = exact_candidates[0]

                # 2. Live CLI Inventory & In-Game Recipe Lookup (if not busy and connected)
                cli_live_ok = False
                breakdown = {"inventory": 0, "character_storage": 0, "account_storage": 0, "storage_total": 0, "total": 0}
                craft_items = []
                alter_items = []
                gath_items = []

                if not is_busy:
                    try:
                        breakdown = cli_instance.get_item_location_breakdown(stock_target_name)
                        craft_res = cli_instance.get_craftable_items(stock_target_name)
                        craft_items = craft_res.get("items", [])
                        all_alter = cli_instance.get_alterable_items().get("items", [])
                        alter_items = [it for it in all_alter if stock_target_name in it.get("DisplayName", "")]
                        all_gath = cli_instance.get_gatherable_items().get("items", [])
                        gath_items = [it for it in all_gath if stock_target_name in it.get("DisplayName", "")]
                        cli_live_ok = True
                    except Exception:
                        cli_live_ok = False

                self._send_json({
                    "item_name": stock_target_name,
                    "query": item_name,
                    "cli_live_ok": cli_live_ok,
                    "is_busy": is_busy,
                    "owned_bag": breakdown["inventory"],
                    "owned_char_storage": breakdown["character_storage"],
                    "owned_account_storage": breakdown["account_storage"],
                    "owned_storage": breakdown["storage_total"],
                    "owned_all": breakdown["total"],
                    "craft": craft_items,
                    "alter": alter_items,
                    "gather": gath_items,
                    "local_crafts": local_crafts[:25],
                    "local_alters": local_alters[:20],
                    "local_gathers": local_gathers[:20],
                    "local_specials": local_specials[:10],
                    "used_in": used_in[:30]
                })
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/altering":
            global cached_altering_works
            if is_busy:
                with status_lock:
                    if cached_altering_works is not None:
                        self._send_json(cached_altering_works)
                        return
                    else:
                        self._send_json({"completedCount": 0, "works": [], "is_busy": True})
                        return
            try:
                works = cli_instance.get_altering_works()
                with status_lock:
                    cached_altering_works = works
                self._send_json(works)
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/quick_alter_plan":
            global cached_quick_plan, cached_quick_plan_time, cached_quick_plan_key
            query_params = parse_qs(parsed.query)
            category = query_params.get("category", ["all"])[0].strip()
            alter_order = query_params.get("alter_order", [None])[0]
            force_refresh = query_params.get("force", ["0"])[0] == "1"
            req_key = f"{category}:{alter_order or manager_instance.get_alter_order()}"

            if is_busy:
                with status_lock:
                    if cached_quick_plan is not None and cached_quick_plan_key == req_key:
                        resp = dict(cached_quick_plan)
                        resp["is_busy"] = True
                        self._send_json(resp)
                        return
                self._send_json({"is_busy": True, "can_start": False, "facilities": []})
                return

            now = time.time()
            if not force_refresh:
                with status_lock:
                    if cached_quick_plan is not None and cached_quick_plan_key == req_key and (now - cached_quick_plan_time) < 6.0:
                        self._send_json(cached_quick_plan)
                        return

            try:
                plan = manager_instance.analyze_quick_alter(category if category != "all" else None, alter_order=alter_order)
                with status_lock:
                    cached_quick_plan = plan
                    cached_quick_plan_time = time.time()
                    cached_quick_plan_key = req_key
                self._send_json(plan)
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/cli_config":
            try:
                curr_path = cli_instance.cli_path or ""
                exists = os.path.isfile(curr_path) if curr_path else False
                saved_path = get_saved_cli_path() or ""
                self._send_json({
                    "cli_path": curr_path,
                    "exists": exists,
                    "saved_path": saved_path,
                    "discovery_source": getattr(cli_instance, "discovery_source", ""),
                })
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/recipe":
            query_params = parse_qs(parsed.query)
            item_name = query_params.get("name", [""])[0].strip()
            if not item_name:
                self._send_error("아이템 이름을 입력해주세요.", 400)
                return
            try:
                recipe = manager_instance.get_recipe_ingredients(item_name)
                is_known = bool(recipe)
                self._send_json({
                    "item_name": item_name,
                    "ingredients": recipe,
                    "is_known": is_known
                })
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/recipes_all":
            try:
                self._send_json({
                    "recipes": manager_instance.recipes,
                    "alter_recipes": STANDARD_ALTER_RECIPES,
                    "gatherables": sorted(list(KNOWN_GATHERABLE_ITEMS)),
                    "non_gatherables": sorted(list(KNOWN_NON_GATHERABLE_MATERIALS))
                })
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/selectable_quests":
            if is_busy:
                self._send_json({"quests": [], "is_busy": True})
                return
            try:
                quests = manager_instance.scan_all_selectable_quests()
                self._send_json({"quests": quests})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/zero_gather_recommendations":
            if is_busy:
                self._send_json({"items": [], "is_busy": True})
                return
            try:
                recs = manager_instance.recommend_zero_gather_crafts()
                self._send_json({"items": recs})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/safety_stocks":
            try:
                items = manager_instance.get_safety_stocks_status() if not is_busy else []
                self._send_json({
                    "items": items,
                    "config": settings_store.get("safety_stocks", {}) or {},
                    "is_busy": is_busy
                })
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/missions":
            if is_busy:
                self._send_json({"daily": [], "weekly": [], "is_busy": True})
                return
            try:
                daily = cli_instance.get_daily_missions()
                weekly = cli_instance.get_weekly_missions()
                self._send_json({"daily": daily, "weekly": weekly})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/music_and_social":
            if is_busy:
                self._send_json({"scores": [], "instruments": [], "is_busy": True})
                return
            try:
                scores = cli_instance.get_music_scores()
                instruments = cli_instance.get_instruments()
                self._send_json({
                    "scores": scores,
                    "instruments": instruments
                })
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/homework/state":
            try:
                self._send_json(homework_instance.get_dashboard_state())
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/homework/export_backup":
            try:
                self._send_json(homework_instance.export_backup_bundle())
            except Exception as e:
                self._send_error(e)
            return

        self._send_data(404, b"Not Found", "text/plain")

    def do_POST(self):
        record_heartbeat()
        global is_busy, current_task_info
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/heartbeat":
            self._send_json({"status": "alive"})
            return

        if path == "/api/dashboard_closed":
            # If Mabinogi Mobile is running, keep server alive without scheduling shutdown
            try:
                if cli_instance.check_game_running():
                    self._send_json({"status": "game_running_keep_alive"})
                    return
            except Exception:
                pass
            schedule_shutdown(delay=1800.0, reason="웹 대시보드 창이 닫히고 게임도 종료되어")
            self._send_json({"status": "closing"})
            return

        if path == "/api/shutdown":
            self._send_json({"status": "shutting_down"})
            threading.Timer(0.3, shutdown_server).start()
            return

        if path == "/api/set_cli_path":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length)
            try:
                req_json = json.loads(body_bytes.decode("utf-8"))
                new_path = req_json.get("path", "").strip()
                if not new_path:
                    self._send_error("경로를 입력해주세요.", 400)
                    return
                ok = cli_instance.set_cli_path(new_path)
                if not ok:
                    self._send_error("해당 경로 또는 폴더에서 MabinogiMobile_CLI.exe를 찾을 수 없습니다. 폴더 경로 또는 파일명을 확인해주세요.", 400)
                    return
                global cached_status_time, cached_character_time, cached_status_data, cached_character_info, cached_delivery_targets
                with status_lock:
                    cached_status_time = 0.0
                    cached_character_time = 0.0
                    cached_status_data = None
                add_log("success", f"[연결 설정] 게임 CLI 경로가 갱신되었습니다: {cli_instance.cli_path}")
                self._send_json({
                    "status": "success",
                    "cli_path": cli_instance.cli_path,
                    "source": getattr(cli_instance, "discovery_source", "")
                })
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/scan_cli_path":
            try:
                found = cli_instance.discover_cli_path()
                with status_lock:
                    cached_status_time = 0.0
                    cached_character_time = 0.0
                    cached_status_data = None
                if found:
                    add_log("success", f"[경로 자동 탐색] CLI 경로를 감지했습니다: {found} ({cli_instance.discovery_source})")
                    self._send_json({
                        "status": "found",
                        "cli_path": found,
                        "source": getattr(cli_instance, "discovery_source", "")
                    })
                else:
                    self._send_json({
                        "status": "not_found",
                        "message": "게임 프로세스, 레지스트리 및 드라이브에서 MabinogiMobile_CLI.exe를 찾을 수 없습니다."
                    })
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/open_data_folder":
            try:
                data_dir = get_user_data_dir()
                if sys.platform == "win32":
                    os.startfile(data_dir)
                else:
                    subprocess.Popen(["xdg-open", data_dir])
                add_log("info", f"[데이터 폴더] 설정 및 레시피 저장 폴더를 열었습니다: {data_dir}")
                self._send_json({"status": "ok", "path": data_dir})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/client_error":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length)
            try:
                err_data = json.loads(body_bytes.decode("utf-8", errors="ignore"))
                print(f"[CLIENT JS ERROR] {err_data}")
                add_log("error", f"[브라우저 JS 오류] {err_data.get('msg')} (line {err_data.get('lineNo')})")
            except Exception:
                pass
            self._send_json({"status": "ok"})
            return

        if path == "/api/produce":
            if is_busy:
                self._send_error("이미 다른 작업이 진행 중입니다.", 409)
                return

            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length)
            try:
                req_json = json.loads(body_bytes.decode("utf-8"))
                item_name = req_json.get("item_name")
                target_count = int(req_json.get("target_count", 1))

                if not item_name or target_count <= 0:
                    self._send_error("올바른 아이템 이름과 수량을 입력해주세요.", 400)
                    return

                manager_instance.reset_abort()
                is_busy = True
                current_task_info = f"'{item_name}' {target_count}개 제작 진행 중..."

                def worker():
                    global is_busy, current_task_info, last_execution_summary, last_summary_id
                    try:
                        res = manager_instance.resolve_and_produce(item_name, target_count, callback=add_log)
                        if isinstance(res, dict) and "summary" in res:
                            with status_lock:
                                last_execution_summary = res["summary"]
                                last_summary_id += 1
                        if not manager_instance.abort_requested:
                            manager_instance.try_auto_play_music(callback=add_log)
                    except Exception as err:
                        add_log("error", f"작업 실패: {str(err)}")
                        with status_lock:
                            last_execution_summary = {
                                "title": f"'{item_name}' 제작 작업 리포트 (오류 중단)",
                                "status": "aborted",
                                "status_text": "오류 중단",
                                "duration_text": "0초",
                                "wings": {"initial": 0, "final": 0, "used": 0},
                                "gather": {"count": 0, "items": []},
                                "alter": {"count": 0, "items": []},
                                "craft": {"count": 0, "items": []},
                                "collected_facilities": [],
                                "deferred_works": [{"recipe": item_name, "reason": str(err)}]
                            }
                            last_summary_id += 1
                    finally:
                        is_busy = False
                        current_task_info = ""

                threading.Thread(target=worker, daemon=True).start()
                self._send_json({"status": "started", "item_name": item_name, "target_count": target_count})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/settings":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                settings_store.update(req_json)
                add_log("info", f"설정이 업데이트되었습니다: {req_json}")
                self._send_json({"status": "success", "settings": settings_store.get_all()})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/discord/test":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                url = req_json.get("webhook_url") or settings_store.get("discord_webhook_url", "")
                success, msg = send_discord_test_message(url)
                if success:
                    add_log("success", "[디스코드] 테스트 웹훅 메시지가 성공적으로 전송되었습니다.")
                    self._send_json({"status": "success", "message": msg})
                else:
                    add_log("warn", f"[디스코드] 테스트 웹훅 전송 실패: {msg}")
                    self._send_json({"status": "error", "message": msg}, 400)
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/execute_batch":
            if is_busy:
                self._send_error("이미 다른 작업이 진행 중입니다.", 409)
                return

            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length) if length > 0 else b"{}"
            req_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
            alter_order = req_json.get("alter_order")
            goal_fulfillment_mode = req_json.get("goal_fulfillment_mode")
            if goal_fulfillment_mode is None:
                goal_fulfillment_mode = settings_store.get("goal_fulfillment_mode", True)

            manager_instance.reset_abort()
            is_busy = True
            order_name = "상위 티어 우선" if (alter_order or settings_store.get("alter_order")) != "low_tier" else "하위 티어 우선"
            mode_tag = " [목표 완수 모드]" if goal_fulfillment_mode else ""
            current_task_info = f"전체 주간 납품 일괄 최적화 제작 진행 중 ({order_name}){mode_tag}..."

            def batch_worker():
                global is_busy, current_task_info, last_execution_summary, last_summary_id
                try:
                    def update_task_status(msg):
                        global current_task_info
                        current_task_info = msg

                    plan = None
                    with status_lock:
                        if cached_batch_plan is not None and (time.time() - cached_batch_plan_time) < 6.0 and cached_batch_plan_order == alter_order:
                            plan = copy.deepcopy(cached_batch_plan)
                    if plan is None:
                        plan = manager_instance.analyze_batch_plan(alter_order=alter_order)
                    res = manager_instance.execute_batch_deliveries(
                        plan, callback=add_log, alter_order=alter_order,
                        goal_fulfillment_mode=goal_fulfillment_mode,
                        status_reporter=update_task_status
                    )
                    if isinstance(res, dict) and "summary" in res:
                        with status_lock:
                            last_execution_summary = res["summary"]
                            last_summary_id += 1
                        if goal_fulfillment_mode and not manager_instance.abort_requested:
                            threading.Thread(
                                target=notify_discord_goal_completion,
                                args=(res["summary"], "전체 주간 납품"),
                                daemon=True
                            ).start()
                    if not manager_instance.abort_requested:
                        manager_instance.try_auto_play_music(callback=add_log)
                except Exception as err:
                    add_log("error", f"일괄 제작 실패: {str(err)}")
                    err_summary = {
                        "title": "주간 납품 작업 리포트 (오류 중단)",
                        "status": "error",
                        "status_text": "오류 중단",
                        "duration_text": "0초",
                        "wings": {"initial": 0, "final": 0, "used": 0},
                        "gather": {"count": 0, "items": []},
                        "alter": {"count": 0, "items": []},
                        "craft": {"count": 0, "items": []},
                        "collected_facilities": [],
                        "deferred_works": [{"recipe": "일괄 납품", "reason": str(err)}]
                    }
                    with status_lock:
                        last_execution_summary = err_summary
                        last_summary_id += 1
                    if goal_fulfillment_mode and not manager_instance.abort_requested:
                        threading.Thread(
                            target=notify_discord_goal_completion,
                            args=(err_summary, "전체 주간 납품"),
                            daemon=True
                        ).start()
                finally:
                    is_busy = False
                    current_task_info = ""

            threading.Thread(target=batch_worker, daemon=True).start()
            self._send_json({"status": "started"})
            return

        if path == "/api/execute_custom":
            if is_busy:
                self._send_error("이미 다른 작업이 진행 중입니다.", 409)
                return

            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length) if length > 0 else b"{}"
            req_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
            alter_order = req_json.get("alter_order")
            goal_fulfillment_mode = req_json.get("goal_fulfillment_mode")
            if goal_fulfillment_mode is None:
                goal_fulfillment_mode = settings_store.get("goal_fulfillment_mode", True)

            manager_instance.reset_abort()
            is_busy = True
            order_name = "상위 티어 우선" if (alter_order or settings_store.get("alter_order")) != "low_tier" else "하위 티어 우선"
            mode_tag = " [목표 완수 모드]" if goal_fulfillment_mode else ""
            current_task_info = f"개별 지정 아이템 일괄 최적화 제작 진행 중 ({order_name}){mode_tag}..."

            def custom_batch_worker():
                global is_busy, current_task_info, last_execution_summary, last_summary_id, cached_custom_targets
                try:
                    def update_task_status(msg):
                        global current_task_info
                        current_task_info = msg

                    plan = None
                    with status_lock:
                        if cached_custom_plan is not None and (time.time() - cached_custom_plan_time) < 6.0 and cached_custom_plan_order == alter_order:
                            plan = copy.deepcopy(cached_custom_plan)
                    if plan is None:
                        plan = manager_instance.analyze_custom_plan(alter_order=alter_order)
                    res = manager_instance.execute_batch_deliveries(
                        plan, callback=add_log, alter_order=alter_order,
                        goal_fulfillment_mode=goal_fulfillment_mode,
                        status_reporter=update_task_status
                    )
                    if isinstance(res, dict) and "summary" in res:
                        res["summary"]["title"] = "개별 지정 아이템 제작 완료 리포트" if not goal_fulfillment_mode else "개별 지정 목표 완수 리포트"
                        with status_lock:
                            last_execution_summary = res["summary"]
                            last_summary_id += 1
                        if goal_fulfillment_mode and not manager_instance.abort_requested:
                            threading.Thread(
                                target=notify_discord_goal_completion,
                                args=(res["summary"], "지정 제작"),
                                daemon=True
                            ).start()
                    if not manager_instance.abort_requested:
                        manager_instance.try_auto_play_music(callback=add_log)
                except Exception as err:
                    add_log("error", f"개별 지정 일괄 제작 실패: {str(err)}")
                    err_summary = {
                        "title": "개별 지정 제작 리포트 (오류 중단)",
                        "status": "error",
                        "status_text": "오류 중단",
                        "duration_text": "0초",
                        "wings": {"initial": 0, "final": 0, "used": 0},
                        "gather": {"count": 0, "items": []},
                        "alter": {"count": 0, "items": []},
                        "craft": {"count": 0, "items": []},
                        "collected_facilities": [],
                        "deferred_works": [{"recipe": "개별 지정 제작", "reason": str(err)}]
                    }
                    with status_lock:
                        last_execution_summary = err_summary
                        last_summary_id += 1
                    if goal_fulfillment_mode and not manager_instance.abort_requested:
                        threading.Thread(
                            target=notify_discord_goal_completion,
                            args=(err_summary, "지정 제작"),
                            daemon=True
                        ).start()
                finally:
                    is_busy = False
                    current_task_info = ""
                    with status_lock:
                        cached_custom_targets = None

            threading.Thread(target=custom_batch_worker, daemon=True).start()
            self._send_json({"status": "started"})
            return

        if path in ("/api/abort_quick_alter", "/api/abort_pipeline"):
            add_log("warn", "[중지 요청 접수] 사용자가 진행 중인 작업의 중단 명령을 전송했습니다.")
            manager_instance.request_abort()
            try:
                cli_instance.stop_action()
            except Exception:
                pass
            self._send_json({"status": "aborted", "message": "중지 요청이 성공적으로 전송되었습니다."})
            return

        if path == "/api/execute_quick_alter":
            if is_busy:
                self._send_error("이미 다른 작업이 진행 중입니다.", 409)
                return

            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                category = req_json.get("category", "all")
                alter_order = req_json.get("alter_order")

                manager_instance.reset_abort()
                is_busy = True
                cat_disp = "전체 가공대" if category == "all" else f"'{category}' 가공대"
                order_name = "상위 티어 우선" if (alter_order or settings_store.get("alter_order")) != "low_tier" else "하위 티어 우선"
                current_task_info = f"7슬롯 최고 레벨 {cat_disp} 빠른 실행 진행 중 ({order_name})..."

                def quick_alter_worker():
                    global is_busy, current_task_info, last_execution_summary, last_summary_id
                    try:
                        plan = manager_instance.analyze_quick_alter(category if category != "all" else None, alter_order=alter_order)
                        res = manager_instance.execute_quick_alter(plan, callback=add_log, alter_order=alter_order)
                        if isinstance(res, dict) and "summary" in res:
                            with status_lock:
                                last_execution_summary = res["summary"]
                                last_summary_id += 1
                        if not manager_instance.abort_requested:
                            manager_instance.try_auto_play_music(callback=add_log)
                    except Exception as err:
                        add_log("error", f"빠른 가공 실행 실패: {str(err)}")
                    finally:
                        is_busy = False
                        current_task_info = ""

                threading.Thread(target=quick_alter_worker, daemon=True).start()
                self._send_json({"status": "started", "category": category})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/collect_altering":
            if is_busy:
                self._send_error("다른 작업이 진행 중이어서 가공품을 수령할 수 없습니다.", 409)
                return
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length)
            try:
                req_json = json.loads(body_bytes.decode("utf-8"))
                item_name = req_json.get("item_name")
                is_busy = True
                current_task_info = f"\'{item_name}\' 가공품 수령 중..."
                try:
                    res = cli_instance.complete_altering_work(item_name)
                    add_log("success", f"\'{item_name}\' 가공품 수령 완료: {res}")
                    self._send_json({"status": "success", "result": res})
                finally:
                    is_busy = False
                    current_task_info = ""
            except Exception as e:
                add_log("error", f"가공품 수령 실패: {str(e)}")
                self._send_error(e)
            return

        if path == "/api/delete_alarm":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length)
            try:
                req_json = json.loads(body_bytes.decode("utf-8"))
                key = req_json.get("key")
                alarm_store.delete_alarm(key)
                self._send_json({"status": "success"})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/clear_alarms":
            try:
                alarm_store.clear()
                self._send_json({"status": "success"})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/add_manual_alarm":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length)
            try:
                req_json = json.loads(body_bytes.decode("utf-8"))
                facility = req_json.get("facility", "가공 시설")
                item_name = req_json.get("item_name")
                remaining_sec = int(req_json.get("remaining_seconds", 0))

                # Check all works at this facility to find the true last completion time
                works_info = cli_instance.get_altering_works()
                all_works = works_info.get("works", [])
                f_works = [
                    w for w in all_works 
                    if (w.get("FacilityName") == facility or get_facility_for_material(w.get("DisplayName", "")) == facility) 
                    and not w.get("IsCompleted") and int(w.get("RemainingSeconds", 0)) > 0
                ]
                if f_works:
                    total_sec = sum(int(w.get("RemainingSeconds", 0)) for w in f_works)
                    item_names = list(dict.fromkeys(w.get("DisplayName", "") for w in f_works if w.get("DisplayName")))
                    if len(item_names) == 1:
                        summary = f"{item_names[0]} ({len(f_works)}슬롯)"
                    else:
                        summary = f"{item_names[0]} 외 {len(f_works)-1}건 ({len(f_works)}슬롯)"
                    al = alarm_store.add_facility_alarm(facility, summary, total_sec, source="시설 알람", works_count=len(f_works))
                elif remaining_sec > 0:
                    summary = item_name or f"{facility} 작업"
                    al = alarm_store.add_facility_alarm(facility, summary, remaining_sec, source="시설 알람", works_count=1)
                else:
                    self._send_error(f"'{facility}'에 진행 중인 가공 작업이 없습니다.", 400)
                    return

                self._send_json({"status": "success", "alarm": al})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/add_current_quest_target":
            if is_busy:
                self._send_error("다른 작업 진행 중에는 퀘스트 목록을 조회할 수 없습니다.", 409)
                return
            try:
                added = manager_instance.add_detected_quests_to_targets()
                invalidate_plan_caches()
                if added:
                    add_log("success", f"[주간 납품 등록] 현재 퀘스트 {len(added)}건이 납품 목표 목록에 등록되었습니다.")
                else:
                    add_log("warn", "현재 게임 내 퀘스트 추적창에서 감지된 주간 납품 퀘스트가 없습니다.")
                self._send_json({"status": "success", "added": added})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/add_delivery_target":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length)
            try:
                req_json = json.loads(body_bytes.decode("utf-8"))
                item_name = req_json.get("item_name", "").strip()
                goal = int(req_json.get("goal", 1))
                quest_title = req_json.get("quest_title", "주간 납품")
                req_current = req_json.get("current")
                if not item_name or goal <= 0:
                    self._send_error("올바른 아이템명과 수량을 입력해주세요.", 400)
                    return
                existing_entry = delivery_target_store.targets.get(item_name, {})
                prev_cur = int(existing_entry.get("current", 0))
                eff_cur = prev_cur if is_busy else manager_instance.get_effective_owned(item_name, include_storage=False)
                if req_current is not None and str(req_current).strip() != "":
                    try:
                        cur = max(0, int(req_current))
                    except Exception:
                        cur = max(prev_cur, eff_cur)
                else:
                    cur = max(prev_cur, eff_cur)
                entry = delivery_target_store.add_or_update(item_name, goal, cur, quest_title)
                invalidate_plan_caches()
                add_log("success", f"[납품 목표 등록] '{item_name}' (목표: {goal}개 / 현재: {cur}개) 등록 완료")
                self._send_json({"status": "success", "target": entry})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/update_delivery_target_current":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length)
            try:
                req_json = json.loads(body_bytes.decode("utf-8"))
                item_name = req_json.get("item_name", "").strip()
                current = int(req_json.get("current", 0))
                if not item_name:
                    self._send_error("올바른 아이템명을 입력해주세요.", 400)
                    return
                entry = delivery_target_store.update_current(item_name, current)
                invalidate_plan_caches()
                if entry:
                    add_log("info", f"[보유 수량 수정] '{item_name}' 보유 수량이 {entry['current']}/{entry['goal']}개로 갱신되었습니다.")
                    self._send_json({"status": "success", "target": entry})
                else:
                    self._send_error("해당 아이템을 찾을 수 없습니다.", 404)
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/reset_delivery_targets_current":
            try:
                reset_count = delivery_target_store.reset_all_current()
                invalidate_plan_caches()
                with status_lock:
                    cached_delivery_targets = None
                char_name = delivery_target_store.get_characters_summary().get("active_character_name", "현재 캐릭터")
                add_log("info", f"[{char_name} 보유 수량 초기화] 등록된 {reset_count}개 납품 품목의 보유 수량이 모두 0개로 초기화되었습니다.")
                self._send_json({
                    "status": "success",
                    "reset_count": reset_count,
                    "characters_info": delivery_target_store.get_characters_summary()
                })
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/sync_delivery_targets_inventory":
            try:
                cleared_count = delivery_target_store.clear_all_overrides()
                invalidate_plan_caches()
                with status_lock:
                    cached_delivery_targets = None
                char_name = delivery_target_store.get_characters_summary().get("active_character_name", "현재 캐릭터")
                add_log("info", f"[{char_name} 가방 재고 재동기화] 수동 0개 고정({cleared_count}개 항목)을 해제하고 인게임 가방 수량과 다시 동기화합니다.")
                self._send_json({
                    "status": "success",
                    "cleared_count": cleared_count,
                    "characters_info": delivery_target_store.get_characters_summary()
                })
            except Exception as e:
                self._send_error(e)
            return

        # --- Multi-Character Delivery Profile Management Endpoints ---
        if path == "/api/delivery_characters/select":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                char_id = str(req_json.get("char_id") or "").strip()
                selected = delivery_target_store.select_character(char_id)
                if not selected:
                    self._send_error("선택할 캐릭터 프로필을 찾을 수 없습니다.", 404)
                    return
                homework_instance.on_delivery_character_selected(char_id)
                invalidate_plan_caches()
                with status_lock:
                    cached_delivery_targets = None
                add_log("info", f"[캐릭터 탭 전환] '{selected.get('name', char_id)}' 프로필로 전환했습니다.")
                self._send_json({
                    "status": "success",
                    "characters_info": delivery_target_store.get_characters_summary(),
                    "homework": homework_instance.get_dashboard_state()
                })
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/delivery_characters/add":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                name = str(req_json.get("name") or "").strip()
                init_mode = str(req_json.get("init_mode") or "default_18").strip()
                link_ingame = bool(req_json.get("link_current_ingame", False))
                live_char = None
                if link_ingame and not is_busy:
                    try:
                        live_char = cli_instance.get_my_info(bypass_cache=True)
                    except Exception:
                        live_char = cached_character_info
                created = delivery_target_store.add_character(
                    name=name,
                    init_mode=init_mode,
                    link_current_ingame=link_ingame,
                    ingame_char_info=live_char or cached_character_info
                )
                homework_instance.on_delivery_character_added(created)
                invalidate_plan_caches()
                with status_lock:
                    cached_delivery_targets = None
                add_log("success", f"[캐릭터 추가] '{created.get('name')}' 프로필이 생성되었습니다 (기본 {len(created.get('targets', {}))}개 품목 등록).")
                self._send_json({
                    "status": "success",
                    "character": created,
                    "characters_info": delivery_target_store.get_characters_summary(),
                    "homework": homework_instance.get_dashboard_state()
                })
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/delivery_characters/rename":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                char_id = str(req_json.get("char_id") or "").strip()
                new_name = str(req_json.get("name") or "").strip()
                if not new_name:
                    self._send_error("변경할 캐릭터 이름을 입력해주세요.", 400)
                    return
                chars_before = delivery_target_store.get_characters_summary().get("characters", [])
                old_name = ""
                for c in chars_before:
                    if c.get("id") == char_id:
                        old_name = c.get("name", "")
                        break
                updated = delivery_target_store.rename_character(char_id, new_name)
                if not updated:
                    self._send_error("이름을 변경할 캐릭터 프로필을 찾을 수 없습니다.", 404)
                    return
                if old_name:
                    homework_instance.on_delivery_character_renamed(old_name, updated.get("name", new_name))
                add_log("info", f"[캐릭터 이름 변경] 프로필 이름이 '{updated.get('name')}'(으)로 변경되었습니다.")
                self._send_json({
                    "status": "success",
                    "characters_info": delivery_target_store.get_characters_summary(),
                    "homework": homework_instance.get_dashboard_state()
                })
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/delivery_characters/delete":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                char_id = str(req_json.get("char_id") or "").strip()
                chars_before = delivery_target_store.get_characters_summary().get("characters", [])
                deleted_name = ""
                for c in chars_before:
                    if c.get("id") == char_id:
                        deleted_name = c.get("name", "")
                        break
                ok, msg = delivery_target_store.delete_character(char_id)
                if not ok:
                    self._send_error(msg, 400)
                    return
                if deleted_name:
                    homework_instance.on_delivery_character_deleted(deleted_name)
                invalidate_plan_caches()
                with status_lock:
                    cached_delivery_targets = None
                add_log("info", f"[캐릭터 삭제] {msg}")
                self._send_json({
                    "status": "success",
                    "message": msg,
                    "characters_info": delivery_target_store.get_characters_summary(),
                    "homework": homework_instance.get_dashboard_state()
                })
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/delivery_characters/link_ingame":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                char_id = str(req_json.get("char_id") or "").strip()
                live_char = None
                if not is_busy:
                    try:
                        live_char = cli_instance.get_my_info(bypass_cache=True)
                        if live_char:
                            with status_lock:
                                cached_character_info = live_char
                                cached_character_time = time.time()
                    except Exception:
                        live_char = None
                ok, msg = delivery_target_store.link_character_to_ingame(
                    char_id=char_id or None,
                    char_info=live_char or cached_character_info
                )
                if not ok:
                    self._send_error(msg, 400)
                    return
                homework_instance.on_delivery_character_linked(live_char or cached_character_info)
                invalidate_plan_caches()
                with status_lock:
                    cached_delivery_targets = None
                add_log("success", f"[인게임 캐릭터 연동] {msg}")
                self._send_json({
                    "status": "success",
                    "message": msg,
                    "characters_info": delivery_target_store.get_characters_summary(),
                    "homework": homework_instance.get_dashboard_state()
                })
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/delivery_characters/toggle_auto_switch":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                enabled = bool(req_json.get("enabled", True))
                delivery_target_store.set_auto_switch(enabled)
                add_log("info", f"[캐릭터 자동 전환] 인게임 캐릭터 변경 시 탭 자동 전환 기능이 {'켜졌습니다(ON)' if enabled else '꺼졌습니다(OFF)'}.")
                self._send_json({
                    "status": "success",
                    "characters_info": delivery_target_store.get_characters_summary()
                })
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/delivery_characters/complete_delivery":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                char_id = str(req_json.get("char_id") or "").strip() or None
                ok, msg, details = delivery_target_store.complete_character_delivery(char_id=char_id)
                if not ok:
                    self._send_error(msg, 400)
                    return
                invalidate_plan_caches()
                with status_lock:
                    cached_delivery_targets = None
                add_log("success", msg)
                self._send_json({
                    "status": "success",
                    "message": msg,
                    "details": details,
                    "characters_info": delivery_target_store.get_characters_summary(),
                })
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/delivery_characters/set_weekly_delivered":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                char_id = str(req_json.get("char_id") or "").strip() or None
                delivered = bool(req_json.get("delivered", False))
                ok, msg = delivery_target_store.set_character_weekly_delivered(char_id=char_id, delivered=delivered)
                if not ok:
                    self._send_error(msg, 400)
                    return
                add_log("info", f"[주간 납품 완료 마크] {msg}")
                self._send_json({
                    "status": "success",
                    "message": msg,
                    "characters_info": delivery_target_store.get_characters_summary(),
                })
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/reorder_delivery_targets":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                ordered_names = req_json.get("ordered_names")
                from_item = str(req_json.get("from_item") or "").strip()
                to_item = str(req_json.get("to_item") or "").strip()
                mode = str(req_json.get("mode") or "swap").strip()
                if isinstance(ordered_names, list) and ordered_names:
                    delivery_target_store.reorder_targets(ordered_names, sync_order_all_characters=True)
                elif from_item and to_item:
                    delivery_target_store.move_target_position(from_item, to_item, mode=mode, sync_order_all_characters=True)
                else:
                    self._send_error("변경할 순서 정보가 없습니다.", 400)
                    return
                invalidate_plan_caches()
                with status_lock:
                    cached_delivery_targets = None
                self._send_json({
                    "status": "success",
                    "targets": delivery_target_store.get_all(),
                    "characters_info": delivery_target_store.get_characters_summary(),
                })
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/delete_delivery_target":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length)
            try:
                req_json = json.loads(body_bytes.decode("utf-8"))
                item_name = str(req_json.get("item_name", "")).strip()
                if item_name:
                    delivery_target_store.delete(item_name)
                    invalidate_plan_caches()
                    add_log("info", f"[납품 목표 삭제] '{item_name}' 항목이 목표 목록에서 제거되었습니다.")
                self._send_json({"status": "success"})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/clear_delivery_targets":
            try:
                delivery_target_store.clear()
                invalidate_plan_caches()
                add_log("info", "[납품 목표 초기화] 모든 등록된 납품 목표가 삭제되었습니다.")
                self._send_json({"status": "success"})
            except Exception as e:
                self._send_error(e)
            return

        # --- Custom Craft Targets CRUD ---
        if path == "/api/add_custom_target":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length)
            try:
                req_json = json.loads(body_bytes.decode("utf-8"))
                item_name = req_json.get("item_name", "").strip()
                goal = int(req_json.get("goal", 1))
                req_current = req_json.get("current")

                if not item_name or goal <= 0:
                    self._send_error("올바른 아이템명과 수량을 입력해주세요.", 400)
                    return
                existing_entry = custom_target_store.targets.get(item_name, {})
                prev_cur = int(existing_entry.get("current", 0))
                eff_cur = prev_cur if is_busy else manager_instance.get_effective_owned(item_name)
                if req_current is not None and str(req_current).strip() != "":
                    try:
                        cur = max(0, int(req_current))
                    except Exception:
                        cur = max(prev_cur, eff_cur)
                else:
                    cur = max(prev_cur, eff_cur)
                entry = custom_target_store.add_or_update(item_name, goal, cur, quest_title="개별 지정 제작")
                invalidate_plan_caches()
                add_log("success", f"[개별 제작 등록] '{item_name}' (목표: {goal}개 / 현재: {cur}개) 등록 완료")
                self._send_json({"status": "success", "target": entry})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/update_custom_target_current":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length)
            try:
                req_json = json.loads(body_bytes.decode("utf-8"))
                item_name = req_json.get("item_name", "").strip()
                current = int(req_json.get("current", 0))
                if not item_name:
                    self._send_error("올바른 아이템명을 입력해주세요.", 400)
                    return
                entry = custom_target_store.update_current(item_name, current)
                invalidate_plan_caches()
                if entry:
                    add_log("info", f"[개별 보유 수량 수정] '{item_name}' 보유 수량이 {entry['current']}/{entry['goal']}개로 갱신되었습니다.")
                    self._send_json({"status": "success", "target": entry})
                else:
                    self._send_error("해당 아이템을 찾을 수 없습니다.", 404)
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/delete_custom_target":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length)
            try:
                req_json = json.loads(body_bytes.decode("utf-8"))
                item_name = str(req_json.get("item_name", "")).strip()
                if item_name:
                    custom_target_store.delete(item_name)
                    invalidate_plan_caches()
                    add_log("info", f"[개별 제작 삭제] '{item_name}' 항목이 목표 목록에서 제거되었습니다.")
                self._send_json({"status": "success"})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/clear_custom_targets":
            try:
                custom_target_store.clear()
                invalidate_plan_caches()
                add_log("info", "[개별 제작 초기화] 모든 등록된 개별 제작 목표가 삭제되었습니다.")
                self._send_json({"status": "success"})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/reset_custom_targets_current":
            try:
                reset_count = custom_target_store.reset_all_current()
                invalidate_plan_caches()
                add_log("info", f"[개별 제작 수량 초기화] 등록된 {reset_count}개 목표의 보유 수량이 모두 0개로 초기화되었습니다.")
                self._send_json({"status": "success", "reset_count": reset_count})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/sync_custom_targets_inventory":
            try:
                sync_count = custom_target_store.clear_all_overrides()
                invalidate_plan_caches()
                add_log("info", f"[인벤토리 동기화] 등록된 {sync_count}개 개별 제작 목표의 수동 수량이 해제되어 실제 인벤토리와 다시 동기화됩니다.")
                self._send_json({"status": "success", "sync_count": sync_count})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/update_recipe":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length)
            try:
                req_json = json.loads(body_bytes.decode("utf-8"))
                item_name = req_json.get("item_name", "").strip()
                ingredients = req_json.get("ingredients", {})
                if not item_name:
                    self._send_error("아이템 이름을 입력해주세요.", 400)
                    return
                clean_ingredients = {}
                if isinstance(ingredients, dict):
                    for k, v in ingredients.items():
                        k_str = str(k).strip()
                        try:
                            v_int = int(v)
                        except Exception:
                            continue
                        if k_str and v_int > 0:
                            clean_ingredients[k_str] = v_int
                manager_instance.update_recipe(item_name, clean_ingredients)
                invalidate_plan_caches()
                add_log("success", f"[레시피 갱신] '{item_name}' 제작 레시피 저장 완료: {clean_ingredients}")
                self._send_json({"status": "success", "item_name": item_name, "ingredients": clean_ingredients})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/save_delivery_preset":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length)
            try:
                req_json = json.loads(body_bytes.decode("utf-8"))
                preset_name = req_json.get("name", "").strip()
                if not preset_name:
                    self._send_error("프리셋 이름을 입력해주세요.", 400)
                    return
                # Save current delivery targets as preset
                targets = delivery_target_store.get_all()
                if not targets:
                    self._send_error("저장할 납품 목표가 없습니다. 먼저 납품 목표를 등록해주세요.", 400)
                    return
                items = [{"item_name": t["item_name"], "goal": t["goal"], "quest_title": t.get("quest_title", "주간 납품")} for t in targets]
                preset = delivery_preset_store.save_preset(preset_name, items)
                add_log("success", f"[프리셋 저장] '{preset_name}' ({len(items)}개 항목) 프리셋이 저장되었습니다.")
                self._send_json({"status": "success", "preset": preset})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/delete_delivery_preset":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length)
            try:
                req_json = json.loads(body_bytes.decode("utf-8"))
                preset_name = req_json.get("name", "")
                if preset_name:
                    delivery_preset_store.delete_preset(preset_name)
                    add_log("info", f"[프리셋 삭제] '{preset_name}' 프리셋이 삭제되었습니다.")
                self._send_json({"status": "success"})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/load_delivery_preset":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length)
            try:
                req_json = json.loads(body_bytes.decode("utf-8"))
                preset_name = req_json.get("name", "")
                reset_current = bool(req_json.get("reset_current", False))
                preset = delivery_preset_store.get_preset(preset_name)
                if not preset:
                    self._send_error(f"'{preset_name}' 프리셋을 찾을 수 없습니다.", 404)
                    return
                # Backup previous target currents if not resetting
                prev_targets = {t["item_name"]: t for t in delivery_target_store.get_all()}
                delivery_target_store.clear()
                loaded_count = 0
                for item in preset.get("items", []):
                    item_name = item.get("item_name", "")
                    goal = int(item.get("goal", 1))
                    quest_title = item.get("quest_title", "주간 납품")
                    if item_name and goal > 0:
                        if reset_current:
                            cur = 0
                        else:
                            prev_entry = prev_targets.get(item_name, {})
                            prev_cur = int(prev_entry.get("current", 0))
                            eff_cur = prev_cur if is_busy else manager_instance.get_effective_owned(item_name)
                            cur = max(prev_cur, eff_cur)
                        delivery_target_store.add_or_update(item_name, goal, cur, quest_title)
                        loaded_count += 1
                mode_str = " (보유 수량 0개로 초기화)" if reset_current else " (기존 보유 수량 승계)"
                invalidate_plan_caches()
                add_log("success", f"[프리셋 불러오기] '{preset_name}' ({loaded_count}개 항목) 프리셋이 등록되었습니다.{mode_str}")
                self._send_json({"status": "success", "loaded_count": loaded_count, "reset_current": reset_current})
            except Exception as e:
                self._send_error(e)
            return

        if path.startswith("/api/collect_all_completed_altering"):
            if is_busy:
                self._send_error("이미 다른 작업이 진행 중입니다.", 409)
                return

            only_fully = ("mode=fully_completed" in path)
            is_busy = True
            current_task_info = "시설 전체 완료 가공품 수령 중..." if only_fully else "완료 가공품 순차 수령 중..."

            def collect_worker():
                global is_busy, current_task_info
                try:
                    collected = manager_instance.collect_completed_altering_works(
                        callback=add_log,
                        only_fully_completed=only_fully
                    )
                    if not collected:
                        if only_fully:
                            add_log("info", "모든 작업이 완료된 가공 시설이 없습니다 (진행 중인 작업 완료 대기).")
                        else:
                            add_log("info", "수령할 완료된 가공품이 없습니다.")
                except Exception as err:
                    add_log("error", f"가공품 수령 실패: {str(err)}")
                finally:
                    is_busy = False
                    current_task_info = ""

            threading.Thread(target=collect_worker, daemon=True).start()
            self._send_json({"status": "started"})
            return

        if path == "/api/import_selected_quests":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                items = req_json.get("items", [])
                if not isinstance(items, list) or not items:
                    self._send_error("가져올 퀘스트 항목을 1개 이상 선택해주세요.", 400)
                    return

                imported_delivery = 0
                imported_custom = 0
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    item_name = str(it.get("item_name") or "").strip()
                    goal = int(it.get("goal") or 1)
                    if not item_name or goal <= 0:
                        continue
                    cur = int(it.get("current") or 0)
                    if not is_busy:
                        try:
                            cur = max(cur, manager_instance.get_effective_owned(item_name))
                        except Exception:
                            pass
                    q_title = str(it.get("quest_title") or "퀘스트 선택 가져오기").strip()
                    target_mode = str(it.get("target_mode") or "delivery").strip()
                    if target_mode == "custom":
                        custom_target_store.add_or_update(item_name, goal, cur, quest_title=q_title)
                        imported_custom += 1
                    else:
                        delivery_target_store.add_or_update(item_name, goal, cur, quest_title=q_title)
                        imported_delivery += 1

                invalidate_plan_caches()
                add_log("success", f"[퀘스트 선택 가져오기] 주간 납품 {imported_delivery}건, 지정 제작 {imported_custom}건 등록 완료")
                self._send_json({
                    "status": "success",
                    "imported_delivery": imported_delivery,
                    "imported_custom": imported_custom
                })
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/safety_stocks":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                action = str(req_json.get("action") or "save").strip()
                cur_stocks = dict(settings_store.get("safety_stocks") or {})

                if action == "save":
                    item_name = str(req_json.get("item_name") or "").strip()
                    min_qty = int(req_json.get("min_qty") or 0)
                    if not item_name or min_qty <= 0:
                        self._send_error("올바른 아이템명과 최소 유지 수량을 입력해주세요.", 400)
                        return
                    cur_stocks[item_name] = min_qty
                    settings_store.update({"safety_stocks": cur_stocks})
                    add_log("success", f"[안전 재고 설정] '{item_name}' 최소 유지 재고 {min_qty}개로 저장")
                    self._send_json({"status": "success", "stocks": manager_instance.get_safety_stocks_status()})
                elif action == "delete":
                    item_name = str(req_json.get("item_name") or "").strip()
                    if item_name in cur_stocks:
                        del cur_stocks[item_name]
                        settings_store.update({"safety_stocks": cur_stocks})
                        add_log("info", f"[안전 재고 삭제] '{item_name}' 안전 재고 항목 제거")
                    self._send_json({"status": "success", "stocks": manager_instance.get_safety_stocks_status()})
                elif action == "apply":
                    added = manager_instance.apply_safety_stocks_to_custom()
                    invalidate_plan_caches()
                    if added:
                        names = ", ".join([f"{a['item_name']}(+{a['deficit']}개)" for a in added])
                        add_log("success", f"[안전 재고 자동 보충] 부족분 {len(added)}건을 지정 제작 목표에 일괄 추가했습니다: {names}")
                    else:
                        add_log("info", "[안전 재고 자동 보충] 모든 상비약/요리 안전 재고가 이미 충분합니다.")
                    self._send_json({"status": "success", "added": added, "stocks": manager_instance.get_safety_stocks_status()})
                else:
                    self._send_error("알 수 없는 action입니다.", 400)
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/execute_social_or_music":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                action = str(req_json.get("action") or "").strip()
                if action == "play_music":
                    title = str(req_json.get("title") or "").strip()
                    res = cli_instance.play_music_score(title if title else None)
                    add_log("success", f"[악보 연주] '{title or '기본 악보'}' 연주 시작 요청 완료")
                    self._send_json({"status": "success", "result": res})
                elif action == "change_instrument":
                    name = str(req_json.get("name") or "").strip()
                    if not name:
                        self._send_error("변경할 악기 이름을 선택해주세요.", 400)
                        return
                    res = cli_instance.change_instrument(name)
                    add_log("success", f"[악기 변경] '{name}' 장착 요청 완료")
                    self._send_json({"status": "success", "result": res})
                elif action == "stand_up":
                    res = cli_instance.stand_up()
                    add_log("info", "[캐릭터 제어] 자리에서 일어나기(stand_up) 실행")
                    self._send_json({"status": "success", "result": res})
                elif action == "stop_action":
                    res = cli_instance.stop_action()
                    add_log("info", "[캐릭터 제어] 현재 행동 중지(stop_action) 실행")
                    self._send_json({"status": "success", "result": res})
                else:
                    self._send_error("지원하지 않는 동작입니다.", 400)
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/execute_smart_fishing":
            if is_busy:
                self._send_error("이미 다른 작업이 진행 중입니다.", 409)
                return

            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                fish_name = str(req_json.get("fish_name") or "고등어").strip()
                target_count = int(req_json.get("target_count") or 30)
                max_minutes = int(req_json.get("max_minutes") or 30)

                manager_instance.reset_abort()
                is_busy = True
                current_task_info = f"[스마트 자동 낚시] '{fish_name}' 목표 {target_count}마리 (최대 {max_minutes}분) 진행 중..."

                def fishing_worker():
                    global is_busy, current_task_info, last_execution_summary, last_summary_id
                    try:
                        def update_task_status(msg):
                            global current_task_info
                            current_task_info = msg

                        res = manager_instance.execute_smart_fishing(
                            fish_name=fish_name,
                            target_count=target_count,
                            max_minutes=max_minutes,
                            callback=add_log,
                            status_reporter=update_task_status
                        )
                        if isinstance(res, dict) and "summary" in res:
                            with status_lock:
                                last_execution_summary = res["summary"]
                                last_summary_id += 1
                        if not manager_instance.abort_requested:
                            manager_instance.try_auto_play_music(callback=add_log)
                    except Exception as err:
                        add_log("error", f"스마트 낚시 오류: {str(err)}")
                    finally:
                        is_busy = False
                        current_task_info = ""

                threading.Thread(target=fishing_worker, daemon=True).start()
                self._send_json({"status": "started", "fish_name": fish_name, "target_count": target_count, "max_minutes": max_minutes})
            except Exception as e:
                self._send_error(e)
            return

        # --- Homework Manager Endpoints ---
        if path == "/api/homework/select_character":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                char_name = str(req_json.get("char_name") or "").strip()
                res = homework_instance.select_character(char_name)
                invalidate_plan_caches()
                with status_lock:
                    cached_delivery_targets = None
                add_log("info", f"[캐릭터 탭 전환] '{res.get('active_character', char_name)}' 프로필로 전환했습니다.")
                self._send_json({
                    "status": "success",
                    "homework": res,
                    "characters_info": delivery_target_store.get_characters_summary()
                })
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/homework/toggle_task":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                char_name = str(req_json.get("char_name") or "").strip()
                cat_key = str(req_json.get("category") or "daily").strip()
                task_name = str(req_json.get("task_name") or "").strip()
                force_value = req_json.get("value", None)
                res = homework_instance.toggle_task(char_name, cat_key, task_name, force_value=force_value)
                self._send_json({"status": "success", "homework": res})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/homework/complete_step":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                char_name = str(req_json.get("char_name") or "").strip() or None
                res = homework_instance.complete_current_step(char_name=char_name)
                self._send_json(res)
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/homework/rollback_step":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                char_name = str(req_json.get("char_name") or "").strip() or None
                res = homework_instance.rollback_last_step(char_name=char_name)
                self._send_json(res)
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/homework/batch_category":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                char_name = str(req_json.get("char_name") or "").strip()
                cat_key = str(req_json.get("category") or "daily").strip()
                completed = bool(req_json.get("completed", True))
                res = homework_instance.batch_set_category(char_name, cat_key, completed=completed)
                self._send_json({"status": "success", "homework": res})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/homework/batch_weekly_task":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                task_name = str(req_json.get("task_name") or "").strip()
                completed = bool(req_json.get("completed", True))
                res = homework_instance.batch_set_weekly_task_all_chars(task_name, completed=completed)
                self._send_json({"status": "success", "homework": res})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/homework/save_memo":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                char_name = str(req_json.get("char_name") or "").strip()
                memo_text = str(req_json.get("memo") or "")
                res = homework_instance.save_memo(char_name, memo_text)
                self._send_json({"status": "success", "homework": res})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/homework/character_crud":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                action = str(req_json.get("action") or "").strip()
                if action == "add":
                    res = homework_instance.add_character(
                        name=str(req_json.get("name") or ""),
                        char_class=str(req_json.get("char_class") or ""),
                        combat_power=req_json.get("combat_power", 0),
                        title=str(req_json.get("title") or ""),
                    )
                elif action == "update":
                    res = homework_instance.update_character(
                        old_name=str(req_json.get("old_name") or ""),
                        new_name=str(req_json.get("new_name") or ""),
                        char_class=req_json.get("char_class"),
                        combat_power=req_json.get("combat_power"),
                        title=req_json.get("title"),
                    )
                elif action == "delete":
                    res = homework_instance.delete_character(str(req_json.get("name") or ""))
                elif action == "move":
                    res = homework_instance.move_character(
                        name=str(req_json.get("name") or ""),
                        direction=int(req_json.get("direction") or -1),
                    )
                else:
                    self._send_error("알 수 없는 캐릭터 관리 명령입니다.", 400)
                    return
                invalidate_plan_caches()
                with status_lock:
                    cached_delivery_targets = None
                res["characters_info"] = delivery_target_store.get_characters_summary()
                self._send_json(res)
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/homework/task_crud":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                action = str(req_json.get("action") or "").strip()
                cat_key = str(req_json.get("category") or "daily").strip()
                if action == "add":
                    res = homework_instance.add_task(cat_key, str(req_json.get("task_name") or ""))
                elif action == "rename":
                    res = homework_instance.rename_task(
                        cat_key,
                        str(req_json.get("old_name") or ""),
                        str(req_json.get("new_name") or ""),
                    )
                elif action == "delete":
                    res = homework_instance.delete_task(cat_key, str(req_json.get("task_name") or ""))
                elif action == "move":
                    res = homework_instance.move_task(
                        cat_key,
                        str(req_json.get("task_name") or ""),
                        int(req_json.get("direction") or -1),
                    )
                elif action == "set_targets":
                    res = homework_instance.set_task_target_characters(
                        cat_key,
                        str(req_json.get("task_name") or ""),
                        req_json.get("target_chars"),
                    )
                elif action == "reset_defaults":
                    res = homework_instance.reset_category_default_tasks(cat_key)
                else:
                    self._send_error("알 수 없는 숙제 항목 관리 명령입니다.", 400)
                    return
                self._send_json(res)
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/homework/settings":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                res = homework_instance.update_settings(req_json)
                self._send_json(res)
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/homework/toggle_weekly_focus":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                res = homework_instance.set_weekly_focus_done(
                    char_name=req_json.get("char_name"),
                    done=req_json.get("done"),
                )
                self._send_json(res)
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/homework/import_backup":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                res = homework_instance.import_backup_bundle(req_json)
                invalidate_plan_caches()
                with status_lock:
                    cached_delivery_targets = None
                res["characters_info"] = delivery_target_store.get_characters_summary()
                self._send_json(res)
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/homework/reimport_legacy":
            try:
                res = homework_instance.reimport_from_legacy_overlay()
                invalidate_plan_caches()
                with status_lock:
                    cached_delivery_targets = None
                res["characters_info"] = delivery_target_store.get_characters_summary()
                self._send_json(res)
            except Exception as e:
                self._send_error(e)
            return

        self._send_data(404, b"Not Found", "text/plain")

def _get_app_data_dir() -> str:
    """Returns a dedicated user-data-dir for the app window, isolated from the user's normal Chrome profile."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, ".mabi_supporter_browser_data")

def _cleanup_browser_locks(data_dir: str):
    """
    Removes stale lock files (SingletonLock, SingletonSocket, SingletonCookie)
    left behind by unclean Chrome shutdowns. Without this cleanup, Chrome sees
    the lock, thinks another instance owns the profile, and silently exits
    without opening a window — producing only a background process.
    """
    if not os.path.isdir(data_dir):
        return
    lock_files = ["SingletonLock", "SingletonSocket", "SingletonCookie", "lockfile"]
    for name in lock_files:
        lock_path = os.path.join(data_dir, name)
        try:
            if os.path.exists(lock_path):
                os.remove(lock_path)
        except OSError:
            pass

def launch_app_window(url: str):
    """
    Opens the web UI in a regular Chrome tab (or default browser tab)
    just like a normal website, rather than a standalone isolated app window.
    """
    chrome_paths = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    ]
    edge_paths = [
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
    ]

    for p in chrome_paths:
        if os.path.isfile(p):
            try:
                # Opens as a standard new Chrome tab
                subprocess.Popen([p, url])
                return
            except Exception:
                pass

    for p in edge_paths:
        if os.path.isfile(p):
            try:
                subprocess.Popen([p, url])
                return
            except Exception:
                pass

    try:
        webbrowser.open_new_tab(url)
    except Exception:
        pass

import socket

active_server_port = 8080

def get_local_ip() -> str:
    """Returns local LAN IP (e.g. 192.168.0.x) for mobile device connection."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def start_server(port=8080):
    global server_instance, server_start_time, active_server_port
    server_start_time = time.time()
    ThreadingHTTPServer.allow_reuse_address = True

    # 1. First check if a running instance on target port is already active
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/heartbeat", timeout=0.8) as resp:
            if resp.status == 200:
                launch_app_window(f"http://127.0.0.1:{port}")
                return
    except Exception:
        pass

    # 2. Try to bind starting from port up to port+10 (prefer 0.0.0.0 for local Wi-Fi LAN, fallback to 127.0.0.1)
    server = None
    active_port = port
    active_server_port = port
    for p in range(port, port + 10):
        try:
            server = ThreadingHTTPServer(("0.0.0.0", p), RequestHandler)
            active_port = p
            active_server_port = p
            break
        except OSError:
            try:
                server = ThreadingHTTPServer(("127.0.0.1", p), RequestHandler)
                active_port = p
                active_server_port = p
                break
            except OSError:
                continue

    if server is None:
        time.sleep(1.0)
        try:
            server = ThreadingHTTPServer(("0.0.0.0", port), RequestHandler)
            active_port = port
            active_server_port = port
        except OSError:
            try:
                server = ThreadingHTTPServer(("127.0.0.1", port), RequestHandler)
                active_port = port
                active_server_port = port
            except OSError:
                launch_app_window(f"http://127.0.0.1:{port}")
                return

    server_instance = server

    # Start auto-shutdown watchdog thread
    threading.Thread(target=watchdog_loop, daemon=True).start()
    # Background homework telemetry (auto-check works even with the dashboard minimized)
    threading.Thread(target=homework_telemetry_loop, daemon=True).start()

    # Automatically launch standalone desktop app window once server is confirmed listening
    def _open_app_when_ready():
        time.sleep(0.4)
        launch_app_window(f"http://127.0.0.1:{active_port}")

    threading.Thread(target=_open_app_when_ready, daemon=True).start()

    try:
        server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>모비노기 지원도구</title>
  <link rel="icon" type="image/x-icon" href="/favicon.ico">
  <link rel="apple-touch-icon" href="/api/icon">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Pretendard:wght@400;600;700&family=JetBrains+Mono:wght@400;600;700&family=Fira+Code:wght@400;600;700&display=swap" rel="stylesheet">
  <script>
    (function() {
      try {
        var t = localStorage.getItem('mabi_theme') || 'dark';
        document.documentElement.setAttribute('data-theme', t);
      } catch (e) {}
    })();
  </script>
  <style>
    :root,
    :root[data-theme="dark"] {
      /* [다크모드 ON - 기본 설정] 눈부심 없는 부드러운 다크 톤 & 편안한 대비 */
      --bg: #121316; /* 칠흑 대신 깊고 편안한 딥 차콜 */
      --bg-panel: #18191D;
      --bg-sub-panel: #1F2126;
      --bg-elevated: #1E2025;
      --bg-table-header: #23252A;
      --bg-console: #151619;
      --bg-mcb: #18191D;
      --bg-mcb-running: #1B231E;
      --line: #2A2C31;
      --line-subtle: #222428;
      --border-line: #2A2C31;
      --border-focus: #9BA2AB;
      --text: #C4C8CE; /* 기존 #E5E5E5의 눈부심 완화: 부드럽고 선명한 소프트 실버 */
      --text-main: #C4C8CE;
      --text-bright: #DFE2E6; /* 순백 #F5F5F5 대신 차분한 펄 화이트 (눈 피로 제로) */
      --text-title: #DFE2E6;
      --text-muted: #7D848D;
      --text-dim: #545A62;
      --invert-bg: #C5CAD0; /* 눈부시지 않은 차콜 그레이 반전 배지 */
      --invert-text: #141618;
      --badge-manual-bg: #23252A;
      --badge-dim-bg: #1B1D21;
      --table-hover-bg: #1D1F24;
      --tab-pill-bg: #222429;
      --card-border: rgba(255, 255, 255, 0.22);
      --card-border-hover: rgba(255, 255, 255, 0.55);
      --card-border-done: rgba(255, 255, 255, 0.10);
      --card-box-shadow: 0 0 8px rgba(0, 0, 0, 0.25);
      --card-box-shadow-hover: 0 0 12px rgba(255, 255, 255, 0.10);
      --btn-boxed-bg: rgba(255, 255, 255, 0.05);
      --btn-boxed-hover: rgba(255, 255, 255, 0.10);
      --btn-boxed-shadow: 0 0 4px rgba(255, 255, 255, 0.04);
      --btn-facility-idle-bg: rgba(255, 255, 255, 0.02);
      --gauge-empty-bg: rgba(255, 255, 255, 0.04);
      --log-warn-color: #D4A373;
      --modal-overlay-bg: rgba(0, 0, 0, 0.75);
      --modal-shadow: 0 0 24px rgba(0, 0, 0, 0.6);
      --mcb-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
      --badge-shadow: 0 0 4px rgba(197, 202, 208, 0.15);
      --bar-gather: #3FB950;
      --bar-alter: #6E8898;
      --bar-special: #8B949E;
      --font-sans: 'Pretendard', -apple-system, BlinkMacSystemFont, sans-serif;
      --font-mono: 'JetBrains Mono', 'Fira Code', 'D2Coding', Consolas, monospace;
      --fs-lg: 16px;
      --fs-md: 14px;
      --fs-sm: 12px;
    }

    :root[data-theme="light"] {
      /* [다크모드 OFF] 눈이 편안한 따뜻한 웜그레이 & 소프트 대비 팔레트 */
      --bg: #EAE9E4; /* 아주 조금 노란색이 감돌며 한 톤 차분한 웜그레이 */
      --bg-panel: #F6F5F1; /* 눈부심 없는 부드러운 밀크/아이보리 웜화이트 */
      --bg-sub-panel: #DFDDD6; /* 톤다운된 서브 박스/내부 패널 */
      --bg-elevated: #F6F5F1;
      --bg-table-header: #DBD9D2;
      --bg-console: #F6F5F1;
      --bg-mcb: #F6F5F1;
      --bg-mcb-running: #E8EFE8;
      --line: #CAC8BF; /* 너무 도드라지지 않는 부드러운 웜그레이 선 */
      --line-subtle: #DBD9D1;
      --border-line: #CAC8BF;
      --border-focus: #484A4D;
      --text: #404347; /* 칠흑이 아닌 눈이 편안한 소프트 잉크 차콜 */
      --text-main: #404347;
      --text-bright: #26282B; /* 부드럽게 강조되는 딥 차콜 */
      --text-title: #26282B;
      --text-muted: #72767B;
      --text-dim: #989C9F;
      --invert-bg: #323539; /* 너무 새까맣지 않은 차콜 반전 배지 */
      --invert-text: #F7F6F2;
      --badge-manual-bg: #DFDDD6;
      --badge-dim-bg: #E5E4DD;
      --table-hover-bg: #E3E2DA;
      --tab-pill-bg: #DFDDD6;
      --card-border: rgba(0, 0, 0, 0.12);
      --card-border-hover: rgba(0, 0, 0, 0.30);
      --card-border-done: rgba(0, 0, 0, 0.06);
      --card-box-shadow: 0 1px 3px rgba(0, 0, 0, 0.03);
      --card-box-shadow-hover: 0 2px 8px rgba(0, 0, 0, 0.06);
      --btn-boxed-bg: rgba(0, 0, 0, 0.04);
      --btn-boxed-hover: rgba(0, 0, 0, 0.08);
      --btn-boxed-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
      --btn-facility-idle-bg: rgba(0, 0, 0, 0.03);
      --gauge-empty-bg: rgba(0, 0, 0, 0.05);
      --log-warn-color: #B45309;
      --modal-overlay-bg: rgba(28, 30, 32, 0.40);
      --modal-shadow: 0 12px 30px rgba(0, 0, 0, 0.12);
      --mcb-shadow: 0 2px 10px rgba(0, 0, 0, 0.05);
      --badge-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
      --bar-gather: #16A34A;
      --bar-alter: #2563EB;
      --bar-special: #D97706;
      --font-sans: 'Pretendard', -apple-system, BlinkMacSystemFont, sans-serif;
      --font-mono: 'JetBrains Mono', 'Fira Code', 'D2Coding', Consolas, monospace;
      --fs-lg: 16px;
      --fs-md: 14px;
      --fs-sm: 12px;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg);
      color: var(--text);
      font-family: var(--font-sans);
      font-size: var(--fs-md);
      line-height: 1.5;
      word-break: keep-all;
      min-height: 100vh;
      padding: 20px 24px 40px;
      transition: background-color 0.2s ease, color 0.15s ease;
    }
    header, .card, .facility-card, .master-command-bar, .console, .modal-dialog, .bom-table, input {
      transition: background-color 0.2s ease, border-color 0.2s ease, color 0.15s ease;
    }
    ::-webkit-scrollbar {
      width: 6px;
      height: 6px;
    }
    ::-webkit-scrollbar-track {
      background: transparent;
    }
    ::-webkit-scrollbar-thumb {
      background: var(--line);
      border-radius: 3px;
    }
    ::-webkit-scrollbar-thumb:hover {
      background: var(--text-muted);
    }
    .container { max-width: 1320px; margin: 0 auto; }

    /* Monospace & Tabular Numeric Rules */
    .mono-num,
    .cli-spinner,
    .cli-gauge,
    .status-delim,
    .sym-prefix,
    .badge,
    .mcb-tab-pill,
    .input-number,
    .bom-table td:nth-child(3),
    .bom-table td:nth-child(4),
    .bom-table td:nth-child(5),
    .bom-table td:nth-child(6) {
      font-family: var(--font-mono);
      font-variant-numeric: tabular-nums;
    }
    .cli-spinner {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-family: var(--font-mono);
      font-variant-numeric: tabular-nums;
      font-weight: 700;
      color: var(--text-bright);
      white-space: nowrap;
      vertical-align: middle;
    }
    .cli-spinner .spinner-bracket {
      display: inline-block;
      color: var(--text-dim);
      font-family: var(--font-mono);
    }
    .cli-spinner .spinner-char {
      display: inline-block;
      width: 1.2ch;
      text-align: center;
      font-family: var(--font-mono);
      color: var(--text-bright);
    }
    .cli-gauge {
      display: inline-flex;
      align-items: center;
      gap: 1px;
      vertical-align: middle;
      font-family: var(--font-mono);
      letter-spacing: 0;
      color: var(--text-bright);
      line-height: 1;
      user-select: none;
    }
    .cli-gauge .gauge-bracket {
      color: inherit;
      font-family: var(--font-mono);
      font-weight: 600;
      opacity: 0.85;
      line-height: 1;
    }
    .cli-gauge .gauge-cells {
      display: inline-flex;
      align-items: center;
      gap: 2px;
      margin: 0 1px;
    }
    .cli-gauge .gauge-cell {
      display: inline-block;
      width: 9px;
      height: 9px;
      box-sizing: border-box;
      border-radius: 1px;
      flex-shrink: 0;
    }
    .cli-gauge .gauge-cell.filled {
      background-color: currentColor;
      border: 1px solid currentColor;
    }
    .cli-gauge .gauge-cell.empty {
      background-color: var(--gauge-empty-bg);
      border: 1.5px solid currentColor;
      opacity: 0.45;
    }
    .sym-prefix {
      color: var(--text-muted);
      font-weight: 700;
      margin-right: 6px;
      user-select: none;
      display: inline-block;
      width: 10px;
      text-align: left;
      flex-shrink: 0;
      vertical-align: baseline;
    }
    .card-title .sym-prefix,
    .modal-header .sym-prefix,
    h1 .sym-prefix,
    h2 .sym-prefix,
    h3 .sym-prefix {
      width: auto;
    }

    /* Header (Sticky Line-Centric Terminal Bar) */
    header {
      position: sticky;
      top: 0;
      z-index: 1000;
      display: flex;
      flex-direction: column;
      padding: 12px 0;
      background: var(--bg);
      border-bottom: 1px solid var(--line);
      margin-bottom: 20px;
      gap: 10px;
    }
    .header-top {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 10px;
    }
    .logo-area { display: flex; align-items: center; gap: 10px; }
    .logo-icon {
      width: 24px; height: 24px;
      display: flex; align-items: center; justify-content: center;
      font-family: var(--font-mono);
      font-size: var(--fs-md);
      font-weight: 700;
      color: var(--invert-text);
      background: var(--invert-bg);
      border-radius: 0;
      flex-shrink: 0;
    }
    .title {
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }
    .title h1 {
      font-size: 24px;
      font-weight: 700;
      color: var(--text-bright);
      display: flex;
      align-items: center;
      gap: 10px;
      letter-spacing: -0.3px;
    }

    /* Header Action Buttons (Tier 3: Hover Bracket Style) */
    .header-actions {
      display: flex;
      align-items: center;
      gap: 2px 2px;
      margin-left: auto;
      flex-shrink: 1;
      flex-wrap: wrap;
      max-width: 100%;
    }
    .header-action-btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 2px 3px;
      font-family: var(--font-sans);
      font-size: 12px;
      font-weight: 400;
      color: var(--text-muted);
      background: transparent;
      border: none;
      cursor: pointer;
      white-space: nowrap;
      user-select: none;
      transition: color 0.12s ease;
    }
    .header-action-btn .btn-icon { display: none; }
    .header-action-btn::before {
      content: "[";
      font-family: var(--font-mono);
      margin-right: 1px;
      opacity: 0;
      color: var(--text-bright);
    }
    .header-action-btn::after {
      content: "]";
      font-family: var(--font-mono);
      margin-left: 1px;
      opacity: 0;
      color: var(--text-bright);
    }
    .header-action-btn:hover {
      color: var(--text-bright);
      text-shadow: 0 0 0.5px currentColor;
    }
    .header-action-btn:hover::before,
    .header-action-btn:hover::after {
      opacity: 1;
    }

    /* 상단/하단 영역 구분선 */
    .header-divider {
      height: 1px;
      width: 100%;
      background: var(--line);
    }

    /* 하단 상태 바: 메이븐 (Lv.100) | 날개 7,647 | 무게 1108/1870 */
    .header-status-bar {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      font-size: var(--fs-md);
      color: var(--text);
      font-family: var(--font-mono);
      font-variant-numeric: tabular-nums;
      background: transparent;
      border: none;
      border-radius: 0;
      padding: 2px 0;
    }
    .status-pills { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .status-delim {
      color: var(--text-dim);
      font-weight: 700;
      user-select: none;
    }
    .pill {
      padding: 0;
      border: none;
      background: transparent;
      font-size: var(--fs-md);
      font-weight: 600;
      color: var(--text);
      display: inline-flex;
      align-items: center;
      gap: 6px;
      white-space: nowrap;
    }
    .pill.online { color: var(--text-bright); }
    .pill.offline { color: var(--invert-text); background: var(--invert-bg); padding: 1px 6px; border-radius: 0; font-weight: 700; }
    .pill.warning { color: var(--text-bright); }
    .pill.wings { color: var(--text); }
    .pill.weight { color: var(--text); }

    @media (max-width: 640px) {
      .header-actions {
        width: 100%;
        justify-content: flex-start;
      }
    }

    /* Layout & Surface Cards (Line-Divided Terminal Layout) */
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
    @media (max-width: 1024px) { .grid { grid-template-columns: 1fr; } }

    .card {
      background: transparent;
      border: none;
      border-top: 1px solid var(--line);
      border-radius: 0;
      padding: 18px 0 8px 0;
      margin-bottom: 20px;
      box-shadow: none;
    }
    .card-sub-box {
      background: transparent;
      border: none;
      border-top: 1px solid var(--line-subtle);
      border-bottom: 1px solid var(--line-subtle);
      border-radius: 0;
      padding: 10px 0;
    }
    .target-card {
      background: transparent;
      border: 1px solid var(--card-border);
      border-radius: 0;
      padding: 10px 12px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      gap: 6px;
      box-shadow: var(--card-box-shadow);
      transition: border-color 0.15s ease, box-shadow 0.15s ease;
    }
    .target-card:hover {
      border-color: var(--card-border-hover);
      box-shadow: var(--card-box-shadow-hover);
    }
    .target-card.target-done {
      border-color: var(--card-border-done);
      opacity: 0.55;
      box-shadow: none;
    }
    .card-title {
      font-size: var(--fs-lg);
      font-weight: 700;
      color: var(--text-bright);
      margin-bottom: 12px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 10px;
    }

    /* Tier 2: Tight Invert Badges & Subtle Counters */
    .badge {
      font-size: var(--fs-sm);
      padding: 1px 6px;
      border-radius: 0;
      font-weight: 700;
      background: var(--invert-bg);
      color: var(--invert-text);
      white-space: nowrap;
      flex-shrink: 0;
      display: inline-flex;
      align-items: center;
      line-height: 1.4;
      box-shadow: var(--badge-shadow);
    }
    .badge.delivery {
      background: var(--invert-bg);
      color: var(--invert-text);
      border: none;
      box-shadow: var(--badge-shadow);
    }
    .badge.manual {
      background: var(--badge-manual-bg);
      color: var(--text);
      border: 1px solid var(--line);
      box-shadow: var(--badge-shadow);
    }
    .badge-dim {
      font-size: var(--fs-sm);
      padding: 1px 6px;
      border-radius: 0;
      font-weight: 600;
      background: var(--badge-dim-bg);
      color: var(--text-muted);
      border: 1px solid var(--line);
      font-family: var(--font-mono);
      box-shadow: var(--badge-shadow);
    }

    /* Buttons Hierarchy */
    /* Default .btn: clean text button, borderless, transparent background */
    .btn {
      padding: 2px 4px;
      border-radius: 2px;
      border: none;
      background: transparent;
      cursor: pointer;
      font-family: var(--font-sans);
      font-weight: 500;
      font-size: var(--fs-md);
      display: inline-flex;
      align-items: center;
      gap: 2px;
      color: var(--text-muted);
      white-space: nowrap;
      flex-shrink: 0;
      user-select: none;
      transition: all 0.12s ease;
    }
    .btn::before {
      content: "[";
      font-family: var(--font-mono);
      margin-right: 1px;
      opacity: 0;
      color: var(--text-bright);
      transition: opacity 0.1s ease;
    }
    .btn::after {
      content: "]";
      font-family: var(--font-mono);
      margin-left: 1px;
      opacity: 0;
      color: var(--text-bright);
      transition: opacity 0.1s ease;
    }
    .btn:hover:not(:disabled) {
      background: transparent !important;
      border: none;
      color: var(--text-bright);
      transform: none;
    }
    .btn:hover:not(:disabled)::before,
    .btn:hover:not(:disabled)::after {
      opacity: 1;
    }
    .btn:disabled {
      opacity: 0.35;
      cursor: not-allowed;
    }
    .btn-sm {
      padding: 1px 4px;
      font-size: var(--fs-sm);
      line-height: 1.35;
    }

    /* Buttons positioned alongside regular text: borderless subtle light box (연한 박스, 테두리 없음, 좌우 [, ] 없음 + 은은한 화이트 광채) */
    .btn-boxed,
    .target-card .btn,
    .preset-item .btn,
    .alarm-item .btn,
    .btn-manual-bell {
      background: var(--btn-boxed-bg) !important;
      border: none !important;
      border-radius: 0 !important;
      padding: 2px 7px !important;
      color: var(--text-main) !important;
      font-weight: 500 !important;
      line-height: 1.35;
      box-shadow: var(--btn-boxed-shadow) !important;
      transition: all 0.15s ease;
    }
    .btn-boxed::before, .btn-boxed::after,
    .target-card .btn::before, .target-card .btn::after,
    .preset-item .btn::before, .preset-item .btn::after,
    .alarm-item .btn::before, .alarm-item .btn::after,
    .btn-manual-bell::before, .btn-manual-bell::after {
      content: none !important;
      display: none !important;
    }
    .btn-boxed:hover:not(:disabled),
    .target-card .btn:hover:not(:disabled),
    .preset-item .btn:hover:not(:disabled),
    .alarm-item .btn:hover:not(:disabled),
    .btn-manual-bell:hover:not(:disabled) {
      background: var(--btn-boxed-hover) !important;
      border: none !important;
      color: var(--text-bright) !important;
      box-shadow: var(--card-box-shadow-hover) !important;
    }

    /* Tier 1: 최상위 메인 실행 버튼 (강조 1단계: 솔리드 녹색 박스, #FFFFFF 화이트 텍스트, 무테두리, 직각 코너 + 은은한 에메랄드 광채) */
    .btn-solid-core,
    .btn-mcb-start,
    #btn-batch-execute,
    #btn-quick-alter-execute,
    #btn-custom-execute {
      background: #10B981 !important;
      color: #FFFFFF !important;
      font-family: var(--font-sans) !important;
      font-size: var(--fs-md) !important;
      font-weight: 700 !important;
      line-height: 1 !important;
      height: 38px !important;
      min-height: 38px !important;
      max-height: 38px !important;
      box-sizing: border-box !important;
      padding: 0 18px !important;
      display: inline-flex;
      align-items: center !important;
      justify-content: center !important;
      border-radius: 0 !important;
      border: none !important;
      box-shadow: 0 0 14px rgba(16, 185, 129, 0.45), 0 0 4px rgba(16, 185, 129, 0.25) !important;
      text-shadow: none !important;
      cursor: pointer;
      white-space: nowrap !important;
      flex-shrink: 0;
      vertical-align: middle;
      transition: all 0.15s ease;
    }
    .btn-solid-core::before, .btn-solid-core::after,
    .btn-mcb-start::before, .btn-mcb-start::after,
    #btn-batch-execute::before, #btn-batch-execute::after,
    #btn-quick-alter-execute::before, #btn-quick-alter-execute::after,
    #btn-custom-execute::before, #btn-custom-execute::after {
      content: none !important;
      display: none !important;
    }
    .btn-solid-core:hover:not(:disabled),
    .btn-mcb-start:hover:not(:disabled),
    #btn-batch-execute:hover:not(:disabled),
    #btn-quick-alter-execute:hover:not(:disabled),
    #btn-custom-execute:hover:not(:disabled) {
      background: #34D399 !important;
      color: #FFFFFF !important;
      border: none !important;
      box-shadow: 0 0 20px rgba(52, 211, 153, 0.65), 0 0 6px rgba(52, 211, 153, 0.35) !important;
      transform: translateY(-1px) !important;
    }
    .btn-solid-core:disabled,
    .btn-mcb-start:disabled,
    #btn-batch-execute:disabled,
    #btn-quick-alter-execute:disabled,
    #btn-custom-execute:disabled {
      opacity: 0.45 !important;
      cursor: not-allowed !important;
      transform: none !important;
      box-shadow: none !important;
    }
    .btn-solid-core.btn-sm {
      height: 28px !important;
      min-height: 28px !important;
      max-height: 28px !important;
      padding: 0 10px !important;
      font-size: var(--fs-sm) !important;
    }

    /* Tier 2: 중간 위계 액션 버튼 (강조 2단계: 1단계와 동일한 에메랄드에 투명도를 많이 준 연한 배경 + 선명한 녹색 텍스트, 무테두리, 직각 + 은은한 광채) */
    #btn-collect-all-altering {
      background: rgba(16, 185, 129, 0.18) !important;
      color: #10B981 !important;
      font-size: var(--fs-sm) !important;
      font-weight: 700 !important;
      padding: 3px 10px !important;
      height: 26px !important;
      min-height: 26px !important;
      max-height: 26px !important;
      border-radius: 0 !important;
      border: none !important;
      box-shadow: 0 0 10px rgba(16, 185, 129, 0.28) !important;
      text-shadow: none !important;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      transition: all 0.15s ease;
    }
    #btn-collect-all-altering::before, #btn-collect-all-altering::after {
      content: none !important;
      display: none !important;
    }
    #btn-collect-all-altering:hover:not(:disabled) {
      background: rgba(16, 185, 129, 0.30) !important;
      color: #34D399 !important;
      border: none !important;
      box-shadow: 0 0 16px rgba(52, 211, 153, 0.5) !important;
      transform: translateY(-1px) !important;
    }

    /* Facility Header Collect Button (강조 2단계: 1단계와 동일한 에메랄드 고투명도 배경 + 선명한 녹색 텍스트, 무테두리, 직각 + 은은한 광채) */
    .btn-facility-collect {
      background: rgba(16, 185, 129, 0.18) !important;
      color: #10B981 !important;
      font-size: 11px !important;
      font-weight: 700 !important;
      padding: 3px 8px !important;
      border-radius: 0 !important;
      border: none !important;
      box-shadow: 0 0 10px rgba(16, 185, 129, 0.28) !important;
      cursor: pointer;
      line-height: 1.3 !important;
      white-space: nowrap !important;
      text-shadow: none !important;
      transition: all 0.12s ease;
    }
    .btn-facility-collect:hover {
      background: rgba(16, 185, 129, 0.30) !important;
      color: #34D399 !important;
      border: none !important;
      box-shadow: 0 0 16px rgba(52, 211, 153, 0.5) !important;
    }
    .btn-facility-collect::before,
    .btn-facility-collect::after {
      content: none !important;
      display: none !important;
    }
    .btn-facility-collect-partial {
      background: rgba(245, 158, 11, 0.15) !important;
      color: #F59E0B !important;
      font-size: 11px !important;
      font-weight: 700 !important;
      padding: 3px 8px !important;
      border-radius: 0 !important;
      border: 1px solid rgba(245, 158, 11, 0.3) !important;
      box-shadow: 0 0 8px rgba(245, 158, 11, 0.2) !important;
      cursor: pointer;
      line-height: 1.3 !important;
      white-space: nowrap !important;
      transition: all 0.12s ease;
    }
    .btn-facility-collect-partial:hover {
      background: rgba(245, 158, 11, 0.28) !important;
      color: #FBBF24 !important;
      box-shadow: 0 0 12px rgba(245, 158, 11, 0.35) !important;
    }
    .btn-facility-alarm {
      background: rgba(59, 130, 246, 0.12) !important;
      color: #60A5FA !important;
      font-size: 10px !important;
      font-weight: 600 !important;
      padding: 2px 6px !important;
      border-radius: 2px !important;
      border: 1px solid rgba(59, 130, 246, 0.25) !important;
      cursor: pointer;
      line-height: 1.3 !important;
      white-space: nowrap !important;
      transition: all 0.12s ease;
    }
    .btn-facility-alarm:hover {
      background: rgba(59, 130, 246, 0.22) !important;
      color: #93C5FD !important;
      border-color: rgba(59, 130, 246, 0.45) !important;
    }
    .btn-facility-alarm.active {
      background: rgba(16, 185, 129, 0.18) !important;
      color: #34D399 !important;
      border-color: rgba(16, 185, 129, 0.4) !important;
      box-shadow: 0 0 6px rgba(16, 185, 129, 0.25) !important;
    }
    .btn-facility-idle {
      background: var(--btn-facility-idle-bg) !important;
      color: var(--text-dim) !important;
      font-size: 11px !important;
      font-weight: 500 !important;
      padding: 2px 6px !important;
      border: none !important;
      border-radius: 2px !important;
      cursor: default;
      line-height: 1.3 !important;
      white-space: nowrap !important;
      opacity: 0.6;
    }
    .btn-facility-idle::before,
    .btn-facility-idle::after {
      content: none !important;
      display: none !important;
    }

    /*Inputs */
    .form-row { display: flex; gap: 10px; margin-bottom: 12px; align-items: center; }
    .form-group { display: flex; flex-direction: column; gap: 4px; }
    .form-group label { font-size: var(--fs-sm); color: var(--text-muted); font-weight: 600; }
    .input-text, .input-number {
      background: var(--bg-panel);
      border: 1px solid var(--line);
      color: var(--text-bright);
      padding: 7px 10px;
      border-radius: 0;
      font-size: var(--fs-md);
      outline: none;
      transition: border-color 0.15s;
    }
    .input-text:focus, .input-number:focus { border-color: var(--invert-bg); }

    /* Console */
    .console {
      background: var(--bg-console);
      border: none;
      border-top: 1px solid var(--line);
      border-bottom: 1px solid var(--line);
      border-radius: 0;
      padding: 12px 10px;
      height: 340px;
      overflow-y: auto;
      font-family: var(--font-mono);
      font-size: var(--fs-sm);
      display: flex;
      flex-direction: column;
      gap: 4px;
    }
    .log-line { line-height: 1.5; word-break: break-all; }
    .log-time { color: var(--text-dim); margin-right: 6px; font-family: var(--font-mono); }
    .log-info { color: var(--text); }
    .log-success { color: var(--text-bright); font-weight: 700; }
    .log-warn { color: var(--log-warn-color); }
    .log-error { color: var(--invert-text); background: var(--invert-bg); padding: 0 4px; font-weight: 700; }
    .log-action { color: var(--text-bright); font-weight: 600; }

    .busy-indicator {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-size: var(--fs-md);
      color: var(--text-bright);
      font-weight: 600;
      font-family: var(--font-mono);
    }

    /* Facility 3x2 Grid */
    .facility-grid {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 16px;
    }
    @media(max-width: 960px) { .facility-grid { grid-template-columns: repeat(2, 1fr); } }
    @media(max-width: 600px) { .facility-grid { grid-template-columns: 1fr; } }

    .facility-card {
      background: transparent;
      border: none;
      border-top: 1px solid var(--line);
      border-radius: 0;
      padding: 10px 0;
      display: flex;
      flex-direction: column;
      justify-content: flex-start;
      min-height: 92px;
    }
    .facility-card.facility-idle {
      min-height: 68px;
    }
    .hide-satisfied-rows tr.row-satisfied {
      display: none !important;
    }
    /* Custom Autocomplete Dropdown & Search Enhancements */
    .ac-wrapper {
      position: relative;
    }
    .ac-dropdown {
      position: absolute;
      left: 0;
      right: 0;
      top: calc(100% + 2px);
      background: var(--bg-panel);
      border: 1px solid var(--invert-bg);
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.55);
      z-index: 1200;
      max-height: 240px;
      overflow-y: auto;
      display: none;
    }
    .ac-item {
      padding: 6px 10px;
      font-size: var(--fs-sm);
      color: var(--text);
      cursor: pointer;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      border-bottom: 1px solid var(--line-subtle);
    }
    .ac-item:last-child {
      border-bottom: none;
    }
    .ac-item:hover,
    .ac-item.active {
      background: var(--bg-row-hover);
      color: var(--text-bright);
    }
    .search-chip {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 2px 7px;
      font-size: 11px;
      background: var(--btn-boxed-bg);
      color: var(--text-muted);
      border: 1px solid var(--line-subtle);
      cursor: pointer;
      user-select: none;
      transition: all 0.12s ease;
    }
    .search-chip:hover {
      color: var(--text-bright);
      border-color: var(--text-muted);
      background: var(--btn-boxed-hover);
    }
    .facility-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 8px;
      padding-bottom: 6px;
      border-bottom: 1px solid var(--line-subtle);
    }
    .facility-title {
      font-weight: 700;
      font-size: var(--fs-md);
      color: var(--text-bright);
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .facility-items {
      display: flex;
      flex-direction: column;
      gap: 4px;
      max-height: 110px;
      overflow-y: auto;
      padding-right: 8px; /* Breathing room so 완료 badge does not touch the scrollbar */
    }
    .facility-item-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: var(--fs-sm);
      padding: 3px 2px 3px 0;
      border-bottom: 1px dashed var(--line-subtle);
    }
    .facility-item-row:last-child { border-bottom: none; }
    .item-name-text {
      color: var(--text);
      font-weight: 400;
      max-width: 145px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .btn-manual-bell {
      background: var(--btn-boxed-bg);
      border: none;
      border-radius: 2px;
      color: var(--text-main);
      cursor: pointer;
      font-family: var(--font-mono);
      font-size: 11px;
      margin-left: 6px;
      padding: 1px 6px;
      line-height: 1.2;
      transition: all 0.12s ease;
    }
    .btn-manual-bell::before,
    .btn-manual-bell::after {
      content: none !important;
      display: none !important;
    }
    .btn-manual-bell:hover {
      background: var(--btn-boxed-hover);
      border: none;
      color: var(--text-bright);
    }

    /* BOM Table & Group Separators */
    .bom-table-container {
      background: transparent;
      border: none;
      border-radius: 0;
      overflow-x: auto;
      margin-top: 8px;
    }
    .bom-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
      margin-top: 0;
    }
    .bom-table th {
      background: var(--bg-table-header);
      color: var(--text-muted);
      text-align: left;
      padding: 7px 10px;
      font-size: 11px;
      font-weight: 700;
      border-top: none;
      border-bottom: 1px solid var(--line);
      white-space: nowrap;
      letter-spacing: 0.3px;
    }
    .bom-table td {
      padding: 6px 10px;
      border-bottom: 1px solid var(--line-subtle);
      color: var(--text);
      font-size: 12px;
      white-space: nowrap;
      background: transparent;
    }
    /* Unified background across all rows (no zebra striping) */
    .bom-table tbody tr td {
      background: transparent;
    }
    .bom-table tbody tr:hover td {
      background: var(--table-hover-bg) !important;
    }
    /* Satisfied / Done rows dimmed to emphasize remaining work */
    .bom-table tr.row-satisfied td {
      color: var(--text-dim) !important;
    }
    .bom-table tr.row-satisfied .badge-dim,
    .bom-table tr.row-satisfied .badge {
      opacity: 0.5;
    }
    .bom-table tr.row-alter td:first-child,
    .bom-table tr.row-gather td:first-child,
    .bom-table tr.row-special td:first-child,
    .bom-table tr.row-satisfied td:first-child {
      border-left: none !important;
      padding-left: 10px;
    }
    /* Colored bullet dot in front of material name */
    .bom-dot {
      display: inline-block;
      width: 7px;
      height: 7px;
      border-radius: 50%;
      margin-right: 8px;
      vertical-align: middle;
      flex-shrink: 0;
    }
    .dot-alter {
      background-color: #60A5FA;
      box-shadow: 0 0 4px rgba(96, 165, 250, 0.5);
    }
    .dot-gather {
      background-color: #34D399;
      box-shadow: 0 0 4px rgba(52, 211, 153, 0.5);
    }
    .dot-special {
      background-color: #F59E0B;
      box-shadow: 0 0 4px rgba(245, 158, 11, 0.5);
    }
    .bom-table tr.row-satisfied .bom-dot {
      opacity: 0.35;
      box-shadow: none;
    }
    .bom-table .badge-dim,
    .bom-table .badge {
      font-size: 11px;
      padding: 1px 5px;
    }

    /* Collapsed Bar Visual Hierarchy */
    #altering-collapsed-bar,
    #delivery-targets-collapsed-bar,
    #quick-alter-collapsed-bar {
      background: transparent !important;
      border: none !important;
      border-top: 1px solid var(--line-subtle) !important;
      border-radius: 0 !important;
      padding: 8px 0 !important;
      margin-top: 10px !important;
      cursor: pointer;
      transition: color 0.15s ease;
    }
    #altering-collapsed-bar:hover,
    #delivery-targets-collapsed-bar:hover,
    #quick-alter-collapsed-bar:hover {
      background: transparent !important;
      color: var(--text-bright) !important;
    }

    /* 7-Slot Quick Alter Styling */
    .quick-tab {
      background: transparent;
      border: 1px solid var(--line);
      color: var(--text-muted);
      font-size: var(--fs-md);
      font-weight: 600;
      padding: 4px 10px;
      border-radius: 0;
    }
    .quick-tab::before, .quick-tab::after { content: none !important; display: none !important; }
    .quick-tab:hover { color: var(--text-bright); border-color: var(--text-muted); }
    .quick-tab.active {
      background: var(--invert-bg) !important;
      color: var(--invert-text) !important;
      border-color: var(--invert-bg) !important;
      font-weight: 700;
      box-shadow: var(--badge-shadow) !important;
    }

    /* Multi-Character Profile Tabs & Summary Bar */
    .char-profile-section {
      margin-top: 10px;
      padding: 10px 12px;
      background: var(--bg-sub-panel);
      border: 1px solid var(--line);
    }
    .char-profile-top-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 8px;
    }
    .char-tabs-list {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(195px, 1fr));
      gap: 6px;
      align-items: stretch;
    }
    .char-tab-btn {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 6px;
      width: 100%;
      box-sizing: border-box;
      background: var(--bg-panel);
      border: 1px solid var(--line);
      color: var(--text-muted);
      font-size: var(--fs-sm);
      font-weight: 600;
      padding: 6px 10px;
      cursor: pointer;
      transition: all 0.12s ease;
    }
    .char-tab-left {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      min-width: 0;
      overflow: hidden;
    }
    .char-tab-right {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      flex-shrink: 0;
    }
    .char-tab-btn::before, .char-tab-btn::after {
      content: none !important;
      display: none !important;
    }
    .char-tab-btn:hover {
      border-color: var(--text-muted);
      color: var(--text-bright);
    }
    .char-tab-btn.active {
      background: var(--invert-bg) !important;
      color: var(--invert-text) !important;
      border-color: var(--invert-bg) !important;
      font-weight: 700;
      box-shadow: var(--badge-shadow) !important;
    }
    .char-tab-btn.active .char-tab-progress,
    .char-tab-btn.active .char-tab-sub {
      color: var(--invert-text) !important;
      opacity: 0.9;
    }
    .char-ingame-dot {
      display: inline-block;
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background-color: #34D399;
      box-shadow: 0 0 5px rgba(52, 211, 153, 0.8);
      flex-shrink: 0;
    }
    .char-tab-btn.active .char-ingame-dot {
      background-color: #10B981;
      box-shadow: 0 0 4px rgba(16, 185, 129, 0.9);
    }
    .char-delivered-badge {
      display: inline-flex;
      align-items: center;
      gap: 2px;
      font-size: 10px;
      font-weight: 700;
      padding: 1px 4px;
      color: #34D399;
      border: 1px solid rgba(52, 211, 153, 0.45);
      background: rgba(16, 185, 129, 0.14);
      line-height: 1.2;
    }
    .char-tab-btn.active .char-delivered-badge {
      color: #064E3B !important;
      background: #34D399 !important;
      border-color: #10B981 !important;
    }
    .delivery-tools-drawer {
      margin-bottom: 12px;
      padding: 12px;
      background: var(--bg-sub-panel);
      border: 1px solid var(--line);
    }
    /* Drag & Drop Target Card Reordering */
    .target-card {
      transition: transform 0.12s ease, border-color 0.12s ease, opacity 0.12s ease;
    }
    .target-card.dragging {
      opacity: 0.45;
      border-style: dashed !important;
    }
    .target-card.drag-over {
      border-color: var(--text-bright) !important;
      box-shadow: 0 0 0 1px var(--text-bright) !important;
      transform: translateY(-1px);
    }
    .target-drag-handle {
      cursor: grab;
      color: var(--text-dim);
      font-size: 13px;
      padding: 0 4px 0 0;
      user-select: none;
    }
    .target-drag-handle:hover {
      color: var(--text-bright);
    }
    .target-drag-handle:active {
      cursor: grabbing;
    }
    .btn-move-card {
      padding: 1px 5px !important;
      font-size: 10px !important;
      line-height: 1.2 !important;
      color: var(--text-muted);
    }
    .char-context-banner {
      margin-top: 8px;
      padding: 6px 10px;
      border: 1px solid var(--line-subtle);
      background: var(--bg-panel);
      font-size: var(--fs-sm);
      color: var(--text-muted);
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px;
    }
    .quick-cat-card {
      background: transparent;
      border: none;
      border-top: 1px solid var(--line-subtle);
      border-radius: 0;
      padding: 12px 0;
      margin-bottom: 12px;
    }
    .quick-tier-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
      margin-top: 8px;
      background: transparent;
      border: none;
      border-radius: 0;
    }
    .quick-tier-table th {
      background: var(--bg-table-header);
      color: var(--text-muted);
      text-align: left;
      padding: 6px 8px;
      font-size: 11px;
      font-weight: 700;
      border-top: none;
      border-bottom: 1px solid var(--line);
    }
    .quick-tier-table td {
      padding: 5px 8px;
      border-bottom: 1px solid var(--line-subtle);
      font-size: 12px;
    }
    .quick-tier-table tr:hover td {
      background: var(--table-hover-bg);
    }

    /* Master Command Bar Mode Switcher Tabs (Tier 2: Tight Invert Box when Active + subtle glow) */
    .mcb-mode-tabs {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 12px;
      padding-bottom: 10px;
      border-bottom: 1px solid var(--line);
      flex-wrap: wrap;
    }
    .mcb-mode-tab {
      background: transparent;
      border: 1px solid var(--line);
      border-radius: 0;
      padding: 4px 10px;
      display: inline-flex;
      align-items: center;
      gap: 8px;
      cursor: pointer;
      color: var(--text-muted);
      font-family: var(--font-sans);
      font-size: var(--fs-md);
      font-weight: 600;
      user-select: none;
      outline: none;
      transition: all 0.12s ease;
    }
    .mcb-mode-tab:hover {
      border-color: var(--text-muted);
      color: var(--text-bright);
    }
    .mcb-mode-tab .mcb-tab-pill {
      font-family: var(--font-mono);
      font-size: var(--fs-sm);
      font-weight: 700;
      padding: 0 5px;
      border-radius: 0;
      background: var(--tab-pill-bg);
      color: var(--text-muted);
    }
    /* Active Mode Tab -> Tight Invert Box (#DFDFDF bg + #121212 text + subtle ambient glow) */
    .mcb-mode-tab.active,
    .mcb-mode-tab.active#btn-mode-delivery,
    .mcb-mode-tab.active#btn-mode-quick-alter,
    .mcb-mode-tab.active#btn-mode-custom-craft {
      background: var(--invert-bg) !important;
      border-color: var(--invert-bg) !important;
      color: var(--invert-text) !important;
      font-weight: 700;
      box-shadow: var(--badge-shadow) !important;
    }
    .mcb-mode-tab.active .mcb-tab-pill,
    .mcb-mode-tab.active#btn-mode-delivery .mcb-tab-pill,
    .mcb-mode-tab.active#btn-mode-quick-alter .mcb-tab-pill,
    .mcb-mode-tab.active#btn-mode-custom-craft .mcb-tab-pill {
      background: var(--invert-text) !important;
      color: var(--invert-bg) !important;
    }

    /* Alteration Order Segmented Control (Tight Invert Active Filter) */
    .alter-order-container {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: transparent;
      border: none;
      padding: 0;
    }
    .alter-order-label {
      font-size: var(--fs-sm);
      font-weight: 600;
      color: var(--text-muted);
      display: flex;
      align-items: center;
      gap: 4px;
    }
    .segmented-control {
      display: inline-flex;
      background: transparent;
      border: 1px solid var(--line);
      border-radius: 0;
      padding: 1px;
      gap: 2px;
    }
    .seg-btn {
      background: transparent;
      border: none;
      color: var(--text-muted);
      font-family: var(--font-sans);
      font-size: var(--fs-sm);
      font-weight: 600;
      padding: 2px 8px;
      border-radius: 0;
      cursor: pointer;
      white-space: nowrap;
      display: inline-flex;
      align-items: center;
      gap: 4px;
    }
    .seg-btn:hover {
      color: var(--text-bright);
    }
    .seg-btn.active {
      background: var(--invert-bg);
      color: var(--invert-text);
      font-weight: 700;
      box-shadow: var(--badge-shadow);
    }

    /* Tab Content */
    .mode-tab-content {
      padding-top: 4px;
    }

    /* Modal Dialogs (Line-Centric Terminal Modal) */
    .modal-overlay {
      position: fixed;
      top: 0; left: 0; right: 0; bottom: 0;
      background: var(--modal-overlay-bg);
      display: flex;
      align-items: center;
      justify-content: center;
      z-index: 9999;
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.15s ease;
    }
    .modal-overlay.active {
      opacity: 1;
      pointer-events: auto;
    }
    .modal-dialog {
      background: var(--bg-panel);
      border: 1px solid var(--invert-bg);
      border-radius: 0;
      width: 90%;
      max-width: 680px;
      max-height: 85vh;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      box-shadow: var(--modal-shadow);
    }
    .modal-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 14px 20px;
      background: var(--bg-elevated);
      border-bottom: 1px solid var(--line);
    }
    .modal-body {
      padding: 18px 20px;
      overflow-y: auto;
      font-size: var(--fs-md);
      color: var(--text);
    }
    .modal-footer {
      display: flex;
      justify-content: flex-end;
      gap: 10px;
      padding: 12px 20px;
      background: var(--bg-elevated);
      border-top: 1px solid var(--line);
    }
    .summary-stat-grid {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 10px;
      margin-bottom: 16px;
    }
    .summary-stat-card {
      background: var(--bg);
      border: 1px solid var(--line);
      border-radius: 0;
      padding: 10px;
      text-align: left;
      box-shadow: var(--card-box-shadow);
    }
    .summary-stat-card .label {
      font-size: var(--fs-sm);
      color: var(--text-muted);
      margin-bottom: 4px;
    }
    .summary-stat-card .val {
      font-size: var(--fs-lg);
      font-weight: 700;
      font-family: var(--font-mono);
      color: var(--text-bright);
    }
    .summary-section {
      background: transparent;
      border-top: 1px solid var(--line);
      padding: 12px 0;
      margin-bottom: 8px;
    }
    .summary-section-title {
      font-size: var(--fs-md);
      font-weight: 700;
      color: var(--text-bright);
      margin-bottom: 8px;
      display: flex;
      align-items: baseline;
      gap: 6px;
    }
    .summary-section-title .sym-prefix,
    .summary-extra-row .sym-prefix {
      margin-right: 0 !important;
      display: inline-block;
      width: 10px;
      flex-shrink: 0;
    }
    .summary-extra-row {
      display: flex;
      align-items: baseline;
      gap: 6px;
      margin-bottom: 4px;
    }
    .summary-item-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 5px 0;
      border-bottom: 1px dashed var(--line-subtle);
      font-size: var(--fs-md);
    }
    .summary-item-row:last-child { border-bottom: none; }

    /* Master Command Bar (Sticky Terminal Control Deck) */
    .master-command-bar {
      position: sticky;
      top: var(--header-height, 102px);
      z-index: 850;
      margin-bottom: 18px;
      background: var(--bg-mcb);
      border: none;
      border-top: 1px solid var(--line);
      border-bottom: 1px solid var(--line);
      border-radius: 0;
      padding: 12px 14px;
      box-shadow: var(--mcb-shadow);
      transition: top 0.1s ease;
    }
    .master-command-bar.running {
      border-top-color: var(--invert-bg);
      border-bottom-color: var(--invert-bg);
      background: var(--bg-mcb-running);
    }
    .mcb-inner {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      flex-wrap: wrap;
    }
    .mcb-left {
      display: flex;
      flex-direction: column;
      gap: 6px;
      flex: 1;
      min-width: 260px;
    }
    .mcb-title-row {
      display: flex;
      flex-direction: column;
      align-items: flex-start;
      gap: 3px;
    }
    .mcb-briefing-row,
    .mcb-eta-row {
      display: flex;
      align-items: baseline;
      gap: 6px;
      width: 100%;
    }
    .mcb-briefing-row .sym-prefix,
    .mcb-eta-row .sym-prefix {
      margin-right: 0 !important;
      font-size: var(--fs-md);
      font-family: var(--font-sans);
      color: var(--text-muted);
      font-weight: 700;
      display: inline-block;
      width: 10px;
      flex-shrink: 0;
      line-height: 1.4;
    }
    .mcb-briefing {
      font-size: var(--fs-md);
      font-weight: 600;
      color: var(--text-bright);
      font-family: var(--font-sans);
      line-height: 1.4;
    }
    .mcb-eta-badge {
      display: inline-block;
      color: var(--text-muted);
      font-family: var(--font-sans);
      font-size: var(--fs-md);
      font-weight: 600;
      padding: 0;
      white-space: nowrap;
      line-height: 1.4;
    }
    .mcb-options-row {
      display: flex;
      align-items: center;
      gap: 16px;
      flex-wrap: wrap;
      margin-top: 2px;
    }
    /* Tier 3: Text Checkbox ([x] / [ ]) */
    .mcb-option-toggle,
    .mcb-pill-btn {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      cursor: pointer;
      background: transparent;
      border: none;
      padding: 2px 0;
      font-family: var(--font-sans);
      font-size: var(--fs-md);
      font-weight: 400;
      color: var(--text-muted);
      user-select: none;
      outline: none;
    }
    .mcb-option-toggle:hover,
    .mcb-pill-btn:hover {
      color: var(--text-bright);
      text-shadow: 0 0 0.5px currentColor;
    }
    .mcb-option-toggle input[type="checkbox"] {
      display: none;
    }
    .cli-check-box {
      font-family: var(--font-mono);
      font-weight: 700;
      color: var(--text-bright);
      display: inline-block;
      width: 3ch;
      text-align: center;
    }
    .mcb-right {
      display: flex;
      align-items: center;
      gap: 12px;
      flex-shrink: 0;
      min-width: 0;
      max-width: 100%;
    }
    .master-command-bar.running .mcb-right {
      width: 100%;
      border-top: 1px dashed var(--line-subtle);
      padding-top: 8px;
      margin-top: 2px;
    }
    #mcb-box-running {
      width: 100%;
      justify-content: space-between;
    }
    /* Tier 1 Abort: 즉시 중지 버튼 (솔리드 레드 배경, #FFFFFF 화이트 텍스트, 무테두리, 직각 코너 + 은은한 레드 광채) */
    .btn-danger-abort {
      background: #EF4444 !important;
      color: #FFFFFF !important;
      font-family: var(--font-sans) !important;
      font-size: var(--fs-md) !important;
      font-weight: 700 !important;
      line-height: 1 !important;
      height: 38px !important;
      min-height: 38px !important;
      max-height: 38px !important;
      box-sizing: border-box !important;
      padding: 0 18px !important;
      display: inline-flex;
      align-items: center !important;
      justify-content: center !important;
      border-radius: 0 !important;
      border: none !important;
      box-shadow: 0 0 14px rgba(239, 68, 68, 0.55), 0 0 4px rgba(239, 68, 68, 0.35) !important;
      text-shadow: none !important;
      cursor: pointer;
      white-space: nowrap !important;
      flex-shrink: 0;
      vertical-align: middle;
      transition: all 0.15s ease;
    }
    .btn-danger-abort::before, .btn-danger-abort::after {
      content: none !important;
      display: none !important;
    }
    .btn-danger-abort:hover:not(:disabled) {
      background: #F87171 !important;
      color: #FFFFFF !important;
      border: none !important;
      box-shadow: 0 0 22px rgba(248, 113, 113, 0.75), 0 0 6px rgba(248, 113, 113, 0.45) !important;
      transform: translateY(-1px) !important;
    }
    .btn-danger-abort:disabled {
      opacity: 0.45 !important;
      cursor: not-allowed !important;
      transform: none !important;
      box-shadow: none !important;
    }
    .mcb-running-info {
      text-align: left;
      min-width: 0;
      flex: 1;
    }
    .mcb-running-title {
      font-size: var(--fs-md);
      font-weight: 700;
      color: var(--text-bright);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .mcb-running-desc {
      font-size: var(--fs-sm);
      color: var(--text-muted);
      max-width: 100%;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      margin-top: 2px;
    }
    @media (max-width: 820px) {
      .master-command-bar {
        padding: 10px 12px;
        top: var(--header-height, 102px);
      }
      .mcb-inner {
        flex-direction: column;
        align-items: stretch;
        gap: 10px;
      }
      .mcb-left { min-width: 0; }
      .mcb-right { width: 100%; justify-content: stretch; }
      #mcb-box-idle { width: 100%; }
      #mcb-box-running { width: 100%; justify-content: space-between; }
      .btn-mcb-start { width: 100%; justify-content: center; }
      .mcb-running-info { text-align: left; }
      .mcb-running-desc { max-width: 100%; }
    }

    /* Mobile / Smartphone View Optimization (max-width: 540px) */
    @media (max-width: 540px) {
      .container {
        padding: 0 10px;
      }
      header {
        padding: 8px 0;
        gap: 8px;
        margin-bottom: 12px;
      }
      .title h1 {
        font-size: 18px;
        letter-spacing: -0.5px;
      }
      .header-actions {
        width: 100%;
        gap: 2px 6px;
        padding-top: 4px;
      }
      .header-action-btn {
        padding: 3px 6px;
        font-size: 12px;
      }
      .header-status-bar {
        font-size: 12px;
        gap: 4px 6px;
      }
      .master-command-bar {
        padding: 8px 10px;
        margin-bottom: 12px;
      }
      .mcb-mode-tabs {
        gap: 4px;
        margin-bottom: 8px;
        padding-bottom: 8px;
        width: 100%;
      }
      .mcb-mode-tab {
        flex: 1 1 auto;
        justify-content: center;
        padding: 5px 6px;
        font-size: 12px;
        gap: 4px;
      }
      .mcb-mode-tab .mcb-tab-pill {
        font-size: 10px;
        padding: 0 4px;
      }
      .mcb-briefing {
        font-size: 13px;
        line-height: 1.4;
      }
      .mcb-options-row {
        gap: 10px;
        font-size: 12px;
      }
      .btn-mcb-start,
      .btn-solid-core,
      .btn-danger-abort {
        padding: 0 16px !important;
        font-size: 14px !important;
        width: 100%;
        height: 40px !important;
        min-height: 40px !important;
        max-height: 40px !important;
        justify-content: center;
      }
      .bom-table th,
      .bom-table td {
        padding: 5px 6px;
        font-size: 11px;
      }
      .modal-dialog {
        width: 96%;
        max-width: 96%;
        max-height: 92vh;
      }
      .modal-header {
        padding: 10px 14px;
      }
      .modal-body {
        padding: 12px 14px;
      }
      .modal-footer {
        padding: 10px 14px;
      }
    }

    /* Top-Level Category Switcher (생활 지원 vs 숙제 지원) */
    .top-mode-switcher {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      margin-left: 12px;
    }
    .top-mode-btn {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 5px 12px;
      font-size: var(--fs-md);
      font-weight: 600;
      color: var(--text-muted);
      background: transparent;
      border: 1px solid var(--line);
      border-radius: 0;
      cursor: pointer;
      transition: all 0.12s ease;
      font-family: var(--font-sans);
    }
    .top-mode-btn::before, .top-mode-btn::after {
      content: none !important;
      display: none !important;
    }
    .top-mode-btn:hover {
      color: var(--text-bright);
      border-color: var(--text-muted);
    }
    .top-mode-btn.active {
      background: var(--invert-bg) !important;
      color: var(--invert-text) !important;
      border-color: var(--invert-bg) !important;
      font-weight: 700;
      box-shadow: var(--badge-shadow) !important;
    }
    .top-mode-pill {
      font-size: var(--fs-sm);
      font-weight: 700;
      padding: 0 5px;
      border-radius: 0;
      border: none;
      background: var(--tab-pill-bg);
      color: var(--text-muted);
      font-family: var(--font-mono);
    }
    .top-mode-btn.active .top-mode-pill {
      background: var(--invert-text) !important;
      color: var(--invert-bg) !important;
    }

    /* Homework Support Specific Layouts (Monochrome Living Support Style) */
    #hw-character-tabs-list {
      grid-template-columns: repeat(auto-fill, minmax(215px, 1fr));
    }
    .hw-checklist-grid {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 14px;
      margin-bottom: 16px;
      align-items: start;
    }
    @media (max-width: 1024px) {
      .hw-checklist-grid {
        grid-template-columns: 1fr;
      }
    }
    .hw-cat-card {
      background: var(--bg-panel);
      border-top: 2px solid var(--invert-bg);
      border-bottom: 1px solid var(--line);
      padding: 12px 14px;
      display: flex;
      flex-direction: column;
      gap: 6px;
      box-shadow: var(--panel-shadow);
    }
    .hw-cat-collapsed-bar {
      padding: 4px 0 0 0;
      font-size: var(--fs-sm);
      color: var(--text-muted);
      cursor: pointer;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      user-select: none;
    }
    .hw-cat-collapsed-bar:hover {
      color: var(--text-bright);
    }
    #btn-hw-focus-mode.active {
      background: var(--invert-bg) !important;
      color: var(--invert-text) !important;
      border-color: var(--invert-bg) !important;
      font-weight: 700 !important;
      box-shadow: var(--badge-shadow) !important;
    }

    .hw-task-list {
      display: flex;
      flex-direction: column;
      gap: 2px;
    }
    .hw-task-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 6px;
      padding: 3px 8px;
      min-height: 24px;
      line-height: 1.25;
      background: var(--bg-sub-panel);
      border: 1px solid var(--line-subtle);
      cursor: pointer;
      transition: all 0.1s ease;
      user-select: none;
    }
    .hw-task-row:hover {
      border-color: var(--text-muted);
      background: var(--bg-row-hover);
    }
    .hw-task-row.done {
      opacity: 0.55;
    }
    .hw-task-row.done .hw-task-name {
      text-decoration: line-through;
      color: var(--text-dim);
      font-weight: 500;
    }
    .hw-task-row.current-step {
      border-color: var(--invert-bg);
      border-left: 3px solid var(--invert-bg);
      background: var(--bg-elevated);
    }
    .hw-task-left {
      display: flex;
      align-items: center;
      gap: 6px;
      min-width: 0;
      flex: 1;
    }
    .hw-task-check {
      font-family: var(--font-mono);
      font-weight: 700;
      font-size: 12px;
      color: var(--text-bright);
    }
    .hw-task-row.done .hw-task-check {
      color: var(--text-muted);
    }
    .hw-task-name {
      font-size: var(--fs-sm);
      color: var(--text-bright);
      font-weight: 600;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      display: inline-flex;
      align-items: center;
      gap: 5px;
    }
    .hw-keycap {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 17px;
      height: 18px;
      padding: 0 4px;
      font-family: var(--font-mono, "JetBrains Mono", Consolas, monospace);
      font-size: 11px;
      font-weight: 600;
      color: var(--text-bright);
      background: var(--badge-dim-bg, #1B1D21);
      border: 1px solid var(--line, #333842);
      border-radius: 2px;
      line-height: 1;
      vertical-align: middle;
      text-decoration: none !important;
      user-select: none;
      flex-shrink: 0;
    }
    .hw-task-row.done .hw-keycap {
      opacity: 0.45;
      border-color: var(--line, #333842);
      background: transparent;
      color: var(--text-dim);
      text-decoration: none !important;
    }
    #btn-hw-complete-step .hw-keycap {
      background: rgba(0, 0, 0, 0.22);
      border: 1px solid rgba(255, 255, 255, 0.4);
      color: #FFFFFF;
      margin: 0 4px;
      font-size: 11px;
      height: 20px;
      min-width: 20px;
      padding: 0 5px;
      border-radius: 2px;
      text-decoration: none !important;
    }
    .hw-progress-bar-wrap {
      height: 4px;
      background: var(--bg-sub-panel);
      border: 1px solid var(--line-subtle);
      overflow: hidden;
    }
    .hw-progress-bar-fill {
      height: 100%;
      background: var(--invert-bg);
      transition: width 0.25s ease;
    }

    .hw-matrix-cell {
      text-align: center;
      cursor: pointer;
      user-select: none;
      transition: background 0.12s ease;
      font-family: var(--font-mono);
      font-weight: 700;
    }
    .hw-matrix-cell:hover {
      background: var(--table-hover-bg) !important;
    }
    .hw-matrix-cell.done {
      color: var(--text-bright);
      background: var(--bg-sub-panel);
    }
    .hw-matrix-cell.todo {
      color: var(--text-muted);
      font-weight: 500;
    }
    .hw-matrix-cell.excluded {
      color: var(--text-dim);
      opacity: 0.4;
      cursor: default;
    }
    .hw-telemetry-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(135px, 1fr));
      gap: 8px;
    }
    .hw-stat-box {
      padding: 8px 10px;
      background: var(--bg-sub-panel);
      border: 1px solid var(--line-subtle);
      display: flex;
      flex-direction: column;
      gap: 2px;
    }
    .hw-stat-label {
      font-size: 11px;
      color: var(--text-muted);
    }
    .hw-stat-val {
      font-size: var(--fs-md);
      font-weight: 700;
      color: var(--text-bright);
      font-family: var(--font-mono);
    }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <!-- 1층: 좌측(타이틀 & 최상위 모드 전환) / 우측(보조 메뉴) -->
      <div class="header-top">
        <div class="logo-area" style="display: flex; align-items: center; flex-wrap: wrap; gap: 6px;">
          <div class="title">
            <h1>모비노기 지원도구 <span class="badge">v2.0.2</span></h1>
          </div>
          <div class="top-mode-switcher" role="tablist" aria-label="지원도구 메인 모드 전환">
            <button type="button" id="btn-top-mode-living" class="top-mode-btn active" onclick="switchTopAppMode('living')" title="주간 납품 18개 품목, 빠른 재료 가공, 개별 아이템 제작 파이프라인">
              <span>⚒️ 생활 지원</span>
              <span class="top-mode-pill" id="top-living-summary-pill">납품·가공·제작</span>
            </button>
            <button type="button" id="btn-top-mode-homework" class="top-mode-btn" onclick="switchTopAppMode('homework')" title="6캐릭터 일일·집중·주간 숙제 체크리스트, 주간 매트릭스, 인게임 미션·던전 자동 감지 및 정각 결계 알림">
              <span>📋 숙제 지원</span>
              <span class="top-mode-pill" id="top-hw-summary-pill">캐릭터 숙제</span>
            </button>
          </div>
        </div>
        <div class="header-actions">
          <button id="btn-toggle-theme" class="header-action-btn" onclick="toggleTheme()" title="다크모드 끄기 (눈이 편안한 웜그레이 화면으로 전환)">
            <span class="btn-label" id="theme-toggle-label">다크모드 ON</span>
          </button>
          <button class="header-action-btn" onclick="openUserDataFolder()" title="레시피, 프리셋, 설정이 저장되는 내 문서 폴더 열기 (버전 업데이트 시 자동 유지)">
            <span class="btn-label">데이터 폴더</span>
          </button>
          <button class="header-action-btn" onclick="openMobileConnectModal()" title="스마트폰 브라우저로 실시간 모니터링 (QR 코드 접속)">
            <span class="btn-label">스마트폰 접속</span>
          </button>
          <button class="header-action-btn btn-settings" onclick="openCliModal()" title="게임 CLI 설치 경로 및 연결 설정">
            <span class="btn-label">연결 설정</span>
          </button>
          <button class="header-action-btn" onclick="openAiToolboxModal()" title="창고 털이 추천, 상비약 안전 재고, 스마트 낚시, 미션·악보·소셜 리모컨 등 AI 편의 도구함">
            <span class="btn-label">AI 편의 도구함</span>
          </button>
          <button class="header-action-btn" onclick="openGuideModal()" title="처음 사용자를 위한 1분 퀵스타트, 주간 납품 18개 실전 튜토리얼 및 디스코드 웹훅 설정법">
            <span class="btn-label">사용 가이드 &amp; 튜토리얼</span>
          </button>
          <button id="btn-show-summary" class="header-action-btn btn-summary" style="display: none;" onclick="openSummaryModal()" title="최근 완료된 작업의 채집, 가공, 제작, 날개 소모 결과를 확인합니다">
            <span class="btn-label">최근 작업 리포트</span>
          </button>
          <button class="header-action-btn btn-shutdown" onclick="manualServerShutdown()" title="웹 대시보드와 백그라운드 서버를 종료합니다">
            <span class="btn-label">서버 종료</span>
          </button>
        </div>
      </div>

      <!-- 구분 선 -->
      <div class="header-divider"></div>

      <!-- 2층: 캐릭터 상태 바 (세로선 | 구분) -->
      <div class="header-status-bar status-pills" id="status-pills">
        <div class="pill online" id="pill-status" onclick="openCliModal()" style="cursor: pointer;" title="클릭하여 연결 상태 및 설정 확인">커넥터 연결 확인 중</div>
        <span class="status-delim">|</span>
        <div class="pill activity" id="pill-activity" title="인게임 캐릭터 실시간 활동 상태">상태: 대기 중</div>
        <span class="status-delim">|</span>
        <div class="pill wings" id="pill-wings">날개 -</div>
        <span class="status-delim">|</span>
        <div class="pill weight" id="pill-weight">무게 -/-</div>
        <span class="status-delim">|</span>
        <div class="pill" id="pill-env" title="현재 지역/채널, 에린 시간 및 포만감" style="color: var(--text-muted);">환경 확인 중</div>
        <span class="status-delim">|</span>
        <div class="pill" id="pill-omen-barrier" onclick="switchTopAppMode('homework')" style="cursor: pointer; color: var(--text-muted);" title="매시간 정각(00분~05분) 불길한 소환의 결계 알림 타이머">⚡ 결계 확인 중</div>
      </div>
    </header>

    <!-- Hourly Omen Barrier Active Alert Banner -->
    <div id="omen-barrier-banner" style="display: none; margin-bottom: 14px; padding: 10px 16px; background: var(--bg-panel); border-top: 2px solid var(--invert-bg); border-bottom: 1px solid var(--line);">
      <div style="display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap;">
        <div style="display: flex; align-items: center; gap: 10px;">
          <span class="badge mono-num">결계 진행 시간</span>
          <span id="omen-barrier-banner-text" style="font-size: var(--fs-md); color: var(--text-bright); font-weight: 600;">현재 정각 불길한 소환의 결계가 열려 있습니다! (매시간 00분 ~ 05분)</span>
        </div>
        <div style="display: flex; gap: 8px; align-items: center;">
          <button type="button" class="btn btn-sm btn-boxed" onclick="switchTopAppMode('homework')">숙제 탭 열기</button>
          <button type="button" class="btn btn-sm btn-boxed" onclick="dismissOmenBarrierBanner()">이번 시간 닫기</button>
        </div>
      </div>
    </div>

    <!-- Connection Alert Banner (shown when disconnected) -->
    <div id="cli-connection-alert" style="display: none; margin-bottom: 20px; padding: 14px 16px; background: var(--bg-panel); border-top: 1px solid var(--invert-bg); border-bottom: 1px solid var(--line);">
      <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; flex-wrap: wrap;">
        <div style="display: flex; gap: 10px; align-items: flex-start; flex: 1; min-width: 280px;">
          <div id="cli-alert-icon" class="mono-num" style="font-size: var(--fs-md); font-weight: 700; color: var(--text-bright);">[!]</div>
          <div style="flex: 1;">
            <div id="cli-alert-title" style="font-size: var(--fs-md); font-weight: 700; color: var(--text-bright); margin-bottom: 4px;">게임 연결 끊김</div>
            <div id="cli-alert-desc" style="font-size: var(--fs-sm); color: var(--text-muted); line-height: 1.5;"><span class="sym-prefix">&gt;</span>게임 클라이언트 상태를 확인해주세요.</div>
            <!-- Quick path setter form inside alert banner -->
            <div id="cli-alert-path-box" style="display: none; margin-top: 10px; gap: 8px; align-items: center; flex-wrap: wrap;">
              <input type="text" id="quick-cli-path-input" class="input-text" placeholder="예: D:\Nexon\MabinogiMobile 또는 C:\Nexon\MabinogiMobile" style="flex: 1; min-width: 260px;">
              <button class="btn btn-sm" onclick="saveQuickCliPath()">경로 저장 &amp; 연결</button>
              <button class="btn btn-sm" onclick="triggerCliScan()">자동 다시 검색</button>
            </div>
          </div>
        </div>
        <div>
          <button class="btn btn-sm" onclick="openCliModal()">연결 가이드 열기</button>
        </div>
      </div>
    </div>

    <!-- ========================================================= -->
    <!-- [TOP VIEW 1] ⚒️ 생활 지원 (Living Support View)           -->
    <!-- ========================================================= -->
    <div id="top-view-living">

    <!-- Collapsible 3x2 Facility Altering Queue & Delivery-Only Alarm Section -->
    <div class="card" id="altering-main-card">
      <div class="card-title" style="margin-bottom: 0;">
        <div style="display: flex; align-items: center; gap: 10px;">
          <span><span class="sym-prefix">//</span>가공 시설별 대기열 현황</span>
          <span id="altering-summary-pill" class="badge">확인 중...</span>
        </div>
        <div style="display: flex; gap: 8px; align-items: center;">
          <button id="btn-collect-all-altering" class="btn btn-sm btn-solid-core" onclick="collectAllCompletedAltering()" title="모든 시설의 완료된 가공 슬롯을 즉시 순차 수령합니다">완료 가공품 수령</button>
          <button class="btn btn-sm" onclick="loadAlteringQueue()">새로고침</button>
          <button class="btn btn-sm" id="btn-toggle-altering" onclick="toggleAlteringView()">접어두기 ^</button>
        </div>
      </div>

      <!-- Collapsed Compact Bar (shown when collapsed) -->
      <div id="altering-collapsed-bar" style="display: none; margin-top: 10px; padding: 8px 0; border-top: 1px solid var(--line-subtle); font-size: var(--fs-md); color: var(--text-muted); cursor: pointer;" onclick="toggleAlteringView()">
        <span><span class="sym-prefix">&gt;</span><span id="altering-quick-summary">가공 시설 상태 요약 정보...</span></span>
      </div>

      <!-- Expanded Content (shown by default) -->
      <div id="altering-expanded-content" style="margin-top: 14px;">
        <!-- 3x2 Grid for 6 Facilities -->
        <div class="facility-grid" id="facility-grid-container">
          <!-- Dynamically populated 6 facility cards -->
        </div>

        <!-- Delivery-Exclusive Alarm Section -->
        <div style="margin-top: 14px; border-top: 1px solid var(--line); padding-top: 12px;">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; flex-wrap: wrap; gap: 8px;">
            <div style="display: flex; align-items: center; gap: 8px;">
              <span style="font-weight: 700; font-size: var(--fs-md); color: var(--text-bright);"><span class="sym-prefix">&gt;</span>주간 납품 전용 가공 알람 목록</span>
              <span id="alarm-count-badge" class="badge">0개 활성</span>
            </div>
            <div style="display: flex; gap: 6px; align-items: center;">
              <button class="btn btn-sm" onclick="testAlarmSound()">소리 테스트</button>
              <button class="btn btn-sm" onclick="requestPushPermission()">브라우저 알림 허용</button>
              <button class="btn btn-sm" onclick="clearAllAlarms()">알람 비우기</button>
            </div>
          </div>
          <div id="alarm-list-container" style="max-height: 120px; overflow-y: auto; font-size: var(--fs-sm); display: flex; flex-direction: column; gap: 4px; background: var(--bg-sub-panel); border: 1px solid var(--line-subtle); border-radius: 0; padding: 6px 10px;">
            <p style="color: var(--text-muted); font-size: var(--fs-sm);"><span class="sym-prefix">&gt;</span>현재 등록된 주간 납품 알람이 없습니다. (납품 파이프라인 가동 시 자동 등록됩니다)</p>
          </div>
        </div>
      </div>
    </div>

    <!-- Global Master Command Bar (3개 모드 탭 + 브리핑 & 즉시 실행/중지) -->
    <div id="master-command-bar" class="master-command-bar">
      <!-- 3-Mode Compact Segmented Tab Buttons -->
      <div class="mcb-mode-tabs" role="tablist">
        <button class="mcb-mode-tab active" id="btn-mode-delivery" role="tab" aria-selected="true" onclick="switchMainMode('delivery')">
          <span>주간 납품</span>
          <span id="tab-delivery-pill" class="mcb-tab-pill delivery">0개 등록</span>
        </button>
        <button class="mcb-mode-tab" id="btn-mode-quick-alter" role="tab" aria-selected="false" onclick="switchMainMode('quick_alter')">
          <span>빠른 실행</span>
          <span id="tab-quick-alter-pill" class="mcb-tab-pill quick">7슬롯</span>
        </button>
        <button class="mcb-mode-tab" id="btn-mode-custom-craft" role="tab" aria-selected="false" onclick="switchMainMode('custom_craft')">
          <span>지정 제작</span>
          <span id="tab-custom-craft-pill" class="mcb-tab-pill custom">0개 등록</span>
        </button>
      </div>

      <div class="mcb-inner">
        <!-- Left: Current mode briefing & options -->
        <div class="mcb-left">
          <div class="mcb-title-row">
            <span id="mcb-mode-badge" style="display: none;"></span>
            <div class="mcb-briefing-row">
              <span class="sym-prefix">&gt;</span>
              <span id="mcb-briefing-text" class="mcb-briefing">목표 분석 대기 중...</span>
            </div>
            <div id="mcb-eta-row" class="mcb-eta-row" style="display: none;">
              <span class="sym-prefix">&gt;</span>
              <span id="mcb-eta-badge" class="mcb-eta-badge">예상 완수: -</span>
            </div>
          </div>
          <div class="mcb-options-row">
            <label class="mcb-option-toggle" id="mcb-goal-toggle-label" title="가공 수령 -> 다음 티어 가공 -> 완제품 제작까지 멈추지 않고 100% 완수할 때까지 연속 자동 실행">
              <input type="checkbox" id="mcb-goal-toggle" onchange="onGoalFulfillmentToggleChanged(this.checked)">
              <span class="cli-check-box" id="mcb-goal-check-sym">□</span>
              <span class="mcb-toggle-label">목표 완수 모드</span>
            </label>
            <button class="mcb-pill-btn" id="mcb-btn-sound" onclick="toggleSoundAlarmSetting()" title="목표 완수/작업 완료 시 효과음 알림 (기본값: 무음)">
              <span class="cli-check-box" id="mcb-sound-icon">□</span>
              <span id="mcb-sound-text">알림음 꺼짐</span>
            </button>
            <button class="mcb-pill-btn" id="mcb-btn-discord" onclick="openDiscordModal()" title="목표 완수 모드 디스코드 웹훅 알림 설정 (클릭하여 설정 열기)">
              <span class="cli-check-box" id="mcb-discord-icon" onclick="event.stopPropagation(); toggleDiscordAlarmSetting();" title="알림 켜기/끄기">□</span>
              <span id="mcb-discord-text">디스코드 알림</span>
            </button>
            <button class="mcb-pill-btn" id="mcb-btn-auto-collect" onclick="toggleAutoCollectSetting()" title="[옵션 #3] 가공 시설별 걸어둔 모든 작업 완료 시 백그라운드 자동 수령 (개별 완료 무시, 옵션 ON/OFF)">
              <span class="cli-check-box" id="mcb-auto-collect-icon">□</span>
              <span id="mcb-auto-collect-text">자동 수령</span>
            </button>
            <button class="mcb-pill-btn" id="mcb-btn-auto-music" onclick="toggleAutoPlayMusicSetting()" title="[옵션 #7] 작업 완료 후 대기 상태 진입 시 보유 악보를 자동 연주합니다 (옵션 ON/OFF)">
              <span class="cli-check-box" id="mcb-auto-music-icon">□</span>
              <span id="mcb-auto-music-text">대기 중 연주</span>
            </button>
            <button class="mcb-pill-btn btn" id="mcb-btn-order" onclick="toggleAlterOrderSetting()" title="가공 순서 우선순위 (상위 티어 / 하위 티어 우선)">
              <span id="mcb-order-text">상위티어 우선</span>
            </button>
          </div>
        </div>

        <!-- Right: Master Start / Stop Controls -->
        <div class="mcb-right">
          <!-- Idle State: Core Solid Start Button -->
          <div id="mcb-box-idle" style="display: flex; gap: 8px; align-items: center;">
            <button class="btn btn-mcb-start" id="mcb-btn-start" onclick="triggerMasterStartAction()">
              <span id="mcb-start-icon" style="display:none;"></span>
              <span class="btn-label" id="mcb-start-label">주간 납품 일괄 실행</span>
            </button>
          </div>
          <!-- Running State: Live Text Spinner & Abort Button -->
          <div id="mcb-box-running" style="display: none; gap: 12px; align-items: center;">
            <div class="mcb-running-info">
              <div class="mcb-running-title" id="mcb-running-title">작업 진행 중...</div>
              <div class="mcb-running-desc" id="mcb-running-desc">실시간 상태 동기화 중</div>
            </div>
            <button class="btn btn-danger-abort" onclick="abortMasterPipeline()" title="현재 진행 중인 작업을 즉시 안전하게 멈춥니다">
              <span>즉시 중지</span>
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- Mode 1: 주간 납품 관리 & 일괄 실행 탭 컨텐츠 (기본 활성) -->
    <div id="tab-content-delivery" class="mode-tab-content">
      <!-- Weekly Delivery Targets Manager Section -->
      <div class="card" id="delivery-targets-main-card">
        <div class="card-title" style="margin-bottom: 0;">
          <div style="display: flex; align-items: center; gap: 10px; flex-shrink: 0;">
            <span><span class="sym-prefix">//</span>주간 납품 목표 등록 &amp; 관리</span>
            <span id="target-count-badge" class="badge delivery">0개 등록</span>
          </div>
          <div style="display: flex; flex-wrap: wrap; gap: 6px; align-items: center;">
            <button type="button" class="btn btn-sm btn-boxed" id="btn-toggle-delivery-tools" onclick="toggleDeliveryToolsDrawer()" title="품목 직접 추가, 인게임 퀘스트 가져오기, 프리셋 저장/불러오기 도구를 펼치거나 접습니다">+ 품목·프리셋 관리 v</button>
            <button class="btn btn-sm" onclick="resetAllTargetsCurrent()" title="현재 선택된 캐릭터의 모든 등록 목표 보유 수량을 0개로 일괄 초기화합니다">수량 0개 초기화</button>
            <button class="btn btn-sm" onclick="syncTargetsWithInventory()" title="0개 초기화(고정)를 해제하고 현재 인게임 가방 수량과 다시 동기화합니다">가방 재동기화</button>
            <button class="btn btn-sm" onclick="loadDeliveryTargets()">새로고침</button>
            <button class="btn btn-sm" id="btn-toggle-delivery-targets" onclick="toggleDeliveryTargetsView()">접어두기 ^</button>
          </div>
        </div>

        <!-- Multi-Character Profile Bar & Overall Completion Summary -->
        <div class="char-profile-section" id="delivery-character-bar">
          <div class="char-profile-top-row">
            <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">
              <span style="font-weight: 700; font-size: var(--fs-sm); color: var(--text-bright);"><span class="sym-prefix">&gt;</span>캐릭터별 납품 프로필</span>
              <span id="char-overall-summary-badge" class="badge-dim mono-num">납품 완료: 0/1 캐릭터 (월 06시 초기화)</span>
            </div>
            <div style="display: flex; align-items: center; gap: 5px; flex-wrap: wrap;">
              <button type="button" class="btn btn-sm btn-solid-core" id="btn-complete-char-delivery" onclick="completeActiveCharacterDelivery()" title="현재 선택된 캐릭터의 주간 납품을 완료 처리하고, 해당 캐릭터의 보유 수량에서 목표 수량만큼 차감합니다 (매주 월요일 오전 6시 완료 마크 자동 초기화)">✓ 납품 완료 (목표량 차감)</button>
              <button type="button" class="btn btn-sm btn-boxed" id="btn-unmark-char-delivery" style="display: none;" onclick="unmarkActiveCharacterDelivery()" title="수량 변경 없이 현재 캐릭터의 [✓ 납품완료] 마크만 해제합니다">완료 해제</button>
              <button type="button" class="btn btn-sm btn-boxed" id="btn-toggle-char-autoswitch" onclick="toggleCharacterAutoSwitch()" title="인게임에서 다른 캐릭터로 로그인하면 해당 캐릭터의 납품 탭으로 자동 전환합니다">
                <span id="icon-char-autoswitch" style="font-family: var(--font-mono); margin-right: 3px;">■</span>자동전환
              </button>
              <button type="button" class="btn btn-sm btn-boxed" onclick="linkActiveCharacterToIngame()" title="현재 선택된 탭을 지금 게임에 접속 중인 캐릭터와 연동합니다">현 캐릭터 연동</button>
              <button type="button" class="btn btn-sm btn-boxed" onclick="promptRenameActiveCharacter()" title="현재 선택된 캐릭터 탭의 이름을 변경합니다">이름변경</button>
              <button type="button" class="btn btn-sm btn-boxed" onclick="deleteActiveCharacterProfile()" title="현재 선택된 캐릭터 탭을 삭제합니다">삭제</button>
              <button type="button" class="btn btn-sm btn-boxed" onclick="promptAddCharacterProfile()" title="새로운 캐릭터 프로필을 추가하고 기본 18개 주간 납품 목록으로 초기화합니다">+ 캐릭터 추가</button>
            </div>
          </div>
          <div class="char-tabs-list" id="delivery-char-tabs-list">
            <!-- Populated dynamically by renderDeliveryCharacterBar() -->
          </div>
          <div id="delivery-char-context-banner" class="char-context-banner" style="display: none;"></div>
        </div>

        <!-- Collapsed Compact Bar (shown when collapsed) -->
        <div id="delivery-targets-collapsed-bar" style="display: none; margin-top: 10px; padding: 8px 0; border-top: 1px solid var(--line-subtle); font-size: var(--fs-md); color: var(--text-muted); cursor: pointer;" onclick="toggleDeliveryTargetsView()">
          <span style="flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;"><span class="sym-prefix">&gt;</span><span id="delivery-targets-quick-summary">등록된 주간 납품 목표 요약 정보...</span></span>
        </div>

        <!-- Expanded Content (shown by default) -->
        <div id="delivery-targets-expanded-content" style="margin-top: 12px;">
          <!-- Collapsible Drawer for Manual Add, Quest Import, and Presets (Collapsed by default so 18 cards appear immediately) -->
          <div id="delivery-tools-drawer" class="delivery-tools-drawer" style="display: none;">
            <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; margin-bottom: 10px; padding-bottom: 10px; border-bottom: 1px solid var(--line-subtle);">
              <span style="font-size: var(--fs-sm); font-weight: 700; color: var(--text-bright);"><span class="sym-prefix">&gt;</span>퀘스트 연동 및 목록 일괄 도구</span>
              <div style="display: flex; gap: 6px; flex-wrap: wrap; align-items: center;">
                <button class="btn btn-sm btn-solid-core" onclick="openQuestSelectModal('delivery')" title="인게임 추적 중인 모든 퀘스트 목록을 띄워 원하는 품목만 체크하여 가져옵니다">퀘스트 선택 가져오기</button>
                <button class="btn btn-sm btn-boxed" onclick="addCurrentQuestTarget()" title="게임 내 추적 중인 납품 퀘스트를 즉시 목록에 추가합니다">현재 퀘스트 즉시 등록</button>
                <button class="btn btn-sm btn-boxed" onclick="clearDeliveryTargets()" title="현재 선택된 캐릭터의 납품 목표 목록을 모두 비웁니다">전체 비우기</button>
              </div>
            </div>

            <!-- Quick Manual Add Input Form -->
            <div style="display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; margin-bottom: 12px; padding-bottom: 12px; border-bottom: 1px solid var(--line-subtle);">
              <div class="ac-wrapper" style="flex: 2; min-width: 170px;">
                <label style="font-size: var(--fs-sm); color: var(--text-muted); font-weight: 600; margin-bottom: 4px; display: block;">납품 아이템명 (초성·부분 검색 지원)</label>
                <input type="text" id="target-input-item" class="input-text" autocomplete="off" placeholder="예: 론 엣지소드S (또는 ㄹㅇㅈ)" style="width: 100%;" oninput="onItemInputAutocomplete(this, 'ac-target-input-item')" onkeydown="handleAutocompleteKeydown(event, this, 'ac-target-input-item', addManualTarget)">
                <div id="ac-target-input-item" class="ac-dropdown"></div>
              </div>
              <div style="flex: 1; min-width: 80px;">
                <label style="font-size: var(--fs-sm); color: var(--text-muted); font-weight: 600; margin-bottom: 4px; display: block;">목표 수량</label>
                <input type="number" id="target-input-goal" class="input-number" min="1" max="100" value="6" style="width: 100%;" onkeydown="if(event.key==='Enter') addManualTarget()">
              </div>
              <div style="flex: 1; min-width: 90px;">
                <label style="font-size: var(--fs-sm); color: var(--text-muted); font-weight: 600; margin-bottom: 4px; display: block;">현재 보유 (선택)</label>
                <input type="number" id="target-input-current" class="input-number" min="0" max="100" placeholder="자동 감지" style="width: 100%;" onkeydown="if(event.key==='Enter') addManualTarget()">
              </div>
              <div>
                <button class="btn btn-boxed" onclick="addManualTarget()">+ 직접 추가</button>
              </div>
            </div>

            <!-- Preset Save/Load Section -->
            <div>
              <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; flex-wrap: wrap; gap: 8px;">
                <div style="display: flex; align-items: center; gap: 8px;">
                  <span style="font-weight: 700; font-size: var(--fs-sm); color: var(--text-bright);"><span class="sym-prefix">&gt;</span>주간 납품 프리셋 (퀘스트 조합 저장)</span>
                  <span id="preset-count-badge" class="badge-dim">0개 저장됨</span>
                </div>
                <div style="display: flex; gap: 6px; align-items: center;">
                  <input type="text" id="preset-save-name" class="input-text" placeholder="프리셋 이름 (예: 대장간 세트)" style="width: 180px; padding: 4px 8px; font-size: var(--fs-sm);">
                  <button class="btn btn-sm btn-boxed" onclick="saveCurrentAsPreset()">현재 목록 저장</button>
                </div>
              </div>
              <div id="preset-list-container" style="display: flex; flex-wrap: wrap; gap: 8px; min-height: 24px;">
                <span style="color: var(--text-muted); font-size: var(--fs-sm);"><span class="sym-prefix">&gt;</span>저장된 프리셋이 없습니다. 납품 목표를 등록한 뒤 이름을 지정해 저장하세요.</span>
              </div>
            </div>
          </div>

          <!-- Registered Delivery Targets Grid / List -->
          <div id="delivery-targets-container" style="display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 10px;">
            <p style="color: var(--text-muted); font-size: var(--fs-sm); grid-column: 1/-1;"><span class="sym-prefix">&gt;</span>등록된 주간 납품 목표가 없습니다.</p>
          </div>
        </div>
      </div>

      <!-- Unified Batch Planner Section -->
      <div class="card">
        <div class="card-title">
          <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap;">
            <span><span class="sym-prefix">//</span>주간 납품 통합 플래너</span>
            <span class="badge delivery">최적화 일괄 모드</span>
          </div>
          <div style="display: flex; align-items: center; gap: 12px; flex-wrap: wrap;">
            <div class="alter-order-container" title="가공 등록 순서 설정">
              <span class="alter-order-label">가공 우선순위:</span>
              <div class="segmented-control" id="seg-delivery-alter-order">
                <button type="button" class="seg-btn active" id="btn-delivery-alter-high" onclick="setAlterOrder('high_tier')" title="오래 걸리는 고티어 가공품(T7~T1)부터 시설 슬롯에 우선 등록합니다">
                  <span>상위 티어</span>
                </button>
                <button type="button" class="seg-btn" id="btn-delivery-alter-low" onclick="setAlterOrder('low_tier')" title="기초 재료(T1~T7)부터 시설 슬롯에 순차 등록합니다">
                  <span>하위 티어</span>
                </button>
              </div>
            </div>
            <button class="btn btn-sm" onclick="loadBatchPlan()">새로고침</button>
          </div>
        </div>

        <!-- Full-Width Aggregated Materials Table Container -->
        <div>
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; flex-wrap: wrap; gap: 8px;">
            <div style="display: flex; align-items: center; gap: 8px;">
              <h4 style="font-size: var(--fs-md); font-weight: 700; color: var(--text-bright); margin: 0;"><span class="sym-prefix">&gt;</span>전체 통합 필요 1차 가공품 &amp; 채집 분석표</h4>
              <span id="batch-status-text" style="font-size: var(--fs-sm); color: var(--text-muted);">계산 중...</span>
            </div>
            <div style="display: flex; align-items: center; gap: 8px;">
              <button type="button" class="mcb-pill-btn" id="btn-hide-satisfied-delivery" onclick="toggleHideSatisfiedBom()" title="이미 보유량이 충분한 완료 재료를 표에서 숨기고 부족한 재료만 표시합니다" style="font-size: var(--fs-sm);">
                <span class="cli-check-box" id="icon-hide-satisfied-delivery">□</span>
                <span>완료 재료 숨김</span>
              </button>
              <div id="batch-tasks-container" style="display: none;"></div>
            </div>
          </div>
          <div class="bom-table-container">
            <table class="bom-table">
              <thead>
                <tr>
                  <th style="min-width: 140px;">재료명</th>
                  <th style="min-width: 70px; text-align: center;">구분/시설</th>
                  <th style="min-width: 80px; text-align: right;">총 소요</th>
                  <th style="min-width: 110px; text-align: center;">가방(창고)</th>
                  <th style="min-width: 140px;">상태</th>
                </tr>
              </thead>
              <tbody id="bom-table-body">
                <tr><td colspan="5" style="text-align: center; color: var(--text-muted); padding: 14px;">분석 데이터를 불러오는 중...</td></tr>
              </tbody>
            </table>
          </div>
        </div>

        <!-- Facility Slot Status & Warning -->
        <div id="facility-slots-section" style="margin-top: 14px; display: none; border-top: 1px solid var(--line); padding-top: 12px;">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
            <span style="font-size: var(--fs-md); font-weight: 700; color: var(--text-bright);"><span class="sym-prefix">&gt;</span>시설별 가공 슬롯 현황 (시설당 7슬롯)</span>
            <span id="slot-summary-badge" style="font-size: var(--fs-sm); color: var(--text-muted);"></span>
          </div>
          <div id="facility-slots-container" style="display: flex; flex-wrap: wrap; gap: 8px;"></div>
        </div>

        <div id="slot-warnings-banner" style="margin-top: 10px; display: none; border-left: 3px solid var(--invert-bg); padding: 8px 12px; background: var(--bg-panel);">
          <div style="font-size: var(--fs-md); font-weight: 700; color: var(--text-bright); margin-bottom: 4px;">[!] 가공 슬롯 부족 사전 안내</div>
          <div id="slot-warnings-list" style="font-size: var(--fs-sm); color: var(--text-muted); line-height: 1.6;"></div>
        </div>

        <!-- Action Button for Batch Pipeline -->
        <div style="margin-top: 16px; display: flex; justify-content: flex-end; gap: 8px; align-items: center;">
          <button id="btn-batch-abort" class="btn btn-danger-abort" style="display: none;" onclick="abortQuickAlter()">
            즉시 중지
          </button>
          <button id="btn-batch-execute" class="btn btn-solid-core" onclick="executeBatchPipeline()">
            주간 납품 일괄 실행
          </button>
        </div>
      </div>
    </div>

    <!-- Mode 2: 7슬롯 가공대 빠른 실행 탭 컨텐츠 -->
    <div id="tab-content-quick-alter" class="mode-tab-content" style="display: none;">
      <div class="card" id="quick-alter-main-card">
        <div class="card-title" style="margin-bottom: 0;">
          <div style="display: flex; align-items: center; gap: 10px;">
            <span><span class="sym-prefix">//</span>7슬롯 최고 레벨 가공대 빠른 실행</span>
            <span class="badge">7슬롯 최적화</span>
          </div>
          <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap;">
            <div class="alter-order-container" title="가공 우선순위 설정">
              <span class="alter-order-label">가공 우선순위:</span>
              <div class="segmented-control" id="seg-quick-alter-order">
                <button type="button" class="seg-btn active" id="btn-quick-alter-high" onclick="setAlterOrder('high_tier')" title="오래 걸리는 최고 티어(T7 -> T1)부터 1슬롯씩 대기열에 등록합니다">
                  <span>상위 티어</span>
                </button>
                <button type="button" class="seg-btn" id="btn-quick-alter-low" onclick="setAlterOrder('low_tier')" title="기초 하위 티어(T1 -> T7)부터 1슬롯씩 대기열에 등록합니다">
                  <span>하위 티어</span>
                </button>
              </div>
            </div>
            <button class="btn btn-sm" onclick="loadQuickAlterPlan()">새로고침</button>
            <button class="btn btn-sm" id="btn-toggle-quick-alter" onclick="toggleQuickAlterView()">접어두기 ^</button>
          </div>
        </div>

        <!-- Collapsed Compact Bar (shown when collapsed) -->
        <div id="quick-alter-collapsed-bar" style="display: none; margin-top: 10px; padding: 8px 0; border-top: 1px solid var(--line-subtle); font-size: var(--fs-md); color: var(--text-muted); cursor: pointer;" onclick="toggleQuickAlterView()">
          <span><span class="sym-prefix">&gt;</span><span id="quick-alter-quick-summary">7슬롯 가공대 빠른 실행 계획 요약...</span></span>
        </div>

        <!-- Expanded Content -->
        <div id="quick-alter-expanded-content" style="margin-top: 12px;">
          <p id="quick-alter-desc-text" style="font-size: var(--fs-sm); color: var(--text-muted); margin-bottom: 12px; line-height: 1.6;">
            <span class="sym-prefix">&gt;</span>완료된 가공품이 있으면 일괄 수령하여 슬롯(최대 7개)을 확보하고, 부족한 원자재를 사전에 모두 채집한 뒤 최고 티어 재료부터 순서대로 1슬롯씩 대기열에 등록합니다. (가공 전용 재료가 부족한 티어는 자동으로 건너뜁니다)
          </p>

          <!-- Category Filter Tabs -->
          <div style="display: flex; gap: 6px; margin-bottom: 14px; flex-wrap: wrap;">
            <button class="btn btn-sm quick-tab active" id="tab-cat-all" onclick="selectQuickCategory('all')">전체 가공대 일괄</button>
            <button class="btn btn-sm quick-tab" id="tab-cat-금속" onclick="selectQuickCategory('금속')">금속 (백금강괴 ~ 철괴)</button>
            <button class="btn btn-sm quick-tab" id="tab-cat-목재" onclick="selectQuickCategory('목재')">목재 (특급목재 ~ 목재)</button>
            <button class="btn btn-sm quick-tab" id="tab-cat-가죽" onclick="selectQuickCategory('가죽')">가죽 (특급가죽 ~ 가죽)</button>
            <button class="btn btn-sm quick-tab" id="tab-cat-옷감" onclick="selectQuickCategory('옷감')">옷감 (특급옷감 ~ 옷감)</button>
          </div>

          <!-- Quick Alter Plan Container -->
          <div id="quick-alter-plan-container" style="display: flex; flex-direction: column; gap: 12px;">
            <p style="color: var(--text-muted); font-size: var(--fs-sm);"><span class="sym-prefix">&gt;</span>가공대 상태 및 고티어 계획을 분석 중입니다...</p>
          </div>

          <!-- Bottom Action Bar -->
          <div style="margin-top: 16px; display: flex; justify-content: space-between; align-items: center; border-top: 1px solid var(--line); padding-top: 14px; flex-wrap: wrap; gap: 10px;">
            <div id="quick-gather-summary" style="font-size: var(--fs-sm); color: var(--text-muted); display: flex; align-items: center; gap: 6px;">
              <!-- Gathering summary populated by JS -->
            </div>
            <div style="display: flex; gap: 8px; align-items: center;">
              <button id="btn-quick-alter-abort" class="btn btn-danger-abort" style="display: none;" onclick="abortQuickAlter()">
                즉시 중지
              </button>
              <button id="btn-quick-alter-execute" class="btn btn-solid-core" onclick="executeQuickAlter()">
                7슬롯 빠른 가공 실행
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Mode 3: 개별 아이템 지정 제작 탭 컨텐츠 -->
    <div id="tab-content-custom-craft" class="mode-tab-content" style="display: none;">
      <!-- Custom Targets Manager Section -->
      <div class="card" id="custom-targets-main-card">
        <div class="card-title" style="margin-bottom: 0;">
          <div style="display: flex; align-items: center; gap: 10px; flex-shrink: 0;">
            <span><span class="sym-prefix">//</span>개별 제작 목표 품목 등록 &amp; 관리</span>
            <span id="custom-target-count-badge" class="badge">0개 등록</span>
          </div>
          <div style="display: flex; flex-wrap: wrap; gap: 6px; align-items: center;">
            <button class="btn btn-sm btn-solid-core" onclick="openQuestSelectModal('custom')" title="인게임 추적 중인 퀘스트 목록을 띄워 원하는 품목만 체크하여 지정 제작에 가져옵니다">퀘스트 선택 가져오기</button>
            <button class="btn btn-sm" onclick="openAiToolboxModal('zero_gather')" title="채집 없이 보유 재료만으로 즉시 제작 가능한 품목을 추천합니다">창고 털이 추천</button>
            <button class="btn btn-sm" onclick="openAiToolboxModal('safety_stock')" title="전투 식량·물약 안전 재고 부족분을 원클릭 보충합니다">상비약 보충</button>
            <button class="btn btn-sm" onclick="resetAllCustomTargetsCurrent()" title="모든 개별 제작 목표의 현재 보유 수량을 0개로 일괄 초기화합니다">수량 0개로 초기화</button>
            <button class="btn btn-sm" onclick="syncCustomTargetsInventory()" title="게임 내 인벤토리/창고 실제 보유량으로 수량을 다시 동기화합니다">인벤 동기화</button>
            <button class="btn btn-sm" onclick="openRecipeManagerModal()" title="아이템별 1회 제작 소요 재료를 확인하고 수정합니다">레시피 도감/수정</button>
            <button class="btn btn-sm" onclick="clearCustomTargets()">전체 비우기</button>
            <button class="btn btn-sm" onclick="loadCustomTargets()">새로고침</button>
          </div>
        </div>

        <div style="margin-top: 14px;">
          <!-- Quick Manual Add Input Form -->
          <div style="margin-bottom: 14px; padding-bottom: 14px; border-bottom: 1px solid var(--line);">
            <div style="font-size: var(--fs-sm); color: var(--text-muted); margin-bottom: 8px;">
              <span><span class="sym-prefix">&gt;</span>제작할 아이템 직접 등록 (원하는 품목과 수량을 등록하면 아래 플래너가 모든 하위 재료를 일괄 계산합니다)</span>
            </div>
            <div style="display: flex; gap: 10px; align-items: center; flex-wrap: wrap;">
              <div class="ac-wrapper" style="flex: 2; min-width: 200px;">
                <input type="text" id="custom-input-item" class="input-text" autocomplete="off" placeholder="아이템명 입력 (예: 론 엣지소드S, 은 펜던트 또는 초성 ㄹㅇㅈ)" style="width: 100%;" oninput="onItemInputAutocomplete(this, 'ac-custom-input-item')" onkeydown="handleAutocompleteKeydown(event, this, 'ac-custom-input-item', addManualCustomTarget)">
                <div id="ac-custom-input-item" class="ac-dropdown"></div>
              </div>
              <div style="display: flex; align-items: center; gap: 6px;">
                <span style="font-size: var(--fs-sm); color: var(--text-muted); white-space: nowrap;">목표 수량:</span>
                <input type="number" id="custom-input-count" class="input-number" value="1" min="1" max="999" style="width: 75px; text-align: center;" onkeydown="if(event.key==='Enter') addManualCustomTarget()">
                <span style="font-size: var(--fs-sm); color: var(--text-muted);">개</span>
              </div>
              <button class="btn" onclick="addManualCustomTarget()">+ 목표 추가</button>
            </div>
            <div style="display: flex; gap: 6px; align-items: center; margin-top: 8px; flex-wrap: wrap;">
              <span style="font-size: var(--fs-sm); color: var(--text-muted);"><span class="sym-prefix">&gt;</span>빠른 추천:</span>
              <button type="button" class="btn btn-sm" onclick="quickAddCustomTarget('론 엣지소드S', 4)">론 엣지소드S x4</button>
              <button type="button" class="btn btn-sm" onclick="quickAddCustomTarget('은 펜던트', 4)">은 펜던트 x4</button>
              <button type="button" class="btn btn-sm" onclick="quickAddCustomTarget('실크 로브', 2)">실크 로브 x2</button>
              <button type="button" class="btn btn-sm" onclick="quickAddCustomTarget('가죽 아머', 2)">가죽 아머 x2</button>
            </div>
          </div>

          <!-- Registered Custom Targets Grid / List -->
          <div id="custom-targets-container" style="display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px;">
            <p style="color: var(--text-muted); font-size: var(--fs-sm); grid-column: 1/-1;"><span class="sym-prefix">&gt;</span>등록된 개별 제작 목표가 없습니다. 위 입력창에서 제작할 아이템을 등록하세요.</p>
          </div>
        </div>
      </div>

      <!-- Unified Custom Batch Planner Section -->
      <div class="card" id="custom-plan-main-card">
        <div class="card-title">
          <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap;">
            <span><span class="sym-prefix">//</span>개별 지정 제작 통합 플래너</span>
            <span class="badge">개별 지정 일괄 모드</span>
          </div>
          <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap;">
            <div class="alter-order-container" title="가공 등록 순서 설정">
              <span class="alter-order-label">가공 우선순위:</span>
              <div class="segmented-control" id="seg-custom-alter-order">
                <button type="button" class="seg-btn active" id="btn-custom-alter-high" onclick="setAlterOrder('high_tier')" title="오래 걸리는 고티어 가공품(T7~T1)부터 시설 슬롯에 우선 등록합니다">
                  <span>상위 티어</span>
                </button>
                <button type="button" class="seg-btn" id="btn-custom-alter-low" onclick="setAlterOrder('low_tier')" title="기초 재료(T1~T7)부터 시설 슬롯에 순차 등록합니다">
                  <span>하위 티어</span>
                </button>
              </div>
            </div>
            <button class="btn btn-sm" onclick="loadCustomPlan()">새로고침</button>
          </div>
        </div>

        <!-- Full-Width Aggregated Materials Table Container -->
        <div>
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; flex-wrap: wrap; gap: 8px;">
            <div style="display: flex; align-items: center; gap: 8px;">
              <h4 style="font-size: var(--fs-md); font-weight: 700; color: var(--text-bright); margin: 0;"><span class="sym-prefix">&gt;</span>개별 제작 통합 필요 1차 가공품 &amp; 채집 분석표</h4>
              <span id="custom-batch-status-text" style="font-size: var(--fs-sm); color: var(--text-muted);">계산 중...</span>
            </div>
            <div>
              <button type="button" class="mcb-pill-btn" id="btn-hide-satisfied-custom" onclick="toggleHideSatisfiedBom()" title="이미 보유량이 충분한 완료 재료를 표에서 숨기고 부족한 재료만 표시합니다" style="font-size: var(--fs-sm);">
                <span class="cli-check-box" id="icon-hide-satisfied-custom">□</span>
                <span>완료 재료 숨김</span>
              </button>
            </div>
          </div>
          <div class="bom-table-container">
            <table class="bom-table">
              <thead>
                <tr>
                  <th style="min-width: 140px;">재료명</th>
                  <th style="min-width: 70px; text-align: center;">구분/시설</th>
                  <th style="min-width: 80px; text-align: right;">총 소요</th>
                  <th style="min-width: 110px; text-align: center;">가방(창고)</th>
                  <th style="min-width: 140px;">상태</th>
                </tr>
              </thead>
              <tbody id="custom-batch-materials-tbody">
                <tr>
                  <td colspan="5" style="text-align: center; color: var(--text-muted); padding: 20px;">개별 제작 목표를 등록하면 전체 소요 자재가 분석됩니다.</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <!-- Facility Slots Status & Overflow Warnings Section -->
        <div id="custom-facility-slots-section" style="margin-top: 14px; display: none; border-top: 1px solid var(--line); padding-top: 12px;">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
            <span style="font-size: var(--fs-md); font-weight: 700; color: var(--text-bright);"><span class="sym-prefix">&gt;</span>가공 시설별 슬롯 현황 &amp; 수용 가능 여부</span>
            <span id="custom-slot-summary-badge" style="font-size: var(--fs-sm);"></span>
          </div>
          <div id="custom-facility-slots-container" style="display: flex; gap: 8px; flex-wrap: wrap;"></div>
        </div>

        <!-- Overflow Warning Banner -->
        <div id="custom-slot-warnings-banner" style="display: none; margin-top: 10px; padding: 8px 12px; background: var(--bg-panel); border-left: 3px solid var(--invert-bg); font-size: var(--fs-sm); color: var(--text);">
          <div style="font-weight: 700; margin-bottom: 4px; color: var(--text-bright);">[!] 시설 슬롯 부족 안내</div>
          <div id="custom-slot-warnings-list"></div>
        </div>

        <!-- Bottom Execution Action Bar -->
        <div style="margin-top: 16px; display: flex; justify-content: space-between; align-items: center; border-top: 1px solid var(--line); padding-top: 14px; flex-wrap: wrap; gap: 10px;">
          <div id="custom-plan-summary-text" style="font-size: var(--fs-sm); color: var(--text-muted); display: flex; align-items: center; gap: 6px;">
            <!-- Summary populated by JS -->
          </div>
          <div style="display: flex; gap: 8px; align-items: center;">
            <button id="btn-custom-abort" class="btn btn-danger-abort" style="display: none;" onclick="abortQuickAlter()">
              즉시 중지
            </button>
            <button id="btn-custom-execute" class="btn btn-solid-core" onclick="executeCustomPipeline()">
              개별 제작 일괄 실행
            </button>
          </div>
        </div>
      </div>
    </div>

    </div><!-- /#top-view-living -->

    <!-- ========================================================= -->
    <!-- [TOP VIEW 2] 📋 숙제 지원 (Homework Support View)         -->
    <!-- ========================================================= -->
    <div id="top-view-homework" style="display: none;">

      <!-- Homework Support Master Command Bar -->
      <div id="homework-master-command-bar" class="master-command-bar">
        <div class="mcb-mode-tabs" role="tablist">
          <button type="button" class="mcb-mode-tab active" id="btn-hw-tab-main" onclick="switchHomeworkSubTab('main')">
            <span>캐릭터 숙제 &amp; 인게임 상태</span>
            <span class="mcb-tab-pill" id="hw-tab-pill-main">진행 중</span>
          </button>
          <button type="button" class="mcb-mode-tab" id="btn-hw-tab-summary" onclick="switchHomeworkSubTab('summary')">
            <span>전체 캐릭터 종합 현황표</span>
            <span class="mcb-tab-pill" id="hw-tab-pill-summary">6캐릭</span>
          </button>
          <button type="button" class="mcb-mode-tab" id="btn-hw-tab-matrix" onclick="switchHomeworkSubTab('matrix')">
            <span>주간 숙제 매트릭스 체크표</span>
            <span class="mcb-tab-pill" id="hw-tab-pill-matrix">주간</span>
          </button>
          <button type="button" class="mcb-mode-tab" id="btn-hw-tab-settings" onclick="switchHomeworkSubTab('settings')">
            <span>숙제·캐릭터·알림 설정</span>
            <span class="mcb-tab-pill">관리</span>
          </button>
        </div>

        <div class="mcb-inner">
          <div class="mcb-left">
            <div class="mcb-briefing" id="hw-mcb-briefing">
              <span class="sym-prefix">&gt;</span>숙제 진행 현황을 불러오는 중입니다...
            </div>
            <div class="mcb-options-row">
              <button type="button" class="mcb-pill-btn" id="btn-hw-opt-reset" onclick="toggleHomeworkSetting('auto_reset_enabled')" title="매일 오전 06:00 일일/집중 숙제 및 월요일 오전 06:00 주간 숙제 자동 초기화">
                <span class="cli-check-box" id="icon-hw-opt-reset">■</span>
                <span>06시 자동 초기화</span>
              </button>
              <button type="button" class="mcb-pill-btn" id="btn-hw-opt-autoswitch" onclick="toggleHomeworkSetting('auto_detect_character')" title="인게임 로그인 캐릭터(이름/생활력/매력/전투력/직업) 감지 시 자동 탭 전환">
                <span class="cli-check-box" id="icon-hw-opt-autoswitch">■</span>
                <span>인게임 캐릭터 자동 전환</span>
              </button>
              <button type="button" class="mcb-pill-btn" id="btn-hw-opt-autocheck" onclick="toggleHomeworkSetting('auto_check_missions')" title="인게임 일일 미션, 던전 클리어, 어비스 클리어, 사냥터 전리품 감지 시 해당 숙제 자동 체크">
                <span class="cli-check-box" id="icon-hw-opt-autocheck">■</span>
                <span>미션·던전·어비스 자동 체크</span>
              </button>
              <button type="button" class="mcb-pill-btn" id="btn-hw-opt-omen" onclick="toggleHomeworkSetting('omen_barrier_alert')" title="매시간 정각(00분~05분) 불길한 소환의 결계 알림 배너 및 사운드">
                <span class="cli-check-box" id="icon-hw-opt-omen">■</span>
                <span>정각 결계 알림</span>
              </button>
            </div>
          </div>

          <div class="mcb-right" style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
            <button type="button" class="btn btn-boxed" id="btn-hw-focus-mode" onclick="toggleHomeworkSetting('focus_mode_enabled')" title="켜면 마스터바의 추천 순서와 완료/되돌리기 버튼이 [일일 숙제] 대신 [집중 숙제]를 대상으로 작동합니다">
              <span id="icon-hw-focus-mode" style="font-family: var(--font-mono); margin-right: 4px;">□</span>집중 모드
            </button>
            <button type="button" class="btn btn-boxed" id="btn-hw-rollback" onclick="rollbackHomeworkStep()" title="방금 완료한 숙제 단계를 이전 상태로 되돌립니다">
              ↩ 되돌리기
            </button>
            <button type="button" class="btn btn-mcb-start" id="btn-hw-complete-step" onclick="completeCurrentHomeworkStep()" title="현재 순서의 숙제를 즉시 완료 처리하고 다음 순서로 넘어갑니다">
              ✓ 현재 숙제 완료
            </button>
          </div>
        </div>
      </div>

      <!-- Unified Multi-Character Profile Bar (Shared with Weekly Delivery) -->
      <div class="card" id="hw-character-bar-card" style="margin-bottom: 16px;">
        <div class="card-title" style="margin-bottom: 0;">
          <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap;">
            <span><span class="sym-prefix">//</span>다중 캐릭터 숙제 &amp; 납품 통합 프로필</span>
            <span id="hw-recommended-focus-badge" class="badge-dim mono-num">집중 추천: 확인 중</span>
            <span id="hw-ingame-match-badge" class="badge-dim mono-num" style="display: none;"></span>
          </div>
          <div style="display: flex; flex-wrap: wrap; gap: 6px; align-items: center;">
            <button type="button" class="btn btn-sm btn-boxed" onclick="linkActiveCharacterToIngame()" title="현재 선택된 탭을 지금 게임에 접속 중인 캐릭터와 연동합니다">현 캐릭터 연동</button>
            <button type="button" class="btn btn-sm btn-boxed" onclick="promptRenameActiveCharacter()" title="현재 선택된 캐릭터 탭의 이름을 변경합니다">이름변경</button>
            <button type="button" class="btn btn-sm btn-boxed" onclick="promptAddCharacterProfile()" title="새로운 캐릭터 프로필을 추가합니다">+ 캐릭터 추가</button>
            <button type="button" class="btn btn-sm" onclick="refreshHomeworkLiveMissions()" title="인게임 미션, 진행 퀘스트를 즉시 새로고침합니다">새로고침</button>
            <button type="button" class="btn btn-sm" onclick="switchHomeworkSubTab('settings')">캐릭터·숙제 편집</button>
          </div>
        </div>
        <div class="char-profile-section" id="hw-character-bar">
          <div class="char-tabs-list" id="hw-character-tabs-list">
            <!-- Populated by renderHomeworkCharacterBar() -->
          </div>
        </div>
      </div>

      <!-- ===================================================== -->
      <!-- [HW SUB-TAB 1] 캐릭터 숙제 & 인게임 상태               -->
      <!-- ===================================================== -->
      <div id="hw-subview-main">

        <!-- 3-Column Homework Checklist Grid (Daily / Focus / Weekly) -->
        <div class="hw-checklist-grid">
          <!-- Column 1: 일일 숙제 -->
          <div class="hw-cat-card cat-daily">
            <div style="display: flex; justify-content: space-between; align-items: center; gap: 6px; padding-bottom: 6px; border-bottom: 1px solid var(--line); flex-wrap: wrap;">
              <div style="display: flex; align-items: center; gap: 6px;">
                <span style="font-size: var(--fs-md); font-weight: 700; color: var(--text-bright);"><span class="sym-prefix">//</span>일일 숙제</span>
                <span class="badge-dim">매일 06시</span>
              </div>
              <div style="display: flex; align-items: center; gap: 4px; flex-wrap: wrap;">
                <span id="hw-daily-prog-text" class="mono-num" style="font-size: var(--fs-sm); font-weight: 700; color: var(--text-bright);">0/0</span>
                <button type="button" class="btn btn-sm btn-boxed" onclick="batchHomeworkCategory('daily', true)">전체완료</button>
                <button type="button" class="btn btn-sm btn-boxed" onclick="batchHomeworkCategory('daily', false)">초기화</button>
                <button type="button" class="btn btn-sm" id="btn-toggle-hw-cat-daily" onclick="toggleHwCategoryView('daily')">접기 ^</button>
              </div>
            </div>
            <div id="hw-cat-collapsed-daily" class="hw-cat-collapsed-bar" style="display: none;" onclick="toggleHwCategoryView('daily')">
              <span id="hw-cat-collapsed-text-daily"><span class="sym-prefix">&gt;</span>일일 숙제 요약...</span>
              <span style="font-size: 11px;">펼치기 v</span>
            </div>
            <div id="hw-cat-body-daily" style="display: flex; flex-direction: column; gap: 6px;">
              <div class="hw-progress-bar-wrap">
                <div id="hw-daily-prog-bar" class="hw-progress-bar-fill" style="width: 0%;"></div>
              </div>
              <div id="hw-daily-task-list" class="hw-task-list"></div>
            </div>
          </div>

          <!-- Column 2: 집중 숙제 -->
          <div class="hw-cat-card cat-focus">
            <div style="display: flex; justify-content: space-between; align-items: center; gap: 6px; padding-bottom: 6px; border-bottom: 1px solid var(--line); flex-wrap: wrap;">
              <div style="display: flex; align-items: center; gap: 6px; flex-wrap: wrap;">
                <span style="font-size: var(--fs-md); font-weight: 700; color: var(--text-bright);"><span class="sym-prefix">//</span>집중 숙제</span>
                <span id="hw-focus-last-date" class="badge-dim mono-num">기록 없음</span>
              </div>
              <div style="display: flex; align-items: center; gap: 4px; flex-wrap: wrap;">
                <span id="hw-focus-prog-text" class="mono-num" style="font-size: var(--fs-sm); font-weight: 700; color: var(--text-bright);">0/0</span>
                <button type="button" class="btn btn-sm btn-boxed" id="btn-hw-toggle-weekly-focus" onclick="toggleActiveCharWeeklyFocus()" title="이번 주 이 캐릭터의 집중 숙제 완료(✓집중) 표시를 수동으로 체크하거나 해제합니다 (매주 월요일 오전 6시 초기화)">✓집중체크</button>
                <button type="button" class="btn btn-sm btn-boxed" onclick="batchHomeworkCategory('focus', true)">전체완료</button>
                <button type="button" class="btn btn-sm btn-boxed" onclick="batchHomeworkCategory('focus', false)">초기화</button>
                <button type="button" class="btn btn-sm" id="btn-toggle-hw-cat-focus" onclick="toggleHwCategoryView('focus')">접기 ^</button>
              </div>
            </div>
            <div id="hw-cat-collapsed-focus" class="hw-cat-collapsed-bar" style="display: none;" onclick="toggleHwCategoryView('focus')">
              <span id="hw-cat-collapsed-text-focus"><span class="sym-prefix">&gt;</span>집중 숙제 요약...</span>
              <span style="font-size: 11px;">펼치기 v</span>
            </div>
            <div id="hw-cat-body-focus" style="display: flex; flex-direction: column; gap: 6px;">
              <div class="hw-progress-bar-wrap">
                <div id="hw-focus-prog-bar" class="hw-progress-bar-fill" style="width: 0%;"></div>
              </div>
              <div id="hw-focus-task-list" class="hw-task-list"></div>
            </div>
          </div>

          <!-- Column 3: 주간 숙제 -->
          <div class="hw-cat-card cat-weekly">
            <div style="display: flex; justify-content: space-between; align-items: center; gap: 6px; padding-bottom: 6px; border-bottom: 1px solid var(--line); flex-wrap: wrap;">
              <div style="display: flex; align-items: center; gap: 6px;">
                <span style="font-size: var(--fs-md); font-weight: 700; color: var(--text-bright);"><span class="sym-prefix">//</span>주간 숙제</span>
                <span class="badge-dim">월요일 06시</span>
              </div>
              <div style="display: flex; align-items: center; gap: 4px; flex-wrap: wrap;">
                <span id="hw-weekly-prog-text" class="mono-num" style="font-size: var(--fs-sm); font-weight: 700; color: var(--text-bright);">0/0</span>
                <button type="button" class="btn btn-sm btn-boxed" onclick="batchHomeworkCategory('weekly', true)">전체완료</button>
                <button type="button" class="btn btn-sm btn-boxed" onclick="batchHomeworkCategory('weekly', false)">초기화</button>
                <button type="button" class="btn btn-sm" id="btn-toggle-hw-cat-weekly" onclick="toggleHwCategoryView('weekly')">접기 ^</button>
              </div>
            </div>
            <div id="hw-cat-collapsed-weekly" class="hw-cat-collapsed-bar" style="display: none;" onclick="toggleHwCategoryView('weekly')">
              <span id="hw-cat-collapsed-text-weekly"><span class="sym-prefix">&gt;</span>주간 숙제 요약...</span>
              <span style="font-size: 11px;">펼치기 v</span>
            </div>
            <div id="hw-cat-body-weekly" style="display: flex; flex-direction: column; gap: 6px;">
              <div class="hw-progress-bar-wrap">
                <div id="hw-weekly-prog-bar" class="hw-progress-bar-fill" style="width: 0%;"></div>
              </div>
              <div id="hw-weekly-task-list" class="hw-task-list"></div>
            </div>
          </div>
        </div>

        <!-- Character Memo & Weekly Delivery Quick Status Card -->
        <div class="card">
          <div class="card-title">
            <span><span class="sym-prefix">//</span><span id="hw-memo-char-title">캐릭터</span> 개별 메모장 &amp; 주간 납품 연동</span>
            <div style="display: flex; gap: 6px; align-items: center;">
              <span id="hw-memo-save-status" style="font-size: 11px; color: var(--text-muted);">자동 저장 지원</span>
              <button type="button" class="btn btn-sm btn-boxed" onclick="saveActiveCharacterMemo()">메모 저장</button>
            </div>
          </div>
          <textarea id="hw-char-memo-input" class="input-text" rows="2" style="width: 100%; resize: vertical; font-family: var(--font-sans); line-height: 1.5;" placeholder="이 캐릭터의 장비 목표, 룬 정가 계획, 주간 특이사항 등을 자유롭게 적어두세요. (포커스 해제 시 자동 저장)" onblur="saveActiveCharacterMemo()"></textarea>

          <!-- Weekly Delivery Quick Status Box -->
          <div id="hw-delivery-quick-box" style="margin-top: 10px; padding: 8px 12px; background: var(--bg-sub-panel); border: 1px solid var(--line-subtle); display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap;">
            <div>
              <div style="font-size: var(--fs-sm); font-weight: 700; color: var(--text-bright);">
                <span class="sym-prefix">&gt;</span>주간 납품 연동 상태: <span id="hw-delivery-quick-status" class="mono-num">확인 중...</span>
              </div>
              <div style="font-size: 11px; color: var(--text-muted); margin-top: 2px;">생활 지원 탭의 주간 납품 캐릭터 프로필과 실시간 공유됩니다.</div>
            </div>
            <div style="display: flex; gap: 6px; align-items: center;">
              <button type="button" class="btn btn-sm btn-boxed" onclick="switchTopAppMode('living')">⚒️ 주간 납품 탭으로 이동</button>
            </div>
          </div>
        </div>

        <!-- Live In-Game Missions & Quests Tracker Card -->
        <div class="card" id="hw-ingame-missions-card">
          <div class="card-title">
            <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">
              <span><span class="sym-prefix">//</span>인게임 실시간 미션 · 주간 의뢰 · 진행 퀘스트 트래커</span>
              <span id="hw-live-missions-badge" class="badge-dim">CLI 실시간 수집</span>
            </div>
            <div style="display: flex; gap: 6px; align-items: center;">
              <button type="button" class="btn btn-sm" onclick="refreshHomeworkLiveMissions()">새로고침</button>
            </div>
          </div>
          <div class="hw-checklist-grid" style="margin-bottom: 0;">
            <!-- Col 1: 인게임 일일 미션 -->
            <div style="background: var(--bg-sub-panel); border: 1px solid var(--line-subtle); padding: 10px 12px;">
              <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; padding-bottom: 6px; border-bottom: 1px solid var(--line-subtle);">
                <span style="font-size: var(--fs-md); font-weight: 700; color: var(--text-bright);"><span class="sym-prefix">&gt;</span>인게임 일일 미션 [N]</span>
                <span id="hw-live-daily-count" class="badge-dim mono-num">0건</span>
              </div>
              <div id="hw-live-daily-missions-list" style="max-height: 220px; overflow-y: auto; display: flex; flex-direction: column; gap: 4px; font-size: var(--fs-sm);">
                <div style="color: var(--text-muted);">게임 연결 시 실시간 일일 미션 진행도가 표시됩니다.</div>
              </div>
            </div>

            <!-- Col 2: 인게임 주간 미션 -->
            <div style="background: var(--bg-sub-panel); border: 1px solid var(--line-subtle); padding: 10px 12px;">
              <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; padding-bottom: 6px; border-bottom: 1px solid var(--line-subtle);">
                <span style="font-size: var(--fs-md); font-weight: 700; color: var(--text-bright);"><span class="sym-prefix">&gt;</span>인게임 주간 미션 &amp; 어비스/길드</span>
                <span id="hw-live-weekly-count" class="badge-dim mono-num">0건</span>
              </div>
              <div id="hw-live-weekly-missions-list" style="max-height: 220px; overflow-y: auto; display: flex; flex-direction: column; gap: 4px; font-size: var(--fs-sm);">
                <div style="color: var(--text-muted);">게임 연결 시 실시간 주간 미션 진행도가 표시됩니다.</div>
              </div>
            </div>

            <!-- Col 3: 진행 중인 퀘스트 & 던전/사냥터 상태 -->
            <div style="background: var(--bg-sub-panel); border: 1px solid var(--line-subtle); padding: 10px 12px;">
              <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; padding-bottom: 6px; border-bottom: 1px solid var(--line-subtle);">
                <span style="font-size: var(--fs-md); font-weight: 700; color: var(--text-bright);"><span class="sym-prefix">&gt;</span>진행 중인 퀘스트 · 사냥터 · 던전</span>
                <span id="hw-live-quests-count" class="badge-dim mono-num">0건</span>
              </div>
              <div id="hw-live-quests-list" style="max-height: 220px; overflow-y: auto; display: flex; flex-direction: column; gap: 4px; font-size: var(--fs-sm);">
                <div style="color: var(--text-muted);">게임 연결 시 진행 중인 퀘스트 및 던전 상태가 표시됩니다.</div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- ===================================================== -->
      <!-- [HW SUB-TAB 2] 전체 캐릭터 종합 현황표                 -->
      <!-- ===================================================== -->
      <div id="hw-subview-summary" style="display: none;">
        <div class="card">
          <div class="card-title">
            <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">
              <span><span class="sym-prefix">//</span>전체 캐릭터 종합 숙제 · 스펙 · 재화 현황표</span>
              <span class="badge">일일 / 집중 / 주간 / 납품 한눈에 보기</span>
            </div>
            <div style="display: flex; gap: 6px; align-items: center;">
              <button type="button" class="btn btn-sm" onclick="switchHomeworkSubTab('matrix')">주간 매트릭스 체크표 보기</button>
            </div>
          </div>
          <div class="bom-table-container">
            <table class="bom-table">
              <thead>
                <tr>
                  <th style="min-width: 130px;">캐릭터 (직업)</th>
                  <th style="min-width: 110px;">전투력 / 생활·매력</th>
                  <th style="min-width: 100px; text-align: center;">무게 / 은동전·공물</th>
                  <th style="min-width: 110px; text-align: center;">일일 숙제</th>
                  <th style="min-width: 140px; text-align: center;">집중 숙제 (최근 완료일)</th>
                  <th style="min-width: 110px; text-align: center;">주간 숙제</th>
                  <th style="min-width: 110px; text-align: center;">주간 납품</th>
                  <th style="min-width: 160px;">캐릭터 메모</th>
                  <th style="min-width: 90px; text-align: center;">작업</th>
                </tr>
              </thead>
              <tbody id="hw-summary-tbody">
                <!-- Populated by renderHomeworkSummaryTable() -->
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- ===================================================== -->
      <!-- [HW SUB-TAB 3] 주간 숙제 매트릭스 체크표               -->
      <!-- ===================================================== -->
      <div id="hw-subview-matrix" style="display: none;">
        <div class="card">
          <div class="card-title">
            <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">
              <span><span class="sym-prefix">//</span>전 캐릭터 주간 숙제 매트릭스 체크표 (클릭하여 즉시 완료/해제)</span>
              <span class="badge">월요일 06:00 자동 초기화</span>
            </div>
            <div style="display: flex; gap: 6px; align-items: center;">
              <button type="button" class="btn btn-sm" onclick="switchHomeworkSubTab('main')">개별 캐릭터 뷰로 돌아가기</button>
            </div>
          </div>
          <div class="bom-table-container">
            <table class="bom-table" id="hw-matrix-table">
              <thead id="hw-matrix-thead"></thead>
              <tbody id="hw-matrix-tbody"></tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- ===================================================== -->
      <!-- [HW SUB-TAB 4] 숙제·캐릭터·알림 설정                   -->
      <!-- ===================================================== -->
      <div id="hw-subview-settings" style="display: none;">
        <div class="grid">
          <!-- Left Column: 캐릭터 프로필 통합 관리 -->
          <div class="card">
            <div class="card-title">
              <span><span class="sym-prefix">//</span>캐릭터 프로필 통합 관리 (숙제 &amp; 주간 납품 공통)</span>
            </div>
            <div style="font-size: var(--fs-sm); color: var(--text-muted); margin-bottom: 10px;">
              <span class="sym-prefix">&gt;</span>여기서 캐릭터를 추가·수정·순서 변경하면 <strong>숙제 지원</strong>과 <strong>생활 지원(주간 납품)</strong> 양쪽에 즉시 반영됩니다.
            </div>
            <div style="display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 12px; padding: 10px; background: var(--bg-sub-panel); border: 1px solid var(--line-subtle);">
              <input type="text" id="hw-new-char-name" class="input-text" placeholder="캐릭터명 (예: 카르브)" style="width: 120px;">
              <input type="text" id="hw-new-char-class" class="input-text" placeholder="직업 (예: 수도사)" style="width: 110px;">
              <input type="number" id="hw-new-char-cp" class="input-text" placeholder="전투력 (선택)" style="width: 110px;">
              <input type="text" id="hw-new-char-title" class="input-text" placeholder="칭호 (선택)" style="width: 120px;">
              <button type="button" class="btn btn-solid-core btn-sm" onclick="addHomeworkCharacter()">+ 캐릭터 추가</button>
            </div>
            <div class="bom-table-container">
              <table class="bom-table">
                <thead>
                  <tr>
                    <th>캐릭터명</th>
                    <th>직업</th>
                    <th>전투력</th>
                    <th>칭호</th>
                    <th style="text-align: center;">순서/관리</th>
                  </tr>
                </thead>
                <tbody id="hw-settings-char-tbody"></tbody>
              </table>
            </div>
          </div>

          <!-- Right Column: 카테고리별 숙제 항목 & 대상 캐릭터 필터 관리 -->
          <div class="card">
            <div class="card-title">
              <span><span class="sym-prefix">//</span>숙제 항목 편집 &amp; 캐릭터별 표시 필터</span>
              <div style="display: flex; gap: 6px;">
                <select id="hw-settings-cat-select" class="input-text" onchange="renderHomeworkSettingsTasks()" style="padding: 3px 8px;">
                  <option value="daily">일일 숙제 (매일 06시)</option>
                  <option value="focus">집중 숙제 (권장 로테이션)</option>
                  <option value="weekly">주간 숙제 (월요일 06시)</option>
                </select>
                <button type="button" class="btn btn-sm" onclick="resetHomeworkCategoryDefaults()">기본값 복원</button>
              </div>
            </div>
            <div style="display: flex; gap: 6px; margin-bottom: 12px;">
              <input type="text" id="hw-new-task-input" class="input-text" placeholder="새 숙제 항목 이름 입력 (예: 필드 보스, 심층 던전 등)" style="flex: 1;" onkeydown="if(event.key==='Enter'){event.preventDefault();addHomeworkTaskItem();}">
              <button type="button" class="btn btn-solid-core btn-sm" onclick="addHomeworkTaskItem()">+ 숙제 항목 추가</button>
            </div>
            <div id="hw-settings-tasks-container" style="max-height: 360px; overflow-y: auto; display: flex; flex-direction: column; gap: 8px;">
              <!-- Populated by renderHomeworkSettingsTasks() -->
            </div>
          </div>
        </div>

        <!-- Bottom Settings Card: 디스코드 웹훅 & 데이터 백업/복원 -->
        <div class="card">
          <div class="card-title">
            <span><span class="sym-prefix">//</span>디스코드 이벤트 알림 &amp; 숙제 데이터 백업·복원</span>
          </div>
          <div class="grid" style="margin-bottom: 0;">
            <div>
              <div style="font-size: var(--fs-md); font-weight: 700; color: var(--text-bright); margin-bottom: 8px;"><span class="sym-prefix">&gt;</span>디스코드 웹훅 실시간 알림 설정</div>
              <div style="display: flex; gap: 6px; margin-bottom: 10px; flex-wrap: wrap;">
                <input type="text" id="hw-discord-webhook-input" class="input-text" placeholder="https://discord.com/api/webhooks/..." style="flex: 1; min-width: 240px;">
                <button type="button" class="btn btn-sm btn-solid-core" onclick="saveHomeworkDiscordSettings()">웹훅 저장</button>
                <button type="button" class="btn btn-sm" onclick="testDiscordWebhook()">테스트 전송</button>
              </div>
              <div style="display: flex; gap: 10px; flex-wrap: wrap; font-size: var(--fs-sm);">
                <label style="display: inline-flex; align-items: center; gap: 4px; cursor: pointer;">
                  <input type="checkbox" id="hw-wh-dungeon" onchange="saveHomeworkDiscordSettings()"> 일반 던전 클리어
                </label>
                <label style="display: inline-flex; align-items: center; gap: 4px; cursor: pointer;">
                  <input type="checkbox" id="hw-wh-abyss" onchange="saveHomeworkDiscordSettings()"> 어비스 던전 클리어
                </label>
                <label style="display: inline-flex; align-items: center; gap: 4px; cursor: pointer;">
                  <input type="checkbox" id="hw-wh-hunting" onchange="saveHomeworkDiscordSettings()"> 사냥터 전리품 완료
                </label>
                <label style="display: inline-flex; align-items: center; gap: 4px; cursor: pointer;">
                  <input type="checkbox" id="hw-wh-gathering" onchange="saveHomeworkDiscordSettings()"> 채집 완료
                </label>
                <label style="display: inline-flex; align-items: center; gap: 4px; cursor: pointer;">
                  <input type="checkbox" id="hw-wh-crafting" onchange="saveHomeworkDiscordSettings()"> 가공 완료
                </label>
              </div>
            </div>

            <div>
              <div style="font-size: var(--fs-md); font-weight: 700; color: var(--text-bright); margin-bottom: 8px;"><span class="sym-prefix">&gt;</span>숙제 설정·체크 기록 백업 및 레거시 연동</div>
              <div style="font-size: var(--fs-sm); color: var(--text-muted); margin-bottom: 10px;">
                모든 캐릭터 및 숙제 체크 기록은 <code style="color: var(--text-bright);">내 문서\MobiLivingSupporter</code> 폴더에 자동 저장됩니다.
              </div>
              <div style="display: flex; gap: 8px; flex-wrap: wrap;">
                <button type="button" class="btn btn-sm" onclick="exportHomeworkBackup()">JSON 백업 파일 내보내기</button>
                <button type="button" class="btn btn-sm" onclick="document.getElementById('hw-backup-file-input').click()">JSON 백업 파일 불러오기</button>
                <input type="file" id="hw-backup-file-input" accept=".json" style="display: none;" onchange="importHomeworkBackupFile(event)">
                <button type="button" class="btn btn-sm" onclick="reimportLegacyOverlayData()" title="기존 '모비 숙제 오버레이' 폴더의 캐릭터 및 숙제 데이터를 다시 가져와 병합합니다">기존 숙제 오버레이 데이터 다시 가져오기</button>
              </div>
            </div>
          </div>
        </div>
      </div>

    </div><!-- /#top-view-homework -->

    <!-- Bottom 2-Column Grid: Live Console & Recipe Search -->
    <div class="grid">
      <!-- Left Column: Live Console Section -->
      <div>
        <div class="card">
          <div class="card-title">
            <span><span class="sym-prefix">//</span>실시간 실행 콘솔</span>
            <div id="busy-box" style="display: none;" class="busy-indicator">
              <span class="cli-spinner"><span class="spinner-bracket">[</span><span class="spinner-char">/</span><span class="spinner-bracket">]</span></span>
              <span id="busy-text">작업 진행 중...</span>
              <button class="btn btn-sm" onclick="abortQuickAlter()">중지</button>
            </div>
          </div>
          <div class="console" id="console-logs">
            <div class="log-line"><span class="log-time">[SYSTEM]</span> <span class="log-info">대시보드가 준비되었습니다.</span></div>
          </div>
        </div>
      </div>

      <!-- Right Column: Recipe Quick Search Section -->
      <div>
        <div class="card">
          <div class="card-title">
            <div style="display: flex; align-items: center; gap: 8px;">
              <span><span class="sym-prefix">//</span>레시피 &amp; 재료 스마트 조회</span>
              <span class="badge-dim" id="catalog-count-badge">초성·역방향 검색</span>
            </div>
            <button class="btn btn-sm" onclick="openRecipeManagerModal()" title="아이템 제작 레시피 도감 열기">레시피 수정</button>
          </div>
          <div class="form-row" style="margin-bottom: 8px;">
            <div class="ac-wrapper" style="flex: 1;">
              <input type="text" id="search-query" class="input-text" autocomplete="off" style="width: 100%;" placeholder="아이템·재료명 또는 초성 입력 (예: ㄹㅇㅈ, 목재+, 특수강괴)" oninput="onSearchQueryInput(this)" onkeydown="handleAutocompleteKeydown(event, this, 'ac-search-query', doSearch)">
              <div id="ac-search-query" class="ac-dropdown"></div>
            </div>
            <button class="btn btn-sm" onclick="doSearch()">조회</button>
          </div>
          <div id="search-quick-chips" style="display: flex; gap: 4px; flex-wrap: wrap; align-items: center; margin-bottom: 8px;">
            <span style="font-size: 11px; color: var(--text-dim);">빠른 조회:</span>
            <span class="search-chip" onclick="quickSearchItem('론 엣지소드S')">론 엣지소드S</span>
            <span class="search-chip" onclick="quickSearchItem('특수강괴')">특수강괴</span>
            <span class="search-chip" onclick="quickSearchItem('상급 목재+')">상급 목재+</span>
            <span class="search-chip" onclick="quickSearchItem('최상급 가죽')">최상급 가죽</span>
            <span class="search-chip" onclick="quickSearchItem('나무 진액')">나무 진액</span>
          </div>
          <div id="search-results" style="margin-top: 8px; max-height: 270px; overflow-y: auto; font-size: var(--fs-sm);">
            <div style="color: var(--text-muted); padding: 8px 0;">
              <span class="sym-prefix">&gt;</span>아이템명이나 초성(예: <code class="mono-num">ㄹㅇㅈ</code>, <code class="mono-num">ㅊㅅㄱㅁㅈ</code>)을 입력하면 <strong>1회 제작/가공 재료, 가방·창고 보유량, 해당 재료가 쓰이는 상위 품목(사용처)</strong>을 즉시 확인할 수 있습니다.
            </div>
          </div>
        </div>
      </div>
    </div>
    <datalist id="all-items-datalist"></datalist>
  </div>

  <!-- Execution Summary Modal -->
  <div id="summary-modal-overlay" class="modal-overlay" onclick="handleModalOverlayClick(event)">
    <div class="modal-dialog" role="dialog" aria-modal="true" aria-labelledby="modal-summary-title">
      <div class="modal-header">
        <div>
          <h3 id="modal-summary-title" style="font-size: var(--fs-lg); font-weight: 700; color: var(--text-bright);"><span class="sym-prefix">//</span>작업 진행 종합 결과</h3>
          <div id="modal-summary-subtitle" style="font-size: var(--fs-sm); color: var(--text-muted); margin-top: 2px;"><span class="sym-prefix">&gt;</span>소요 시간: -</div>
        </div>
        <button class="btn btn-sm" onclick="closeSummaryModal()">닫기</button>
      </div>
      <div class="modal-body" id="modal-summary-body">
        <!-- 4 Metric Cards -->
        <div class="summary-stat-grid">
          <div class="summary-stat-card">
            <div class="label">날개 소모</div>
            <div class="val" id="sum-card-wings">0개</div>
            <div id="sum-card-wings-sub" class="mono-num" style="font-size: var(--fs-sm); color: var(--text-muted); margin-top: 2px;">(0 -&gt; 0)</div>
          </div>
          <div class="summary-stat-card">
            <div class="label">필드 채집</div>
            <div class="val" id="sum-card-gather">0품목</div>
            <div id="sum-card-gather-sub" class="mono-num" style="font-size: var(--fs-sm); color: var(--text-muted); margin-top: 2px;">총 0개 획득</div>
          </div>
          <div class="summary-stat-card">
            <div class="label">가공 대기열</div>
            <div class="val" id="sum-card-alter">0슬롯</div>
            <div id="sum-card-alter-sub" class="mono-num" style="font-size: var(--fs-sm); color: var(--text-muted); margin-top: 2px;">0품목 등록</div>
          </div>
          <div class="summary-stat-card">
            <div class="label">최종 제작</div>
            <div class="val" id="sum-card-craft">0개</div>
            <div id="sum-card-craft-sub" class="mono-num" style="font-size: var(--fs-sm); color: var(--text-muted); margin-top: 2px;">0품목 완료</div>
          </div>
        </div>

        <!-- Detail Sections -->
        <div id="sum-detail-craft" class="summary-section" style="display: none;">
          <div class="summary-section-title">
            <span class="sym-prefix">&gt;</span><span>납품/최종 아이템 제작 내역</span>
          </div>
          <div id="sum-list-craft"></div>
        </div>

        <div id="sum-detail-alter" class="summary-section" style="display: none;">
          <div class="summary-section-title">
            <span class="sym-prefix">&gt;</span><span>시설 가공 대기열 등록 내역</span>
          </div>
          <div id="sum-list-alter"></div>
        </div>

        <div id="sum-detail-gather" class="summary-section" style="display: none;">
          <div class="summary-section-title">
            <span class="sym-prefix">&gt;</span><span>원자재 필드 채집 내역</span>
          </div>
          <div id="sum-list-gather"></div>
        </div>

        <div id="sum-detail-extras" class="summary-section" style="display: none;">
          <div class="summary-section-title">
            <span class="sym-prefix">&gt;</span><span>추가 수령 및 알림 사항</span>
          </div>
          <div id="sum-list-extras" style="font-size: var(--fs-sm); line-height: 1.6; color: var(--text-muted);"></div>
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn btn-sm" onclick="copySummaryText()">텍스트 복사</button>
        <button class="btn btn-sm btn-solid-core" onclick="closeSummaryModal()">확인</button>
      </div>
    </div>
  </div>

  <!-- CLI Connection & Setup Modal -->
  <div class="modal-overlay" id="cli-modal-overlay" onclick="handleCliModalOverlayClick(event)">
    <div class="modal-dialog" style="max-width: 580px;">
      <div class="modal-header">
        <div>
          <h3 style="font-size: var(--fs-lg); font-weight: 700; color: var(--text-bright);"><span class="sym-prefix">//</span>마비노기 모바일 연동 설정 &amp; 연결 가이드</h3>
          <div style="font-size: var(--fs-sm); color: var(--text-muted); margin-top: 2px;"><span class="sym-prefix">&gt;</span>게임 클라이언트 연동 상태 및 설치 폴더 관리</div>
        </div>
        <button class="btn btn-sm" onclick="closeCliModal()">닫기</button>
      </div>
      <div class="modal-body" style="line-height: 1.6;">
        <!-- Status Box -->
        <div id="cli-modal-status-box" style="margin-bottom: 16px; padding: 12px 0; border-bottom: 1px solid var(--line);">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
            <span style="font-weight: 700; color: var(--text-bright);"><span class="sym-prefix">&gt;</span>연결 상태</span>
            <span id="cli-modal-status-badge" class="badge">연결 확인 중...</span>
          </div>
          <div style="font-size: var(--fs-sm); color: var(--text-muted); word-break: break-all; display: flex; flex-direction: column; gap: 4px;">
            <div><strong>현재 인식된 경로:</strong> <span id="cli-modal-path-text" class="mono-num" style="color: var(--text);">-</span></div>
            <div><strong>탐색 경로 출처:</strong> <span id="cli-modal-source-text" style="color: var(--text-muted);">-</span></div>
          </div>
        </div>

        <!-- Path Input Form -->
        <div style="margin-bottom: 16px;">
          <label style="display: block; font-weight: 700; color: var(--text-bright); margin-bottom: 6px; font-size: var(--fs-md);"><span class="sym-prefix">&gt;</span>마비노기 모바일 설치 폴더 (또는 CLI 경로)</label>
          <div style="display: flex; gap: 8px;">
            <input type="text" id="cli-modal-input" class="input-text" placeholder="예: D:\Nexon\MabinogiMobile 또는 C:\Nexon\MabinogiMobile" style="flex: 1;">
            <button class="btn btn-solid-core" onclick="saveCliPathFromModal()">저장 &amp; 연결</button>
          </div>
          <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 8px; flex-wrap: wrap; gap: 6px;">
            <span style="font-size: var(--fs-sm); color: var(--text-muted);">* 설치 폴더만 입력하셔도 MabinogiMobile_CLI.exe를 자동 감지합니다.</span>
            <button class="btn btn-sm" onclick="triggerCliScan()">자동 다시 검색</button>
          </div>
        </div>

        <!-- 3-Step Setup Checklist -->
        <div style="padding: 12px 0; border-top: 1px solid var(--line);">
          <div style="font-weight: 700; color: var(--text-bright); margin-bottom: 8px; font-size: var(--fs-md);">
            <span class="sym-prefix">&gt;</span>연결 필수 체크리스트 (3단계)
          </div>
          <ol style="margin: 0; padding-left: 20px; font-size: var(--fs-sm); color: var(--text); display: flex; flex-direction: column; gap: 6px;">
            <li><strong>PC 클라이언트 실행:</strong> 마비노기 모바일 PC 클라이언트를 켜고 원하는 캐릭터로 로그인합니다.</li>
            <li><strong>인게임 AI 옵션 활성화:</strong> 게임 내 [환경설정] -&gt; [기타]에서 <strong>[MM AI 에이전트 활성화]</strong>를 ON으로 켜주세요.</li>
            <li><strong>설치 드라이브 확인:</strong> 게임이 D:, E: 드라이브 등 기본 경로와 다른 곳에 설치된 경우 위의 폴더 경로를 입력해주세요.</li>
          </ol>
        </div>

        <!-- Persistent User Data Folder Section -->
        <div style="margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--line); display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap;">
          <div style="flex: 1; min-width: 240px;">
            <div style="font-weight: 700; color: var(--text-bright); font-size: var(--fs-md);">
              <span class="sym-prefix">&gt;</span>사용자 설정 &amp; 레시피 영구 저장소
            </div>
            <div style="font-size: var(--fs-sm); color: var(--text-muted); margin-top: 2px;">
              새 버전을 다운받아도 레시피·프리셋·목표·설정은 내 문서에 안전하게 자동 보존됩니다.
            </div>
            <div id="cli-modal-data-folder-path" class="mono-num" style="font-size: var(--fs-sm); color: var(--text-dim); margin-top: 2px; word-break: break-all;">
              -
            </div>
          </div>
          <div>
            <button class="btn btn-sm" onclick="openUserDataFolder()">폴더 열기</button>
          </div>
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn btn-sm btn-solid-core" onclick="closeCliModal()">닫기</button>
      </div>
    </div>
  </div>

  <!-- Mobile Connection Modal (스마트폰 접속 안내 & QR 코드) -->
  <div class="modal-overlay" id="mobile-connect-modal" onclick="handleMobileConnectModalOverlayClick(event)">
    <div class="modal-dialog" style="max-width: 480px; text-align: center;">
      <div class="modal-header">
        <div style="text-align: left;">
          <h3 style="font-size: var(--fs-lg); font-weight: 700; color: var(--text-bright); margin: 0;"><span class="sym-prefix">//</span>스마트폰 브라우저 접속 안내</h3>
          <div style="font-size: var(--fs-sm); color: var(--text-muted); margin-top: 2px;"><span class="sym-prefix">&gt;</span>동일 Wi-Fi 환경에서 모바일 실시간 모니터링</div>
        </div>
        <button class="btn btn-sm" onclick="closeMobileConnectModal()">닫기</button>
      </div>
      <div class="modal-body">
        <div style="padding: 10px 0; border-bottom: 1px solid var(--line); text-align: left; font-size: var(--fs-sm); color: var(--text); line-height: 1.6;">
          <strong><span class="sym-prefix">&gt;</span>이용 방법</strong><br>
          1. PC와 스마트폰이 <strong>동일한 Wi-Fi(공유기)</strong>에 연결되어 있어야 합니다.<br>
          2. 스마트폰 기본 카메라로 아래 <strong>QR 코드</strong>를 비추면 즉시 대시보드가 열립니다.
        </div>

        <!-- QR Code Container -->
        <div style="margin: 18px auto 14px; width: 190px; height: 190px; padding: 10px; background: #ffffff; border-radius: 0; display: flex; align-items: center; justify-content: center;">
          <img id="mobile-qr-img" src="" alt="스마트폰 접속 QR 코드" style="width: 170px; height: 170px; display: block;" onerror="this.style.display='none'; document.getElementById('mobile-qr-fallback').style.display='block';">
          <div id="mobile-qr-fallback" style="display: none; color: #000; font-size: var(--fs-sm); font-weight: 600;">QR 생성 중...</div>
        </div>

        <!-- URL Copy Box -->
        <div style="display: flex; gap: 8px; align-items: center; justify-content: center; margin-bottom: 8px;">
          <input type="text" id="mobile-connect-url-input" class="input-text mono-num" readonly style="width: 240px; text-align: center; font-weight: 700;">
          <button class="btn btn-sm" onclick="copyMobileUrl()">URL 복사</button>
        </div>
        <div id="mobile-connect-copy-msg" style="font-size: var(--fs-sm); color: var(--text-bright); height: 16px;"></div>
      </div>
      <div class="modal-footer">
        <button class="btn btn-sm btn-solid-core" onclick="closeMobileConnectModal()">닫기</button>
      </div>
    </div>
  </div>

  <!-- Recipe Manager & Edit Modal -->
  <div class="modal-overlay" id="recipe-modal-overlay" onclick="handleRecipeModalOverlayClick(event)">
    <div class="modal-dialog" style="max-width: 560px;">
      <div class="modal-header">
        <div>
          <h3 id="recipe-modal-title" style="font-size: var(--fs-lg); font-weight: 700; color: var(--text-bright); margin: 0;"><span class="sym-prefix">//</span>제작 레시피 설정 / 수정</h3>
          <div id="recipe-modal-subtitle" style="font-size: var(--fs-sm); color: var(--text-muted); margin-top: 2px;"><span class="sym-prefix">&gt;</span>아이템 1회 제작에 필요한 소요 재료를 설정합니다</div>
        </div>
        <div style="display: flex; gap: 6px; align-items: center;">
          <button class="btn btn-sm" onclick="openUserDataFolder()" title="내 문서의 레시피 파일(recipes_cache.json) 저장 폴더 열기">저장 폴더</button>
          <button class="btn btn-sm" onclick="closeRecipeModal()">닫기</button>
        </div>
      </div>
      <div class="modal-body">
        <div style="padding-bottom: 10px; margin-bottom: 12px; border-bottom: 1px solid var(--line); font-size: var(--fs-sm); color: var(--text-muted); line-height: 1.5;">
          <span class="sym-prefix">&gt;</span><strong>레시피 안내:</strong> 여기서 올바른 1회 제작 필요 재료와 수량을 등록해두면 모든 하위 재료/채집이 정확히 계산됩니다.
        </div>

        <div style="margin-bottom: 14px; display: flex; align-items: center; gap: 8px;">
          <span style="font-size: var(--fs-md); font-weight: 700; color: var(--text-bright); white-space: nowrap;">대상 아이템:</span>
          <input type="text" id="recipe-modal-item-name" list="all-items-datalist" class="input-text" style="flex: 1;" placeholder="예: 삶은 달걀" onkeydown="if(event.key==='Enter') loadRecipeForCurrentInput()">
          <button type="button" class="btn btn-sm btn-boxed" onclick="loadRecipeForCurrentInput()">조회</button>
        </div>

        <div style="margin-bottom: 10px; display: flex; justify-content: space-between; align-items: center;">
          <span style="font-size: var(--fs-md); font-weight: 700; color: var(--text-bright);"><span class="sym-prefix">&gt;</span>필요 재료 목록 (1회 제작당)</span>
          <button type="button" class="btn btn-sm btn-boxed" onclick="addRecipeModalRow('', 1)">+ 재료 추가</button>
        </div>

        <div id="recipe-modal-rows-container" style="display: flex; flex-direction: column; gap: 8px; max-height: 260px; overflow-y: auto; padding-right: 4px;">
          <!-- Dynamically generated rows -->
        </div>
      </div>
      <div class="modal-footer" style="display: flex; justify-content: space-between; align-items: center;">
        <span id="recipe-modal-msg" style="font-size: var(--fs-sm); color: var(--text-muted);"></span>
        <div style="display: flex; gap: 8px;">
          <button class="btn btn-sm" onclick="closeRecipeModal()">취소</button>
          <button class="btn btn-sm btn-solid-core" onclick="saveRecipeFromModal()">레시피 저장</button>
        </div>
      </div>
    </div>
  </div>

  <!-- Discord Webhook Modal -->
  <div class="modal-overlay" id="discord-modal-overlay" onclick="handleDiscordModalOverlayClick(event)">
    <div class="modal-dialog" style="max-width: 560px;">
      <div class="modal-header">
        <div>
          <h3 style="font-size: var(--fs-lg); font-weight: 700; color: var(--text-bright); margin: 0;"><span class="sym-prefix">//</span>디스코드 웹훅 알림 연동</h3>
          <div style="font-size: var(--fs-sm); color: var(--text-muted); margin-top: 2px;"><span class="sym-prefix">&gt;</span>목표 완수 모드 작업 완료 시 디스코드 채널로 리포트 전송</div>
        </div>
        <button class="btn btn-sm" onclick="closeDiscordModal()">닫기</button>
      </div>
      <div class="modal-body" style="line-height: 1.6;">
        <!-- Status Box -->
        <div style="margin-bottom: 16px; padding: 12px 14px; background: var(--bg-panel); border: 1px solid var(--line);">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
            <span style="font-weight: 700; color: var(--text-bright);"><span class="sym-prefix">&gt;</span>연동 상태</span>
            <span id="discord-modal-status-badge" class="badge">설정 확인 중...</span>
          </div>
          <div style="font-size: var(--fs-sm); color: var(--text-muted);">
            목표 완수 모드에서 가공, 채집, 완제품 제작 등 <strong>할 수 있는 모든 작업이 완료되었을 때</strong> 디스코드로 결과 요약 리포트를 자동으로 전송합니다.
          </div>
        </div>

        <!-- Webhook URL Input -->
        <div style="margin-bottom: 16px;">
          <label style="display: block; font-weight: 700; color: var(--text-bright); margin-bottom: 6px; font-size: var(--fs-md);">
            <span class="sym-prefix">&gt;</span>디스코드 웹훅 URL
          </label>
          <input type="text" id="discord-webhook-url-input" class="input-text mono-num" style="width: 100%; box-sizing: border-box;" placeholder="https://discord.com/api/webhooks/...">
          <div style="margin-top: 10px; padding: 10px 12px; background: rgba(255,255,255,0.03); border: 1px dashed var(--line); font-size: var(--fs-sm); border-radius: 2px;">
            <div style="font-weight: 700; color: var(--text-bright); margin-bottom: 6px;">💡 디스코드 웹훅 생성 3단계 (처음이신가요?)</div>
            <ol style="margin: 0; padding-left: 18px; color: var(--text-muted); line-height: 1.6;">
              <li>알림을 받고 싶은 <strong>디스코드 채널 우클릭</strong> &gt; <strong>[채널 편집]</strong> 선택</li>
              <li>왼쪽 메뉴의 <strong>[연동]</strong> &gt; <strong>[웹훅 만들기]</strong> (또는 [웹훅 보기]) 클릭</li>
              <li>생성된 웹훅 항목에서 <strong>[웹훅 URL 복사]</strong>를 누른 후 위 입력창에 붙여넣기!</li>
            </ol>
            <div style="margin-top: 6px; color: var(--text-dim); font-size: 11px;">
              * URL을 붙여넣고 아래 <strong>[테스트 전송]</strong>을 눌러 채널에 메시지가 잘 오는지 즉시 확인해 보세요.
            </div>
          </div>
        </div>

        <!-- Notification Toggle -->
        <div style="margin-bottom: 16px;">
          <label class="mcb-option-toggle" style="cursor: pointer; display: inline-flex; align-items: center; gap: 8px;">
            <input type="checkbox" id="discord-alarm-enabled-input" onchange="onDiscordModalToggleChange(this.checked)">
            <span class="cli-check-box" id="discord-enabled-check-sym">□</span>
            <span style="font-size: var(--fs-md); font-weight: 600; color: var(--text-bright);">목표 완수 모드 완료 시 디스코드 알림 활성화</span>
          </label>
        </div>

        <!-- Test Result Banner -->
        <div id="discord-test-result" style="display: none; padding: 10px 12px; margin-bottom: 8px; font-size: var(--fs-sm); border: 1px solid var(--line);"></div>
      </div>
      <div class="modal-footer" style="display: flex; justify-content: space-between; align-items: center;">
        <button class="btn btn-sm" id="btn-discord-test" onclick="testDiscordWebhook()">테스트 전송</button>
        <div style="display: flex; gap: 8px;">
          <button class="btn btn-sm" onclick="closeDiscordModal()">취소</button>
          <button class="btn btn-sm btn-solid-core" onclick="saveDiscordSettings()">저장</button>
        </div>
      </div>
    </div>
  </div>

  <!-- Interactive User Guide & Tutorial Modal -->
  <div class="modal-overlay" id="guide-modal-overlay" onclick="handleGuideModalOverlayClick(event)">
    <div class="modal-dialog" style="max-width: 680px; max-height: 85vh; display: flex; flex-direction: column;">
      <div class="modal-header">
        <div>
          <h3 style="font-size: var(--fs-lg); font-weight: 700; color: var(--text-bright); margin: 0;"><span class="sym-prefix">//</span>사용 가이드 &amp; 단계별 실전 튜토리얼</h3>
          <div style="font-size: var(--fs-sm); color: var(--text-muted); margin-top: 2px;"><span class="sym-prefix">&gt;</span>처음 사용자를 위한 원스톱 안내서 및 핵심 기능 활용법</div>
        </div>
        <button class="btn btn-sm" onclick="closeGuideModal()">닫기</button>
      </div>

      <!-- Guide Tabs Navigation -->
      <div style="display: flex; gap: 4px; padding: 10px 16px 0 16px; border-bottom: 1px solid var(--line); background: var(--bg-panel); overflow-x: auto;">
        <button class="btn btn-sm guide-tab-btn active" id="gtab-btn-quick" onclick="switchGuideTab('quick')">1. 시작 전 필수 설정</button>
        <button class="btn btn-sm guide-tab-btn" id="gtab-btn-tutorial" onclick="switchGuideTab('tutorial')">2. 주간 납품 18개 실전 튜토리얼</button>
        <button class="btn btn-sm guide-tab-btn" id="gtab-btn-discord" onclick="switchGuideTab('discord')">3. 디스코드 웹훅 알림</button>
        <button class="btn btn-sm guide-tab-btn" id="gtab-btn-tips" onclick="switchGuideTab('tips')">4. 7슬롯 가공 &amp; 개별 제작</button>
      </div>

      <div class="modal-body" style="line-height: 1.7; overflow-y: auto; padding: 16px 20px; font-size: var(--fs-md);">
        
        <!-- Tab 1: Quickstart -->
        <div id="gtab-pane-quick" class="guide-tab-pane">
          <div style="font-weight: 700; color: var(--text-bright); font-size: var(--fs-lg); margin-bottom: 8px;">
            <span class="sym-prefix">&gt;</span>프로그램 실행 전 꼭 확인해야 할 3가지
          </div>
          <p style="color: var(--text-muted); margin-top: 0;">
            모비노기 생활 지원도구는 마비노기 모바일 PC 클라이언트가 자체 지원하는 넥슨 공식 AI 에이전트 CLI와 연동하여 동작합니다.
          </p>

          <div style="display: flex; flex-direction: column; gap: 12px; margin-top: 14px;">
            <div style="padding: 12px 14px; background: var(--bg-panel); border: 1px solid var(--line); border-left: 3px solid var(--text-bright);">
              <div style="font-weight: 700; color: var(--text-bright); margin-bottom: 4px;">① [인게임 설정] AI 에이전트 켜기 (가장 중요)</div>
              <div style="color: var(--text-muted); font-size: var(--fs-sm);">
                게임 실행 후 <strong>[환경설정] → [기타] (또는 게임 설정)</strong>에서 <strong>[MM AI 에이전트 활성화]</strong>를 반드시 <strong>ON</strong>으로 켜주세요.<br>
                <span style="color: var(--text-dim); font-size: 11px;">* 이 옵션이 켜져 있어야 게임 클라이언트가 백그라운드 통신 포트를 열어줍니다.</span>
              </div>
            </div>

            <div style="padding: 12px 14px; background: var(--bg-panel); border: 1px solid var(--line); border-left: 3px solid var(--text-bright);">
              <div style="font-weight: 700; color: var(--text-bright); margin-bottom: 4px;">② 게임 창을 '최소화(_)'하지 마세요!</div>
              <div style="color: var(--text-muted); font-size: var(--fs-sm);">
                유니티(Unity) 엔진 특성상 작업 표시줄로 창을 최소화하면 게임 엔진의 프레임과 통신이 일시 정지됩니다.<br>
                <strong>전체 창모드로 두시거나 다른 작업 창 뒤에 겹쳐두시면</strong> 백그라운드에서도 채집과 가공이 멈춤 없이 완벽하게 진행됩니다.
              </div>
            </div>

            <div style="padding: 12px 14px; background: var(--bg-panel); border: 1px solid var(--line); border-left: 3px solid var(--text-bright);">
              <div style="font-weight: 700; color: var(--text-bright); margin-bottom: 4px;">③ 프로그램 실행 및 대시보드 자동 연결</div>
              <div style="color: var(--text-muted); font-size: var(--fs-sm);">
                압축을 푼 폴더의 <strong>'모비노기_생활_지원도구.exe'</strong>를 실행하면 브라우저 대시보드가 자동으로 열립니다.<br>
                상단 헤더에 내 캐릭터 <strong>직업과 레벨(예: 화염술사 (Lv.100))</strong> 및 <strong>보유 날개 수량</strong>이 초록 불로 정상 표시되는지 확인하세요!
              </div>
            </div>
          </div>
        </div>

        <!-- Tab 2: Tutorial (Weekly Delivery 18 items) -->
        <div id="gtab-pane-tutorial" class="guide-tab-pane" style="display: none;">
          <div style="font-weight: 700; color: var(--text-bright); font-size: var(--fs-lg); margin-bottom: 8px;">
            <span class="sym-prefix">&gt;</span>실전 튜토리얼: 주간 납품 18개 한 번에 끝내기
          </div>
          <p style="color: var(--text-muted); margin-top: 0;">
            프로그램에 기본 등록되어 있는 <strong>18개 주간 핵심 의뢰 항목</strong>을 기준으로, 채집부터 가공, 완제품 제작까지 완전 자동 완수하는 5단계 실전 가이드입니다.
          </p>

          <div style="display: flex; flex-direction: column; gap: 14px; margin-top: 14px;">
            <div style="padding: 12px 14px; background: var(--bg-panel); border: 1px solid var(--line);">
              <div style="font-weight: 700; color: var(--text-bright); margin-bottom: 4px;">1단계. 18개 기본 목표 확인하기</div>
              <div style="color: var(--text-muted); font-size: var(--fs-sm);">
                대시보드 첫 번째 탭 <strong>[주간 납품]</strong>에 로터스 힐링 완드, 론 엣지소드S, 양털, 달걀, 우유 등 18개 의뢰 목록이 기본 세팅되어 있습니다.<br>
                만약 목표 수량을 변경하고 싶다면 각 항목의 숫자를 클릭하여 손쉽게 수정할 수 있습니다.
              </div>
            </div>

            <div style="padding: 12px 14px; background: var(--bg-panel); border: 1px solid var(--line);">
              <div style="font-weight: 700; color: var(--text-bright); margin-bottom: 4px;">2단계. [인벤토리 수량 동기화] 클릭</div>
              <div style="color: var(--text-muted); font-size: var(--fs-sm);">
                상단 버튼의 <strong>[인벤토리 수량 동기화]</strong>를 누르면 캐릭터 가방뿐만 아니라 <strong>개인 창고, 계정 공용 창고</strong>에 있는 보유량까지 1초 만에 합산 계산하여 이미 가지고 있는 수량은 제외하고 <strong>'정확히 부족한 수량만'</strong> 스마트하게 산출합니다.
              </div>
            </div>

            <div style="padding: 12px 14px; background: var(--bg-panel); border: 1px solid var(--line);">
              <div style="font-weight: 700; color: var(--text-bright); margin-bottom: 4px;">3단계. [목표 완수 모드] 체크 확인</div>
              <div style="color: var(--text-muted); font-size: var(--fs-sm);">
                상단 마스터 커맨드 바의 <strong>[■ 목표 완수 모드]</strong>를 켜두시면 재료가 모자랄 때 멈추지 않고, <strong>원자재 자동 필드 채집 → 7슬롯 가공대 일괄 가공 → 완제품 제작</strong>까지 100% 끝날 때까지 알아서 연속 실행합니다.
              </div>
            </div>

            <div style="padding: 12px 14px; background: var(--bg-panel); border: 1px solid var(--line);">
              <div style="font-weight: 700; color: var(--text-bright); margin-bottom: 4px;">4단계. [주간 납품 일괄 실행] 클릭!</div>
              <div style="color: var(--text-muted); font-size: var(--fs-sm);">
                우측의 <strong>[주간 납품 일괄 실행]</strong> 버튼을 누르면 즉시 파이프라인이 가동됩니다.<br>
                하단 콘솔 창에서 캐릭터가 나무를 베고, 가공품을 수령하고, 대장간에서 장비를 제작하는 전 과정을 실시간 로그로 확인할 수 있습니다.
              </div>
            </div>

            <div style="padding: 12px 14px; background: var(--bg-panel); border: 1px solid var(--line);">
              <div style="font-weight: 700; color: var(--text-bright); margin-bottom: 4px;">5단계. 매주 목요일 초기화 시 팁!</div>
              <div style="color: var(--text-muted); font-size: var(--fs-sm);">
                새로운 주간이 시작되면 납품 탭의 <strong>[새 주간 시작 (수량 초기화)]</strong> 버튼을 한 번만 클릭하세요.<br>
                목표 목록을 다시 입력할 필요 없이 진행도가 깔끔하게 0으로 리셋되어 즉시 새로운 한 주 작업을 시작할 수 있습니다.
              </div>
            </div>
          </div>
        </div>

        <!-- Tab 3: Discord Webhook -->
        <div id="gtab-pane-discord" class="guide-tab-pane" style="display: none;">
          <div style="font-weight: 700; color: var(--text-bright); font-size: var(--fs-lg); margin-bottom: 8px;">
            <span class="sym-prefix">&gt;</span>디스코드 웹훅 알림 연동 가이드
          </div>
          <p style="color: var(--text-muted); margin-top: 0;">
            납품이나 대량 가공을 켜두고 자리를 비우거나 외출하더라도, <strong>모든 작업이 끝나면 스마트폰 디스코드로 완료 보고서(작업 시간, 날개 소모량, 완성 아이템 요약)</strong>가 즉시 도착합니다.
          </p>

          <div style="display: flex; flex-direction: column; gap: 12px; margin-top: 14px;">
            <div style="padding: 12px 14px; background: var(--bg-panel); border: 1px solid var(--line);">
              <div style="font-weight: 700; color: var(--text-bright); margin-bottom: 4px;">1. 디스코드에서 웹훅 URL 만들기 (10초 소요)</div>
              <div style="color: var(--text-muted); font-size: var(--fs-sm);">
                ① 본인의 디스코드 서버에서 알림을 받고 싶은 채널(예: #알림 또는 개인 봇 채널)을 우클릭합니다.<br>
                ② <strong>[채널 편집] → [연동] → [웹훅 만들기]</strong> 버튼을 누릅니다.<br>
                ③ 생성된 웹훅의 <strong>[웹훅 URL 복사]</strong> 버튼을 클릭합니다.
              </div>
            </div>

            <div style="padding: 12px 14px; background: var(--bg-panel); border: 1px solid var(--line);">
              <div style="font-weight: 700; color: var(--text-bright); margin-bottom: 4px;">2. 대시보드에 URL 등록 및 테스트</div>
              <div style="color: var(--text-muted); font-size: var(--fs-sm);">
                ① 대시보드 상단 상태 바의 <strong>[디스코드: 꺼짐]</strong> 또는 마스터 바의 <strong>[디스코드 알림]</strong> 버튼을 클릭합니다.<br>
                ② 복사한 URL을 입력창에 붙여넣고, <strong>[목표 완수 모드 완료 시 알림 활성화]</strong>를 체크합니다.<br>
                ③ 하단의 <strong>[테스트 전송]</strong>을 눌러 내 디스코드 채널에 '연동 성공' 메시지가 도착하는지 확인 후 <strong>[저장]</strong>을 누르면 끝!
              </div>
            </div>

            <div style="padding: 12px 14px; background: var(--bg-panel); border: 1px solid var(--line);">
              <div style="font-weight: 700; color: var(--text-bright); margin-bottom: 4px;">3. 도착하는 알림 리포트 내용 미리보기</div>
              <div style="color: var(--text-muted); font-size: var(--fs-sm);">
                • <strong>총 소요 시간:</strong> 작업 시작부터 완수까지 걸린 정확한 시간 안내<br>
                • <strong>날개 소모량:</strong> 시작 전후 날개 변화량 측정<br>
                • <strong>완성 아이템:</strong> 달성 완료된 장비/소모품 목록 및 수량<br>
                • <strong>가공/채집 실적:</strong> 몇 번의 가공과 채집을 수행했는지 요약 통계 제공
              </div>
            </div>
          </div>
        </div>

        <!-- Tab 4: 7-Slot Altering & Individual Crafting -->
        <div id="gtab-pane-tips" class="guide-tab-pane" style="display: none;">
          <div style="font-weight: 700; color: var(--text-bright); font-size: var(--fs-lg); margin-bottom: 8px;">
            <span class="sym-prefix">&gt;</span>7슬롯 가공대 &amp; 개별 제작 플래너 활용법
          </div>
          <p style="color: var(--text-muted); margin-top: 0;">
            주간 납품 외에도 특정 재료를 대량으로 가공하거나 원하는 장비를 제작할 때 유용한 고급 기능들입니다.
          </p>

          <div style="display: flex; flex-direction: column; gap: 12px; margin-top: 14px;">
            <div style="padding: 12px 14px; background: var(--bg-panel); border: 1px solid var(--line);">
              <div style="font-weight: 700; color: var(--text-bright); margin-bottom: 4px;">■ 7슬롯 빠른 가공대 탭</div>
              <div style="color: var(--text-muted); font-size: var(--fs-sm);">
                • 금속, 목재, 가죽, 옷감 등 4대 가공 시설의 <strong>남은 슬롯과 완료된 대기열을 실시간 1초 타이머로 동기화</strong>합니다.<br>
                • 완료된 가공품이 있으면 우측의 <strong>[완료 가공품 수령]</strong> 버튼으로 모든 시설의 결과물을 한 번에 일괄 수령할 수 있습니다.<br>
                • <strong>[7슬롯 빠른 가공 시작]</strong>을 누르면 슬롯 여유분에 맞춰 상위 티어 재료부터 빈틈없이 자동 등록합니다.
              </div>
            </div>

            <div style="padding: 12px 14px; background: var(--bg-panel); border: 1px solid var(--line);">
              <div style="font-weight: 700; color: var(--text-bright); margin-bottom: 4px;">■ 개별 제작 &amp; 통합 플래너 탭 (3번째 탭)</div>
              <div style="color: var(--text-muted); font-size: var(--fs-sm);">
                • 만들고 싶은 장비나 소모품을 검색하여 여러 개 등록해 두면, <strong>하위 원자재 채집량과 중간 1차 가공품 소요량을 일괄 계산(BOM)</strong>해 줍니다.<br>
                • 재료가 부족해도 알아서 벌목/양털 깎기/광석 채굴을 진행한 뒤 가공대를 거쳐 최종 완제품까지 연속 제작합니다.
              </div>
            </div>

            <div style="padding: 12px 14px; background: var(--bg-panel); border: 1px solid var(--line);">
              <div style="font-weight: 700; color: var(--text-bright); margin-bottom: 4px;">■ 스마트 채집 &amp; 단계적 자동 폴백</div>
              <div style="color: var(--text-muted); font-size: var(--fs-sm);">
                • <strong>나무 진액은 뾰족 나무</strong>, <strong>통나무는 굵은 나무</strong> 등 최적의 타겟을 자동 선별합니다.<br>
                • 캐릭터의 생활 채집 레벨이 부족하더라도 에러로 멈추지 않고 <strong>하위 자원으로 자동 폴백</strong>하여 안전하게 재료를 모읍니다.
              </div>
            </div>
          </div>
        </div>

      </div>
      <div class="modal-footer" style="display: flex; justify-content: flex-end; align-items: center; border-top: 1px solid var(--line); padding: 12px 20px;">
        <button class="btn btn-sm btn-solid-core" onclick="closeGuideModal()">확인 (가이드 닫기)</button>
      </div>
    </div>
  </div>

  <!-- Selective Quest Import Modal (#11) -->
  <div class="modal-overlay" id="quest-select-modal-overlay" onclick="if(event.target && event.target.id==='quest-select-modal-overlay') closeQuestSelectModal()">
    <div class="modal-dialog" style="max-width: 640px; max-height: 85vh; display: flex; flex-direction: column;">
      <div class="modal-header">
        <div>
          <h3 style="font-size: var(--fs-lg); font-weight: 700; color: var(--text-bright); margin: 0;"><span class="sym-prefix">//</span>인게임 퀘스트 선택 가져오기</h3>
          <div style="font-size: var(--fs-sm); color: var(--text-muted); margin-top: 2px;"><span class="sym-prefix">&gt;</span>추적 중인 퀘스트 목록을 확인하고 원하는 항목만 체크하여 등록하세요</div>
        </div>
        <button class="btn btn-sm" onclick="closeQuestSelectModal()">닫기</button>
      </div>
      <div class="modal-body" style="overflow-y: auto; padding: 14px 18px; line-height: 1.6;">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; flex-wrap: wrap; gap: 8px; padding-bottom: 8px; border-bottom: 1px solid var(--line);">
          <div style="display: flex; gap: 6px; align-items: center;">
            <button type="button" class="btn btn-sm" onclick="toggleSelectAllQuests(true)">전체 선택</button>
            <button type="button" class="btn btn-sm" onclick="toggleSelectAllQuests(false)">선택 해제</button>
            <button type="button" class="btn btn-sm" onclick="loadSelectableQuests()">다시 스캔</button>
          </div>
          <div style="display: flex; align-items: center; gap: 6px; font-size: var(--fs-sm);">
            <span style="color: var(--text-muted);">가져올 위치 일괄 설정:</span>
            <button type="button" class="btn btn-sm" onclick="setAllQuestImportModes('delivery')">주간 납품으로</button>
            <button type="button" class="btn btn-sm" onclick="setAllQuestImportModes('custom')">지정 제작으로</button>
          </div>
        </div>
        <div id="quest-select-list-container" style="display: flex; flex-direction: column; gap: 8px; min-height: 120px;">
          <div style="color: var(--text-muted); font-size: var(--fs-sm); padding: 16px 0; text-align: center;">인게임 추적 퀘스트를 스캔 중입니다...</div>
        </div>
      </div>
      <div class="modal-footer" style="display: flex; justify-content: space-between; align-items: center;">
        <span id="quest-select-summary-text" style="font-size: var(--fs-sm); color: var(--text-muted);">선택된 항목: 0건</span>
        <div style="display: flex; gap: 8px;">
          <button class="btn btn-sm" onclick="closeQuestSelectModal()">취소</button>
          <button class="btn btn-sm btn-solid-core" onclick="importSelectedQuests()">선택 항목 가져오기</button>
        </div>
      </div>
    </div>
  </div>

  <!-- AI Convenience Toolbox Modal (#2, #5, #7, #8, #9) -->
  <div class="modal-overlay" id="ai-toolbox-modal-overlay" onclick="if(event.target && event.target.id==='ai-toolbox-modal-overlay') closeAiToolboxModal()">
    <div class="modal-dialog" style="max-width: 720px; max-height: 86vh; display: flex; flex-direction: column;">
      <div class="modal-header">
        <div>
          <h3 style="font-size: var(--fs-lg); font-weight: 700; color: var(--text-bright); margin: 0;"><span class="sym-prefix">//</span>AI 커넥터 스마트 편의 도구함</h3>
          <div style="font-size: var(--fs-sm); color: var(--text-muted); margin-top: 2px;"><span class="sym-prefix">&gt;</span>창고 털이 추천 · 상비약 자동 보충 · 스마트 낚시 · 미션 &amp; 악보 연주</div>
        </div>
        <button class="btn btn-sm" onclick="closeAiToolboxModal()">닫기</button>
      </div>

      <!-- Toolbox Tabs -->
      <div style="display: flex; gap: 4px; padding: 10px 16px 0 16px; border-bottom: 1px solid var(--line); background: var(--bg-panel); overflow-x: auto;">
        <button class="btn btn-sm toolbox-tab-btn active" id="ttab-btn-zero_gather" onclick="switchAiToolboxTab('zero_gather')">1. 창고 털이 추천</button>
        <button class="btn btn-sm toolbox-tab-btn" id="ttab-btn-safety_stock" onclick="switchAiToolboxTab('safety_stock')">2. 상비약·요리 보충</button>
        <button class="btn btn-sm toolbox-tab-btn" id="ttab-btn-fishing" onclick="switchAiToolboxTab('fishing')">3. 스마트 자동 낚시</button>
        <button class="btn btn-sm toolbox-tab-btn" id="ttab-btn-social" onclick="switchAiToolboxTab('social')">4. 미션 &amp; 악보 연주</button>
      </div>

      <div class="modal-body" style="overflow-y: auto; padding: 16px 20px; line-height: 1.6;">
        <!-- Tab 1: Zero-Gather Craft Recommender (#8) -->
        <div id="ttab-pane-zero_gather" class="toolbox-tab-pane">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; flex-wrap: wrap; gap: 8px;">
            <div>
              <div style="font-weight: 700; color: var(--text-bright); font-size: var(--fs-md);"><span class="sym-prefix">&gt;</span>채집 0회! 보유 재료만으로 즉시 제작·가공 가능한 품목</div>
              <div style="font-size: var(--fs-sm); color: var(--text-muted);">현재 가방·개인창고·공용창고에 쌓여있는 재료를 분석하여 추가 채집 없이 바로 만들 수 있는 아이템을 추천합니다.</div>
            </div>
            <button class="btn btn-sm" onclick="loadZeroGatherRecommendations()">재고 다시 분석</button>
          </div>
          <div id="zero-gather-list-container" style="display: flex; flex-direction: column; gap: 8px; max-height: 420px; overflow-y: auto;">
            <div style="color: var(--text-muted); font-size: var(--fs-sm); padding: 16px 0; text-align: center;">창고 및 가방 재고를 스캔 중입니다...</div>
          </div>
        </div>

        <!-- Tab 2: Safety Stock Manager (#9) -->
        <div id="ttab-pane-safety_stock" class="toolbox-tab-pane" style="display: none;">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; flex-wrap: wrap; gap: 8px;">
            <div>
              <div style="font-weight: 700; color: var(--text-bright); font-size: var(--fs-md);"><span class="sym-prefix">&gt;</span>전투 식량 · 회복 물약 · 붕대 안전 재고 자동 보충</div>
              <div style="font-size: var(--fs-sm); color: var(--text-muted);">최소 유지 수량을 설정해 두면 현재 보유량(가방+창고)과 비교하여 부족한 수량만 지정 제작 목표에 원클릭 등록합니다.</div>
            </div>
            <button class="btn btn-sm btn-solid-core" onclick="applySafetyStocksToCustom()">부족분 일괄 제작 등록</button>
          </div>

          <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 12px; padding: 10px; background: var(--bg-panel); border: 1px solid var(--line);">
            <input type="text" id="safety-stock-item-input" list="all-items-datalist" class="input-text" placeholder="소모품/요리 이름 (예: 상급 회복 물약, 감자전)" style="flex: 2; min-width: 180px;">
            <div style="display: flex; align-items: center; gap: 4px;">
              <span style="font-size: var(--fs-sm); color: var(--text-muted);">최소 유지:</span>
              <input type="number" id="safety-stock-qty-input" class="input-number mono-num" value="10" min="1" max="999" style="width: 70px; text-align: center;">
              <span style="font-size: var(--fs-sm); color: var(--text-muted);">개</span>
            </div>
            <button class="btn btn-sm" onclick="saveSafetyStockEntry()">+ 안전 재고 등록/수정</button>
          </div>

          <div id="safety-stock-list-container" style="display: flex; flex-direction: column; gap: 8px;">
            <div style="color: var(--text-muted); font-size: var(--fs-sm); padding: 12px 0; text-align: center;">안전 재고 현황을 불러오는 중...</div>
          </div>
        </div>

        <!-- Tab 3: Smart Auto-Fishing (#5) -->
        <div id="ttab-pane-fishing" class="toolbox-tab-pane" style="display: none;">
          <div style="font-weight: 700; color: var(--text-bright); font-size: var(--fs-md); margin-bottom: 4px;">
            <span class="sym-prefix">&gt;</span>스마트 자동 낚시 매니저 (목표 마릿수 · 제한 시간 · 가방 98% 안전 중단)
          </div>
          <p style="font-size: var(--fs-sm); color: var(--text-muted); margin-top: 0; margin-bottom: 12px;">
            인게임 기본 낚시는 스스로 멈추지 않아 가방이 가득 차거나 날개가 소진될 수 있습니다. 목표 수량 또는 제한 시간에 도달하거나 가방 무게가 98%에 이르면 자동으로 낚시를 안전 종료합니다.
          </p>

          <div style="padding: 14px; background: var(--bg-panel); border: 1px solid var(--line); display: flex; flex-direction: column; gap: 12px;">
            <div style="display: flex; gap: 12px; flex-wrap: wrap; align-items: flex-end;">
              <div style="flex: 2; min-width: 180px;">
                <label style="font-size: var(--fs-sm); color: var(--text-muted); font-weight: 600; display: block; margin-bottom: 4px;">대상 물고기 / 낚시터 아이템명</label>
                <input type="text" id="fishing-item-input" class="input-text" value="고등어" placeholder="예: 고등어, 연어, 은어, 갈치" style="width: 100%;">
              </div>
              <div style="flex: 1; min-width: 100px;">
                <label style="font-size: var(--fs-sm); color: var(--text-muted); font-weight: 600; display: block; margin-bottom: 4px;">목표 획득 수량</label>
                <input type="number" id="fishing-count-input" class="input-number mono-num" value="30" min="1" max="500" style="width: 100%;">
              </div>
              <div style="flex: 1; min-width: 100px;">
                <label style="font-size: var(--fs-sm); color: var(--text-muted); font-weight: 600; display: block; margin-bottom: 4px;">최대 제한 시간(분)</label>
                <input type="number" id="fishing-minutes-input" class="input-number mono-num" value="30" min="1" max="180" style="width: 100%;">
              </div>
            </div>
            <div style="display: flex; gap: 6px; flex-wrap: wrap; align-items: center;">
              <span style="font-size: var(--fs-sm); color: var(--text-muted);">빠른 어종 선택:</span>
              <button type="button" class="btn btn-sm" onclick="document.getElementById('fishing-item-input').value='고등어'">고등어</button>
              <button type="button" class="btn btn-sm" onclick="document.getElementById('fishing-item-input').value='연어'">연어</button>
              <button type="button" class="btn btn-sm" onclick="document.getElementById('fishing-item-input').value='은어'">은어</button>
              <button type="button" class="btn btn-sm" onclick="document.getElementById('fishing-item-input').value='청어'">청어</button>
              <button type="button" class="btn btn-sm" onclick="document.getElementById('fishing-item-input').value='메기'">메기</button>
              <button type="button" class="btn btn-sm" onclick="document.getElementById('fishing-item-input').value='붕어'">붕어</button>
            </div>
            <div style="display: flex; justify-content: flex-end; gap: 8px; margin-top: 4px;">
              <button type="button" class="btn btn-sm btn-danger-abort" onclick="abortMasterPipeline()">현재 작업 중지</button>
              <button type="button" class="btn btn-solid-core" onclick="startSmartFishing()">스마트 자동 낚시 시작</button>
            </div>
          </div>
        </div>

        <!-- Tab 4: Missions & Music Score (#2, #7) -->
        <div id="ttab-pane-social" class="toolbox-tab-pane" style="display: none;">
          <div style="display: flex; flex-direction: column; gap: 14px;">
            <!-- Music & Instrument Remote -->
            <div style="padding: 12px 14px; background: var(--bg-panel); border: 1px solid var(--line);">
              <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; flex-wrap: wrap; gap: 8px;">
                <span style="font-weight: 700; color: var(--text-bright);"><span class="sym-prefix">&gt;</span>악보 연주 &amp; 악기 설정 (대기 중 자동 연주 옵션 연동)</span>
                <button class="btn btn-sm" onclick="loadMusicAndSocialData()">목록 새로고침</button>
              </div>
              <div style="display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 8px;">
                <select id="remote-music-select" class="input-text" style="flex: 2; min-width: 180px;">
                  <option value="">기본 연주 (보유 악보 자동 선택)</option>
                </select>
                <button class="btn btn-sm btn-solid-core" onclick="triggerRemoteMusicPlay()">즉시 악보 연주</button>
                <button class="btn btn-sm" onclick="savePreferredAutoMusic()">대기 연주곡으로 저장</button>
              </div>
              <div style="display: flex; gap: 8px; flex-wrap: wrap; align-items: center;">
                <select id="remote-instrument-select" class="input-text" style="flex: 2; min-width: 180px;">
                  <option value="">보유 악기 목록 확인 중...</option>
                </select>
                <button class="btn btn-sm" onclick="triggerRemoteInstrumentChange()">악기 장착 변경</button>
                <button class="btn btn-sm" onclick="triggerRemoteCharacterAction('stand_up')">일어나기(stand_up)</button>
                <button class="btn btn-sm" onclick="triggerRemoteCharacterAction('stop_action')">행동 중지</button>
              </div>
            </div>

            <!-- Daily / Weekly Missions Progress -->
            <div style="padding: 12px 14px; background: var(--bg-panel); border: 1px solid var(--line);">
              <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                <span style="font-weight: 700; color: var(--text-bright);"><span class="sym-prefix">&gt;</span>일일 / 주간 미션 진행 현황</span>
                <button class="btn btn-sm" onclick="loadMissionsData()">미션 새로고침</button>
              </div>
              <div id="missions-list-box" style="font-size: var(--fs-sm); color: var(--text-muted); max-height: 220px; overflow-y: auto;">미션 데이터를 불러오는 중...</div>
            </div>
          </div>
        </div>
      </div>

      <div class="modal-footer" style="display: flex; justify-content: flex-end;">
        <button class="btn btn-sm btn-solid-core" onclick="closeAiToolboxModal()">닫기</button>
      </div>
    </div>
  </div>


  <script>
    // Global Error Handlers (Reports browser errors to server logs)
    window.onerror = function(msg, url, lineNo, colNo, err) {
      console.error('Client Error:', msg, lineNo, colNo, err);
      try {
        fetch('/api/client_error', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ msg: String(msg), lineNo: lineNo, colNo: colNo, stack: err ? err.stack : '' })
        }).catch(function(){});
      } catch(e) {}
      return false;
    };
    window.onunhandledrejection = function(event) {
      console.error('Unhandled Promise Rejection:', event.reason);
      try {
        fetch('/api/client_error', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ msg: 'Unhandled Promise: ' + String(event.reason) })
        }).catch(function(){});
      } catch(e) {}
    };

    // Dynamic Sticky Header Height Calculator
    function updateStickyHeaderOffset() {
      const header = document.querySelector('header');
      if (header) {
        const h = header.offsetHeight;
        document.documentElement.style.setProperty('--header-height', `${h}px`);
      }
    }
    window.addEventListener('resize', updateStickyHeaderOffset);
    window.addEventListener('orientationchange', updateStickyHeaderOffset);

    // ==========================================
    // Modern CLI Text Helpers (Gauge & Spinner)
    // ==========================================
    const CLI_SPINNER_CHARS = ['/', '-', '\\', '|'];
    let cliSpinnerFrameIdx = 0;

    function getCurrentSpinnerText() {
      const ch = CLI_SPINNER_CHARS[cliSpinnerFrameIdx % CLI_SPINNER_CHARS.length];
      return `<span class="spinner-bracket">[</span><span class="spinner-char">${ch}</span><span class="spinner-bracket">]</span>`;
    }

    function renderGaugeCells(filled, len = 5) {
      let cells = '';
      for (let i = 0; i < len; i++) {
        const isFilled = i < filled;
        cells += `<span class="gauge-cell ${isFilled ? 'filled' : 'empty'}"></span>`;
      }
      return `<span class="gauge-bracket">[</span><span class="gauge-cells">${cells}</span><span class="gauge-bracket">]</span>`;
    }

    function makeTextGauge(cur, max, len = 5) {
      const c = Math.max(0, Number(cur) || 0);
      const m = Math.max(1, Number(max) || 1);
      const ratio = Math.min(1, Math.max(0, c / m));
      let filled = Math.round(ratio * len);
      if (c > 0 && filled === 0) filled = 1;
      if (c >= m) filled = len;
      else if (c < m && filled === len && len > 1) filled = len - 1;
      return renderGaugeCells(filled, len);
    }

    function getWeightColorByRatio(ratio) {
      if (ratio <= 0.6) {
        return '#E5E7EB'; // 0~60%: 여유 있을 땐 깨끗한 흰색
      }
      // 60% ~ 100%: 흰색(#E5E7EB)에서 붉은색(#EF4444)으로 점진적 보간
      const t = Math.min(1.0, (ratio - 0.6) / 0.4);
      const r = Math.round(229 + (239 - 229) * t);
      const g = Math.round(231 + (68 - 231) * t);
      const b = Math.round(235 + (68 - 235) * t);
      if (ratio > 1.0) {
        return '#EF4444'; // 100% 초과 시 선명한 적색
      }
      return `rgb(${r}, ${g}, ${b})`;
    }

    function makeWeightGaugeHtml(cur, max, len = 5) {
      const c = Math.max(0, Number(cur) || 0);
      const m = Math.max(1, Number(max) || 1);
      const ratio = c / m;
      let filled = Math.round(Math.min(1, ratio) * len);
      if (c > 0 && filled === 0) filled = 1;
      if (c >= m) filled = len;
      else if (c < m && filled === len && len > 1) filled = len - 1;

      const gaugeInner = renderGaugeCells(filled, len);
      const color = getWeightColorByRatio(ratio);
      const isOver = ratio >= 0.98;
      const glowStyle = isOver ? 'text-shadow: 0 0 6px rgba(239, 68, 68, 0.7); font-weight: 700;' : '';
      const percentStr = (ratio * 100).toFixed(1);
      const remain = Math.max(0, m - c);

      return `<span class="cli-gauge" style="color: ${color}; ${glowStyle}" title="무게 ${percentStr}% (남은 여유: ${remain})">${gaugeInner}</span>`;
    }

    setInterval(function() {
      if (document.hidden) return;
      cliSpinnerFrameIdx = (cliSpinnerFrameIdx + 1) % CLI_SPINNER_CHARS.length;
      const ch = CLI_SPINNER_CHARS[cliSpinnerFrameIdx];
      const spinners = document.querySelectorAll('.cli-spinner');
      for (let i = 0; i < spinners.length; i++) {
        const s = spinners[i];
        const charEl = s.querySelector('.spinner-char');
        if (charEl) {
          charEl.textContent = ch;
        } else {
          s.innerHTML = `<span class="spinner-bracket">[</span><span class="spinner-char">${ch}</span><span class="spinner-bracket">]</span>`;
        }
      }
    }, 250);

    // Execution Summary State & Functions
    let cachedLastSummary = null;
    let shownSummaryId = 0;
    let isInitialStatusLoaded = false;

    function openSummaryModal() {
      const modal = document.getElementById('summary-modal-overlay');
      if (modal) modal.classList.add('active');
    }

    function closeSummaryModal() {
      const modal = document.getElementById('summary-modal-overlay');
      if (modal) modal.classList.remove('active');
    }

    function handleModalOverlayClick(e) {
      if (e.target && e.target.id === 'summary-modal-overlay') {
        closeSummaryModal();
      }
    }

    // ESC key closes modals
    window.addEventListener('keydown', function(e) {
      if (e.key === 'Escape') {
        closeSummaryModal();
        if (typeof closeCliModal === 'function') closeCliModal();
        if (typeof closeRecipeModal === 'function') closeRecipeModal();
        if (typeof closeDiscordModal === 'function') closeDiscordModal();
        if (typeof closeGuideModal === 'function') closeGuideModal();
        if (typeof closeQuestSelectModal === 'function') closeQuestSelectModal();
        if (typeof closeAiToolboxModal === 'function') closeAiToolboxModal();
      }
    });

    function renderSummaryModal(sum) {
      if (!sum) return;
      cachedLastSummary = sum;

      const btnShow = document.getElementById('btn-show-summary');
      if (btnShow) btnShow.style.display = 'inline-flex';

      const titleEl = document.getElementById('modal-summary-title');
      const subtitleEl = document.getElementById('modal-summary-subtitle');
      if (titleEl) {
        let cleanTitle = sum.title || '작업 완료 리포트';
        cleanTitle = cleanTitle.replace(/\s*\(백그라운드\s*가공\s*진행\s*중\)/g, '').trim();
        if (sum.status === 'aborted') {
          titleEl.innerText = `// ${cleanTitle} --- 사용자 중지 ---`;
        } else if (sum.status === 'error') {
          titleEl.innerText = `// ${cleanTitle} --- 오류 중단 ---`;
        } else {
          titleEl.innerText = `// ${cleanTitle}`;
        }
      }
      const durText = sum.duration_text || (sum.duration_seconds ? sum.duration_seconds + '초' : '0초');
      let statText = sum.status_text || '완료';
      statText = statText.replace(/\s*\(백그라운드\s*가공\s*진행\s*중\)\s*/g, '').trim() || '완료';
      if (subtitleEl) subtitleEl.innerText = `// 소요 시간: ${durText} | 상태: ${statText}`;

      // 1. Wings
      const wings = sum.wings || {};
      const cardWings = document.getElementById('sum-card-wings');
      const cardWingsSub = document.getElementById('sum-card-wings-sub');
      if (cardWings) cardWings.innerText = `${(wings.used || 0).toLocaleString()}개 소모`;
      if (cardWingsSub) cardWingsSub.innerText = `(${wings.initial || 0} -> ${wings.final || 0}개)`;

      // 2. Gather
      const gatherItems = (sum.gather && sum.gather.items) ? sum.gather.items : (sum.gathered || []);
      const cardGather = document.getElementById('sum-card-gather');
      const cardGatherSub = document.getElementById('sum-card-gather-sub');
      let totalGatherGained = sum.total_gathered_count !== undefined ? sum.total_gathered_count : 0;
      if (totalGatherGained === 0) {
        gatherItems.forEach(it => { totalGatherGained += (it.gained || 0); });
      }
      if (cardGather) cardGather.innerText = `${gatherItems.length}개 품목`;
      if (cardGatherSub) cardGatherSub.innerText = `총 ${totalGatherGained.toLocaleString()}개 획득`;

      // 3. Alter
      const alterItems = (sum.alter && sum.alter.items) ? sum.alter.items : (sum.altered || []);
      const totalAlterSlots = (sum.alter && sum.alter.count !== undefined) ? sum.alter.count : (sum.total_altered_slots || 0);
      const cardAlter = document.getElementById('sum-card-alter');
      const cardAlterSub = document.getElementById('sum-card-alter-sub');
      if (cardAlter) cardAlter.innerText = `${totalAlterSlots}슬롯 등록`;
      if (cardAlterSub) cardAlterSub.innerText = `${alterItems.length}개 레시피`;

      // 4. Craft
      const craftItems = (sum.craft && sum.craft.items) ? sum.craft.items : (sum.crafted || []);
      const totalCraftedQty = (sum.craft && sum.craft.count !== undefined) ? sum.craft.count : (sum.total_crafted_count || 0);
      const cardCraft = document.getElementById('sum-card-craft');
      const cardCraftSub = document.getElementById('sum-card-craft-sub');
      if (cardCraft) cardCraft.innerText = `${totalCraftedQty}개 완성`;
      if (cardCraftSub) cardCraftSub.innerText = `${craftItems.length}개 품목`;

      // Craft detail list
      const secCraft = document.getElementById('sum-detail-craft');
      const listCraft = document.getElementById('sum-list-craft');
      if (secCraft && listCraft) {
        if (craftItems.length > 0) {
          secCraft.style.display = 'block';
          listCraft.innerHTML = craftItems.map(it => {
            const itemName = it.item || it.name || it.item_name || '아이템';
            const craftCount = it.count !== undefined ? it.count : (it.crafted || 0);
            return `
              <div class="summary-item-row">
                <span style="font-weight: 700; color: var(--text-main);">${itemName}</span>
                <span class="badge mono-num">${craftCount}개 제작</span>
              </div>
            `;
          }).join('');
        } else {
          secCraft.style.display = 'none';
        }
      }

      // Alter detail list
      const secAlter = document.getElementById('sum-detail-alter');
      const listAlter = document.getElementById('sum-list-alter');
      if (secAlter && listAlter) {
        if (alterItems.length > 0) {
          secAlter.style.display = 'block';
          listAlter.innerHTML = alterItems.map(it => {
            const recipeName = it.recipe || it.item || it.name || '가공';
            const facilityName = it.facility || '가공 시설';
            const slotsCount = it.slots !== undefined ? it.slots : (it.registered || 1);
            const expectedYield = it.expected_yield !== undefined ? it.expected_yield : (it.yield_est || 0);
            return `
              <div class="summary-item-row">
                <div>
                  <span style="font-weight: 700; color: var(--text-main);">${recipeName}</span>
                  <span style="font-size: var(--fs-sm); color: var(--text-muted); margin-left: 6px;">(${facilityName})</span>
                </div>
                <span class="badge-dim mono-num">${slotsCount}슬롯 (산출: +${expectedYield}개)</span>
              </div>
            `;
          }).join('');
        } else {
          secAlter.style.display = 'none';
        }
      }

      // Gather detail list
      const secGather = document.getElementById('sum-detail-gather');
      const listGather = document.getElementById('sum-list-gather');
      if (secGather && listGather) {
        if (gatherItems.length > 0) {
          secGather.style.display = 'block';
          listGather.innerHTML = gatherItems.map(it => {
            const itemName = it.name || it.item || '채집물';
            const gainedCount = it.gained || 0;
            const requiredCount = it.required || 0;
            const reqText = requiredCount > 0 ? ` (목표: ${requiredCount}개)` : '';
            return `
              <div class="summary-item-row">
                <span style="font-weight: 700; color: var(--text-main);">${itemName}</span>
                <span class="badge-dim mono-num">+${gainedCount}개 획득${reqText}</span>
              </div>
            `;
          }).join('');
        } else {
          secGather.style.display = 'none';
        }
      }

      // Extras list (pre-collected facilities & deferred works)
      const secExtras = document.getElementById('sum-detail-extras');
      const listExtras = document.getElementById('sum-list-extras');
      if (secExtras && listExtras) {
        const extraLines = [];
        const collectedList = sum.collected_facilities || sum.collected || [];
        if (collectedList.length > 0) {
          extraLines.push(`<span class="sym-prefix">&gt;</span><span><strong>사전 완료 가공품 즉시 수령</strong>: ${collectedList.join(', ')}</span>`);
        }
        const deferredList = sum.deferred_works || sum.deferred || [];
        if (deferredList.length > 0) {
          const defNames = deferredList.map(d => {
            const rName = d.recipe || d.item_name || '가공';
            const reason = d.reason || (d.deferred ? `${d.deferred}회 보류` : '보류');
            const fac = d.facility ? `[${d.facility}] ` : '';
            return `${fac}${rName} (${reason})`;
          }).join(', ');
          extraLines.push(`<span class="sym-prefix">&gt;</span><span><strong>슬롯/재료 대기로 유예된 가공</strong>: ${defNames}</span>`);
        }
        if (extraLines.length > 0) {
          secExtras.style.display = 'block';
          listExtras.innerHTML = extraLines.map(line => `<div class="summary-extra-row">${line}</div>`).join('');
        } else {
          secExtras.style.display = 'none';
        }
      }
    }

    function copySummaryText() {
      if (!cachedLastSummary) return;
      const s = cachedLastSummary;
      let text = s.summary_text;

      if (!text) {
        let cleanTitle = (s.title || '작업 진행 종합 리포트').replace(/\s*\(백그라운드\s*가공\s*진행\s*중\)/g, '').trim();
        let cleanStatus = (s.status_text || '완료').replace(/\s*\(백그라운드\s*가공\s*진행\s*중\)\s*/g, '').trim() || '완료';
        const lines = [
          '============================================================',
          `// ${cleanTitle}`,
          '============================================================',
          `// 진행 상태: ${cleanStatus}`,
          `// 소요 시간: ${s.duration_text || (s.duration_seconds ? s.duration_seconds + '초' : '0초')}`,
          `// 정령의 날개: ${(s.wings ? s.wings.used : 0)}개 소모 (${s.wings ? s.wings.initial : 0} -> ${s.wings ? s.wings.final : 0}개)`,
          ''
        ];
        const gItems = (s.gather && s.gather.items) ? s.gather.items : (s.gathered || []);
        lines.push(`=== 원자재 채집 === 총 ${gItems.length}품목`);
        gItems.forEach(it => {
          lines.push(`  - ${it.name || it.item}: +${it.gained}개 (목표: ${it.required}개)`);
        });
        lines.push('');
        const aItems = (s.alter && s.alter.items) ? s.alter.items : (s.altered || []);
        lines.push(`=== 가공 대기열 등록 === 총 ${(s.alter ? s.alter.count : s.total_altered_slots || 0)}슬롯`);
        aItems.forEach(it => {
          lines.push(`  - ${it.recipe || it.item} (${it.facility}): ${it.slots || it.registered}슬롯 (예상: +${it.expected_yield || it.yield_est}개)`);
        });
        lines.push('');
        const cItems = (s.craft && s.craft.items) ? s.craft.items : (s.crafted || []);
        lines.push(`=== 최종 제작 완료 === 총 ${(s.craft ? s.craft.count : s.total_crafted_count || 0)}개`);
        cItems.forEach(it => {
          lines.push(`  - ${it.item || it.name}: ${it.count !== undefined ? it.count : it.crafted}개`);
        });
        const coll = s.collected_facilities || s.collected;
        if (coll && coll.length > 0) {
          lines.push('');
          lines.push(`=== 사전 수령 === ${coll.join(', ')}`);
        }
        lines.push('============================================================');
        text = lines.join('\n');
      }

      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(() => {
          alert('요약 리포트가 클립보드에 복사되었습니다.');
        }).catch(() => {
          prompt('리포트 내용을 복사하세요:', text);
        });
      } else {
        prompt('리포트 내용을 복사하세요:', text);
      }
    }

    // CLI Connection & Setup Modal Logic
    function openCliModal() {
      fetchCliConfig();
      fetchUserDataInfo();
      const modal = document.getElementById('cli-modal-overlay');
      if (modal) modal.classList.add('active');
    }

    async function openUserDataFolder() {
      try {
        const res = await fetch('/api/open_data_folder', { method: 'POST' });
        const data = await res.json();
        if (data && data.path) {
          console.log('Opened data folder:', data.path);
        }
      } catch (e) {
        alert('데이터 폴더 열기 실패: ' + e);
      }
    }

    // Mobile Connection Modal & QR Code Handlers
    async function openMobileConnectModal() {
      const modal = document.getElementById('mobile-connect-modal');
      if (!modal) return;
      modal.classList.add('active');
      const input = document.getElementById('mobile-connect-url-input');
      const qrImg = document.getElementById('mobile-qr-img');
      const fallback = document.getElementById('mobile-qr-fallback');
      const copyMsg = document.getElementById('mobile-connect-copy-msg');
      if (copyMsg) copyMsg.innerText = '';
      if (fallback) fallback.style.display = 'none';

      try {
        const res = await fetch('/api/network_info');
        const data = await res.json();
        const mobileUrl = data.mobile_url || window.location.href;
        if (input) input.value = mobileUrl;
        if (qrImg) {
          qrImg.style.display = 'block';
          qrImg.src = 'https://api.qrserver.com/v1/create-qr-code/?size=180x180&data=' + encodeURIComponent(mobileUrl);
        }
      } catch (e) {
        if (input) input.value = window.location.href;
      }
    }

    function closeMobileConnectModal() {
      const modal = document.getElementById('mobile-connect-modal');
      if (modal) modal.classList.remove('active');
    }

    function handleMobileConnectModalOverlayClick(e) {
      if (e.target && e.target.id === 'mobile-connect-modal') {
        closeMobileConnectModal();
      }
    }

    function copyMobileUrl() {
      const input = document.getElementById('mobile-connect-url-input');
      const msg = document.getElementById('mobile-connect-copy-msg');
      if (!input) return;
      input.select();
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(input.value).then(() => {
            if (msg) msg.innerText = '> === 복사 완료 === 접속 URL이 클립보드에 복사되었습니다.';
            setTimeout(() => { if (msg) msg.innerText = ''; }, 3000);
          }).catch(() => {
            document.execCommand('copy');
            if (msg) msg.innerText = '> === 복사 완료 === 접속 URL이 복사되었습니다.';
          });
        } else {
          document.execCommand('copy');
          if (msg) msg.innerText = '> === 복사 완료 === 접속 URL이 복사되었습니다.';
        }
      } catch (e) {
        document.execCommand('copy');
        if (msg) msg.innerText = '> === 복사 완료 === 접속 URL이 복사되었습니다.';
      }
    }

    async function fetchUserDataInfo() {
      try {
        const res = await fetch('/api/user_data_info');
        const data = await res.json();
        const pEl = document.getElementById('cli-modal-data-folder-path');
        if (pEl && data && data.data_dir) {
          pEl.innerText = data.data_dir;
        }
      } catch (e) {
        console.error('fetchUserDataInfo error:', e);
      }
    }

    function closeCliModal() {
      const modal = document.getElementById('cli-modal-overlay');
      if (modal) modal.classList.remove('active');
    }

    function handleCliModalOverlayClick(e) {
      if (e.target && e.target.id === 'cli-modal-overlay') {
        closeCliModal();
      }
    }

    async function fetchCliConfig() {
      try {
        const res = await fetch('/api/cli_config');
        const data = await res.json();
        const pathText = document.getElementById('cli-modal-path-text');
        const sourceText = document.getElementById('cli-modal-source-text');
        const badge = document.getElementById('cli-modal-status-badge');
        const input = document.getElementById('cli-modal-input');
        const quickInput = document.getElementById('quick-cli-path-input');

        if (pathText) pathText.innerText = data.cli_path || '미발견 (경로 직접 지정 필요)';
        if (sourceText) sourceText.innerText = data.discovery_source || '-';
        if (input && !input.value) input.value = data.cli_path || data.saved_path || '';
        if (quickInput && !quickInput.value) quickInput.value = data.cli_path || data.saved_path || '';

        if (badge) {
          if (data.exists) {
            badge.className = 'badge';
            badge.innerText = 'CLI 파일 정상 확인';
          } else {
            badge.className = 'badge-dim';
            badge.innerText = 'CLI 파일 미발견';
          }
        }
      } catch (e) {
        console.error('fetchCliConfig error:', e);
      }
    }

    async function saveCliPathFromModal() {
      const input = document.getElementById('cli-modal-input');
      if (!input || !input.value.trim()) {
        alert('게임 설치 폴더 또는 CLI 경로를 입력해주세요.');
        return;
      }
      const p = input.value.trim();
      try {
        const res = await fetch('/api/set_cli_path', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ path: p })
        });
        const data = await res.json();
        if (res.ok) {
          alert('[완료] CLI 경로가 성공적으로 저장되었습니다. 즉시 재연결을 시도합니다.');
          fetchCliConfig();
          updateStatus();
        } else {
          alert('[오류] ' + (data.error || '경로가 올바르지 않습니다.'));
        }
      } catch (e) {
        alert('요청 중 오류가 발생했습니다: ' + e);
      }
    }

    async function saveQuickCliPath() {
      const input = document.getElementById('quick-cli-path-input');
      if (!input || !input.value.trim()) {
        alert('게임 설치 폴더를 입력해주세요.');
        return;
      }
      const p = input.value.trim();
      try {
        const res = await fetch('/api/set_cli_path', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ path: p })
        });
        const data = await res.json();
        if (res.ok) {
          alert('[완료] 경로가 저장되었습니다. 잠시 후 서버 상태가 갱신됩니다.');
          updateStatus();
        } else {
          alert('[오류] ' + (data.error || '경로가 올바르지 않습니다.'));
        }
      } catch (e) {
        alert('요청 중 오류가 발생했습니다: ' + e);
      }
    }

    async function triggerCliScan() {
      try {
        const res = await fetch('/api/scan_cli_path', { method: 'POST' });
        const data = await res.json();
        if (data.status === 'found') {
          alert('[완료] CLI 실행 파일을 성공적으로 찾았습니다.\n경로: ' + data.cli_path + '\n(' + data.source + ')');
          fetchCliConfig();
          updateStatus();
        } else {
          alert('[안내] 자동 검색으로 찾지 못했습니다.\n게임이 켜져 있는지 확인하거나 직접 폴더 경로를 입력해주세요.');
        }
      } catch (e) {
        alert('검색 중 오류 발생: ' + e);
      }
    }

    function safeGetStorage(key, defVal) {
      try {
        const v = localStorage.getItem(key);
        return v !== null ? v : defVal;
      } catch (e) {
        return defVal;
      }
    }

    function safeSetStorage(key, val) {
      try {
        localStorage.setItem(key, val);
      } catch (e) {}
    }

    let currentTheme = safeGetStorage('mabi_theme', 'dark');

    function applyThemeUI(theme) {
      currentTheme = theme;
      document.documentElement.setAttribute('data-theme', theme);
      safeSetStorage('mabi_theme', theme);
      const label = document.getElementById('theme-toggle-label');
      if (label) {
        label.textContent = theme === 'dark' ? '다크모드 ON' : '다크모드 OFF';
      }
      const btn = document.getElementById('btn-toggle-theme');
      if (btn) {
        btn.setAttribute('title', theme === 'dark' ? '다크모드 끄기 (눈이 편안한 웜그레이 화면으로 전환)' : '다크모드 켜기 (어두운 화면으로 전환)');
      }
    }

    async function toggleTheme() {
      const nextTheme = currentTheme === 'dark' ? 'light' : 'dark';
      applyThemeUI(nextTheme);
      try {
        await fetch('/api/settings', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ theme: nextTheme })
        });
      } catch (e) {
        console.error('테마 설정 저장 실패:', e);
      }
    }

    let currentAlterOrder = safeGetStorage('mabi_alter_order', 'high_tier');
    let currentGoalFulfillmentMode = (safeGetStorage('mabi_goal_fulfillment_mode', 'true') === 'true');
    let currentSoundAlarmEnabled = (safeGetStorage('mabi_sound_alarm_enabled', 'false') === 'true');
    let currentDiscordWebhookUrl = safeGetStorage('mabi_discord_webhook_url', '');
    let currentDiscordAlarmEnabled = (safeGetStorage('mabi_discord_alarm_enabled', 'false') === 'true');
    let currentAutoCollectEnabled = (safeGetStorage('mabi_auto_collect_enabled', 'false') === 'true');
    let currentAutoPlayMusicEnabled = (safeGetStorage('mabi_auto_play_music_enabled', 'false') === 'true');
    let currentAutoPlayMusicTitle = safeGetStorage('mabi_auto_play_music_title', '');
    let lastAutoCollectTriggerTime = 0;
    let currentIsWeightOver = false;
    let currentWeightRatio = 0;

    function checkWeightSafetyConfirm() {
      if (currentIsWeightOver) {
        const msg = currentWeightRatio >= 1.0
          ? '[!] 가방 무게 초과 (정리 필요)\n\n현재 가방 무게가 최대치(100%)를 초과한 상태입니다.\n이대로 진행하면 채집/가공 완료품을 획득하지 못할 수 있습니다.\n\n정리하지 않고 그래도 계속 실행하시겠습니까?'
          : '[!] 가방 무게 98% 도달 (작업 중지 주의)\n\n현재 가방 무게가 작업 안전 중지 기준(98%)에 도달한 상태입니다.\n이대로 진행하면 과적 방지 안전장치(98%)에 의해 채집 및 가공 수령 작업이 즉시 안전 중단될 수 있습니다.\n\n정리하지 않고 그래도 계속 실행하시겠습니까?';
        return confirm(msg);
      }
      return true;
    }
    let cachedBatchPlan = null;
    let cachedCustomPlan = null;
    let cachedQuickPlan = null;

    async function loadSettings() {
      applyThemeUI(currentTheme);
      applyAlterOrderUI();
      updateMasterCommandBar();
      try {
        const res = await fetch('/api/settings');
        const d = await res.json();
        if (d) {
          if (d.theme) {
            applyThemeUI(d.theme);
          }
          if (d.alter_order) {
            currentAlterOrder = d.alter_order;
            safeSetStorage('mabi_alter_order', currentAlterOrder);
            applyAlterOrderUI();
          }
          if (d.goal_fulfillment_mode !== undefined) {
            currentGoalFulfillmentMode = !!d.goal_fulfillment_mode;
            safeSetStorage('mabi_goal_fulfillment_mode', String(currentGoalFulfillmentMode));
          }
          if (d.sound_alarm_enabled !== undefined) {
            currentSoundAlarmEnabled = !!d.sound_alarm_enabled;
            safeSetStorage('mabi_sound_alarm_enabled', String(currentSoundAlarmEnabled));
          }
          if (d.discord_webhook_url !== undefined) {
            currentDiscordWebhookUrl = d.discord_webhook_url || '';
            safeSetStorage('mabi_discord_webhook_url', currentDiscordWebhookUrl);
          }
          if (d.discord_alarm_enabled !== undefined) {
            currentDiscordAlarmEnabled = !!d.discord_alarm_enabled;
            safeSetStorage('mabi_discord_alarm_enabled', String(currentDiscordAlarmEnabled));
          }
          if (d.auto_collect_enabled !== undefined) {
            currentAutoCollectEnabled = !!d.auto_collect_enabled;
            safeSetStorage('mabi_auto_collect_enabled', String(currentAutoCollectEnabled));
          }
          if (d.auto_play_music_enabled !== undefined) {
            currentAutoPlayMusicEnabled = !!d.auto_play_music_enabled;
            safeSetStorage('mabi_auto_play_music_enabled', String(currentAutoPlayMusicEnabled));
          }
          if (d.auto_play_music_title !== undefined) {
            currentAutoPlayMusicTitle = d.auto_play_music_title || '';
            safeSetStorage('mabi_auto_play_music_title', currentAutoPlayMusicTitle);
          }
          updateMasterCommandBar();
        }
      } catch (e) {}
    }

    function applyAlterOrderUI() {
      const isHigh = currentAlterOrder !== 'low_tier';

      const btnDH = document.getElementById('btn-delivery-alter-high');
      const btnDL = document.getElementById('btn-delivery-alter-low');
      if (btnDH && btnDL) {
        btnDH.classList.toggle('active', isHigh);
        btnDL.classList.toggle('active', !isHigh);
      }

      const btnQH = document.getElementById('btn-quick-alter-high');
      const btnQL = document.getElementById('btn-quick-alter-low');
      if (btnQH && btnQL) {
        btnQH.classList.toggle('active', isHigh);
        btnQL.classList.toggle('active', !isHigh);
      }

      const btnCH = document.getElementById('btn-custom-alter-high');
      const btnCL = document.getElementById('btn-custom-alter-low');
      if (btnCH && btnCL) {
        btnCH.classList.toggle('active', isHigh);
        btnCL.classList.toggle('active', !isHigh);
      }

      const descText = document.getElementById('quick-alter-desc-text');
      if (descText) {
        if (isHigh) {
          descText.innerHTML = `<span class="sym-prefix">&gt;</span>완료된 가공품이 있으면 일괄 수령하여 슬롯(최대 7개)을 확보하고, 부족한 원자재를 사전에 모두 채집한 뒤 <strong style="color: var(--text-main);">오래 걸리는 최고 티어 재료(T7 -> T1)부터 순서대로</strong> 1슬롯씩 대기열에 등록합니다. <span style="color: var(--text-muted);">(가공 전용 재료가 부족한 티어는 자동으로 건너뜁니다)</span>`;
        } else {
          descText.innerHTML = `<span class="sym-prefix">&gt;</span>완료된 가공품이 있으면 일괄 수령하여 슬롯(최대 7개)을 확보하고, 부족한 원자재를 사전에 모두 채집한 뒤 <strong style="color: var(--text-main);">기초 하위 티어 재료(T1 -> T7)부터 순서대로</strong> 1슬롯씩 대기열에 등록합니다. <span style="color: var(--text-muted);">(가공 전용 재료가 부족한 티어는 자동으로 건너뜁니다)</span>`;
        }
      }
    }

    async function setAlterOrder(order) {
      currentAlterOrder = order;
      safeSetStorage('mabi_alter_order', order);
      applyAlterOrderUI();
      updateMasterCommandBar();
      try {
        await fetch('/api/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ alter_order: order })
        });
      } catch (e) {}

      if (typeof loadBatchPlan === 'function') loadBatchPlan();
      if (typeof loadQuickAlterPlan === 'function') loadQuickAlterPlan();
      if (typeof loadCustomPlan === 'function') loadCustomPlan();
    }

    // ------------------------------------------
    // ETA (예상 완수 시각) 실시간 산출 함수
    // ------------------------------------------
    function getPipelineEtaInfo(mode) {
      try {
        let plan = null;
        if (mode === 'delivery') plan = cachedBatchPlan;
        else if (mode === 'custom_craft') plan = cachedCustomPlan;
        else if (mode === 'quick_alter') plan = cachedQuickPlan;

        if (!plan || plan.error) return null;
        if (plan.all_completed) return null;

        const isGoalMode = !!currentGoalFulfillmentMode;
        let totalSec = 0;

        if (mode === 'quick_alter') {
          const totalSlots = plan.total_planned_slots || 7;
          const cats = plan.categories || {};
          let maxCatSec = 0;
          Object.values(cats).forEach(c => {
            const tiers = c.planned_tiers || [];
            const sec = tiers.reduce((s, t) => {
              const tierNum = Number(t.tier) || 1;
              const dur = [0, 300, 600, 900, 1200, 1800, 2700, 3600][tierNum] || 600;
              return s + dur;
            }, 0);
            if (sec > maxCatSec) maxCatSec = sec;
          });
          if (maxCatSec === 0) maxCatSec = totalSlots * 600;

          const neededRaw = (plan.raw_materials || []).filter(r => (r.deficit || 0) > 0);
          const gatherSec = neededRaw.length * 45;

          if (isGoalMode) {
            totalSec = gatherSec + maxCatSec + 30;
          } else {
            totalSec = gatherSec + (maxCatSec > 0 ? maxCatSec : 600);
          }
        } else {
          // Delivery or Custom Craft
          const tasks = plan.tasks || plan.targets || plan.custom_tasks || [];
          if (tasks.length === 0) return null;

          const neededRaw = (plan.raw_materials || []).filter(r => (r.deficit || 0) > 0 && r.is_gatherable);
          const gatherSec = neededRaw.length * 45;

          const TIER_SECS = [0, 300, 600, 900, 1200, 1800, 2700, 3600];
          const facSums = {};
          if (typeof ALL_FACILITIES !== 'undefined' && Array.isArray(ALL_FACILITIES)) {
            ALL_FACILITIES.forEach(f => { facSums[f.name] = 0; });
          }

          if (window.cachedAlteringWorks && Array.isArray(window.cachedAlteringWorks)) {
            window.cachedAlteringWorks.forEach(w => {
              if (!w.IsCompleted && w.FacilityName && facSums[w.FacilityName] !== undefined) {
                facSums[w.FacilityName] += Number(w.RemainingSeconds) || 0;
              }
            });
          }

          const materials = plan.intermediate_requirements || plan.materials || [];
          materials.forEach(m => {
            const fac = m.facility;
            const tier = Number(m.tier) || 1;
            const dur = TIER_SECS[tier] || 600;
            const count = Math.max(0, (m.works_to_queue !== undefined ? m.works_to_queue : (m.works_needed || 0)) - (m.in_progress_count || 0));
            if (fac && facSums[fac] !== undefined) {
              facSums[fac] += count * dur;
            }
          });

          const facVals = Object.values(facSums);
          const maxFacSec = facVals.length > 0 ? Math.max(0, ...facVals) : 0;
          const craftCount = plan.craftable_tasks_count || (plan.tasks || []).length || 0;
          const craftSec = Math.min(craftCount * 12, 120);

          if (isGoalMode) {
            totalSec = gatherSec + maxFacSec + craftSec + (maxFacSec > 0 ? 30 : 10);
          } else {
            totalSec = gatherSec + (maxFacSec > 0 ? maxFacSec : craftSec + 15);
          }
        }

        if (totalSec <= 0) return null;

        const minTotal = Math.ceil(totalSec / 60);
        let durStr = '';
        if (minTotal < 60) {
          durStr = `약 ${minTotal}분`;
        } else {
          const h = Math.floor(minTotal / 60);
          const m = minTotal % 60;
          durStr = m > 0 ? `약 ${h}시간 ${m}분` : `약 ${h}시간`;
        }

        const finishDate = new Date(Date.now() + totalSec * 1000);
        let hours = finishDate.getHours();
        const mins = finishDate.getMinutes();
        const ampm = hours < 12 ? '오전' : '오후';
        const displayHours = hours % 12 === 0 ? 12 : hours % 12;
        const timeStr = `${ampm} ${displayHours}:${String(mins).padStart(2, '0')}`;

        return {
          totalSec,
          durStr,
          timeStr,
          isGoalMode,
          badgeText: `예상 완수: ${durStr} (${timeStr}경 완수 예정)`
        };
      } catch (err) {
        console.warn('getPipelineEtaInfo error:', err);
        return null;
      }
    }

    // ==========================================
    // Global Master Command Bar Controls
    // ==========================================
    function updateMasterCommandBar() {
      const badgeMode = document.getElementById('mcb-mode-badge');
      const briefing = document.getElementById('mcb-briefing-text');
      const goalToggle = document.getElementById('mcb-goal-toggle');
      const goalCheckSym = document.getElementById('mcb-goal-check-sym');
      const soundIcon = document.getElementById('mcb-sound-icon');
      const soundText = document.getElementById('mcb-sound-text');
      const orderText = document.getElementById('mcb-order-text');
      const startIcon = document.getElementById('mcb-start-icon');
      const startLabel = document.getElementById('mcb-start-label');
      const btnStart = document.getElementById('mcb-btn-start');

      if (goalToggle) goalToggle.checked = currentGoalFulfillmentMode;
      if (goalCheckSym) {
        goalCheckSym.innerText = currentGoalFulfillmentMode ? '■' : '□';
        goalCheckSym.style.color = currentGoalFulfillmentMode ? 'var(--text-bright)' : 'var(--text-dim)';
      }
      if (soundIcon) {
        soundIcon.innerText = currentSoundAlarmEnabled ? '■' : '□';
        soundIcon.style.color = currentSoundAlarmEnabled ? 'var(--text-bright)' : 'var(--text-dim)';
      }
      if (soundText) soundText.innerText = currentSoundAlarmEnabled ? '알림음 켜짐' : '알림음 꺼짐';
      const pillSound = document.getElementById('pill-sound');
      if (pillSound) {
        if (currentSoundAlarmEnabled) {
          pillSound.innerHTML = `알람 소리: <span style="color: var(--text-bright); font-weight: 700;">켜짐</span>`;
          pillSound.title = '클릭하여 작업 완료 알림음 끄기 (현재 켜짐)';
        } else {
          pillSound.innerHTML = `알람 소리: <span style="color: var(--text-dim);">꺼짐</span>`;
          pillSound.title = '클릭하여 작업 완료 알림음 켜기 (현재 꺼짐)';
        }
      }

      const discordIcon = document.getElementById('mcb-discord-icon');
      const discordText = document.getElementById('mcb-discord-text');
      const pillDiscord = document.getElementById('pill-discord');
      const isDiscordActive = currentDiscordAlarmEnabled && !!currentDiscordWebhookUrl;
      if (discordIcon) {
        discordIcon.innerText = isDiscordActive ? '■' : '□';
        discordIcon.style.color = isDiscordActive ? 'var(--text-bright)' : 'var(--text-dim)';
      }
      if (discordText) {
        if (!currentDiscordWebhookUrl) {
          discordText.innerText = '디스코드 미설정';
        } else {
          discordText.innerText = currentDiscordAlarmEnabled ? '디스코드 켜짐' : '디스코드 꺼짐';
        }
      }
      if (pillDiscord) {
        if (isDiscordActive) {
          pillDiscord.innerHTML = `디스코드: <span style="color: var(--text-bright); font-weight: 700;">켜짐</span>`;
          pillDiscord.title = '클릭하여 디스코드 웹훅 알림 설정 (현재 켜짐)';
        } else if (currentDiscordWebhookUrl) {
          pillDiscord.innerHTML = `디스코드: <span style="color: var(--text-dim);">꺼짐</span>`;
          pillDiscord.title = '클릭하여 디스코드 웹훅 알림 설정 (현재 꺼짐)';
        } else {
          pillDiscord.innerHTML = `디스코드: <span style="color: var(--text-dim);">미설정</span>`;
          pillDiscord.title = '클릭하여 디스코드 웹훅 URL 등록';
        }
      }

      if (orderText) orderText.innerText = currentAlterOrder === 'low_tier' ? '가공: 하위티어 우선' : '가공: 상위티어 우선';
      if (startIcon) startIcon.innerText = '';

      const autoColIcon = document.getElementById('mcb-auto-collect-icon');
      const autoColText = document.getElementById('mcb-auto-collect-text');
      if (autoColIcon) {
        autoColIcon.innerText = currentAutoCollectEnabled ? '■' : '□';
        autoColIcon.style.color = currentAutoCollectEnabled ? 'var(--text-bright)' : 'var(--text-dim)';
      }
      if (autoColText) autoColText.innerText = currentAutoCollectEnabled ? '자동 수령 ON' : '자동 수령';

      const autoMusIcon = document.getElementById('mcb-auto-music-icon');
      const autoMusText = document.getElementById('mcb-auto-music-text');
      if (autoMusIcon) {
        autoMusIcon.innerText = currentAutoPlayMusicEnabled ? '■' : '□';
        autoMusIcon.style.color = currentAutoPlayMusicEnabled ? 'var(--text-bright)' : 'var(--text-dim)';
      }
      if (autoMusText) autoMusText.innerText = currentAutoPlayMusicEnabled ? '대기 연주 ON' : '대기 중 연주';

      if (currentMainMode === 'delivery') {
        if (badgeMode) {
          badgeMode.className = 'badge';
          badgeMode.innerText = '주간 납품';
        }
        if (startLabel) startLabel.innerText = currentGoalFulfillmentMode ? '주간 납품 일괄 실행 (완주)' : '주간 납품 일괄 실행';

        if (briefing) {
          if (cachedBatchPlan) {
            if (cachedBatchPlan.all_completed) {
              briefing.innerHTML = '<span style="color:var(--text-main); font-weight:700;">모든 주간 납품 목표 100% 달성 완료</span>';
              if (btnStart) btnStart.disabled = true;
            } else {
              const taskCount = (cachedBatchPlan.tasks || []).length;
              const alterCount = cachedBatchPlan.total_works_to_queue || 0;
              const neededRaw = (cachedBatchPlan.raw_materials || []).filter(r => r.deficit > 0 && r.is_gatherable);
              const craftableCount = cachedBatchPlan.craftable_tasks_count || 0;
              let parts = [];
              if (taskCount > 0) parts.push(`납품 목표 <span class="mono-num">${taskCount}</span>건`);
              if (craftableCount > 0) parts.push(`<strong style="color:var(--text-main);">즉시 제작 가능 <span class="mono-num">${craftableCount}</span>건</strong>`);
              if (alterCount > 0) parts.push(`가공 등록 <span class="mono-num">${alterCount}</span>슬롯`);
              if (neededRaw.length > 0) parts.push(`채집 필요 <span class="mono-num">${neededRaw.length}</span>종`);
              if (cachedBatchPlan.estimated_wings > 0) parts.push(`예상 날개 <span class="mono-num">약 ${cachedBatchPlan.estimated_wings}개</span>`);
              briefing.innerHTML = parts.join(' | ') || '주간 납품 계획 준비 완료';
              if (cachedBatchPlan.preflight_warnings && cachedBatchPlan.preflight_warnings.length > 0) {
                briefing.innerHTML += ` <span style="color:#F59E0B; font-weight:700; margin-left:6px;" title="${cachedBatchPlan.preflight_warnings.join('\n')}">[!] 사전점검 주의 ${cachedBatchPlan.preflight_warnings.length}건</span>`;
              }
              if (btnStart) btnStart.disabled = isCurrentlyBusy || (taskCount === 0);
            }
          } else {
            briefing.innerText = '주간 납품 목표 계획 분석 대기 중...';
          }
        }
      } else if (currentMainMode === 'custom_craft') {
        if (badgeMode) {
          badgeMode.className = 'badge';
          badgeMode.innerText = '지정 제작';
        }
        if (startLabel) startLabel.innerText = currentGoalFulfillmentMode ? '지정 제작 일괄 실행 (완주)' : '지정 제작 일괄 실행';

        if (briefing) {
          if (cachedCustomPlan) {
            if (cachedCustomPlan.all_completed) {
              briefing.innerHTML = '<span style="color:var(--text-main); font-weight:700;">등록된 모든 지정 제작 목표 달성 완료</span>';
              if (btnStart) btnStart.disabled = true;
            } else {
              const targetCount = (cachedCustomPlan.tasks || cachedCustomPlan.targets || cachedCustomPlan.custom_tasks || []).length;
              const alterCount = cachedCustomPlan.total_works_to_queue || 0;
              const neededRaw = (cachedCustomPlan.raw_materials || []).filter(r => r.deficit > 0 && r.is_gatherable);
              const craftableCount = cachedCustomPlan.craftable_tasks_count || 0;
              let parts = [];
              if (targetCount > 0) parts.push(`등록 목표 <span class="mono-num">${targetCount}</span>개`);
              if (craftableCount > 0) parts.push(`<strong style="color:var(--text-main);">즉시 제작 가능 <span class="mono-num">${craftableCount}</span>건</strong>`);
              if (alterCount > 0) parts.push(`가공 등록 <span class="mono-num">${alterCount}</span>슬롯`);
              if (neededRaw.length > 0) parts.push(`채집 필요 <span class="mono-num">${neededRaw.length}</span>종`);
              if (cachedCustomPlan.estimated_wings > 0) parts.push(`예상 날개 <span class="mono-num">약 ${cachedCustomPlan.estimated_wings}개</span>`);
              briefing.innerHTML = parts.join(' | ') || '지정 제작 계획 준비 완료';
              if (cachedCustomPlan.preflight_warnings && cachedCustomPlan.preflight_warnings.length > 0) {
                briefing.innerHTML += ` <span style="color:#F59E0B; font-weight:700; margin-left:6px;" title="${cachedCustomPlan.preflight_warnings.join('\n')}">[!] 사전점검 주의 ${cachedCustomPlan.preflight_warnings.length}건</span>`;
              }
              if (btnStart) btnStart.disabled = isCurrentlyBusy || (targetCount === 0);
            }
          } else {
            briefing.innerText = '지정 제작 목표 계획 분석 대기 중...';
          }
        }
      } else if (currentMainMode === 'quick_alter') {
        if (badgeMode) {
          badgeMode.className = 'badge';
          badgeMode.innerText = '빠른 실행';
        }
        if (startLabel) startLabel.innerText = '7슬롯 빠른 가공 일괄 실행';

        if (briefing) {
          const catName = currentQuickCategory === 'all' ? '전체 가공대' : `'${currentQuickCategory}'`;
          if (cachedQuickPlan) {
            briefing.innerText = `7슬롯 최고 레벨 가공 루틴 (${catName}) | 수령 -> 원자재 채집 -> 가공 순차 등록`;
            if (btnStart) btnStart.disabled = isCurrentlyBusy || !cachedQuickPlan.can_start;
          } else {
            briefing.innerText = `7슬롯 최고 레벨 가공 루틴 (${catName}) 분석 중...`;
          }
        }
      }

      if (currentIsWeightOver && briefing) {
        const warnLabel = currentWeightRatio >= 1.0 ? '[!] 가방 무게 초과 (정리 필요)' : '[!] 가방 무게 98% 도달 (작업 중단 주의)';
        briefing.innerHTML += ` <span style="color: #EF4444; font-weight: 700; text-shadow: 0 0 6px rgba(239, 68, 68, 0.7); margin-left: 6px;">${warnLabel}</span>`;
      }

      const goalToggleLabel = document.getElementById('mcb-goal-toggle-label');
      if (goalToggleLabel) {
        goalToggleLabel.style.display = (currentMainMode === 'quick_alter') ? 'none' : 'inline-flex';
      }

      const etaRow = document.getElementById('mcb-eta-row');
      const etaBadge = document.getElementById('mcb-eta-badge');
      if (etaRow && etaBadge) {
        if (!isCurrentlyBusy && currentGoalFulfillmentMode && (currentMainMode === 'delivery' || currentMainMode === 'custom_craft')) {
          const etaInfo = getPipelineEtaInfo(currentMainMode);
          if (etaInfo) {
            etaRow.style.display = 'flex';
            etaBadge.innerText = etaInfo.badgeText;
            etaRow.title = `[목표 완수 모드] 100% 달성까지 소요 시간: ${etaInfo.durStr} (${etaInfo.timeStr} 완수 예정)`;
          } else {
            etaRow.style.display = 'none';
          }
        } else {
          etaRow.style.display = 'none';
        }
      }
    }

    async function onGoalFulfillmentToggleChanged(checked) {
      currentGoalFulfillmentMode = !!checked;
      safeSetStorage('mabi_goal_fulfillment_mode', String(currentGoalFulfillmentMode));
      updateMasterCommandBar();
      try {
        await fetch('/api/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ goal_fulfillment_mode: currentGoalFulfillmentMode })
        });
      } catch (e) {}
    }

    async function toggleSoundAlarmSetting() {
      currentSoundAlarmEnabled = !currentSoundAlarmEnabled;
      safeSetStorage('mabi_sound_alarm_enabled', String(currentSoundAlarmEnabled));
      updateMasterCommandBar();
      if (currentSoundAlarmEnabled) {
        playChime(true);
      }
      try {
        await fetch('/api/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ sound_alarm_enabled: currentSoundAlarmEnabled })
        });
      } catch (e) {}
    }

    function handleMcbDiscordClick() {
      openDiscordModal();
    }

    async function toggleDiscordAlarmSetting() {
      if (!currentDiscordWebhookUrl) {
        openDiscordModal();
        return;
      }
      currentDiscordAlarmEnabled = !currentDiscordAlarmEnabled;
      safeSetStorage('mabi_discord_alarm_enabled', String(currentDiscordAlarmEnabled));
      updateMasterCommandBar();
      try {
        await fetch('/api/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ discord_alarm_enabled: currentDiscordAlarmEnabled })
        });
      } catch (e) {}
    }

    async function toggleAutoCollectSetting() {
      currentAutoCollectEnabled = !currentAutoCollectEnabled;
      safeSetStorage('mabi_auto_collect_enabled', String(currentAutoCollectEnabled));
      updateMasterCommandBar();
      try {
        await fetch('/api/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ auto_collect_enabled: currentAutoCollectEnabled })
        });
      } catch (e) {}
    }

    async function toggleAutoPlayMusicSetting() {
      currentAutoPlayMusicEnabled = !currentAutoPlayMusicEnabled;
      safeSetStorage('mabi_auto_play_music_enabled', String(currentAutoPlayMusicEnabled));
      updateMasterCommandBar();
      try {
        await fetch('/api/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ auto_play_music_enabled: currentAutoPlayMusicEnabled })
        });
      } catch (e) {}
    }

    // ==========================================
    // Selective Quest Import Modal JS (#11)
    // ==========================================
    let cachedSelectableQuests = [];
    let defaultQuestImportMode = 'delivery';

    function openQuestSelectModal(defaultMode = 'delivery') {
      defaultQuestImportMode = defaultMode || 'delivery';
      const overlay = document.getElementById('quest-select-modal-overlay');
      if (overlay) overlay.classList.add('active');
      loadSelectableQuests();
    }

    function closeQuestSelectModal() {
      const overlay = document.getElementById('quest-select-modal-overlay');
      if (overlay) overlay.classList.remove('active');
    }

    async function loadSelectableQuests() {
      const container = document.getElementById('quest-select-list-container');
      const sumText = document.getElementById('quest-select-summary-text');
      if (!container) return;
      container.innerHTML = '<div style="color: var(--text-muted); font-size: var(--fs-sm); padding: 16px 0; text-align: center;"><span class="sym-prefix">&gt;</span>인게임 퀘스트 추적창을 스캔하고 있습니다...</div>';
      try {
        const res = await fetch('/api/selectable_quests');
        const data = await res.json();
        const quests = data.quests || [];
        cachedSelectableQuests = quests.map((q, idx) => ({
          ...q,
          _idx: idx,
          _checked: q.is_delivery_quest ? true : (defaultQuestImportMode === 'custom'),
          _targetMode: q.is_delivery_quest ? 'delivery' : defaultQuestImportMode
        }));
        renderSelectableQuestsList();
      } catch (e) {
        container.innerHTML = `<div style="color: var(--text-main); font-size: var(--fs-sm); padding: 12px;">[!] 퀘스트 스캔 실패: ${e}</div>`;
        if (sumText) sumText.innerText = '스캔 오류';
      }
    }

    function renderSelectableQuestsList() {
      const container = document.getElementById('quest-select-list-container');
      const sumText = document.getElementById('quest-select-summary-text');
      if (!container) return;
      if (cachedSelectableQuests.length === 0) {
        container.innerHTML = '<div style="color: var(--text-muted); font-size: var(--fs-sm); padding: 18px 0; text-align: center;"><span class="sym-prefix">&gt;</span>인게임 우측 퀘스트 추적창에서 납품/제작/채집 목표가 포함된 퀘스트를 찾지 못했습니다.<br>게임에서 가져올 퀘스트를 추적 활성화한 뒤 [다시 스캔]을 눌러주세요.</div>';
        if (sumText) sumText.innerText = '감지된 퀘스트 항목: 0건';
        return;
      }

      let html = '';
      cachedSelectableQuests.forEach((q, idx) => {
        const catBadge = q.is_delivery_quest ? '<span class="badge">주간/납품</span>' : `<span class="badge-dim">${q.category || '일반'}</span>`;
        const regTags = [];
        if (q.already_in_delivery) regTags.push('<span class="badge-dim" style="font-size:10px;">납품 등록됨</span>');
        if (q.already_in_custom) regTags.push('<span class="badge-dim" style="font-size:10px;">지정제작 등록됨</span>');
        const checkSym = q._checked ? '■' : '□';

        html += `
          <div style="display: flex; justify-content: space-between; align-items: center; gap: 10px; padding: 8px 10px; background: var(--bg-panel); border: 1px solid ${q._checked ? 'var(--text-bright)' : 'var(--line)'}; flex-wrap: wrap;">
            <label style="display: flex; align-items: center; gap: 8px; cursor: pointer; flex: 1; min-width: 220px;">
              <input type="checkbox" style="display:none;" ${q._checked ? 'checked' : ''} onchange="onToggleQuestSelectItem(${idx}, this.checked)">
              <span class="cli-check-box" style="color: ${q._checked ? 'var(--text-bright)' : 'var(--text-dim)'}; font-size: 15px;">${checkSym}</span>
              <div>
                <div style="display: flex; align-items: center; gap: 6px; flex-wrap: wrap;">
                  ${catBadge}
                  <strong style="color: var(--text-bright); font-size: var(--fs-md);">${q.item_name}</strong>
                  <span class="mono-num" style="color: var(--text-muted); font-size: var(--fs-sm);">(보유 ${q.current} / 목표 ${q.goal}개)</span>
                  ${regTags.join(' ')}
                </div>
                <div style="font-size: var(--fs-sm); color: var(--text-muted); margin-top: 2px;">
                  <span class="sym-prefix">&gt;</span>${q.quest_title} <span style="color: var(--text-dim);">— ${q.raw_target || ''}</span>
                </div>
              </div>
            </label>
            <div style="display: flex; align-items: center; gap: 6px;">
              <select class="input-text" style="padding: 3px 6px; font-size: var(--fs-sm);" onchange="onChangeQuestImportMode(${idx}, this.value)">
                <option value="delivery" ${q._targetMode === 'delivery' ? 'selected' : ''}>주간 납품으로</option>
                <option value="custom" ${q._targetMode === 'custom' ? 'selected' : ''}>지정 제작으로</option>
              </select>
            </div>
          </div>
        `;
      });
      container.innerHTML = html;
      const selectedCnt = cachedSelectableQuests.filter(x => x._checked).length;
      if (sumText) sumText.innerText = `선택된 항목: ${selectedCnt} / ${cachedSelectableQuests.length}건`;
    }

    function onToggleQuestSelectItem(idx, checked) {
      if (cachedSelectableQuests[idx]) {
        cachedSelectableQuests[idx]._checked = !!checked;
        renderSelectableQuestsList();
      }
    }

    function onChangeQuestImportMode(idx, mode) {
      if (cachedSelectableQuests[idx]) {
        cachedSelectableQuests[idx]._targetMode = mode;
      }
    }

    function toggleSelectAllQuests(checked) {
      cachedSelectableQuests.forEach(q => { q._checked = !!checked; });
      renderSelectableQuestsList();
    }

    function setAllQuestImportModes(mode) {
      cachedSelectableQuests.forEach(q => { q._targetMode = mode; });
      renderSelectableQuestsList();
    }

    async function importSelectedQuests() {
      const selected = cachedSelectableQuests.filter(q => q._checked);
      if (selected.length === 0) {
        alert('가져올 퀘스트 항목을 1개 이상 체크해주세요.');
        return;
      }
      const items = selected.map(q => ({
        item_name: q.item_name,
        goal: q.goal,
        current: q.current,
        quest_title: q.quest_title,
        target_mode: q._targetMode || 'delivery'
      }));
      try {
        const res = await fetch('/api/import_selected_quests', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ items })
        });
        const d = await res.json();
        if (d.error) {
          alert('가져오기 실패: ' + d.error);
        } else {
          closeQuestSelectModal();
          pollLogs();
          loadDeliveryTargets();
          loadBatchPlan();
          loadCustomTargets();
          loadCustomPlan();
          alert(`[가져오기 완료] 주간 납품 ${d.imported_delivery || 0}건, 지정 제작 ${d.imported_custom || 0}건이 등록되었습니다.`);
        }
      } catch (e) {
        alert('요청 실패: ' + e);
      }
    }

    // ==========================================
    // AI Convenience Toolbox Modal JS (#2, #5, #7, #8, #9, #13)
    // ==========================================
    function openAiToolboxModal(defaultTab = 'zero_gather') {
      const overlay = document.getElementById('ai-toolbox-modal-overlay');
      if (overlay) {
        overlay.classList.add('active');
        switchAiToolboxTab(defaultTab);
      }
    }

    function closeAiToolboxModal() {
      const overlay = document.getElementById('ai-toolbox-modal-overlay');
      if (overlay) overlay.classList.remove('active');
    }

    function switchAiToolboxTab(tabKey) {
      const tabs = ['zero_gather', 'safety_stock', 'fishing', 'social'];
      tabs.forEach(t => {
        const btn = document.getElementById(`ttab-btn-${t}`);
        const pane = document.getElementById(`ttab-pane-${t}`);
        if (btn) btn.classList.toggle('active', t === tabKey);
        if (pane) pane.style.display = (t === tabKey) ? 'block' : 'none';
      });
      if (tabKey === 'zero_gather') loadZeroGatherRecommendations();
      else if (tabKey === 'safety_stock') loadSafetyStocksStatus();
      else if (tabKey === 'social') {
        loadMusicAndSocialData();
        loadMissionsData();
      }
    }

    async function loadZeroGatherRecommendations() {
      const box = document.getElementById('zero-gather-list-container');
      if (!box) return;
      box.innerHTML = '<div style="color: var(--text-muted); font-size: var(--fs-sm); padding: 14px 0; text-align: center;"><span class="sym-prefix">&gt;</span>가방·창고 보유 재고를 스캔하여 즉시 제작 가능 품목을 계산 중입니다...</div>';
      try {
        const res = await fetch('/api/zero_gather_recommendations');
        const data = await res.json();
        const recs = data.recommendations || [];
        if (recs.length === 0) {
          box.innerHTML = '<div style="color: var(--text-muted); font-size: var(--fs-sm); padding: 14px 0; text-align: center;">현재 채집 없이 보유 재료만으로 즉시 제작 가능한 품목이 없습니다.</div>';
          return;
        }
        box.innerHTML = recs.slice(0, 35).map(r => {
          const safeName = (r.item_name || '').replace(/'/g, "\\'");
          const ingText = Object.entries(r.ingredients || {}).map(([k, v]) => `${k} ${v}개`).join(', ');
          const badge = r.type === 'craft' ? '<span class="badge">즉시 제작</span>' : `<span class="badge-dim">${r.category}</span>`;
          return `
            <div style="display: flex; justify-content: space-between; align-items: center; gap: 10px; padding: 8px 10px; background: var(--bg-panel); border: 1px solid var(--line); flex-wrap: wrap;">
              <div>
                <div style="display: flex; align-items: center; gap: 6px; flex-wrap: wrap;">
                  ${badge}
                  <strong style="color: var(--text-bright); font-size: var(--fs-md);">${r.item_name}</strong>
                  <span class="mono-num" style="color: var(--text-main); font-weight: 700;">최대 ${r.max_craftable}회 가능</span>
                  <span class="mono-num" style="color: var(--text-muted); font-size: var(--fs-sm);">(현재 보유: ${r.owned_now}개)</span>
                </div>
                <div style="font-size: var(--fs-sm); color: var(--text-muted); margin-top: 2px;">
                  <span class="sym-prefix">&gt;</span>1회 소요 재료: ${ingText}
                </div>
              </div>
              <div style="display: flex; gap: 6px;">
                <button class="btn btn-sm" onclick="quickAddCustomTarget('${safeName}', ${ (r.owned_now || 0) + 1 }); alert('\\'${safeName}\\' 1개 추가 제작이 지정 제작에 등록되었습니다.');">+1개 제작 등록</button>
                <button class="btn btn-sm btn-solid-core" onclick="quickAddCustomTarget('${safeName}', ${ (r.owned_now || 0) + r.max_craftable }); closeAiToolboxModal(); switchMainMode('custom_craft');">최대(${r.max_craftable}회) 등록</button>
              </div>
            </div>
          `;
        }).join('');
      } catch (e) {
        box.innerHTML = `<div style="color: var(--text-main); font-size: var(--fs-sm);">[!] 조회 실패: ${e}</div>`;
      }
    }

    async function loadSafetyStocksStatus() {
      const box = document.getElementById('safety-stock-list-container');
      if (!box) return;
      try {
        const res = await fetch('/api/safety_stocks');
        const data = await res.json();
        renderSafetyStocksList(data.stocks || []);
      } catch (e) {
        box.innerHTML = `<div style="color: var(--text-main); font-size: var(--fs-sm);">[!] 안전 재고 조회 실패: ${e}</div>`;
      }
    }

    function renderSafetyStocksList(stocks) {
      const box = document.getElementById('safety-stock-list-container');
      if (!box) return;
      if (!stocks || stocks.length === 0) {
        box.innerHTML = '<div style="color: var(--text-muted); font-size: var(--fs-sm); padding: 12px 0; text-align: center;">등록된 안전 재고 품목이 없습니다. 위 입력창에서 상비약이나 전투 식량을 추가하세요.</div>';
        return;
      }
      box.innerHTML = stocks.map(s => {
        const safeName = (s.item_name || '').replace(/'/g, "\\'");
        const isOk = s.is_satisfied || s.deficit === 0;
        const statusTag = isOk
          ? '<span class="badge">재고 충분</span>'
          : `<span class="badge-dim mono-num" style="color: var(--text-bright); font-weight: 700;">${s.deficit}개 부족</span>`;
        return `
          <div style="display: flex; justify-content: space-between; align-items: center; padding: 8px 10px; background: var(--bg-panel); border: 1px solid var(--line); flex-wrap: wrap; gap: 8px;">
            <div style="display: flex; align-items: center; gap: 8px;">
              ${statusTag}
              <strong style="color: var(--text-bright);">${s.item_name}</strong>
              <span class="mono-num" style="font-size: var(--fs-sm); color: var(--text-muted);">현재 보유 ${s.owned}개 / 최소 유지 ${s.min_qty}개</span>
            </div>
            <div style="display: flex; gap: 6px;">
              <button class="btn btn-sm" onclick="deleteSafetyStockEntry('${safeName}')">삭제</button>
            </div>
          </div>
        `;
      }).join('');
    }

    async function saveSafetyStockEntry() {
      const nameEl = document.getElementById('safety-stock-item-input');
      const qtyEl = document.getElementById('safety-stock-qty-input');
      if (!nameEl || !qtyEl) return;
      const item_name = nameEl.value.trim();
      const min_qty = parseInt(qtyEl.value, 10);
      if (!item_name || isNaN(min_qty) || min_qty <= 0) {
        alert('올바른 아이템명과 최소 유지 수량을 입력해주세요.');
        return;
      }
      try {
        const res = await fetch('/api/safety_stocks', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'save', item_name, min_qty })
        });
        const d = await res.json();
        if (d.error) alert('저장 실패: ' + d.error);
        else {
          nameEl.value = '';
          renderSafetyStocksList(d.stocks || []);
        }
      } catch (e) {
        alert('요청 실패: ' + e);
      }
    }

    async function deleteSafetyStockEntry(item_name) {
      try {
        const res = await fetch('/api/safety_stocks', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'delete', item_name })
        });
        const d = await res.json();
        if (!d.error) renderSafetyStocksList(d.stocks || []);
      } catch (e) {}
    }

    async function applySafetyStocksToCustom() {
      try {
        const res = await fetch('/api/safety_stocks', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'apply' })
        });
        const d = await res.json();
        if (d.error) {
          alert('보충 등록 실패: ' + d.error);
        } else {
          renderSafetyStocksList(d.stocks || []);
          const added = d.added || [];
          if (added.length > 0) {
            loadCustomTargets();
            loadCustomPlan();
            closeAiToolboxModal();
            switchMainMode('custom_craft');
            alert(`부족한 안전 재고 ${added.length}종이 [지정 제작] 목표에 자동 등록되었습니다.`);
          } else {
            alert('모든 안전 재고 품목이 이미 최소 유지 수량 이상 보유 중입니다!');
          }
        }
      } catch (e) {
        alert('요청 실패: ' + e);
      }
    }

    async function startSmartFishing() {
      if (!checkWeightSafetyConfirm()) return;
      const fishEl = document.getElementById('fishing-item-input');
      const cntEl = document.getElementById('fishing-count-input');
      const minEl = document.getElementById('fishing-minutes-input');
      const fish_name = (fishEl ? fishEl.value.trim() : '') || '고등어';
      const target_count = parseInt(cntEl ? cntEl.value : '30', 10) || 30;
      const max_minutes = parseInt(minEl ? minEl.value : '30', 10) || 30;

      if (!confirm(`[스마트 자동 낚시]\n• 대상 어종: ${fish_name}\n• 목표 수량: +${target_count}마리 획득 시 자동 종료\n• 최대 시간: ${max_minutes}분 경과 또는 가방 무게 98% 도달 시 자동 안전 종료\n\n시작하시겠습니까?`)) {
        return;
      }
      try {
        const res = await fetch('/api/execute_smart_fishing', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ fish_name, target_count, max_minutes })
        });
        const d = await res.json();
        if (d.error) {
          alert('스마트 낚시 시작 실패: ' + d.error);
        } else {
          closeAiToolboxModal();
          pollLogs();
          updateStatus();
        }
      } catch (e) {
        alert('요청 실패: ' + e);
      }
    }

    async function loadMusicAndSocialData() {
      try {
        const res = await fetch('/api/music_and_social');
        const d = await res.json();
        const musSel = document.getElementById('remote-music-select');
        if (musSel) {
          const scores = d.scores || d.music_scores || [];
          let html = '<option value="">기본 연주 (보유 악보 자동 선택)</option>';
          scores.forEach(s => {
            const title = s.DisplayTitle || s.Title || s.Name || s.DisplayName || String(s);
            const selected = (title === currentAutoPlayMusicTitle) ? 'selected' : '';
            html += `<option value="${title.replace(/"/g, '&quot;')}" ${selected}>${title}</option>`;
          });
          musSel.innerHTML = html;
        }
        const instSel = document.getElementById('remote-instrument-select');
        if (instSel) {
          const insts = d.instruments || [];
          if (insts.length === 0) {
            instSel.innerHTML = '<option value="">보유 악기 없음</option>';
          } else {
            instSel.innerHTML = insts.map(i => {
              const name = i.Name || i.DisplayName || String(i);
              return `<option value="${name.replace(/"/g, '&quot;')}">${name}</option>`;
            }).join('');
          }
        }
      } catch (e) {
        console.warn('loadMusicAndSocialData error:', e);
      }
    }

    async function loadMissionsData() {
      const box = document.getElementById('missions-list-box');
      if (!box) return;
      try {
        const res = await fetch('/api/missions');
        const d = await res.json();
        const daily = d.daily || [];
        const weekly = d.weekly || [];
        if (daily.length === 0 && weekly.length === 0) {
          box.innerHTML = '<span class="sym-prefix">&gt;</span>조회된 일일/주간 미션 정보가 없습니다.';
          return;
        }
        let html = '';
        const renderList = (arr, tag) => {
          arr.slice(0, 12).forEach(m => {
            const title = m.Title || m.Name || m.Description || '미션';
            const cur = m.CurrentCount ?? m.Progress ?? 0;
            const goal = m.TargetCount ?? m.Goal ?? 1;
            const done = m.IsCompleted || (goal > 0 && cur >= goal);
            html += `<div style="display:flex; justify-content:space-between; padding:3px 0; border-bottom:1px solid var(--line-subtle);"><span>[${tag}] ${title}</span><span class="mono-num" style="font-weight:700; color:${done ? 'var(--text-dim)' : 'var(--text-bright)'};">${done ? '완료' : `${cur}/${goal}`}</span></div>`;
          });
        };
        renderList(daily, '일일');
        renderList(weekly, '주간');
        box.innerHTML = html;
      } catch (e) {
        box.innerHTML = `[!] 미션 조회 실패: ${e}`;
      }
    }

    async function triggerRemoteMusicPlay() {
      const sel = document.getElementById('remote-music-select');
      const title = sel ? sel.value : '';
      try {
        const res = await fetch('/api/execute_social_or_music', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'play_music', title })
        });
        const d = await res.json();
        if (d.error) alert('악보 연주 실패: ' + d.error);
        else pollLogs();
      } catch (e) {
        alert('요청 실패: ' + e);
      }
    }

    async function savePreferredAutoMusic() {
      const sel = document.getElementById('remote-music-select');
      const title = sel ? sel.value : '';
      currentAutoPlayMusicTitle = title;
      safeSetStorage('mabi_auto_play_music_title', title);
      try {
        await fetch('/api/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ auto_play_music_title: title })
        });
        alert(`대기 중 자동 연주 악보가 '${title || '기본 연주'}'(으)로 저장되었습니다.`);
      } catch (e) {}
    }

    async function triggerRemoteInstrumentChange() {
      const sel = document.getElementById('remote-instrument-select');
      const name = sel ? sel.value : '';
      if (!name) return;
      try {
        const res = await fetch('/api/execute_social_or_music', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'change_instrument', name })
        });
        const d = await res.json();
        if (d.error) alert('악기 변경 실패: ' + d.error);
        else pollLogs();
      } catch (e) {}
    }

    async function triggerRemoteCharacterAction(action) {
      try {
        const res = await fetch('/api/execute_social_or_music', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action })
        });
        const d = await res.json();
        if (d.error) alert('실행 실패: ' + d.error);
        else {
          pollLogs();
          updateStatus();
        }
      } catch (e) {}
    }

    function openDiscordModal() {
      const overlay = document.getElementById('discord-modal-overlay');
      const urlInput = document.getElementById('discord-webhook-url-input');
      const enabledCheck = document.getElementById('discord-alarm-enabled-input');
      const checkSym = document.getElementById('discord-enabled-check-sym');
      const badge = document.getElementById('discord-modal-status-badge');
      const testResult = document.getElementById('discord-test-result');

      if (urlInput) urlInput.value = currentDiscordWebhookUrl || '';
      if (enabledCheck) {
        enabledCheck.checked = currentDiscordAlarmEnabled;
        if (checkSym) {
          checkSym.innerText = currentDiscordAlarmEnabled ? '■' : '□';
          checkSym.style.color = currentDiscordAlarmEnabled ? 'var(--text-bright)' : 'var(--text-dim)';
        }
      }
      if (badge) {
        if (currentDiscordAlarmEnabled && currentDiscordWebhookUrl) {
          badge.className = 'badge';
          badge.style.color = 'var(--text-bright)';
          badge.innerText = '알림 켜짐';
        } else if (currentDiscordWebhookUrl) {
          badge.className = 'badge';
          badge.style.color = 'var(--text-dim)';
          badge.innerText = '알림 꺼짐 (URL 등록됨)';
        } else {
          badge.className = 'badge';
          badge.style.color = 'var(--text-dim)';
          badge.innerText = '미등록';
        }
      }
      if (testResult) {
        testResult.style.display = 'none';
        testResult.innerText = '';
      }
      if (overlay) {
        overlay.style.display = '';
        overlay.classList.add('active');
      }
    }

    function closeDiscordModal() {
      const overlay = document.getElementById('discord-modal-overlay');
      if (overlay) {
        overlay.classList.remove('active');
      }
    }

    function handleDiscordModalOverlayClick(e) {
      if (e.target && e.target.id === 'discord-modal-overlay') {
        closeDiscordModal();
      }
    }

    function openGuideModal(defaultTab = 'quick') {
      const overlay = document.getElementById('guide-modal-overlay');
      if (overlay) {
        overlay.classList.add('active');
        switchGuideTab(defaultTab);
      }
    }

    function closeGuideModal() {
      const overlay = document.getElementById('guide-modal-overlay');
      if (overlay) {
        overlay.classList.remove('active');
      }
    }

    function handleGuideModalOverlayClick(e) {
      if (e.target && e.target.id === 'guide-modal-overlay') {
        closeGuideModal();
      }
    }

    function switchGuideTab(tabKey) {
      const tabs = ['quick', 'tutorial', 'discord', 'tips'];
      for (const t of tabs) {
        const btn = document.getElementById(`gtab-btn-${t}`);
        const pane = document.getElementById(`gtab-pane-${t}`);
        if (btn) {
          if (t === tabKey) {
            btn.classList.add('active');
            btn.style.color = 'var(--text-bright)';
            btn.style.borderColor = 'var(--text-bright)';
            btn.style.fontWeight = '700';
            btn.style.background = 'var(--bg-card)';
          } else {
            btn.classList.remove('active');
            btn.style.color = 'var(--text-muted)';
            btn.style.borderColor = 'transparent';
            btn.style.fontWeight = '500';
            btn.style.background = 'transparent';
          }
        }
        if (pane) {
          pane.style.display = (t === tabKey) ? 'block' : 'none';
        }
      }
    }

    function onDiscordModalToggleChange(checked) {
      const checkSym = document.getElementById('discord-enabled-check-sym');
      if (checkSym) {
        checkSym.innerText = checked ? '■' : '□';
        checkSym.style.color = checked ? 'var(--text-bright)' : 'var(--text-dim)';
      }
    }

    async function saveDiscordSettings() {
      const urlInput = document.getElementById('discord-webhook-url-input');
      const enabledCheck = document.getElementById('discord-alarm-enabled-input');
      const url = urlInput ? urlInput.value.trim() : '';
      const enabled = enabledCheck ? enabledCheck.checked : false;

      if (enabled && !url) {
        alert('디스코드 웹훅 알림을 켜려면 먼저 유효한 웹훅 URL을 입력해주세요.');
        return;
      }
      if (url && !url.startsWith('https://discord.com/api/webhooks/') && !url.startsWith('https://discordapp.com/api/webhooks/')) {
        if (!confirm('입력하신 URL이 일반적인 디스코드 웹훅 주소(https://discord.com/api/webhooks/...) 형식이 아닙니다.\n이대로 저장하시겠습니까?')) {
          return;
        }
      }

      currentDiscordWebhookUrl = url;
      currentDiscordAlarmEnabled = enabled;
      safeSetStorage('mabi_discord_webhook_url', url);
      safeSetStorage('mabi_discord_alarm_enabled', String(enabled));
      updateMasterCommandBar();

      try {
        await fetch('/api/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            discord_webhook_url: currentDiscordWebhookUrl,
            discord_alarm_enabled: currentDiscordAlarmEnabled
          })
        });
        closeDiscordModal();
        alert('디스코드 웹훅 설정이 저장되었습니다.');
      } catch (e) {
        alert('설정 저장 중 오류가 발생했습니다: ' + e);
      }
    }

    async function testDiscordWebhook() {
      const urlInput = document.getElementById('discord-webhook-url-input');
      const url = urlInput ? urlInput.value.trim() : currentDiscordWebhookUrl;
      const resBox = document.getElementById('discord-test-result');
      const btnTest = document.getElementById('btn-discord-test');

      if (!url) {
        alert('테스트할 디스코드 웹훅 URL을 입력해주세요.');
        return;
      }

      if (btnTest) btnTest.disabled = true;
      if (resBox) {
        resBox.style.display = 'block';
        resBox.style.color = 'var(--text-muted)';
        resBox.innerText = '디스코드 테스트 메시지 전송 중...';
      }

      try {
        const resp = await fetch('/api/discord/test', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ webhook_url: url })
        });
        const data = await resp.json();
        if (resp.ok && data.status === 'success') {
          if (resBox) {
            resBox.style.display = 'block';
            resBox.style.color = 'var(--text-bright)';
            resBox.style.borderColor = 'var(--line)';
            resBox.innerHTML = `[성공] ${data.message || '디스코드로 테스트 메시지가 정상 전송되었습니다!'}`;
          }
        } else {
          if (resBox) {
            resBox.style.display = 'block';
            resBox.style.color = '#EF4444';
            resBox.style.borderColor = '#EF4444';
            resBox.innerText = `[실패] ${data.message || '웹훅 전송에 실패했습니다.'}`;
          }
        }
      } catch (err) {
        if (resBox) {
          resBox.style.display = 'block';
          resBox.style.color = '#EF4444';
          resBox.innerText = `[오류] 전송 중 네트워크 오류: ${err.message}`;
        }
      } finally {
        if (btnTest) btnTest.disabled = false;
      }
    }

    function toggleAlterOrderSetting() {
      const next = currentAlterOrder === 'low_tier' ? 'high_tier' : 'low_tier';
      setAlterOrder(next);
      updateMasterCommandBar();
    }

    function triggerMasterStartAction() {
      if (!checkWeightSafetyConfirm()) {
        return;
      }
      if (currentMainMode === 'delivery') {
        executeBatchPipeline();
      } else if (currentMainMode === 'custom_craft') {
        executeCustomPipeline();
      } else if (currentMainMode === 'quick_alter') {
        executeQuickAlter();
      }
    }

    async function abortMasterPipeline() {
      if (!confirm('현재 진행 중인 작업(채집/가공/제작/목표 완수 루틴)을 즉시 안전하게 중지하시겠습니까?')) {
        return;
      }
      const titleEl = document.getElementById('mcb-running-title');
      const descEl = document.getElementById('mcb-running-desc');
      if (titleEl) titleEl.innerText = '--- 중지 요청 전송 중... ---';
      if (descEl) descEl.innerText = '> 진행 중인 동작을 안전하게 정리하고 중지합니다';
      try {
        await fetch('/api/abort_pipeline', { method: 'POST' });
        pollLogs();
        updateStatus();
      } catch (e) {
        alert('중지 요청 실패: ' + e);
      }
    }

    let isCurrentlyBusy = false;
    let activeAlarms = {}; // Synchronized strictly from /api/alarms

    const ALL_FACILITIES = [
      { name: '목재 가공 시설', icon: '::' },
      { name: '금속 가공 시설', icon: '::' },
      { name: '옷감 가공 시설', icon: '::' },
      { name: '가죽 가공 시설', icon: '::' },
      { name: '약품 가공 시설', icon: '::' },
      { name: '식품 가공 시설', icon: '::' }
    ];

    function formatFacilityShort(fac) {
      if (!fac) return '-';
      if (fac.includes('채집')) return '채집';
      return fac.replace(/\s*가공\s*시설|\s*시설/g, '').trim();
    }

    function toggleAlteringView() {
      const exp = document.getElementById('altering-expanded-content');
      const bar = document.getElementById('altering-collapsed-bar');
      const btn = document.getElementById('btn-toggle-altering');
      if (!exp || !bar || !btn) return;
      const isExpanded = exp.style.display !== 'none';

      if (isExpanded) {
        exp.style.display = 'none';
        bar.style.display = 'flex';
        btn.innerText = '펼치기 v';
        safeSetStorage('mabi_altering_collapsed', 'true');
      } else {
        exp.style.display = 'block';
        bar.style.display = 'none';
        btn.innerText = '접어두기 ^';
        safeSetStorage('mabi_altering_collapsed', 'false');
      }
    }

    function applyAlteringView() {
      const isCollapsed = safeGetStorage('mabi_altering_collapsed', 'false') === 'true';
      const exp = document.getElementById('altering-expanded-content');
      const bar = document.getElementById('altering-collapsed-bar');
      const btn = document.getElementById('btn-toggle-altering');
      if (!exp || !bar || !btn) return;
      if (isCollapsed) {
        exp.style.display = 'none';
        bar.style.display = 'flex';
        btn.innerText = '펼치기 v';
      } else {
        exp.style.display = 'block';
        bar.style.display = 'none';
        btn.innerText = '접어두기 ^';
      }
    }

    let currentMainMode = 'delivery';

    function switchMainMode(mode) {
      currentMainMode = mode;
      // If summary modal is somehow active, close it so it never blocks user tab view
      closeSummaryModal();

      const btnDelivery = document.getElementById('btn-mode-delivery');
      const btnQuickAlter = document.getElementById('btn-mode-quick-alter');
      const btnCustomCraft = document.getElementById('btn-mode-custom-craft');
      const contentDelivery = document.getElementById('tab-content-delivery');
      const contentQuickAlter = document.getElementById('tab-content-quick-alter');
      const contentCustomCraft = document.getElementById('tab-content-custom-craft');

      if (!btnDelivery || !btnQuickAlter || !btnCustomCraft || !contentDelivery || !contentQuickAlter || !contentCustomCraft) return;

      if (mode === 'quick_alter') {
        btnDelivery.classList.remove('active');
        btnDelivery.setAttribute('aria-selected', 'false');
        btnCustomCraft.classList.remove('active');
        btnCustomCraft.setAttribute('aria-selected', 'false');
        btnQuickAlter.classList.add('active');
        btnQuickAlter.setAttribute('aria-selected', 'true');

        contentDelivery.style.display = 'none';
        contentCustomCraft.style.display = 'none';
        contentQuickAlter.style.display = 'block';
        safeSetStorage('mabi_main_mode_tab', 'quick_alter');

        // Ensure quick alter content is expanded and visible
        const exp = document.getElementById('quick-alter-expanded-content');
        const bar = document.getElementById('quick-alter-collapsed-bar');
        const btn = document.getElementById('btn-toggle-quick-alter');
        if (exp && bar && btn && exp.style.display === 'none') {
          exp.style.display = 'block';
          bar.style.display = 'none';
          btn.innerText = '접어두기 ^';
          safeSetStorage('mabi_quick_alter_collapsed', 'false');
        }

        if (typeof loadQuickAlterPlan === 'function') {
          loadQuickAlterPlan();
        }
      } else if (mode === 'custom_craft') {
        btnDelivery.classList.remove('active');
        btnDelivery.setAttribute('aria-selected', 'false');
        btnQuickAlter.classList.remove('active');
        btnQuickAlter.setAttribute('aria-selected', 'false');
        btnCustomCraft.classList.add('active');
        btnCustomCraft.setAttribute('aria-selected', 'true');

        contentDelivery.style.display = 'none';
        contentQuickAlter.style.display = 'none';
        contentCustomCraft.style.display = 'block';
        safeSetStorage('mabi_main_mode_tab', 'custom_craft');

        if (typeof loadCustomTargets === 'function') {
          loadCustomTargets();
        }
        if (typeof loadCustomPlan === 'function') {
          loadCustomPlan();
        }
      } else {
        btnQuickAlter.classList.remove('active');
        btnQuickAlter.setAttribute('aria-selected', 'false');
        btnCustomCraft.classList.remove('active');
        btnCustomCraft.setAttribute('aria-selected', 'false');
        btnDelivery.classList.add('active');
        btnDelivery.setAttribute('aria-selected', 'true');

        contentQuickAlter.style.display = 'none';
        contentCustomCraft.style.display = 'none';
        contentDelivery.style.display = 'block';
        safeSetStorage('mabi_main_mode_tab', 'delivery');

        if (typeof loadBatchPlan === 'function') {
          loadBatchPlan();
        }
        if (typeof loadDeliveryTargets === 'function') {
          loadDeliveryTargets();
        }
      }
      updateMasterCommandBar();
    }

    function applyMainModeView() {
      const savedMode = safeGetStorage('mabi_main_mode_tab', 'delivery');
      switchMainMode(savedMode);
    }

    function toggleQuickAlterView() {
      const exp = document.getElementById('quick-alter-expanded-content');
      const bar = document.getElementById('quick-alter-collapsed-bar');
      const btn = document.getElementById('btn-toggle-quick-alter');
      if (!exp || !bar || !btn) return;
      const isExpanded = exp.style.display !== 'none';

      if (isExpanded) {
        exp.style.display = 'none';
        bar.style.display = 'flex';
        btn.innerText = '펼치기 v';
        safeSetStorage('mabi_quick_alter_collapsed', 'true');
      } else {
        exp.style.display = 'block';
        bar.style.display = 'none';
        btn.innerText = '접어두기 ^';
        safeSetStorage('mabi_quick_alter_collapsed', 'false');
      }
    }

    function applyQuickAlterView() {
      const isCollapsed = safeGetStorage('mabi_quick_alter_collapsed', 'false') === 'true';
      const exp = document.getElementById('quick-alter-expanded-content');
      const bar = document.getElementById('quick-alter-collapsed-bar');
      const btn = document.getElementById('btn-toggle-quick-alter');
      if (!exp || !bar || !btn) return;
      if (isCollapsed) {
        exp.style.display = 'none';
        bar.style.display = 'flex';
        btn.innerText = '펼치기 v';
      } else {
        exp.style.display = 'block';
        bar.style.display = 'none';
        btn.innerText = '접어두기 ^';
      }
    }

    function toggleDeliveryTargetsView() {
      const exp = document.getElementById('delivery-targets-expanded-content');
      const bar = document.getElementById('delivery-targets-collapsed-bar');
      const btn = document.getElementById('btn-toggle-delivery-targets');
      if (!exp || !bar || !btn) return;
      const isExpanded = exp.style.display !== 'none';

      if (isExpanded) {
        exp.style.display = 'none';
        bar.style.display = 'flex';
        btn.innerText = '펼치기 v';
        safeSetStorage('mabi_delivery_targets_collapsed', 'true');
      } else {
        exp.style.display = 'block';
        bar.style.display = 'none';
        btn.innerText = '접어두기 ^';
        safeSetStorage('mabi_delivery_targets_collapsed', 'false');
      }
    }

    function toggleDeliveryToolsDrawer() {
      const drawer = document.getElementById('delivery-tools-drawer');
      const btn = document.getElementById('btn-toggle-delivery-tools');
      if (!drawer || !btn) return;
      const isOpen = drawer.style.display !== 'none';
      if (isOpen) {
        drawer.style.display = 'none';
        btn.innerText = '+ 품목·프리셋 관리 v';
      } else {
        drawer.style.display = 'block';
        btn.innerText = '품목·프리셋 닫기 ^';
      }
    }

    function applyDeliveryTargetsView() {
      const isCollapsed = safeGetStorage('mabi_delivery_targets_collapsed', 'false') === 'true';
      const exp = document.getElementById('delivery-targets-expanded-content');
      const bar = document.getElementById('delivery-targets-collapsed-bar');
      const btn = document.getElementById('btn-toggle-delivery-targets');
      if (!exp || !bar || !btn) return;
      if (isCollapsed) {
        exp.style.display = 'none';
        bar.style.display = 'flex';
        btn.innerText = '펼치기 v';
      } else {
        exp.style.display = 'block';
        bar.style.display = 'none';
        btn.innerText = '접어두기 ^';
      }
    }

    function formatRemaining(sec) {
      if (sec <= 0) return '완료';
      const h = Math.floor(sec / 3600);
      const m = Math.floor((sec % 3600) / 60);
      const s = sec % 60;
      if (h > 0) return m > 0 ? `${h}시간 ${m}분` : `${h}시간`;
      if (m > 0) return s > 0 ? `${m}분 ${s}초` : `${m}분`;
      return `${s}초`;
    }

    // Melodic 2-tone Chime via Web Audio API
    function playChime(force = false) {
      if (!force && !currentSoundAlarmEnabled) return;
      try {
        const AudioCtx = window.AudioContext || window.webkitAudioContext;
        if (!AudioCtx) return;
        const ctx = new AudioCtx();
        const now = ctx.currentTime;

        const osc1 = ctx.createOscillator();
        const gain1 = ctx.createGain();
        osc1.type = 'sine';
        osc1.frequency.setValueAtTime(587.33, now); // D5
        gain1.gain.setValueAtTime(0.18, now);
        gain1.gain.exponentialRampToValueAtTime(0.001, now + 0.6);
        osc1.connect(gain1);
        gain1.connect(ctx.destination);
        osc1.start(now);
        osc1.stop(now + 0.6);

        const osc2 = ctx.createOscillator();
        const gain2 = ctx.createGain();
        osc2.type = 'sine';
        osc2.frequency.setValueAtTime(880.00, now + 0.18); // A5
        gain2.gain.setValueAtTime(0.22, now + 0.18);
        gain2.gain.exponentialRampToValueAtTime(0.001, now + 0.85);
        osc2.connect(gain2);
        gain2.connect(ctx.destination);
        osc2.start(now + 0.18);
        osc2.stop(now + 0.85);
      } catch (e) {
        console.error('AudioContext error:', e);
      }
    }

    function testAlarmSound() {
      playChime(true);
      alert('[안내] 주간 납품 알람음 테스트가 정상 작동합니다.');
    }

    function requestPushPermission() {
      if (!('Notification' in window)) {
        alert('이 브라우저는 데스크톱 알림을 지원하지 않습니다.');
        return;
      }
      Notification.requestPermission().then(permission => {
        if (permission === 'granted') {
          alert('[완료] 브라우저 푸시 알림이 허용되었습니다. 가공이 완료되면 알림창이 뜹니다.');
          new Notification('모비노기 생활 지원도구', { body: '주간 납품 가공 완료 알림이 활성화되었습니다.' });
        } else {
          alert('알림 권한이 거부되었거나 설정되지 않았습니다.');
        }
      });
    }

    function showPushNotification(title, body) {
      if ('Notification' in window && Notification.permission === 'granted') {
        new Notification(title, { body: body });
      }
    }

    // 1. Load Altering Facility Grid
    let isAlteringQueueLoading = false;
    async function loadAlteringQueue() {
      if (isAlteringQueueLoading) return;
      isAlteringQueueLoading = true;
      try {
        const res = await fetch('/api/altering');
        const data = await res.json();
        const works = data.works || [];
        const now = Date.now();

        // Assign local targetTimestamp for accurate live countdown
        works.forEach(w => {
          const sec = Number(w.RemainingSeconds) || 0;
          if (sec > 0 && !w.IsCompleted) {
            w.targetTimestamp = now + (sec * 1000);
          }
        });

        window.cachedAlteringWorks = works;
        window.cachedAlteringCompletedTotal = Number(data.completedCount) || 0;
        renderAlteringQueue();
        loadDeliveryAlarms();
        updateMasterCommandBar();

        // Option #3: Auto-collect completed altering works when idle
        // Strictly ignore partial completion: only collect facilities where ALL queued works are finished!
        const readyFacilities = getFullyCompletedFacilities(works);
        if (currentAutoCollectEnabled && !isCurrentlyBusy && !currentIsWeightOver && readyFacilities.length > 0) {
          if (Date.now() - lastAutoCollectTriggerTime > 15000) {
            lastAutoCollectTriggerTime = Date.now();
            fetch('/api/collect_all_completed_altering?mode=fully_completed', { method: 'POST' }).then(() => {
              pollLogs();
              updateStatus();
            }).catch(() => {});
          }
        }
      } catch (err) {
        console.error('Error loading altering queue:', err);
      } finally {
        isAlteringQueueLoading = false;
      }
    }

    function getWorkRemainingSeconds(w) {
      if (w.IsCompleted) return 0;
      if (w.targetTimestamp) {
        const diffMs = w.targetTimestamp - Date.now();
        return Math.max(0, Math.ceil(diffMs / 1000));
      }
      return Math.max(0, Number(w.RemainingSeconds) || 0);
    }

    // Returns list of facility names where ALL queued works are completed (0 works in progress)
    function getFullyCompletedFacilities(works) {
      if (!works || works.length === 0) return [];
      const facilityMap = {};
      ALL_FACILITIES.forEach(f => {
        facilityMap[f.name] = { completed: 0, incomplete: 0 };
      });
      works.forEach(w => {
        const fac = w.FacilityName || (typeof getFacilityForMaterial === 'function' ? getFacilityForMaterial(w.DisplayName) : '') || '';
        if (!facilityMap[fac]) {
          facilityMap[fac] = { completed: 0, incomplete: 0 };
        }
        const isComp = w.IsCompleted || getWorkRemainingSeconds(w) === 0;
        if (isComp) {
          facilityMap[fac].completed += 1;
        } else {
          facilityMap[fac].incomplete += 1;
        }
      });
      const readyFacs = [];
      Object.keys(facilityMap).forEach(fac => {
        const info = facilityMap[fac];
        if (info.completed > 0 && info.incomplete === 0) {
          readyFacs.push(fac);
        }
      });
      return readyFacs;
    }

    function renderAlteringQueue() {
      const works = window.cachedAlteringWorks || [];
      const completedTotal = window.cachedAlteringCompletedTotal || 0;

      const grouped = {};
      ALL_FACILITIES.forEach(f => { grouped[f.name] = []; });
      works.forEach(w => {
        const fn = w.FacilityName || '';
        if (!grouped[fn]) grouped[fn] = [];
        grouped[fn].push(w);
      });

      // Summary Pill & Quick Bar
      const inProgressTotal = works.filter(w => !w.IsCompleted && getWorkRemainingSeconds(w) > 0).length;
      const sumPill = document.getElementById('altering-summary-pill');
      if (sumPill) {
        sumPill.innerHTML = `진행 <span class="mono-num">${inProgressTotal}</span>건 | 완료 <span class="mono-num">${completedTotal}</span>건`;
      }
      
      let quickSummaryParts = [];
      ALL_FACILITIES.forEach(f => {
        const list = grouped[f.name] || [];
        if (list.length > 0) {
          const incomplete = list.filter(w => !w.IsCompleted && getWorkRemainingSeconds(w) > 0);
          const totalSec = incomplete.reduce((sum, w) => sum + getWorkRemainingSeconds(w), 0);
          const hasComp = list.some(w => w.IsCompleted || (getWorkRemainingSeconds(w) === 0 && !w.IsCompleted));
          const facShort = f.name.replace(' 가공 시설', '');

          if (totalSec > 0) {
            const compTag = hasComp ? ' <span style="color:var(--text-main);font-size:var(--fs-sm);">(일부완료)</span>' : '';
            quickSummaryParts.push(`${facShort}: <span class="mono-num" style="color:var(--text-main);font-weight:700;">${formatRemaining(totalSec)}</span>${compTag}`);
          } else if (hasComp) {
            quickSummaryParts.push(`${facShort}: <span style="color:var(--text-main);font-weight:700;">=== 완료 ===</span>`);
          }
        }
      });
      const quickSummaryEl = document.getElementById('altering-quick-summary');
      if (quickSummaryEl) {
        quickSummaryEl.innerHTML = quickSummaryParts.length > 0 
          ? quickSummaryParts.join(' &nbsp;|&nbsp; ') 
          : '현재 대기 중인 가공 작업이 없습니다.';
      }

      // Render 3x2 Grid
      const gridEl = document.getElementById('facility-grid-container');
      if (!gridEl) return;
      let gridHtml = '';
      ALL_FACILITIES.forEach(fac => {
        const itemsAtFac = grouped[fac.name] || [];
        const completedAtFac = itemsAtFac.filter(x => x.IsCompleted || getWorkRemainingSeconds(x) === 0);
        const incompleteAtFac = itemsAtFac.filter(x => !x.IsCompleted && getWorkRemainingSeconds(x) > 0);
        const hasCompleted = completedAtFac.length > 0;
        const firstCompletedName = hasCompleted ? completedAtFac[0].DisplayName : '';

        // Facility-wide alarm button (only for facilities that have works queued)
        let alarmBtn = '';
        if (itemsAtFac.length > 0 && incompleteAtFac.length > 0) {
          const hasAlarm = !!activeAlarms[fac.name];
          if (hasAlarm) {
            alarmBtn = `<button class="btn-facility-alarm active" onclick="toggleFacilityAlarm('${fac.name}')" title="[${fac.name}] 모든 작업 완료 시 알람 울림 (클릭하여 알람 해제)">🔔 알람ON</button>`;
          } else {
            alarmBtn = `<button class="btn-facility-alarm" onclick="toggleFacilityAlarm('${fac.name}')" title="[${fac.name}] 걸어둔 모든 작업 완료 시 알람 울림">+시설알람</button>`;
          }
        }

        // Collect button: full completion vs partial completion
        let collectBtn = '';
        if (hasCompleted) {
          if (incompleteAtFac.length === 0) {
            collectBtn = `<button class="btn-facility-collect" onclick="collectAltering('${firstCompletedName}')">전체 완료 수령 (<span class="mono-num">${completedAtFac.length}</span>)</button>`;
          } else {
            collectBtn = `<button class="btn-facility-collect-partial" onclick="collectAltering('${firstCompletedName}')" title="일부 슬롯 완료 (아직 ${incompleteAtFac.length}개 슬롯 진행 중)">부분 수령 (<span class="mono-num">${completedAtFac.length}</span>)</button>`;
          }
        } else {
          collectBtn = `<button class="btn-facility-idle" disabled>수령 대기</button>`;
        }

        let itemsHtml = '';
        if (itemsAtFac.length === 0) {
          itemsHtml = '<div style="color: var(--text-dim); font-size: var(--fs-sm); padding: 8px 0; text-align: center;">작업 없음</div>';
        } else {
          itemsAtFac.forEach((w, wIdx) => {
            const remSec = getWorkRemainingSeconds(w);
            let timeStr = '';
            if (w.IsCompleted || remSec === 0) {
              timeStr = '<span class="badge mono-num">완료</span>';
            } else if (w.State === 'InProgress' || remSec > 0) {
              timeStr = `<span class="mono-num work-countdown" data-fac="${fac.name}" data-widx="${wIdx}" data-state="${w.State || ''}" style="color: var(--text-main); font-weight: 700;">${formatRemaining(remSec)}</span>`;
            } else {
              timeStr = `<span class="mono-num work-countdown" data-fac="${fac.name}" data-widx="${wIdx}" data-state="${w.State || ''}" style="color: var(--text-muted); font-size: var(--fs-sm);">대기 (${formatRemaining(remSec)})</span>`;
            }

            itemsHtml += `
              <div class="facility-item-row">
                <div style="display:flex; align-items:center;">
                  <span class="item-name-text" title="${w.DisplayName}">${w.DisplayName}</span>
                </div>
                <span>${timeStr}</span>
              </div>
            `;
          });
        }

        gridHtml += `
          <div class="facility-card ${itemsAtFac.length === 0 ? 'facility-idle' : ''}">
            <div class="facility-header">
              <span class="facility-title">${formatFacilityShort(fac.name)} <span class="badge-dim mono-num" style="font-size:11px; font-weight:500;">${itemsAtFac.length}/7슬롯</span></span>
              <div style="display:flex; gap:4px; align-items:center;">
                ${alarmBtn}
                ${collectBtn}
              </div>
            </div>
            <div class="facility-items">
              ${itemsHtml}
            </div>
          </div>
        `;
      });
      gridEl.innerHTML = gridHtml;
    }

    // 2. Load & Render Delivery-Exclusive Alarms
    async function loadDeliveryAlarms() {
      try {
        const res = await fetch('/api/alarms');
        const data = await res.json();
        const serverAlarms = data.alarms || [];
        
        serverAlarms.forEach(al => {
          if (!activeAlarms[al.key]) {
            activeAlarms[al.key] = al;
          } else {
            activeAlarms[al.key].targetTimestamp = al.targetTimestamp;
            activeAlarms[al.key].source = al.source;
          }
        });
        const serverKeys = new Set(serverAlarms.map(a => a.key));
        Object.keys(activeAlarms).forEach(k => {
          if (!serverKeys.has(k)) delete activeAlarms[k];
        });

        renderAlarms();
      } catch (e) {
        console.error('Error loading alarms:', e);
      }
    }

    function renderAlarms() {
      const container = document.getElementById('alarm-list-container');
      const countBadge = document.getElementById('alarm-count-badge');
      if (!container || !countBadge) return;
      const keys = Object.keys(activeAlarms);
      const activeCount = keys.filter(k => !activeAlarms[k].notified).length;
      countBadge.innerText = `${activeCount}개 활성`;

      if (keys.length === 0) {
        container.innerHTML = '<p style="color: var(--text-muted); font-size: var(--fs-sm);"><span class="sym-prefix">&gt;</span>현재 등록된 주간 납품 알람이 없습니다. (납품 파이프라인 가동 시 자동 등록됩니다)</p>';
        return;
      }

      let html = '';
      keys.forEach(k => {
        const al = activeAlarms[k];
        const remMs = al.targetTimestamp - Date.now();
        const remSec = Math.max(0, Math.ceil(remMs / 1000));
        const statusHtml = al.notified 
          ? '<span style="color:var(--text-main); font-weight:700;">=== 전체 가공 완료 :: 수령 가능 ===</span>'
          : `<span class="mono-num" style="color:var(--text-main); font-weight:700;"><span class="cli-spinner">${getCurrentSpinnerText()}</span> ${formatRemaining(remSec)} 남음 <small style="color:var(--text-muted); font-size:var(--fs-sm);">(마지막 가공 완료)</small></span>`;

        const badgeClass = al.isDeliveryTarget ? 'badge' : 'badge-dim';

        html += `
          <div class="alarm-item" style="display:flex; justify-content:space-between; align-items:center; padding:6px 10px; border-bottom:1px solid var(--border-line);">
            <div style="display:flex; align-items:center; gap:8px;">
              <span class="${badgeClass}">${formatFacilityShort(al.facility)}</span>
              <span style="font-weight:700; color:var(--text-main); font-size:var(--fs-md);">${al.item}</span>
            </div>
            <div style="display:flex; align-items:center; gap:10px;">
              ${statusHtml}
              <button class="btn btn-sm" onclick="deleteAlarmServer('${k}')" title="알람 삭제">삭제</button>
            </div>
          </div>
        `;
      });
      container.innerHTML = html;
    }

    async function toggleFacilityAlarm(facility) {
      if (activeAlarms[facility]) {
        await deleteAlarmServer(facility);
      } else {
        await addManualAlarm(facility);
      }
    }

    async function addManualAlarm(facility, item_name, remaining_seconds) {
      try {
        await fetch('/api/add_manual_alarm', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ facility, item_name, remaining_seconds })
        });
        loadDeliveryAlarms();
      } catch (e) {
        console.error(e);
      }
    }

    async function deleteAlarmServer(key) {
      delete activeAlarms[key];
      renderAlarms();
      try {
        await fetch('/api/delete_alarm', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ key })
        });
      } catch (e) {
        console.error(e);
      }
    }

    async function clearAllAlarms() {
      if (confirm('모든 가공 알람 목록을 삭제하시겠습니까?')) {
        activeAlarms = {};
        renderAlarms();
        try {
          await fetch('/api/clear_alarms', { method: 'POST' });
        } catch (e) {
          console.error(e);
        }
      }
    }

    function updateAlteringCountdownOnly() {
      const works = window.cachedAlteringWorks || [];
      if (works.length === 0) return;

      // If any work just reached 0 seconds, trigger full renderAlteringQueue once to update '완료' button & badges
      let stateTransitioned = false;
      works.forEach(w => {
        if (!w.IsCompleted && !w._localCompletedNoted && getWorkRemainingSeconds(w) === 0) {
          w._localCompletedNoted = true;
          stateTransitioned = true;
        }
      });
      if (stateTransitioned) {
        renderAlteringQueue();
        return;
      }

      const grouped = {};
      ALL_FACILITIES.forEach(f => { grouped[f.name] = []; });
      works.forEach(w => {
        const fn = w.FacilityName || '';
        if (!grouped[fn]) grouped[fn] = [];
        grouped[fn].push(w);
      });

      // Update countdown spans in-place without destroying DOM/buttons
      const countdownEls = document.querySelectorAll('.work-countdown');
      for (let i = 0; i < countdownEls.length; i++) {
        const el = countdownEls[i];
        const fac = el.getAttribute('data-fac') || '';
        const wIdx = parseInt(el.getAttribute('data-widx') || '0', 10);
        const list = grouped[fac] || [];
        const w = list[wIdx];
        if (!w) continue;
        const remSec = getWorkRemainingSeconds(w);
        if (w.State === 'InProgress' || remSec > 0) {
          el.textContent = formatRemaining(remSec);
        } else {
          el.textContent = `대기 (${formatRemaining(remSec)})`;
        }
      }

      // Update collapsed quick summary bar text
      let quickSummaryParts = [];
      ALL_FACILITIES.forEach(f => {
        const list = grouped[f.name] || [];
        if (list.length > 0) {
          const incomplete = list.filter(w => !w.IsCompleted && getWorkRemainingSeconds(w) > 0);
          const totalSec = incomplete.reduce((sum, w) => sum + getWorkRemainingSeconds(w), 0);
          const hasComp = list.some(w => w.IsCompleted || (getWorkRemainingSeconds(w) === 0 && !w.IsCompleted));
          const facShort = f.name.replace(' 가공 시설', '');

          if (totalSec > 0) {
            const compTag = hasComp ? ' <span style="color:var(--text-main);font-size:var(--fs-sm);">(일부완료)</span>' : '';
            quickSummaryParts.push(`${facShort}: <span class="mono-num" style="color:var(--text-main);font-weight:700;">${formatRemaining(totalSec)}</span>${compTag}`);
          } else if (hasComp) {
            quickSummaryParts.push(`${facShort}: <span style="color:var(--text-main);font-weight:700;">=== 완료 ===</span>`);
          }
        }
      });
      const quickSummaryEl = document.getElementById('altering-quick-summary');
      if (quickSummaryEl) {
        quickSummaryEl.innerHTML = quickSummaryParts.length > 0
          ? quickSummaryParts.join(' &nbsp;|&nbsp; ')
          : '현재 대기 중인 가공 작업이 없습니다.';
      }
    }

    // 1-second Alarm Ticker (triggers strictly when ALL works at a facility are finished)
    window.tickAlarms = function tickAlarms() {
      const now = Date.now();
      let changed = false;
      let hasActiveUnnotified = false;
      const works = window.cachedAlteringWorks || [];

      Object.keys(activeAlarms).forEach(k => {
        const al = activeAlarms[k];
        if (!al.notified) {
          // 해당 가공 시설의 실제 작업 목록 조회
          const facWorks = works.filter(w => (
            w.FacilityName === al.facility || 
            (typeof getFacilityForMaterial === 'function' && getFacilityForMaterial(w.DisplayName) === al.facility)
          ));

          let incompleteCount = 0;
          let completedCount = 0;
          let totalRemainingSec = 0;

          if (facWorks.length > 0) {
            facWorks.forEach(w => {
              const rem = getWorkRemainingSeconds(w);
              if (!w.IsCompleted && rem > 0) {
                incompleteCount++;
                totalRemainingSec += rem;
              } else {
                completedCount++;
              }
            });
          }

          if (incompleteCount > 0) {
            // 이 가공 시설에 아직 끝나지 않은 작업이 남아있음 -> 개별 완료는 무시하고 절대 알람 울리지 않음!
            hasActiveUnnotified = true;
            // 타깃 시간도 실제 남은 시간에 맞춰 실시간 동기화
            const expectedTarget = now + (totalRemainingSec * 1000);
            if (Math.abs(expectedTarget - al.targetTimestamp) > 3000) {
              al.targetTimestamp = expectedTarget;
              changed = true;
            }
          } else {
            // 이 가공 시설에 미완료 작업이 없음 (걸어둔 모든 작업 완료)
            if (facWorks.length > 0 && completedCount > 0) {
              // 걸어둔 모든 작업이 완료된 상태!
              al.notified = true;
              changed = true;
              playChime();
              showPushNotification(
                `[${al.facility}] 모든 가공 완료`,
                `[${al.facility}] 걸어둔 모든 가공 작업(${completedCount}건)이 완료되었습니다! 지금 바로 수령하세요.`
              );
              document.title = `[${al.facility} 전체 가공 완료] 모비노기 생활 지원도구`;
              setTimeout(() => { document.title = '모비노기 생활 지원도구'; }, 8000);
            } else if (now >= al.targetTimestamp) {
              // 캐시에 목록이 없거나 작업 완료 후 시간 도달
              al.notified = true;
              changed = true;
              playChime();
              showPushNotification(
                `[${al.facility}] 가공 완료`,
                `[${al.facility}] 시설의 모든 가공 작업이 완료되었습니다. (${al.item})`
              );
              document.title = `[${al.facility} 가공 완료] 모비노기 생활 지원도구`;
              setTimeout(() => { document.title = '모비노기 생활 지원도구'; }, 8000);
            } else {
              hasActiveUnnotified = true;
            }
          }
        }
      });
      if (changed || (!document.hidden && hasActiveUnnotified)) renderAlarms();
      if (!document.hidden && window.cachedAlteringWorks && window.cachedAlteringWorks.length > 0) {
        updateAlteringCountdownOnly();
      }
    };

    // 2.5. 7-Slot Max Level Alteration Quick Routine
    let currentQuickCategory = 'all';

    function selectQuickCategory(cat) {
      currentQuickCategory = cat;
      document.querySelectorAll('.quick-tab').forEach(b => b.classList.remove('active'));
      const activeBtn = document.getElementById(`tab-cat-${cat}`);
      if (activeBtn) activeBtn.classList.add('active');
      loadQuickAlterPlan();
    }

    async function loadQuickAlterPlan() {
      const container = document.getElementById('quick-alter-plan-container');
      const gatherSum = document.getElementById('quick-gather-summary');
      const execBtn = document.getElementById('btn-quick-alter-execute');
      if (!container) return;

      try {
        const res = await fetch(`/api/quick_alter_plan?category=${encodeURIComponent(currentQuickCategory)}&alter_order=${encodeURIComponent(currentAlterOrder)}`);
        const data = await res.json();
        cachedQuickPlan = data;

        if (data.error) {
          container.innerHTML = `<p style="color: var(--text-main); font-size: var(--fs-md);">[!] 계획 분석 오류: ${data.error}</p>`;
          if (execBtn) execBtn.disabled = true;
          return;
        }

        const categories = data.categories || {};
        const catKeys = Object.keys(categories);
        if (catKeys.length === 0) {
          container.innerHTML = '<p style="color: var(--text-muted); font-size: var(--fs-md);"><span class="sym-prefix">&gt;</span>선택된 가공대 정보가 없습니다.</p>';
          if (execBtn) execBtn.disabled = true;
          return;
        }

        let html = '';
        catKeys.forEach(catName => {
          const c = categories[catName];
          const hasCompleted = c.completed_count > 0;
          const slotsBadge = `<span class="badge-dim mono-num">가용 슬롯: ${c.available_slots}/7</span>`;
          const completedBadge = hasCompleted 
            ? `<span class="badge mono-num">완료 ${c.completed_count}건 수령 대기</span>`
            : '';

          html += `
            <div class="quick-cat-card">
              <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; flex-wrap: wrap; gap: 6px;">
                <div style="display: flex; align-items: center; gap: 8px;">
                  <strong style="font-size: var(--fs-md); color: var(--text-title);">// ${catName} (${c.facility})</strong>
                  ${slotsBadge}
                  ${completedBadge}
                </div>
                <span style="font-size: var(--fs-sm); color: var(--text-muted);">
                  가공 예정: <strong class="mono-num" style="color: var(--text-main);">${c.planned_tiers.length}건</strong> :: 건너뜀: <span class="mono-num">${c.skipped_tiers.length}건</span>
                </span>
              </div>

              <table class="quick-tier-table">
                <thead>
                  <tr>
                    <th style="width: 55px;">티어</th>
                    <th style="width: 140px;">아이템명</th>
                    <th style="width: 150px;">레시피</th>
                    <th>상태 및 사전 소요 재료</th>
                  </tr>
                </thead>
                <tbody>
          `;

          // Planned items
          c.planned_tiers.forEach(p => {
            const isGather = p.needs_gathering;
            let statusHtml = '';
            if (isGather) {
              const reqs = Object.entries(p.gather_reqs || {}).map(([k, v]) => `${k} ${v}개`).join(', ');
              statusHtml = `<span class="badge-dim mono-num">사전 채집 후 가공 [${reqs}]</span>`;
            } else {
              statusHtml = `<span class="badge">즉시 가공 가능</span>`;
            }

            html += `
              <tr>
                <td><span class="badge-dim mono-num">T${p.tier}</span></td>
                <td><strong style="color: var(--text-main);">${p.item_name}</strong></td>
                <td><span style="color: var(--text-muted); font-size: var(--fs-sm);">${p.recipe_name}</span></td>
                <td>${statusHtml}</td>
              </tr>
            `;
          });

          // Skipped items
          c.skipped_tiers.forEach(s => {
            html += `
              <tr style="opacity: 0.55;">
                <td><span class="badge-dim mono-num">T${s.tier}</span></td>
                <td><span style="color: var(--text-muted);">${s.item_name}</span></td>
                <td><span style="color: var(--text-dim); font-size: var(--fs-sm);">-</span></td>
                <td><span style="color: var(--text-muted); font-size: var(--fs-sm);">> 건너뜀 (${s.reason})</span></td>
              </tr>
            `;
          });

          html += `
                </tbody>
              </table>
            </div>
          `;
        });

        container.innerHTML = html;

        // Total gathering summary
        const totalRaw = data.total_raw_needed || {};
        const rawEntries = Object.entries(totalRaw);
        if (rawEntries.length > 0) {
          const rawText = rawEntries.map(([k, v]) => `<strong style="color: var(--text-main);">${k}</strong> <span class="mono-num">${v}</span>개`).join(', ');
          if (gatherSum) gatherSum.innerHTML = `<span class="sym-prefix">&gt;</span><span>사전 부족분 맞춤 채집: ${rawText} <span style="color: var(--text-muted); font-size: var(--fs-sm);">(부족한 수량만 채집 후 즉시 자동 중단)</span></span>`;
        } else {
          if (gatherSum) gatherSum.innerHTML = `<span class="sym-prefix">&gt;</span><span style="color: var(--text-main);">사전 채집이 필요한 원자재 없음 (보유량 충분)</span>`;
        }

        if (execBtn) {
          execBtn.disabled = !data.can_start || isCurrentlyBusy;
        }

        let totalPlanned = 0;
        let totalCompleted = 0;
        catKeys.forEach(k => {
          totalPlanned += (categories[k].planned_tiers || []).length;
          totalCompleted += (categories[k].completed_count || 0);
        });

        // Update mode tab badge
        const quickTabPill = document.getElementById('tab-quick-alter-pill');
        if (quickTabPill) {
          if (data.can_start) {
            quickTabPill.innerText = `${totalPlanned}건 계획`;
          } else {
            quickTabPill.innerText = '7슬롯 최적화';
          }
        }

        // Update collapsed summary bar
        const quickSummaryEl = document.getElementById('quick-alter-quick-summary');
        if (quickSummaryEl) {
          const canStartTag = data.can_start ? '<span style="color:var(--text-main); font-weight:700;">실행 가능</span>' : '<span style="color:var(--text-muted);">가공 예정 없음</span>';
          quickSummaryEl.innerHTML = `<strong>7슬롯 가공 빠른 실행:</strong> 가공 예정 <span class="mono-num" style="color: var(--text-main); font-weight:700;">${totalPlanned}건</span> :: 완료 수령 대기 <span class="mono-num" style="color: var(--text-main); font-weight:700;">${totalCompleted}건</span> :: 상태: ${canStartTag}`;
        }
        updateMasterCommandBar();
      } catch (e) {
        console.error('loadQuickAlterPlan error:', e);
        if (container) container.innerHTML = `<p style="color: var(--text-main); font-size: var(--fs-md);">[!] 계획 불러오기 실패: ${e}</p>`;
      }
    }

    async function executeQuickAlter() {
      if (!checkWeightSafetyConfirm()) {
        return;
      }
      const catText = currentQuickCategory === 'all' ? '전체 가공대' : `'${currentQuickCategory}' 가공대`;
      const orderDesc = currentAlterOrder === 'low_tier' ? '하위 티어(T1~T7)부터 순차 가공 등록' : '최고 티어(T7~T1)부터 순차 가공 등록 (오래 걸리는 가공 우선)';
      const etaInfo = getPipelineEtaInfo('quick_alter');
      const etaLine = etaInfo ? `\n• 예상 완수 시각: ${etaInfo.durStr} (${etaInfo.timeStr}경 완료 예정)` : '';
      if (!confirm(`[7슬롯 빠른 가공]\n${catText} 7슬롯 빠른 가공 루틴을 실행할까요?${etaLine}\n\n1. 완료 가공품 일괄 수령 (슬롯 확보)\n2. 부족한 원자재 사전 일괄 채집\n3. ${orderDesc}`)) {
        return;
      }

      isCurrentlyBusy = true;
      if (typeof applyBusyUI === 'function') applyBusyUI(true, `7슬롯 최고 레벨 ${catText} 빠른 실행 준비 중...`);
      try {
        const res = await fetch('/api/execute_quick_alter', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ category: currentQuickCategory, alter_order: currentAlterOrder })
        });
        const d = await res.json();
        if (d.error) {
          isCurrentlyBusy = false;
          if (typeof applyBusyUI === 'function') applyBusyUI(false);
          alert('실행 오류: ' + d.error);
        } else {
          pollLogs();
          updateStatus();
        }
      } catch (e) {
        isCurrentlyBusy = false;
        if (typeof applyBusyUI === 'function') applyBusyUI(false);
        alert('요청 실패: ' + e);
      }
    }

    async function abortQuickAlter() {
      if (!confirm('현재 진행 중인 작업(채집/가공 루틴)을 즉시 중지하시겠습니까?')) {
        return;
      }
      const abortBtns = [
        document.getElementById('btn-quick-alter-abort'),
        document.getElementById('btn-custom-abort'),
        document.getElementById('btn-batch-abort')
      ];
      try {
        abortBtns.forEach(btn => {
          if (btn) {
            btn.innerText = '중지 요청 중...';
            btn.disabled = true;
          }
        });
        const res = await fetch('/api/abort_quick_alter', { method: 'POST' });
        const d = await res.json();
        pollLogs();
        updateStatus();
        setTimeout(loadQuickAlterPlan, 1000);
        setTimeout(loadCustomPlan, 1000);
        setTimeout(loadAlteringQueue, 1000);
      } catch (e) {
        alert('중지 요청 실패: ' + e);
      } finally {
        abortBtns.forEach(btn => {
          if (btn) {
            btn.innerText = '즉시 중지';
            btn.disabled = false;
          }
        });
      }
    }

    let isDeliveryTargetsLoading = false;
    window.deliveryCharactersInfo = null;
    let lastKnownActiveCharId = null;
    let lastKnownMatchedIngameCharId = null;

    function renderDeliveryCharacterBar(charsInfo) {
      if (!charsInfo) return;
      window.deliveryCharactersInfo = charsInfo;
      lastKnownActiveCharId = charsInfo.active_character_id || null;
      lastKnownMatchedIngameCharId = charsInfo.matched_ingame_character_id || null;

      const tabsEl = document.getElementById('delivery-char-tabs-list');
      const overallBadge = document.getElementById('char-overall-summary-badge');
      const autoSwitchIcon = document.getElementById('icon-char-autoswitch');
      const bannerEl = document.getElementById('delivery-char-context-banner');
      const btnComplete = document.getElementById('btn-complete-char-delivery');
      const btnUnmark = document.getElementById('btn-unmark-char-delivery');

      if (overallBadge && charsInfo.overall) {
        const ov = charsInfo.overall;
        const delivChars = ov.weekly_delivered_characters ?? 0;
        overallBadge.innerText = `납품 완료: ${delivChars}/${ov.total_characters} 캐릭터 (월 06시 초기화) · 준비 달성: ${ov.completed_items}/${ov.total_items} 품목`;
        if (ov.total_characters > 0 && delivChars >= ov.total_characters) {
          overallBadge.className = 'badge mono-num';
        } else {
          overallBadge.className = 'badge-dim mono-num';
        }
      }

      if (btnComplete) {
        const actName = charsInfo.active_character_name || '현재 캐릭터';
        if (charsInfo.active_weekly_delivered) {
          btnComplete.innerText = `✓ ${actName} 납품 완료됨 (재차감)`;
          if (btnUnmark) btnUnmark.style.display = 'inline-flex';
        } else {
          btnComplete.innerText = `✓ ${actName} 납품 완료 (목표량 차감)`;
          if (btnUnmark) btnUnmark.style.display = 'none';
        }
      }

      if (autoSwitchIcon) {
        const on = !!charsInfo.auto_switch_character;
        autoSwitchIcon.innerText = on ? '■' : '□';
        autoSwitchIcon.style.color = on ? 'var(--text-bright)' : 'var(--text-dim)';
      }

      const chars = charsInfo.characters || [];
      if (tabsEl) {
        tabsEl.innerHTML = chars.map(c => {
          const safeId = (c.id || '').replace(/'/g, "\\'");
          const isAct = !!c.is_active;
          const isIngame = !!c.is_ingame;
          const isDelivered = !!c.weekly_delivered;
          const deliveredTag = isDelivered
            ? `<span class="char-delivered-badge" title="이번 주 납품 완료됨 (매주 월요일 오전 6시 초기화)">✓완료</span>`
            : '';
          const doneTag = c.is_all_completed
            ? `<span class="char-tab-progress mono-num" style="font-weight:700;">[${c.completed_count}/${c.total_count}]</span>`
            : `<span class="char-tab-progress mono-num" style="color:var(--text-muted);">(${c.completed_count}/${c.total_count})</span>`;
          const ingameDot = isIngame
            ? `<span class="char-ingame-dot" title="현재 인게임 접속 중인 캐릭터 (${c.signature_label || ''})"></span>`
            : '';
          const jobText = c.signature && c.signature.job ? c.signature.job : '';
          const subLabel = jobText
            ? `<span class="char-tab-sub" style="font-size:11px; color:var(--text-dim); white-space:nowrap;">${jobText}</span>`
            : (!c.signature ? `<span class="char-tab-sub" style="font-size:11px; color:var(--text-dim); white-space:nowrap;">미연동</span>` : '');
          return `
            <button type="button" class="char-tab-btn ${isAct ? 'active' : ''}" onclick="selectDeliveryCharacter('${safeId}')" title="${c.name} — ${c.signature_label || '인게임 미연동'} (${isDelivered ? '이번 주 납품 완료 · ' : ''}${c.completed_count}/${c.total_count} 준비 완료)">
              <span class="char-tab-left">
                ${ingameDot}
                <span style="white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${c.name}</span>
                ${subLabel}
              </span>
              <span class="char-tab-right">
                ${deliveredTag}
                ${doneTag}
              </span>
            </button>
          `;
        }).join('');
      }

      if (bannerEl) {
        const activeChar = chars.find(c => c.is_active);
        const ingameLabel = charsInfo.last_ingame_label || '';
        if (charsInfo.last_ingame_signature && !charsInfo.matched_ingame_character_id) {
          bannerEl.style.display = 'flex';
          bannerEl.innerHTML = `
            <span><span class="sym-prefix">&gt;</span>현재 접속 중인 캐릭터(<strong>${ingameLabel}</strong>)가 아직 프로필에 연동되지 않았습니다.</span>
            <div style="display:flex; gap:6px;">
              <button type="button" class="btn btn-sm" onclick="linkActiveCharacterToIngame()">현재 탭(${activeChar ? activeChar.name : '선택 탭'})에 연동</button>
              <button type="button" class="btn btn-sm btn-solid-core" onclick="quickAddAndLinkIngameCharacter()">+ 새 캐릭터로 추가·연동</button>
            </div>
          `;
        } else if (!charsInfo.is_active_ingame) {
          const matchedChar = chars.find(c => c.id === charsInfo.matched_ingame_character_id);
          bannerEl.style.display = 'flex';
          bannerEl.innerHTML = `
            <span><span class="sym-prefix">&gt;</span>현재 인게임 접속 캐릭터(${matchedChar ? '<strong>' + matchedChar.name + '</strong>' : (ingameLabel || '다른 캐릭터')})와 다른 캐릭터(<strong>${activeChar ? activeChar.name : ''}</strong>)의 저장된 납품 현황을 조회 중입니다. (인게임 가방 수량으로 덮어쓰지 않음)</span>
            ${matchedChar ? `<button type="button" class="btn btn-sm" onclick="selectDeliveryCharacter('${matchedChar.id}')">접속 중인 '${matchedChar.name}' 탭으로 이동</button>` : ''}
          `;
        } else {
          bannerEl.style.display = 'none';
          bannerEl.innerHTML = '';
        }
      }
    }

    async function selectDeliveryCharacter(charId) {
      if (window.deliveryCharactersInfo && window.deliveryCharactersInfo.active_character_id === charId) {
        return;
      }
      try {
        const res = await fetch('/api/delivery_characters/select', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ char_id: charId })
        });
        const d = await res.json();
        if (d.error) {
          alert('캐릭터 전환 실패: ' + d.error);
        } else {
          if (d.characters_info) renderDeliveryCharacterBar(d.characters_info);
          pollLogs();
          loadDeliveryTargets();
          loadBatchPlan();
        }
      } catch (e) {
        alert('요청 실패: ' + e);
      }
    }

    async function promptAddCharacterProfile() {
      const charsInfo = window.deliveryCharactersInfo;
      const nextNum = charsInfo && charsInfo.characters ? (charsInfo.characters.length + 1) : 2;
      const defaultName = `캐릭터 ${nextNum}`;
      const name = prompt('추가할 캐릭터의 이름(별칭)을 입력하세요:\n(새로운 캐릭터는 기본 18개 주간 납품 목록으로 초기화됩니다)', defaultName);
      if (name === null) return;
      const linkNow = confirm('이 새 프로필을 현재 게임에 접속 중인 캐릭터와 바로 연동할까요?\n\n• [확인]: 현재 접속 중인 인게임 캐릭터와 연동 (가방 수량 실시간 반영)\n• [취소]: 인게임 미연동 상태로 추가 (보유 수량 0개로 시작, 나중에 해당 캐릭터 접속 시 연동)');
      try {
        const res = await fetch('/api/delivery_characters/add', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name: name.trim() || defaultName,
            init_mode: 'default_18',
            link_current_ingame: linkNow
          })
        });
        const d = await res.json();
        if (d.error) {
          alert('캐릭터 추가 실패: ' + d.error);
        } else {
          if (d.characters_info) renderDeliveryCharacterBar(d.characters_info);
          pollLogs();
          loadDeliveryTargets();
          loadBatchPlan();
        }
      } catch (e) {
        alert('요청 실패: ' + e);
      }
    }

    async function quickAddAndLinkIngameCharacter() {
      const charsInfo = window.deliveryCharactersInfo;
      const nextNum = charsInfo && charsInfo.characters ? (charsInfo.characters.length + 1) : 2;
      const sigJob = charsInfo && charsInfo.last_ingame_signature ? (charsInfo.last_ingame_signature.job || '') : '';
      const defaultName = sigJob ? `캐릭터 ${nextNum} (${sigJob})` : `캐릭터 ${nextNum}`;
      const name = prompt('현재 인게임에 접속 중인 캐릭터의 프로필 이름(별칭)을 입력하세요:', defaultName);
      if (name === null) return;
      try {
        const res = await fetch('/api/delivery_characters/add', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name: name.trim() || defaultName,
            init_mode: 'default_18',
            link_current_ingame: true
          })
        });
        const d = await res.json();
        if (d.error) {
          alert('캐릭터 추가 실패: ' + d.error);
        } else {
          if (d.characters_info) renderDeliveryCharacterBar(d.characters_info);
          pollLogs();
          loadDeliveryTargets();
          loadBatchPlan();
        }
      } catch (e) {
        alert('요청 실패: ' + e);
      }
    }

    async function promptRenameActiveCharacter() {
      const charsInfo = window.deliveryCharactersInfo;
      const curId = charsInfo ? charsInfo.active_character_id : '';
      const curName = charsInfo ? (charsInfo.active_character_name || '') : '';
      const newName = prompt('현재 선택된 캐릭터 프로필의 새 이름을 입력하세요:', curName);
      if (newName === null || !newName.trim()) return;
      try {
        const res = await fetch('/api/delivery_characters/rename', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ char_id: curId, name: newName.trim() })
        });
        const d = await res.json();
        if (d.error) {
          alert('이름 변경 실패: ' + d.error);
        } else {
          if (d.characters_info) renderDeliveryCharacterBar(d.characters_info);
          pollLogs();
          loadDeliveryTargets();
        }
      } catch (e) {
        alert('요청 실패: ' + e);
      }
    }

    async function deleteActiveCharacterProfile() {
      const charsInfo = window.deliveryCharactersInfo;
      if (!charsInfo || (charsInfo.characters || []).length <= 1) {
        alert('최소 1개의 캐릭터 프로필은 유지되어야 합니다.');
        return;
      }
      const curId = charsInfo.active_character_id;
      const curName = charsInfo.active_character_name || curId;
      if (!confirm(`정말로 '${curName}' 캐릭터 프로필과 해당 캐릭터의 주간 납품 목록을 삭제할까요?`)) return;
      try {
        const res = await fetch('/api/delivery_characters/delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ char_id: curId })
        });
        const d = await res.json();
        if (d.error) {
          alert('캐릭터 삭제 실패: ' + d.error);
        } else {
          if (d.characters_info) renderDeliveryCharacterBar(d.characters_info);
          pollLogs();
          loadDeliveryTargets();
          loadBatchPlan();
        }
      } catch (e) {
        alert('요청 실패: ' + e);
      }
    }

    async function linkActiveCharacterToIngame() {
      const charsInfo = window.deliveryCharactersInfo;
      const curId = charsInfo ? charsInfo.active_character_id : '';
      const curName = charsInfo ? (charsInfo.active_character_name || '현재 탭') : '현재 탭';
      const ingameLabel = (charsInfo && charsInfo.last_ingame_label) ? `\n(감지된 인게임 캐릭터: ${charsInfo.last_ingame_label})` : '';
      if (!confirm(`'${curName}' 프로필을 현재 게임에 로그인된 캐릭터와 연동할까요?${ingameLabel}\n\n연동하면 인게임에서 이 캐릭터로 접속할 때 자동으로 '${curName}' 탭으로 전환됩니다.`)) return;
      try {
        const res = await fetch('/api/delivery_characters/link_ingame', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ char_id: curId })
        });
        const d = await res.json();
        if (d.error) {
          alert('캐릭터 연동 실패: ' + d.error);
        } else {
          if (d.characters_info) renderDeliveryCharacterBar(d.characters_info);
          if (d.homework && typeof onHomeworkStateReceived === 'function') onHomeworkStateReceived(d.homework);
          pollLogs();
          loadDeliveryTargets();
          loadBatchPlan();
        }
      } catch (e) {
        alert('요청 실패: ' + e);
      }
    }

    async function toggleCharacterAutoSwitch() {
      const charsInfo = window.deliveryCharactersInfo;
      const nextVal = !(charsInfo && charsInfo.auto_switch_character);
      try {
        const res = await fetch('/api/delivery_characters/toggle_auto_switch', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled: nextVal })
        });
        const d = await res.json();
        if (!d.error && d.characters_info) {
          renderDeliveryCharacterBar(d.characters_info);
          pollLogs();
        }
      } catch (e) {
        console.error('toggleCharacterAutoSwitch error:', e);
      }
    }

    async function syncTargetsWithInventory() {
      const charsInfo = window.deliveryCharactersInfo;
      const curName = charsInfo ? (charsInfo.active_character_name || '현재 캐릭터') : '현재 캐릭터';
      if (!confirm(`[${curName}] 캐릭터의 수동 수량 고정(0개 초기화 등)을 해제하고 현재 인게임 가방 수량과 다시 동기화할까요?`)) return;
      try {
        const res = await fetch('/api/sync_delivery_targets_inventory', { method: 'POST' });
        const d = await res.json();
        if (d.status === 'success') {
          if (d.characters_info) renderDeliveryCharacterBar(d.characters_info);
          pollLogs();
          loadDeliveryTargets();
          loadBatchPlan();
        } else {
          alert('동기화 실패: ' + (d.error || '알 수 없는 오류'));
        }
      } catch (e) {
        alert('동기화 요청 실패: ' + e);
      }
    }

    async function completeActiveCharacterDelivery() {
      const charsInfo = window.deliveryCharactersInfo;
      const curId = charsInfo ? charsInfo.active_character_id : '';
      const curName = charsInfo ? (charsInfo.active_character_name || '현재 캐릭터') : '현재 캐릭터';
      const alreadyDone = !!(charsInfo && charsInfo.active_weekly_delivered);
      const warnExtra = alreadyDone
        ? `\n[주의] '${curName}' 캐릭터는 이미 이번 주 [✓ 납품완료] 마크가 표시되어 있습니다. 다시 실행하면 목표 수량만큼 한 번 더 차감됩니다.\n`
        : '';
      if (!confirm(`[${curName}] 주간 납품 완료 처리를 실행할까요?${warnExtra}\n• '${curName}' 캐릭터에 등록된 각 납품 품목의 보유 수량에서 목표 수량(Goal)만큼 차감합니다 (예: 14/6 -> 8/6, 6/6 -> 0/6).\n• 다른 캐릭터의 보유 수량에는 영향을 주지 않습니다.\n• '${curName}' 캐릭터에 [✓ 납품완료] 마크가 표시되며, 매주 월요일 오전 6시에 자동으로 초기화됩니다.`)) {
        return;
      }
      try {
        const res = await fetch('/api/delivery_characters/complete_delivery', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ char_id: curId })
        });
        const d = await res.json();
        if (d.error) {
          alert('납품 완료 처리 실패: ' + d.error);
        } else {
          if (d.characters_info) renderDeliveryCharacterBar(d.characters_info);
          pollLogs();
          loadDeliveryTargets();
          loadBatchPlan();
        }
      } catch (e) {
        alert('요청 실패: ' + e);
      }
    }

    async function unmarkActiveCharacterDelivery() {
      const charsInfo = window.deliveryCharactersInfo;
      const curId = charsInfo ? charsInfo.active_character_id : '';
      const curName = charsInfo ? (charsInfo.active_character_name || '현재 캐릭터') : '현재 캐릭터';
      if (!confirm(`'${curName}' 캐릭터의 [✓ 납품완료] 마크를 해제할까요? (보유 수량은 변경되지 않습니다)`)) return;
      try {
        const res = await fetch('/api/delivery_characters/set_weekly_delivered', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ char_id: curId, delivered: false })
        });
        const d = await res.json();
        if (d.error) {
          alert('해제 실패: ' + d.error);
        } else {
          if (d.characters_info) renderDeliveryCharacterBar(d.characters_info);
          pollLogs();
        }
      } catch (e) {
        alert('요청 실패: ' + e);
      }
    }

    // Drag & Drop / Button Reordering for Weekly Delivery Target Cards
    let draggedTargetItemName = null;

    function onTargetCardDragStart(ev, itemName) {
      // Prevent starting drag from interactive controls
      if (ev.target && ev.target.closest && ev.target.closest('button, input, a')) {
        ev.preventDefault();
        return;
      }
      draggedTargetItemName = itemName;
      if (ev.dataTransfer) {
        ev.dataTransfer.effectAllowed = 'move';
        ev.dataTransfer.setData('text/plain', itemName);
      }
      const card = ev.currentTarget;
      if (card) {
        setTimeout(() => card.classList.add('dragging'), 0);
      }
    }

    function onTargetCardDragOver(ev) {
      ev.preventDefault();
      if (ev.dataTransfer) {
        ev.dataTransfer.dropEffect = 'move';
      }
      const card = ev.currentTarget;
      if (card && !card.classList.contains('dragging')) {
        card.classList.add('drag-over');
      }
    }

    function onTargetCardDragLeave(ev) {
      const card = ev.currentTarget;
      if (card) {
        card.classList.remove('drag-over');
      }
    }

    function onTargetCardDragEnd(ev) {
      draggedTargetItemName = null;
      document.querySelectorAll('.target-card.dragging, .target-card.drag-over').forEach(el => {
        el.classList.remove('dragging', 'drag-over');
      });
    }

    async function onTargetCardDrop(ev, targetItemName) {
      ev.preventDefault();
      const card = ev.currentTarget;
      if (card) card.classList.remove('drag-over');
      const fromItem = draggedTargetItemName || (ev.dataTransfer ? ev.dataTransfer.getData('text/plain') : '');
      draggedTargetItemName = null;
      if (!fromItem || !targetItemName || fromItem === targetItemName) return;
      await swapOrMoveDeliveryTarget(fromItem, targetItemName, 'swap');
    }

    async function swapOrMoveDeliveryTarget(fromItem, toItem, mode = 'swap') {
      try {
        const res = await fetch('/api/reorder_delivery_targets', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ from_item: fromItem, to_item: toItem, mode: mode })
        });
        const d = await res.json();
        if (!d.error) {
          loadDeliveryTargets();
          loadBatchPlan();
        }
      } catch (e) {
        console.error('swapOrMoveDeliveryTarget error:', e);
      }
    }

    async function loadDeliveryTargets() {
      if (isDeliveryTargetsLoading) return;
      isDeliveryTargetsLoading = true;
      try {
        const res = await fetch('/api/delivery_targets');
        const data = await res.json();
        if (data.characters_info) {
          renderDeliveryCharacterBar(data.characters_info);
        }
        const targets = data.targets || [];
        const activeCharName = (data.characters_info && data.characters_info.active_character_name) ? data.characters_info.active_character_name : '';
        const container = document.getElementById('delivery-targets-container');
        const badge = document.getElementById('target-count-badge');
        if (badge) {
          const completedCount = targets.filter(t => t.is_completed || t.current >= t.goal).length;
          badge.innerText = activeCharName ? `${activeCharName} · ${completedCount}/${targets.length} 완료` : `${targets.length}개 등록됨`;
        }
        const tabDeliveryPill = document.getElementById('tab-delivery-pill');
        if (tabDeliveryPill) {
          const completedCount = targets.filter(t => t.is_completed || t.current >= t.goal).length;
          tabDeliveryPill.innerText = activeCharName ? `${activeCharName} (${completedCount}/${targets.length})` : `${targets.length}개 등록`;
        }

        // Update Collapsed Bar Quick Summary
        const quickSummaryEl = document.getElementById('delivery-targets-quick-summary');
        if (quickSummaryEl) {
          if (targets.length === 0) {
            quickSummaryEl.innerHTML = '<span style="color: var(--text-muted);">등록된 주간 납품 목표가 없습니다. 클릭하여 펼친 뒤 등록하세요.</span>';
          } else {
            const completedCount = targets.filter(t => t.is_completed || t.current >= t.goal).length;
            const itemTexts = targets.slice(0, 5).map(t => {
              const done = t.is_completed || t.current >= t.goal;
              const styleStr = done ? 'color:var(--text-main);font-weight:700;' : 'color:var(--text-muted);';
              return `<span style="color:var(--text-main);">${t.item_name}</span> (<span class="mono-num" style="${styleStr}">${t.current}/${t.goal}</span>)`;
            });
            const more = targets.length > 5 ? ` <span style="color:var(--text-muted);">외 ${targets.length - 5}건</span>` : '';
            const statusBadge = completedCount === targets.length
              ? '<span class="badge">=== 전체 달성 ===</span>'
              : `<span class="badge-dim mono-num">${completedCount}/${targets.length} 완료</span>`;
            quickSummaryEl.innerHTML = `${statusBadge} ${itemTexts.join(' | ')}${more}`;
          }
        }

        if (!container) return;
        if (targets.length === 0) {
          container.innerHTML = '<p style="color: var(--text-muted); font-size: var(--fs-sm); grid-column: 1/-1;"><span class="sym-prefix">&gt;</span>등록된 주간 납품 목표가 없습니다. 인게임에서 퀘스트를 선택하고 [현재 퀘스트 등록]을 누르거나 직접 추가하세요.</p>';
          return;
        }

        let html = '';
        targets.forEach((t, idx) => {
          const isDone = t.is_completed || (t.current >= t.goal);
          const gaugeStr = makeTextGauge(t.current, t.goal, 5);
          const safeName = (t.item_name || '').replace(/'/g, "\\'");
          const prevItemSafe = idx > 0 ? (targets[idx - 1].item_name || '').replace(/'/g, "\\'") : '';
          const nextItemSafe = idx < targets.length - 1 ? (targets[idx + 1].item_name || '').replace(/'/g, "\\'") : '';

          let recHtml = '';
          const rec = t.recipe || {};
          const ingKeys = Object.keys(rec);
          if (ingKeys.length > 0) {
            const ingListStr = ingKeys.map(k => `${k} ${rec[k]}개`).join(', ');
            recHtml = `
              <div style="border-top: 1px solid var(--border-line); border-bottom: 1px solid var(--border-line); padding: 4px 0; font-size: var(--fs-sm); display: flex; justify-content: space-between; align-items: center; gap: 6px;">
                <span style="color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="1회 제작 재료: ${ingListStr}">
                  <span class="sym-prefix">&gt;</span>1회 재료: <strong style="color: var(--text-main);">${ingListStr}</strong>
                </span>
                <button class="btn btn-sm" onclick="openRecipeModal('${safeName}')">레시피</button>
              </div>
            `;
          }

          html += `
            <div class="target-card ${isDone ? 'target-done' : ''}"
                 draggable="true"
                 ondragstart="onTargetCardDragStart(event, '${safeName}')"
                 ondragover="onTargetCardDragOver(event)"
                 ondragleave="onTargetCardDragLeave(event)"
                 ondrop="onTargetCardDrop(event, '${safeName}')"
                 ondragend="onTargetCardDragEnd(event)">
              <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 6px;">
                <div style="display: flex; align-items: flex-start; gap: 5px; min-width: 0;">
                  <span class="target-drag-handle" title="마우스로 드래그하여 다른 품목과 위치를 맞바꿀 수 있습니다">⠿</span>
                  <div style="min-width: 0;">
                    <div style="font-weight: 700; font-size: var(--fs-md); color: var(--text-title); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">${t.item_name}</div>
                    <div style="font-size: 11px; color: var(--text-dim); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"><span class="sym-prefix">&gt;</span>${t.quest_title || '주간 납품'}</div>
                  </div>
                </div>
                <div style="display: flex; align-items: center; gap: 3px; flex-shrink: 0;">
                  <button class="btn btn-sm btn-move-card" ${idx === 0 ? 'disabled' : `onclick="swapOrMoveDeliveryTarget('${safeName}', '${prevItemSafe}', 'swap')"`} title="이전 위치로 이동">&lt;</button>
                  <button class="btn btn-sm btn-move-card" ${idx === targets.length - 1 ? 'disabled' : `onclick="swapOrMoveDeliveryTarget('${safeName}', '${nextItemSafe}', 'swap')"`} title="다음 위치로 이동">&gt;</button>
                  <span class="${isDone ? 'badge' : 'badge-dim'}">${isDone ? '완료' : '진행 중'}</span>
                  <button class="btn btn-sm" onclick="deleteDeliveryTarget('${safeName}')" title="삭제">삭제</button>
                </div>
              </div>

              ${recHtml}

              <div style="display: flex; justify-content: space-between; align-items: center; font-size: var(--fs-sm); flex-wrap: wrap; gap: 4px;">
                <div style="display: flex; align-items: center; gap: 3px; flex-wrap: wrap;">
                  <span class="cli-gauge">${gaugeStr}</span>
                  <span style="color: var(--text-muted); font-size: 11px;">보유</span>
                  <button class="btn btn-sm" onclick="adjustTargetCurrent('${safeName}', ${t.current}, -1)" title="1개 감소">-</button>
                  <strong class="mono-num" style="color: var(--text-main); cursor: pointer; text-decoration: underline dotted; padding: 0 2px;" onclick="promptEditCurrent('${safeName}', ${t.current})" title="수량 클릭하여 직접 수정">${t.current}</strong>
                  <button class="btn btn-sm" onclick="adjustTargetCurrent('${safeName}', ${t.current}, 1)" title="1개 증가">+</button>
                  <span class="mono-num" style="color: var(--text-muted);">/ ${t.goal}개</span>
                  <button class="btn btn-sm" onclick="updateTargetCurrent('${safeName}', 0)" title="현재 수량 0개로 리셋">0개</button>
                  ${t.storage_count > 0 ? `<small class="mono-num" style="color: var(--text-muted); font-size: 11px;">(가방${t.inventory_count || 0}+창고${t.storage_count})</small>` : ''}
                </div>
                <span class="mono-num" style="color: ${isDone ? 'var(--text-dim)' : 'var(--text-bright)'}; font-weight: 700; white-space: nowrap;">${isDone ? '✓ 준비 완료' : `${t.needed}개 부족`}</span>
              </div>
            </div>
          `;
        });
        container.innerHTML = html;
      } catch (e) {
        console.error('loadDeliveryTargets error:', e);
      } finally {
        isDeliveryTargetsLoading = false;
      }
    }

    async function addCurrentQuestTarget() {
      try {
        const res = await fetch('/api/add_current_quest_target', { method: 'POST' });
        const d = await res.json();
        if (d.status === 'success') {
          if (d.added && d.added.length > 0) {
            pollLogs();
            loadDeliveryTargets();
            loadBatchPlan();
          } else {
            alert('인게임 퀘스트 추적창에 활성화된 주간 납품 퀘스트를 찾지 못했습니다. 게임에서 주간 퀘스트를 추적 중인지 확인해주세요.');
          }
        } else {
          alert('등록 실패: ' + (d.error || '알 수 없는 오류'));
        }
      } catch (e) {
        alert('요청 실패: ' + e);
      }
    }

    async function addManualTarget() {
      const itemEl = document.getElementById('target-input-item');
      const goalEl = document.getElementById('target-input-goal');
      const currentEl = document.getElementById('target-input-current');
      if (!itemEl || !goalEl) return;
      const itemName = itemEl.value.trim();
      const goal = parseInt(goalEl.value, 10);
      if (!itemName || isNaN(goal) || goal <= 0) {
        alert('올바른 아이템명과 목표 수량을 입력해주세요.');
        return;
      }
      const payload = { item_name: itemName, goal: goal };
      if (currentEl && currentEl.value.trim() !== '') {
        const curVal = parseInt(currentEl.value, 10);
        if (!isNaN(curVal) && curVal >= 0) {
          payload.current = curVal;
        }
      }

      try {
        const res = await fetch('/api/add_delivery_target', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const d = await res.json();
        if (d.error) {
          alert('등록 실패: ' + d.error);
        } else {
          itemEl.value = '';
          if (currentEl) currentEl.value = '';
          pollLogs();
          loadDeliveryTargets();
          loadBatchPlan();
        }
      } catch (e) {
        alert('요청 실패: ' + e);
      }
    }

    async function deleteDeliveryTarget(itemName) {
      if (!confirm(`'${itemName}' 항목을 납품 목표 목록에서 삭제할까요?`)) return;
      try {
        await fetch('/api/delete_delivery_target', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ item_name: itemName })
        });
        loadDeliveryTargets();
        loadBatchPlan();
      } catch (e) {
        alert('삭제 실패: ' + e);
      }
    }

    async function clearDeliveryTargets() {
      if (!confirm('등록된 모든 주간 납품 목표를 비울까요?')) return;
      try {
        await fetch('/api/clear_delivery_targets', { method: 'POST' });
        loadDeliveryTargets();
        loadBatchPlan();
      } catch (e) {
        alert('초기화 실패: ' + e);
      }
    }

    async function updateTargetCurrent(itemName, current) {
      try {
        const res = await fetch('/api/update_delivery_target_current', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ item_name: itemName, current: parseInt(current, 10) })
        });
        const d = await res.json();
        if (d.status === 'success') {
          pollLogs();
          loadDeliveryTargets();
          loadBatchPlan();
        } else {
          alert('수량 변경 실패: ' + (d.error || '알 수 없는 오류'));
        }
      } catch (e) {
        alert('요청 실패: ' + e);
      }
    }

    function promptEditCurrent(itemName, currentVal) {
      const val = prompt(`'${itemName}'의 현재 인게임(가방/창고) 보유 수량을 입력하세요:`, currentVal);
      if (val !== null) {
        const num = parseInt(val, 10);
        if (!isNaN(num) && num >= 0) {
          updateTargetCurrent(itemName, num);
        } else {
          alert('0 이상의 숫자를 입력해주세요.');
        }
      }
    }

    function adjustTargetCurrent(itemName, currentVal, delta) {
      const nextVal = Math.max(0, currentVal + delta);
      updateTargetCurrent(itemName, nextVal);
    }

    // Delivery Presets Manager
    async function loadPresets() {
      try {
        const res = await fetch('/api/delivery_presets');
        const data = await res.json();
        const presets = data.presets || [];
        const container = document.getElementById('preset-list-container');
        const badge = document.getElementById('preset-count-badge');
        if (badge) {
          badge.innerText = `${presets.length}개 저장됨`;
        }
        if (!container) return;
        if (presets.length === 0) {
          container.innerHTML = '<span style="color: var(--text-muted); font-size: var(--fs-sm);"><span class="sym-prefix">&gt;</span>저장된 프리셋이 없습니다. 원하는 납품 목표를 등록한 뒤 이름을 입력하여 저장하세요.</span>';
          return;
        }

        let html = '';
        presets.forEach(p => {
          const itemCount = (p.items || []).length;
          const escapedName = (p.name || '').replace(/'/g, "\\'");
          html += `
            <div class="preset-item" style="border: 1px solid var(--border-line); padding: 4px 10px; display: inline-flex; align-items: center; gap: 8px;">
              <span style="font-weight: 700; font-size: var(--fs-md); color: var(--text-main);">${p.name}</span>
              <span class="badge-dim mono-num">${itemCount}개 항목</span>
              <button class="btn btn-sm" onclick="loadPreset('${escapedName}')" title="이 프리셋으로 목표 등록">불러오기</button>
              <button class="btn btn-sm" onclick="deletePreset('${escapedName}')" title="프리셋 삭제">삭제</button>
            </div>
          `;
        });
        container.innerHTML = html;
      } catch (e) {
        console.error('loadPresets error:', e);
      }
    }

    async function saveCurrentAsPreset() {
      const inputEl = document.getElementById('preset-save-name');
      if (!inputEl) return;
      const name = inputEl.value.trim();
      if (!name) {
        alert('저장할 프리셋 이름을 입력해주세요. (예: 대장간 세트, 주간 납품 A)');
        inputEl.focus();
        return;
      }

      try {
        const res = await fetch('/api/save_delivery_preset', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: name })
        });
        const d = await res.json();
        if (d.error) {
          alert('저장 실패: ' + d.error);
        } else {
          inputEl.value = '';
          pollLogs();
          loadPresets();
        }
      } catch (e) {
        alert('요청 실패: ' + e);
      }
    }

    async function resetAllTargetsCurrent() {
      const charsInfo = window.deliveryCharactersInfo;
      const curName = charsInfo ? (charsInfo.active_character_name || '현재 선택된 캐릭터') : '현재 선택된 캐릭터';
      if (!confirm(`[${curName}] 캐릭터에 등록된 모든 주간 납품 목표의 현재 보유 수량을 0개로 초기화할까요?\n\n• 현재 선택된 '${curName}' 캐릭터만 0개로 초기화되며, 다른 캐릭터의 납품 현황은 그대로 유지됩니다.\n• 이후 다시 가방 재고를 읽고 싶으실 때는 [가방 재고 재동기화] 버튼을 누르시면 됩니다.`)) return;
      try {
        const res = await fetch('/api/reset_delivery_targets_current', { method: 'POST' });
        const d = await res.json();
        if (d.status === 'success') {
          if (d.characters_info) renderDeliveryCharacterBar(d.characters_info);
          pollLogs();
          loadDeliveryTargets();
          loadBatchPlan();
        } else {
          alert('초기화 실패: ' + (d.error || '알 수 없는 오류'));
        }
      } catch (e) {
        alert('초기화 요청 실패: ' + e);
      }
    }

    async function loadPreset(name) {
      const resetCurrent = confirm(
        "'" + name + "' 프리셋의 납품 목표를 불러옵니다.\n\n" +
        "• [확인] 누름: 보유 수량을 0개로 초기화하여 새로 시작 (추천: 새 주간 퀘스트)\n" +
        "• [취소] 누름: 기존에 등록/제작된 보유 수량을 유지하며 불러오기"
      );
      try {
        const res = await fetch('/api/load_delivery_preset', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: name, reset_current: resetCurrent })
        });
        const d = await res.json();
        if (d.error) {
          alert('불러오기 실패: ' + d.error);
        } else {
          pollLogs();
          loadDeliveryTargets();
          loadBatchPlan();
        }
      } catch (e) {
        alert('불러오기 요청 실패: ' + e);
      }
    }

    async function deletePreset(name) {
      if (!confirm(`'${name}' 프리셋을 삭제할까요?`)) return;
      try {
        const res = await fetch('/api/delete_delivery_preset', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: name })
        });
        const d = await res.json();
        if (d.error) {
          alert('삭제 실패: ' + d.error);
        } else {
          pollLogs();
          loadPresets();
        }
      } catch (e) {
        alert('삭제 요청 실패: ' + e);
      }
    }

    async function collectAllCompletedAltering() {
      if (!confirm('모든 시설의 완료된 가공품을 즉시 순차 수령할까요? (슬롯이 비워지고 결과물이 가방에 들어옵니다)')) return;
      isCurrentlyBusy = true;
      if (typeof applyBusyUI === 'function') applyBusyUI(true, '모든 시설 완료 가공품 순차 수령 중...');
      try {
        const res = await fetch('/api/collect_all_completed_altering', { method: 'POST' });
        const d = await res.json();
        if (d.error) {
          isCurrentlyBusy = false;
          if (typeof applyBusyUI === 'function') applyBusyUI(false);
          alert('오류: ' + d.error);
        } else {
          pollLogs();
          updateStatus();
        }
      } catch (e) {
        isCurrentlyBusy = false;
        if (typeof applyBusyUI === 'function') applyBusyUI(false);
        alert('요청 실패: ' + e);
      }
    }

    // 3. Batch Planner: Aggregate BOM & Single-Click Pipeline
    async function loadBatchPlan() {
      try {
        const res = await fetch(`/api/batch_plan?alter_order=${encodeURIComponent(currentAlterOrder)}`);
        const plan = await res.json();
        cachedBatchPlan = plan;

        const tasksContainer = document.getElementById('batch-tasks-container');
        const tasks = plan.tasks || [];
        if (tasksContainer) {
          if (tasks.length === 0) {
            tasksContainer.innerHTML = '<p style="color: var(--text-muted); font-size: var(--fs-sm);"><span class="sym-prefix">&gt;</span>등록되거나 진행 중인 주간 납품 퀘스트가 없습니다.</p>';
          } else {
            let tasksHtml = '';
            tasks.forEach(t => {
              const isDone = t.is_completed || (t.needed === 0) || (t.current >= t.goal);
              const gaugeStr = makeTextGauge(t.current, t.goal, 5);
              const badge = isDone 
                ? '<span class="badge">달성 완료</span>' 
                : `<span class="badge-dim mono-num">${t.needed}개 필요</span>`;
              tasksHtml += `
                <div style="display:flex; justify-content:space-between; align-items:center; padding: 6px 4px; border-bottom: 1px solid var(--border-line);">
                  <div style="display:flex; align-items:center; gap:6px; flex-wrap:wrap;">
                    <span class="cli-gauge">${gaugeStr}</span>
                    <span style="font-weight:700; font-size:var(--fs-md); color:${isDone ? 'var(--text-main)' : 'var(--text-title)'};">${t.item_name}</span>
                    <span class="mono-num" style="font-size:var(--fs-sm); color:var(--text-muted);">(${t.current}/${t.goal}개)</span>
                  </div>
                  <div>${badge}</div>
                </div>
              `;
            });
            tasksContainer.innerHTML = tasksHtml;
          }
        }

        const tbody = document.getElementById('bom-table-body');
        const reqs = plan.intermediate_requirements || [];
        const btnExec = document.getElementById('btn-batch-execute');
        const statusText = document.getElementById('batch-status-text');

        if (tbody) {
          applyHideSatisfiedBomUI();
          let rowsHtml = '';
          if (reqs.length > 0) {
            reqs.forEach(r => {
              const queuedBadge = r.queued_works_count > 0 ? `<span class="mono-num" style="color:var(--text-muted); font-size:11px; margin-left:4px;">(대기 ${r.queued_works_count}회)</span>` : '';
              const completedBadge = r.completed_works_count > 0 ? `<span class="mono-num" style="color:var(--text-main); font-size:11px; margin-left:4px;">(수령가능 ${r.completed_works_count}회)</span>` : '';

              const isSatisfied = (r.status === 'satisfied');
              let statusBadge = '';
              if (isSatisfied) {
                statusBadge = '<span style="font-weight:700; white-space:nowrap;">=== 준비 완료 ===</span>' + completedBadge;
              } else if (r.status === 'needs_alter') {
                statusBadge = `<span class="mono-num" style="color:var(--text-main); font-weight:700; white-space:nowrap;">> ${r.works_needed}회 가공 필요</span>` + queuedBadge + completedBadge;
              } else if (r.status === 'in_queue') {
                statusBadge = `<span class="mono-num" style="color:var(--text-main); font-weight:700; white-space:nowrap;">대기열 (${r.queued_works_count}회)</span>` + completedBadge;
              } else if (r.status === 'ready_to_collect') {
                statusBadge = `<span class="badge mono-num" style="white-space:nowrap;">수령 가능 (${r.completed_works_count}회)</span>`;
              }

              const invCnt = r.inventory_count || 0;
              const storeCnt = r.storage_count || 0;
              const bagStorageText = `<span class="mono-num">${invCnt}개</span><span class="mono-num" style="color:var(--text-muted); font-size:11px;">(${storeCnt}개)</span>`;
              const facShort = formatFacilityShort(r.facility);
              const lockTag = r.is_locked ? ` <span class="badge-dim" style="color:#F59E0B;" title="인게임 잠금 설정된 재료">[잠금주의]</span>` : '';
              const warnTag = r.readiness_warning ? ` <span class="badge-dim" style="color:#EF4444;" title="${r.readiness_warning}">[설비/스킬 부족]</span>` : '';

              rowsHtml += `
                <tr class="row-alter ${isSatisfied ? 'row-satisfied' : ''}">
                  <td style="font-weight:700; color:var(--text-main); white-space:nowrap;"><span class="bom-dot dot-alter"></span>${r.item_name}${lockTag}${warnTag}</td>
                  <td style="text-align:center; white-space:nowrap;"><span class="badge-dim">${facShort}</span></td>
                  <td class="mono-num" style="font-weight:700; color:var(--text-main); text-align:right; white-space:nowrap;">${r.total_needed}개</td>
                  <td style="text-align:center; white-space:nowrap;">${bagStorageText}</td>
                  <td style="white-space:nowrap;">${statusBadge}</td>
                </tr>
              `;
            });
          }

          if (plan.raw_materials && plan.raw_materials.length > 0) {
            plan.raw_materials.forEach(rm => {
              let statusBadge = '';
              const isSpecial = rm.is_special_obtain || !rm.is_gatherable;
              const isRmSatisfied = (rm.deficit === 0);
              if (isRmSatisfied) {
                statusBadge = '<span style="font-weight:700; white-space:nowrap;">=== 준비 완료 ===</span>';
              } else if (isSpecial) {
                statusBadge = `<span class="mono-num" style="color:var(--text-muted); font-weight:700; white-space:nowrap;">> 별도 확보 필요 (${rm.deficit}개 부족)</span>`;
              } else if (rm.tool_ok) {
                statusBadge = `<span class="mono-num" style="color:var(--text-main); font-weight:700; white-space:nowrap;">> ${rm.deficit}개 자동 채집</span>`;
              } else {
                statusBadge = `<span class="mono-num" style="color:var(--text-main); font-weight:700; white-space:nowrap;">[!] 도구 필요 (${rm.deficit}개 부족)</span>`;
              }

              const rmInvCnt = rm.inventory_count || 0;
              const rmStoreCnt = rm.storage_count || 0;
              const rmBagStorageText = `<span class="mono-num">${rmInvCnt}개</span><span class="mono-num" style="color:var(--text-muted); font-size:11px;">(${rmStoreCnt}개)</span>`;

              const typeBadge = isSpecial
                ? '<span class="badge-dim">기타</span>'
                : '<span class="badge-dim">채집</span>';
              const rowClass = isSpecial ? 'row-special' : 'row-gather';
              const rmLockTag = rm.is_locked ? ` <span class="badge-dim" style="color:#F59E0B;" title="인게임 잠금 설정된 재료">[잠금주의]</span>` : '';

              rowsHtml += `
                <tr class="${rowClass} ${isRmSatisfied ? 'row-satisfied' : ''}">
                  <td style="font-weight:700; color:var(--text-main); white-space:nowrap;"><span class="bom-dot ${isSpecial ? 'dot-special' : 'dot-gather'}"></span>${rm.item_name}${rmLockTag}</td>
                  <td style="text-align:center; white-space:nowrap;">${typeBadge}</td>
                  <td class="mono-num" style="font-weight:700; color:var(--text-main); text-align:right; white-space:nowrap;">${rm.total_needed}개</td>
                  <td style="text-align:center; white-space:nowrap;">${rmBagStorageText}</td>
                  <td style="white-space:nowrap;">${statusBadge}</td>
                </tr>
              `;
            });
          }

          if (reqs.length === 0 && (!plan.raw_materials || plan.raw_materials.length === 0)) {
            if (plan.all_completed) {
              rowsHtml = '<tr><td colspan="5" style="text-align: center; color: var(--text-main); padding: 16px; font-weight: 700;">모든 주간 납품 목표가 달성 완료되었습니다</td></tr>';
            } else if (tasks.length === 0) {
              rowsHtml = '<tr><td colspan="5" style="text-align: center; color: var(--text-muted); padding: 16px;"><span class="sym-prefix">&gt;</span>등록되거나 진행 중인 주간 납품 퀘스트가 없습니다.</td></tr>';
            } else {
              rowsHtml = '<tr><td colspan="5" style="text-align: center; color: var(--text-main); padding: 16px; font-weight: 700;">모든 원자재 및 가공품 준비 완료! 아래 버튼을 눌러 즉시 완제품을 제작하세요</td></tr>';
            }
          }

          tbody.innerHTML = rowsHtml;
        }

        // Button state and status text (ALWAYS executed)
        if (btnExec && statusText) {
          const craftableCount = plan.craftable_tasks_count || 0;
          const craftableNote = (craftableCount > 0) ? ` (즉시 제작 가능: ${craftableCount}건)` : '';
          const neededRawGather = (plan.raw_materials || []).filter(r => r.deficit > 0 && r.is_gatherable);
          const neededSpecial = (plan.raw_materials || []).filter(r => r.deficit > 0 && (r.is_special_obtain || !r.is_gatherable));
          const gatherNote = (neededRawGather.length > 0) ? ` / 채집 ${neededRawGather.length}종` : '';
          const specialNote = (neededSpecial.length > 0) ? ` (별도 확보 ${neededSpecial.length}종 필요)` : '';
          const alterCount = plan.total_works_to_queue || 0;

          btnExec.className = 'btn btn-solid-core';
          if (tasks.length === 0) {
            btnExec.innerText = '주간 납품 일괄 실행';
            btnExec.disabled = true;
            statusText.innerText = '납품 퀘스트 없음';
          } else if (plan.all_completed) {
            btnExec.innerText = '모든 납품 목표 달성 완료';
            btnExec.disabled = true;
            statusText.innerHTML = '<span style="color:var(--text-main); font-weight:700;">모든 목표 달성 완료</span>';
          } else if (plan.can_craft_immediately || (alterCount === 0 && neededRawGather.length === 0 && craftableCount > 0)) {
            btnExec.innerText = craftableCount > 0 ? `주간 납품 일괄 실행 (즉시 제작 ${craftableCount}건)` : '주간 납품 일괄 실행 (최종 제작)';
            btnExec.disabled = isCurrentlyBusy;
            statusText.innerHTML = `<span style="color:var(--text-main); font-weight:700;">${craftableCount > 0 ? `${craftableCount}건 완제품 즉시 제작 가능` : '모든 재료 준비 완료'}${specialNote}</span>`;
          } else if (alterCount > 0) {
            btnExec.innerText = `주간 납품 일괄 실행 (가공 ${alterCount}회${gatherNote})`;
            btnExec.disabled = isCurrentlyBusy;
            statusText.innerHTML = `<span style="color:var(--text-main); font-weight:700;">가공 ${alterCount}회 등록 필요${gatherNote}${craftableNote}${specialNote}</span>`;
          } else if (neededRawGather.length > 0) {
            btnExec.innerText = `주간 납품 일괄 실행 (채집 ${neededRawGather.length}종)`;
            btnExec.disabled = isCurrentlyBusy;
            statusText.innerHTML = `<span style="color:var(--text-main); font-weight:700;">필드 채집 ${neededRawGather.length}종 필요${craftableNote}${specialNote}</span>`;
          } else if (craftableCount > 0) {
            btnExec.innerText = `주간 납품 일괄 실행 (즉시 제작 ${craftableCount}건)`;
            btnExec.disabled = isCurrentlyBusy;
            statusText.innerHTML = `<span style="color:var(--text-main); font-weight:700;">${craftableCount}건 즉시 제작 가능</span>`;
          } else {
            btnExec.innerText = '주간 납품 일괄 실행 (대기열 진행 중)';
            btnExec.disabled = isCurrentlyBusy;
            statusText.innerHTML = `<span style="color:var(--text-muted); font-weight:700;">대기열 가공 완료 대기 중</span>`;
          }
        }

        // Render facility slots & warnings
        const slotsSec = document.getElementById('facility-slots-section');
        const slotsContainer = document.getElementById('facility-slots-container');
        const warnBanner = document.getElementById('slot-warnings-banner');
        const warnList = document.getElementById('slot-warnings-list');
        const summaryBadge = document.getElementById('slot-summary-badge');

        const facList = plan.facility_slots || [];
        const warnings = plan.slot_warnings || [];

        if (slotsSec && slotsContainer) {
          if (facList.length > 0) {
            slotsSec.style.display = 'block';
            let html = '';
            facList.forEach(f => {
              const max = f.max_slots || 7;
              const avail = f.available_now;
              const needed = f.new_works_needed || 0;
              const isOver = f.overflow > 0;
              
              const progDetail = f.in_progress_detail ? ` (${f.in_progress_detail})` : '';
              const compDetail = f.completed_detail ? ` (${f.completed_detail})` : '';

              html += `
                <div style="flex: 1 1 calc(33.333% - 8px); min-width: 185px; background: var(--bg-sub-panel); border: 1px solid ${isOver ? 'var(--border-focus)' : 'var(--line-subtle)'}; border-radius: 0; padding: 8px 10px; box-shadow: var(--card-box-shadow);">
                  <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                    <span style="font-weight: 700; font-size: var(--fs-sm); color: var(--text-title);">${f.facility}</span>
                    <span class="mono-num" style="font-size: var(--fs-sm); font-weight: 700; color: ${avail > 0 ? 'var(--text-main)' : 'var(--text-muted)'};">
                      여유 ${avail}/${max}
                    </span>
                  </div>
                  <div class="mono-num" style="font-size: var(--fs-sm); color: var(--text-muted); display: flex; justify-content: space-between; flex-wrap: wrap; gap: 4px;">
                    <span>진행:${f.in_progress}${progDetail} | 완료:${f.completed}${compDetail}</span>
                    ${needed > 0 ? `<span style="font-weight: 700; color: var(--text-main);">필요 +${needed}</span>` : `<span style="color: var(--text-dim);">필요 없음</span>`}
                  </div>
                  ${f.overflow > 0 ? `<div class="mono-num" style="font-size: var(--fs-sm); color: var(--text-main); margin-top: 3px; font-weight: 700;">> [!] ${f.overflow}회 초과 (단계적 진행)</div>` : ''}
                </div>
              `;
            });
            slotsContainer.innerHTML = html;
            if (summaryBadge) {
              summaryBadge.innerHTML = plan.has_slot_issue 
                ? '<span style="color: var(--text-main); font-weight: 700;">[!] 일부 시설 슬롯 초과 예상</span>' 
                : '<span style="color: var(--text-muted); font-weight: 600;">--- 전 시설 슬롯 여유 충분 ---</span>';
            }
          } else {
            slotsSec.style.display = 'none';
          }
        }

        if (warnBanner && warnList) {
          if (warnings.length > 0) {
            warnBanner.style.display = 'block';
            warnList.innerHTML = warnings.map(w => `<div style="margin-bottom: 3px;"><span class="sym-prefix">&gt;</span>${w}</div>`).join('');
          } else {
            warnBanner.style.display = 'none';
          }
        }
        updateMasterCommandBar();
      } catch (e) {
        console.error('Error loading batch plan:', e);
      }
    }

    async function executeBatchPipeline() {
      if (!checkWeightSafetyConfirm()) {
        return;
      }
      const modeTitle = currentGoalFulfillmentMode ? '목표 완수 모드' : '주간 납품 일괄 제작';
      const modeExpl = currentGoalFulfillmentMode 
        ? '가공품 완료 시 즉시 수령 -> 다음 티어 가공 -> 완제품 최종 제작까지 100% 완수할 때까지 PC 절전 방지 상태로 계속 진행합니다.'
        : '부족한 가공품이 해당 시설에 일괄 등록되며 주간 납품 전용 알람이 설정됩니다.';
      const orderDesc = currentAlterOrder === 'low_tier' ? '하위 티어 우선 (기초 재료부터)' : '상위 티어 우선 (오래 걸리는 가공부터)';
      const etaInfo = currentGoalFulfillmentMode ? getPipelineEtaInfo('delivery') : null;
      const etaLine = etaInfo ? `\n• 예상 완수 시각: ${etaInfo.durStr} (${etaInfo.timeStr}경 완수 예정)` : '';
      if (confirm(`[${modeTitle}]\n${modeExpl}${etaLine}\n\n• 가공 우선순위: ${orderDesc}\n\n실행하시겠습니까?`)) {
        isCurrentlyBusy = true;
        if (typeof applyBusyUI === 'function') applyBusyUI(true, `전체 주간 납품 일괄 최적화 제작 준비 중... [${currentGoalFulfillmentMode ? '목표 완수 모드' : '1회차 실행'}]`);
        try {
          const res = await fetch('/api/execute_batch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ alter_order: currentAlterOrder, goal_fulfillment_mode: currentGoalFulfillmentMode })
          });
          const d = await res.json();
          if (d.error) {
            isCurrentlyBusy = false;
            if (typeof applyBusyUI === 'function') applyBusyUI(false);
            alert('오류: ' + d.error);
          } else {
            pollLogs();
            updateStatus();
          }
        } catch (e) {
          isCurrentlyBusy = false;
          if (typeof applyBusyUI === 'function') applyBusyUI(false);
          alert('요청 실패: ' + e);
        }
      }
    }

    async function collectAltering(displayName) {
      if (confirm(`'${displayName}' 가공품을 수령하러 이동할까요?`)) {
        isCurrentlyBusy = true;
        if (typeof applyBusyUI === 'function') applyBusyUI(true, `'${displayName}' 가공품 수령 이동 중...`);
        try {
          const res = await fetch('/api/collect_altering', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ item_name: displayName })
          });
          const d = await res.json();
          if (d.error) {
            isCurrentlyBusy = false;
            if (typeof applyBusyUI === 'function') applyBusyUI(false);
            alert('수령 실패: ' + d.error);
          } else {
            isCurrentlyBusy = false;
            if (typeof applyBusyUI === 'function') applyBusyUI(false);
            loadAlteringQueue();
            loadBatchPlan();
            updateStatus();
          }
        } catch (err) {
          isCurrentlyBusy = false;
          if (typeof applyBusyUI === 'function') applyBusyUI(false);
          alert('요청 오류: ' + err);
        }
      }
    }

    // ==========================================
    // Custom Craft Targets & Unified Planner JS
    // ==========================================
    let isCustomTargetsLoading = false;
    async function loadCustomTargets() {
      if (isCustomTargetsLoading) return;
      isCustomTargetsLoading = true;
      try {
        const res = await fetch('/api/custom_targets');
        const data = await res.json();
        const targets = data.targets || [];
        const container = document.getElementById('custom-targets-container');
        const badge = document.getElementById('custom-target-count-badge');
        const pill = document.getElementById('tab-custom-craft-pill');

        if (badge) badge.innerText = `${targets.length}개 등록됨`;
        if (pill) pill.innerText = `${targets.length}개 등록`;

        if (!container) return;
        if (targets.length === 0) {
          container.innerHTML = '<p style="color: var(--text-muted); font-size: var(--fs-sm); grid-column: 1/-1;"><span class="sym-prefix">&gt;</span>등록된 지정 제작 목표가 없습니다. 위 입력창에서 제작할 아이템을 등록하세요.</p>';
          return;
        }

        let html = '';
        targets.forEach(t => {
          const isDone = t.is_completed || (t.current >= t.goal);
          const percent = Math.min(100, Math.round((t.current / (t.goal || 1)) * 100));
          const gaugeStr = makeTextGauge(t.current, t.goal, 5);
          const safeName = (t.item_name || '').replace(/'/g, "\\'");

          let recHtml = '';
          const rec = t.recipe || {};
          const ingKeys = Object.keys(rec);
          if (ingKeys.length > 0) {
            const ingListStr = ingKeys.map(k => `${k} ${rec[k]}개`).join(', ');
            recHtml = `
              <div style="border-top: 1px solid var(--border-line); border-bottom: 1px solid var(--border-line); padding: 4px 0; font-size: var(--fs-sm); display: flex; justify-content: space-between; align-items: center; gap: 6px;">
                <span style="color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="1회 제작 재료: ${ingListStr}">
                  <span class="sym-prefix">&gt;</span>1회 재료: <strong style="color: var(--text-main);">${ingListStr}</strong>
                </span>
                <button class="btn btn-sm" onclick="openRecipeModal('${safeName}')">레시피</button>
              </div>
            `;
          } else {
            recHtml = `
              <div style="border-top: 1px solid var(--border-line); border-bottom: 1px solid var(--border-line); padding: 4px 0; font-size: var(--fs-sm); display: flex; justify-content: space-between; align-items: center; gap: 6px;">
                <span style="color: var(--text-main);"><span class="sym-prefix">&gt;</span>[!] 재료 미확인 (클릭하여 직접 입력)</span>
                <button class="btn btn-sm" onclick="openRecipeModal('${safeName}')">재료 등록</button>
              </div>
            `;
          }

          html += `
            <div class="target-card ${isDone ? 'target-done' : ''}">
              <div style="display: flex; justify-content: space-between; align-items: flex-start;">
                <div>
                  <div style="font-weight: 700; font-size: var(--fs-md); color: var(--text-title);">${t.item_name}</div>
                  <div class="mono-num" style="font-size: var(--fs-sm); color: var(--text-muted);"><span class="sym-prefix">&gt;</span>가방: ${t.inventory_count || 0}개 | 개인창고: ${t.char_storage_count || 0}개 | 공용창고: ${t.account_storage_count || 0}개 (총 ${t.current || 0}개)</div>
                </div>
                <div style="display: flex; align-items: center; gap: 6px;">
                  <span class="${isDone ? 'badge' : 'badge-dim'} mono-num">
                    ${isDone ? '=== 달성 완료 ===' : (t.needed || 0) + '개 부족'}
                  </span>
                  <button class="btn btn-sm" onclick="deleteCustomTarget('${safeName}')" title="삭제">삭제</button>
                </div>
              </div>

              ${recHtml}

              <div style="display: flex; justify-content: space-between; align-items: center; font-size: var(--fs-sm);">
                <span style="color: var(--text-muted);">진행도: <span class="cli-gauge">${gaugeStr}</span></span>
                <span class="mono-num" style="font-weight: 700; color: var(--text-main);">${t.current} / ${t.goal}개 (${percent}%)</span>
              </div>

              <div style="display: flex; justify-content: space-between; align-items: center; font-size: var(--fs-sm); flex-wrap: wrap; gap: 6px; padding-top: 4px;">
                <div style="display: flex; align-items: center; gap: 4px;">
                  <span style="color: var(--text-muted);">보유:</span>
                  <button class="btn btn-sm" onclick="adjustCustomTargetCurrent('${safeName}', ${t.current}, -1)" title="1개 감소">-</button>
                  <strong class="mono-num" style="color: var(--text-main); cursor: pointer; text-decoration: underline dotted;" onclick="promptEditCustomCurrent('${safeName}', ${t.current})" title="수량 클릭하여 직접 수정">${t.current}</strong>
                  <button class="btn btn-sm" onclick="adjustCustomTargetCurrent('${safeName}', ${t.current}, 1)" title="1개 증가">+</button>
                  <button class="btn btn-sm" onclick="updateCustomTargetCurrent('${safeName}', 0)" title="현재 수량 0개로 리셋">0개로</button>
                </div>
                <div style="display: flex; align-items: center; gap: 4px;">
                  <span style="color: var(--text-muted);">목표:</span>
                  <strong class="mono-num" style="color: var(--text-main);">${t.goal}개</strong>
                  <button class="btn btn-sm" onclick="updateCustomTargetGoal('${safeName}', -1)">-1</button>
                  <button class="btn btn-sm" onclick="updateCustomTargetGoal('${safeName}', 1)">+1</button>
                </div>
              </div>
            </div>
          `;
        });
        container.innerHTML = html;
      } catch (e) {
        console.error('loadCustomTargets error:', e);
      } finally {
        isCustomTargetsLoading = false;
      }
    }

    async function addManualCustomTarget() {
      const itemEl = document.getElementById('custom-input-item');
      const countEl = document.getElementById('custom-input-count');
      if (!itemEl || !countEl) return;
      const itemName = itemEl.value.trim();
      const count = parseInt(countEl.value, 10);
      if (!itemName) {
        alert('아이템명을 입력해주세요.');
        return;
      }
      if (isNaN(count) || count <= 0) {
        alert('올바른 목표 수량을 입력해주세요.');
        return;
      }

      try {
        const res = await fetch('/api/add_custom_target', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ item_name: itemName, goal: count })
        });
        const d = await res.json();
        if (d.error) {
          alert('등록 실패: ' + d.error);
        } else {
          itemEl.value = '';
          loadCustomTargets();
          loadCustomPlan();
        }
      } catch (e) {
        alert('요청 오류: ' + e);
      }
    }

    async function quickAddCustomTarget(itemName, count) {
      try {
        const res = await fetch('/api/add_custom_target', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ item_name: itemName, goal: count })
        });
        const d = await res.json();
        if (d.error) {
          alert('등록 실패: ' + d.error);
        } else {
          loadCustomTargets();
          loadCustomPlan();
        }
      } catch (e) {
        alert('요청 오류: ' + e);
      }
    }

    async function updateCustomTargetGoal(itemName, delta) {
      try {
        const res = await fetch('/api/custom_targets');
        const data = await res.json();
        const found = (data.targets || []).find(t => t.item_name === itemName);
        if (found) {
          const newGoal = Math.max(1, (found.goal || 1) + delta);
          await fetch('/api/add_custom_target', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ item_name: itemName, goal: newGoal })
          });
          loadCustomTargets();
          loadCustomPlan();
        }
      } catch (e) {
        console.error('updateCustomTargetGoal error:', e);
      }
    }

    async function updateCustomTargetCurrent(itemName, currentVal) {
      try {
        const res = await fetch('/api/update_custom_target_current', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ item_name: itemName, current: parseInt(currentVal, 10) })
        });
        const d = await res.json();
        if (d.status === 'success') {
          pollLogs();
          loadCustomTargets();
          loadCustomPlan();
        } else {
          alert('수량 변경 실패: ' + (d.error || '알 수 없는 오류'));
        }
      } catch (e) {
        alert('요청 실패: ' + e);
      }
    }

    function adjustCustomTargetCurrent(itemName, currentVal, delta) {
      const nextVal = Math.max(0, currentVal + delta);
      updateCustomTargetCurrent(itemName, nextVal);
    }

    function promptEditCustomCurrent(itemName, currentVal) {
      const val = prompt(`'${itemName}'의 현재 보유(또는 제작 완료) 수량을 입력하세요:`, currentVal);
      if (val !== null) {
        const num = parseInt(val, 10);
        if (!isNaN(num) && num >= 0) {
          updateCustomTargetCurrent(itemName, num);
        } else {
          alert('0 이상의 숫자를 입력해주세요.');
        }
      }
    }

    async function resetAllCustomTargetsCurrent() {
      if (!confirm('등록된 모든 지정 제작 목표의 현재 보유 수량을 0개로 초기화할까요?\n\n(모든 목표 아이템을 처음부터 다시 제작하고자 할 때 유용합니다)')) return;
      try {
        const res = await fetch('/api/reset_custom_targets_current', { method: 'POST' });
        const d = await res.json();
        if (d.status === 'success') {
          pollLogs();
          loadCustomTargets();
          loadCustomPlan();
        } else {
          alert('초기화 실패: ' + (d.error || '알 수 없는 오류'));
        }
      } catch (e) {
        alert('초기화 요청 실패: ' + e);
      }
    }

    async function syncCustomTargetsInventory() {
      try {
        const res = await fetch('/api/sync_custom_targets_inventory', { method: 'POST' });
        const d = await res.json();
        if (d.status === 'success') {
          pollLogs();
          loadCustomTargets();
          loadCustomPlan();
        } else {
          alert('동기화 실패: ' + (d.error || '알 수 없는 오류'));
        }
      } catch (e) {
        alert('동기화 요청 실패: ' + e);
      }
    }

    async function deleteCustomTarget(itemName) {
      if (!confirm(`'${itemName}' 목표를 삭제하시겠습니까?`)) return;
      try {
        await fetch('/api/delete_custom_target', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ item_name: itemName })
        });
        loadCustomTargets();
        loadCustomPlan();
      } catch (e) {
        alert('삭제 실패: ' + e);
      }
    }

    async function clearCustomTargets() {
      if (!confirm('등록된 모든 지정 제작 목표를 비우시겠습니까?')) return;
      try {
        await fetch('/api/clear_custom_targets', { method: 'POST' });
        loadCustomTargets();
        loadCustomPlan();
      } catch (e) {
        alert('초기화 실패: ' + e);
      }
    }

    // ==========================================
    // Recipe Manager & Modal JavaScript
    // ==========================================
    let currentRecipeModalItem = '';

    async function openRecipeModal(itemName) {
      currentRecipeModalItem = itemName || '';
      const overlay = document.getElementById('recipe-modal-overlay');
      const inputEl = document.getElementById('recipe-modal-item-name');
      const titleEl = document.getElementById('recipe-modal-title');
      const container = document.getElementById('recipe-modal-rows-container');
      const msgEl = document.getElementById('recipe-modal-msg');
      if (msgEl) msgEl.innerText = '';

      if (inputEl) inputEl.value = currentRecipeModalItem;
      if (titleEl) titleEl.innerText = currentRecipeModalItem ? `// 제작 레시피 설정: '${currentRecipeModalItem}'` : '// 제작 레시피 설정 / 수정';
      if (overlay) overlay.classList.add('active');

      if (currentRecipeModalItem) {
        await loadRecipeForCurrentInput();
      } else {
        if (container) container.innerHTML = '';
        addRecipeModalRow('', 1);
      }
    }

    function openRecipeManagerModal() {
      openRecipeModal('');
    }

    function closeRecipeModal() {
      const overlay = document.getElementById('recipe-modal-overlay');
      if (overlay) overlay.classList.remove('active');
    }

    function handleRecipeModalOverlayClick(e) {
      if (e.target && e.target.id === 'recipe-modal-overlay') {
        closeRecipeModal();
      }
    }

    async function loadRecipeForCurrentInput() {
      const inputEl = document.getElementById('recipe-modal-item-name');
      const container = document.getElementById('recipe-modal-rows-container');
      const msgEl = document.getElementById('recipe-modal-msg');
      if (!inputEl || !container) return;
      const itemName = inputEl.value.trim();
      if (!itemName) return;

      container.innerHTML = '<div style="color:var(--text-muted); font-size:var(--fs-sm); padding:8px;"><span class="sym-prefix">&gt;</span>레시피 조회 중...</div>';
      try {
        const res = await fetch(`/api/recipe?name=${encodeURIComponent(itemName)}`);
        const data = await res.json();
        container.innerHTML = '';
        const ings = data.ingredients || {};
        const keys = Object.keys(ings);
        if (keys.length === 0) {
          if (msgEl) msgEl.innerHTML = '<span class="sym-prefix">&gt;</span>등록된 레시피가 없습니다. 아래에 재료를 추가해주세요.';
          addRecipeModalRow('', 1);
        } else {
          if (msgEl) msgEl.innerHTML = `<span class="sym-prefix">&gt;</span>총 ${keys.length}개 재료 로드됨`;
          keys.forEach(k => {
            addRecipeModalRow(k, ings[k]);
          });
        }
      } catch (e) {
        container.innerHTML = `<div style="color:var(--text-main); font-size:var(--fs-sm); padding:8px;">[!] 조회 실패: ${e}</div>`;
      }
    }

    function addRecipeModalRow(name, count) {
      const container = document.getElementById('recipe-modal-rows-container');
      if (!container) return;
      const row = document.createElement('div');
      row.className = 'recipe-modal-row';
      row.style.cssText = 'display: flex; gap: 8px; align-items: center; padding: 6px 0; border-bottom: 1px solid var(--border-line);';
      row.innerHTML = `
        <input type="text" list="all-items-datalist" class="recipe-row-name" value="${(name || '').replace(/"/g, '&quot;')}" placeholder="재료명 (예: 달걀, 물이 든 병)" style="flex: 2; padding: 6px 10px;">
        <div style="display: flex; align-items: center; gap: 4px;">
          <input type="number" class="recipe-row-qty mono-num" value="${count || 1}" min="1" max="999" style="width: 65px; padding: 6px 8px; text-align: center;">
          <span style="font-size: var(--fs-sm); color: var(--text-muted);">개</span>
        </div>
        <button type="button" class="btn btn-sm btn-boxed" onclick="this.closest('.recipe-modal-row').remove()" title="이 재료 삭제">삭제</button>
      `;
      container.appendChild(row);
    }

    async function saveRecipeFromModal() {
      const inputEl = document.getElementById('recipe-modal-item-name');
      const container = document.getElementById('recipe-modal-rows-container');
      const msgEl = document.getElementById('recipe-modal-msg');
      if (!inputEl || !container) return;
      const itemName = inputEl.value.trim();
      if (!itemName) {
        alert('대상 아이템명을 입력해주세요.');
        inputEl.focus();
        return;
      }

      const rows = container.querySelectorAll('.recipe-modal-row');
      const ingredients = {};
      rows.forEach(r => {
        const nameInput = r.querySelector('.recipe-row-name');
        const qtyInput = r.querySelector('.recipe-row-qty');
        if (nameInput && qtyInput) {
          const ingName = nameInput.value.trim();
          const qty = parseInt(qtyInput.value, 10);
          if (ingName && !isNaN(qty) && qty > 0) {
            ingredients[ingName] = qty;
          }
        }
      });

      if (Object.keys(ingredients).length === 0) {
        if (!confirm(`'${itemName}'의 필요 재료가 아무것도 입력되지 않았습니다. 빈 레시피로 저장할까요?`)) return;
      }

      try {
        const res = await fetch('/api/update_recipe', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ item_name: itemName, ingredients: ingredients })
        });
        const d = await res.json();
        if (d.status === 'success') {
          if (msgEl) msgEl.innerText = '> === 저장 완료 ===';
          closeRecipeModal();
          pollLogs();
          loadItemCatalog();
          loadCustomTargets();
          loadCustomPlan();
          loadDeliveryTargets();
          loadBatchPlan();
        } else {
          alert('레시피 저장 실패: ' + (d.error || '알 수 없는 오류'));
        }
      } catch (e) {
        alert('저장 요청 실패: ' + e);
      }
    }

    let hideSatisfiedBomRows = (safeGetStorage('mabi_hide_satisfied_bom', 'false') === 'true');

    function applyHideSatisfiedBomUI() {
      const b1 = document.getElementById('bom-table-body');
      const b2 = document.getElementById('custom-batch-materials-tbody');
      if (b1) b1.classList.toggle('hide-satisfied-rows', hideSatisfiedBomRows);
      if (b2) b2.classList.toggle('hide-satisfied-rows', hideSatisfiedBomRows);

      const ic1 = document.getElementById('icon-hide-satisfied-delivery');
      const ic2 = document.getElementById('icon-hide-satisfied-custom');
      [ic1, ic2].forEach(ic => {
        if (ic) {
          ic.innerText = hideSatisfiedBomRows ? '■' : '□';
          ic.style.color = hideSatisfiedBomRows ? 'var(--text-bright)' : 'var(--text-dim)';
        }
      });
    }

    function toggleHideSatisfiedBom() {
      hideSatisfiedBomRows = !hideSatisfiedBomRows;
      safeSetStorage('mabi_hide_satisfied_bom', String(hideSatisfiedBomRows));
      applyHideSatisfiedBomUI();
    }

    async function loadCustomPlan() {
      try {
        const res = await fetch(`/api/custom_plan?alter_order=${encodeURIComponent(currentAlterOrder)}`);
        const plan = await res.json();
        cachedCustomPlan = plan;

        const tbody = document.getElementById('custom-batch-materials-tbody');
        const statusText = document.getElementById('custom-batch-status-text');
        const btnExec = document.getElementById('btn-custom-execute');
        const summaryText = document.getElementById('custom-plan-summary-text');

        if (!tbody) return;
        applyHideSatisfiedBomUI();

        if (plan.error) {
          tbody.innerHTML = `<tr><td colspan="5" style="text-align: center; color: var(--text-main); padding: 20px;">[!] 오류: ${plan.error}</td></tr>`;
          if (statusText) statusText.innerText = '계획 로드 실패';
          return;
        }

        const materials = plan.intermediate_requirements || plan.materials || [];
        const rawMaterials = plan.raw_materials || [];

        if (materials.length === 0 && rawMaterials.length === 0) {
          tbody.innerHTML = `<tr><td colspan="5" style="text-align: center; color: var(--text-muted); padding: 24px;"><span class="sym-prefix">&gt;</span>등록된 지정 제작 목표가 없습니다. 위에서 목표를 추가하세요.</td></tr>`;
          if (statusText) statusText.innerText = '등록된 목표 없음';
          if (btnExec) {
            btnExec.disabled = true;
            btnExec.innerText = '지정 제작 일괄 실행';
            btnExec.className = 'btn btn-solid-core';
          }
          if (summaryText) summaryText.innerHTML = '';
          return;
        }

        let rowsHtml = '';
        materials.forEach(m => {
          const shortFac = formatFacilityShort(m.facility);
          const worksNeeded = (m.works_to_queue !== undefined) ? m.works_to_queue : (m.works_needed || 0);
          const deficit = (m.deficit !== undefined) ? m.deficit : (m.net_needed || 0);
          const queuedCnt = m.queued_works_count || m.in_progress_count || 0;
          const compCnt = m.completed_works_count || 0;

          const queuedBadge = queuedCnt > 0 ? `<span class="mono-num" style="color:var(--text-muted); font-size:11px; margin-left:4px;">(대기 ${queuedCnt}회)</span>` : '';
          const compBadge = compCnt > 0 ? `<span class="mono-num" style="color:var(--text-main); font-size:11px; margin-left:4px;">(수령가능 ${compCnt}회)</span>` : '';

          const isSatisfied = (m.status === 'satisfied' || (deficit === 0 && worksNeeded === 0));
          let statusBadge = '';
          if (isSatisfied) {
            statusBadge = '<span style="font-weight:700; white-space:nowrap;">=== 준비 완료 ===</span>' + compBadge;
          } else if (m.status === 'ready_to_collect' || (compCnt > 0 && worksNeeded === 0)) {
            statusBadge = `<span class="badge mono-num" style="white-space:nowrap;">수령 가능 (${compCnt}회)</span>`;
          } else if (m.status === 'in_queue' || (queuedCnt > 0 && worksNeeded === 0)) {
            statusBadge = `<span class="mono-num" style="color:var(--text-main); font-weight:700; white-space:nowrap;">대기열 (${queuedCnt}회)</span>` + compBadge;
          } else if (worksNeeded > 0) {
            statusBadge = `<span class="mono-num" style="color:var(--text-main); font-weight:700; white-space:nowrap;">> ${worksNeeded}회 가공 필요</span>` + queuedBadge + compBadge;
          } else {
            statusBadge = '<span style="font-weight:700; white-space:nowrap;">=== 준비 완료 ===</span>';
          }

          const invCnt = m.inventory_count || 0;
          const storeCnt = m.storage_count || 0;
          const bagStorageText = `<span class="mono-num">${invCnt}개</span><span class="mono-num" style="color:var(--text-muted); font-size:11px;">(${storeCnt}개)</span>`;
          const mLockTag = m.is_locked ? ` <span class="badge-dim" style="color:#F59E0B;" title="인게임 잠금 설정된 재료">[잠금주의]</span>` : '';
          const mWarnTag = m.readiness_warning ? ` <span class="badge-dim" style="color:#EF4444;" title="${m.readiness_warning}">[설비/스킬 부족]</span>` : '';

          rowsHtml += `
            <tr class="row-alter ${isSatisfied ? 'row-satisfied' : ''}">
              <td style="font-weight:700; color:var(--text-main); white-space:nowrap;"><span class="bom-dot dot-alter"></span>${m.item_name}${mLockTag}${mWarnTag}</td>
              <td style="text-align:center; white-space:nowrap;"><span class="badge-dim">${shortFac}</span></td>
              <td class="mono-num" style="font-weight:700; color:var(--text-main); text-align:right; white-space:nowrap;">${m.total_needed}개</td>
              <td style="text-align:center; white-space:nowrap;">${bagStorageText}</td>
              <td style="white-space:nowrap;">${statusBadge}</td>
            </tr>
          `;
        });

        // Raw materials
        if (rawMaterials.length > 0) {
          rawMaterials.forEach(rm => {
            let statusBadge = '';
            const isSpecial = rm.is_special_obtain || !rm.is_gatherable;
            const isRmSatisfied = (rm.deficit === 0);
            if (isRmSatisfied) {
              statusBadge = '<span style="font-weight:700; white-space:nowrap;">=== 준비 완료 ===</span>';
            } else if (isSpecial) {
              statusBadge = `<span class="mono-num" style="color:var(--text-muted); font-weight:700; white-space:nowrap;">> 별도 확보 필요 (${rm.deficit}개 부족)</span>`;
            } else if (rm.tool_ok) {
              statusBadge = `<span class="mono-num" style="color:var(--text-main); font-weight:700; white-space:nowrap;">> ${rm.deficit}개 자동 채집</span>`;
            } else {
              statusBadge = `<span class="mono-num" style="color:var(--text-main); font-weight:700; white-space:nowrap;">[!] 도구 필요 (${rm.deficit}개 부족)</span>`;
            }

            const rmInvCnt = rm.inventory_count || 0;
            const rmStoreCnt = rm.storage_count || 0;
            const rmBagStorageText = `<span class="mono-num">${rmInvCnt}개</span><span class="mono-num" style="color:var(--text-muted); font-size:11px;">(${rmStoreCnt}개)</span>`;

            const typeBadge = isSpecial
              ? '<span class="badge-dim">기타</span>'
              : '<span class="badge-dim">채집</span>';
            const rowClass = isSpecial ? 'row-special' : 'row-gather';

            rowsHtml += `
              <tr class="${rowClass} ${isRmSatisfied ? 'row-satisfied' : ''}">
                <td style="font-weight:700; color:var(--text-main); white-space:nowrap;"><span class="bom-dot ${isSpecial ? 'dot-special' : 'dot-gather'}"></span>${rm.item_name}</td>
                <td style="text-align:center; white-space:nowrap;">${typeBadge}</td>
                <td class="mono-num" style="font-weight:700; color:var(--text-main); text-align:right; white-space:nowrap;">${rm.total_needed}개</td>
                <td style="text-align:center; white-space:nowrap;">${rmBagStorageText}</td>
                <td style="white-space:nowrap;">${statusBadge}</td>
              </tr>
            `;
          });
        }

        tbody.innerHTML = rowsHtml;

        // Button state and text
        if (btnExec && statusText) {
          const craftableNote = (plan.craftable_tasks_count > 0) ? ` (즉시 제작 가능: ${plan.craftable_tasks_count}건)` : '';
          const neededRawGather = (plan.raw_materials || []).filter(r => r.deficit > 0 && r.is_gatherable);
          const neededSpecial = (plan.raw_materials || []).filter(r => r.deficit > 0 && (r.is_special_obtain || !r.is_gatherable));
          const gatherNote = (neededRawGather.length > 0) ? ` / 채집 ${neededRawGather.length}종` : '';
          const specialNote = (neededSpecial.length > 0) ? ` (별도 확보 ${neededSpecial.length}종 필요)` : '';

          btnExec.className = 'btn btn-solid-core';
          if (plan.all_completed) {
            btnExec.innerText = '모든 지정 목표 제작 완료';
            btnExec.disabled = true;
            statusText.innerHTML = '<span style="color:var(--text-main); font-weight:700;">모든 목표 달성 완료</span>';
          } else if (plan.can_craft_immediately) {
            btnExec.innerText = '지정 제작 일괄 실행 (최종 제작)';
            btnExec.disabled = isCurrentlyBusy;
            statusText.innerHTML = `<span style="color:var(--text-main); font-weight:700;">모든 재료 준비 완료${specialNote}</span>`;
          } else if (plan.total_works_to_queue > 0) {
            btnExec.innerText = `지정 제작 일괄 실행 (가공 ${plan.total_works_to_queue}회${gatherNote})`;
            btnExec.disabled = isCurrentlyBusy;
            statusText.innerHTML = `<span style="color:var(--text-main); font-weight:700;">가공 ${plan.total_works_to_queue}회 등록 필요${gatherNote}${craftableNote}${specialNote}</span>`;
          } else if (neededRawGather.length > 0) {
            btnExec.innerText = `지정 제작 일괄 실행 (채집 ${neededRawGather.length}종)`;
            btnExec.disabled = isCurrentlyBusy;
            statusText.innerHTML = `<span style="color:var(--text-main); font-weight:700;">필드 채집 ${neededRawGather.length}종 필요${craftableNote}${specialNote}</span>`;
          } else if (plan.craftable_tasks_count > 0) {
            btnExec.innerText = `지정 제작 일괄 실행 (즉시 제작 ${plan.craftable_tasks_count}건)`;
            btnExec.disabled = isCurrentlyBusy;
            statusText.innerHTML = `<span style="color:var(--text-main); font-weight:700;">${plan.craftable_tasks_count}건 즉시 제작 가능</span>`;
          } else {
            btnExec.innerText = '지정 제작 일괄 실행 (대기열 진행 중)';
            btnExec.disabled = isCurrentlyBusy;
            statusText.innerHTML = `<span style="color:var(--text-muted); font-weight:700;">대기열 가공 완료 대기 중</span>`;
          }
        }

        // Summary Text
        if (summaryText) {
          const neededRawGather = (plan.raw_materials || []).filter(r => r.deficit > 0);
          const rawText = neededRawGather.length > 0 ? `필드 채집 <strong class="mono-num">${neededRawGather.length}종</strong>` : '채집 불필요';
          const alterText = plan.total_works_to_queue > 0 ? `가공 <strong class="mono-num">${plan.total_works_to_queue}회</strong>` : '가공 불필요';
          summaryText.innerHTML = `<span class="sym-prefix">&gt;</span><span>총 분석 결과: ${rawText} | ${alterText} | 제작 <strong class="mono-num">${(plan.custom_tasks || plan.tasks || plan.targets || []).length}건</strong></span>`;
        }

        // Facility Slots
        const slotsSec = document.getElementById('custom-facility-slots-section');
        const slotsContainer = document.getElementById('custom-facility-slots-container');
        const warnBanner = document.getElementById('custom-slot-warnings-banner');
        const warnList = document.getElementById('custom-slot-warnings-list');
        const summaryBadge = document.getElementById('custom-slot-summary-badge');

        const facList = plan.facility_slots || [];
        const warnings = plan.slot_warnings || [];

        if (slotsSec && slotsContainer) {
          if (facList.length > 0) {
            slotsSec.style.display = 'block';
            let html = '';
            facList.forEach(f => {
              const max = f.max_slots || 7;
              const avail = f.available_now;
              const needed = f.new_works_needed || 0;
              const isOver = f.overflow > 0;
              
              html += `
                <div style="flex: 1 1 calc(33.333% - 8px); min-width: 185px; background: var(--bg-sub-panel); border: 1px solid ${isOver ? 'var(--border-focus)' : 'var(--line-subtle)'}; border-radius: 0; padding: 8px 10px; box-shadow: var(--card-box-shadow);">
                  <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                    <span style="font-weight: 700; font-size: var(--fs-sm); color: var(--text-title);">${f.facility}</span>
                    <span class="mono-num" style="font-size: var(--fs-sm); font-weight: 700; color: ${avail > 0 ? 'var(--text-main)' : 'var(--text-muted)'};">
                      여유 ${avail}/${max}
                    </span>
                  </div>
                  <div class="mono-num" style="font-size: var(--fs-sm); color: var(--text-muted); display: flex; justify-content: space-between; flex-wrap: wrap; gap: 4px;">
                    <span>진행:${f.in_progress} | 완료:${f.completed}</span>
                    ${needed > 0 ? `<span style="font-weight: 700; color: var(--text-main);">필요 +${needed}</span>` : '<span style="color: var(--text-dim);">필요 없음</span>'}
                  </div>
                  ${f.overflow > 0 ? `<div class="mono-num" style="font-size: var(--fs-sm); color: var(--text-main); margin-top: 3px; font-weight: 700;">> [!] ${f.overflow}회 초과 (단계적 진행)</div>` : ''}
                </div>
              `;
            });
            slotsContainer.innerHTML = html;
            if (summaryBadge) {
              summaryBadge.innerHTML = plan.has_slot_issue 
                ? '<span style="color: var(--text-main); font-weight: 700;">[!] 일부 시설 슬롯 초과 예상</span>' 
                : '<span style="color: var(--text-muted); font-weight: 600;">--- 전 시설 슬롯 여유 충분 ---</span>';
            }
          } else {
            slotsSec.style.display = 'none';
          }
        }

        if (warnBanner && warnList) {
          if (warnings.length > 0) {
            warnBanner.style.display = 'block';
            warnList.innerHTML = warnings.map(w => `<div style="margin-bottom: 3px;"><span class="sym-prefix">&gt;</span>${w}</div>`).join('');
          } else {
            warnBanner.style.display = 'none';
          }
        }
        updateMasterCommandBar();
      } catch (e) {
        console.error('loadCustomPlan error:', e);
      }
    }

    async function executeCustomPipeline() {
      if (!checkWeightSafetyConfirm()) {
        return;
      }
      const modeTitle = currentGoalFulfillmentMode ? '목표 완수 모드' : '지정 아이템 일괄 제작';
      const modeExpl = currentGoalFulfillmentMode
        ? '가공품 완료 시 즉시 수령 -> 다음 티어 가공 -> 목표 완제품 최종 제작까지 100% 완수할 때까지 PC 절전 방지 상태로 계속 진행합니다.'
        : '원자재 자동 채집 -> 시설 가공 일괄 등록 -> 최종 아이템 제작 1회 파이프라인을 수행합니다.';
      const orderDesc = currentAlterOrder === 'low_tier' ? '하위 티어 우선 (기초 재료부터)' : '상위 티어 우선 (오래 걸리는 가공부터)';
      const etaInfo = currentGoalFulfillmentMode ? getPipelineEtaInfo('custom_craft') : null;
      const etaLine = etaInfo ? `\n• 예상 완수 시각: ${etaInfo.durStr} (${etaInfo.timeStr}경 완수 예정)` : '';
      if (confirm(`[${modeTitle}]\n${modeExpl}${etaLine}\n\n• 가공 우선순위: ${orderDesc}\n\n실행하시겠습니까?`)) {
        isCurrentlyBusy = true;
        if (typeof applyBusyUI === 'function') applyBusyUI(true, `지정 아이템 일괄 최적화 제작 준비 중... [${currentGoalFulfillmentMode ? '목표 완수 모드' : '1회차 실행'}]`);
        try {
          const res = await fetch('/api/execute_custom', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ alter_order: currentAlterOrder, goal_fulfillment_mode: currentGoalFulfillmentMode })
          });
          const d = await res.json();
          if (d.error) {
            isCurrentlyBusy = false;
            if (typeof applyBusyUI === 'function') applyBusyUI(false);
            alert('오류: ' + d.error);
          } else {
            pollLogs();
            updateStatus();
          }
        } catch (e) {
          isCurrentlyBusy = false;
          if (typeof applyBusyUI === 'function') applyBusyUI(false);
          alert('요청 실패: ' + e);
        }
      }
    }

    // 4. Status, Quests, Manual Crafting
    let statusFailCount = 0;
    let wasBusy = false;

    function applyBusyUI(busy, taskDesc) {
      const busyBox = document.getElementById('busy-box');
      const busyText = document.getElementById('busy-text');
      const produceBtn = document.getElementById('btn-produce');
      const batchBtn = document.getElementById('btn-batch-execute');
      const batchAbortBtn = document.getElementById('btn-batch-abort');
      const quickAlterBtn = document.getElementById('btn-quick-alter-execute');
      const customExecuteBtn = document.getElementById('btn-custom-execute');
      const abortBtn = document.getElementById('btn-quick-alter-abort');
      const customAbortBtn = document.getElementById('btn-custom-abort');

      const mcbBar = document.getElementById('master-command-bar');
      const mcbIdle = document.getElementById('mcb-box-idle');
      const mcbRunning = document.getElementById('mcb-box-running');
      const mcbRunningTitle = document.getElementById('mcb-running-title');
      const mcbRunningDesc = document.getElementById('mcb-running-desc');
      const mcbBtnStart = document.getElementById('mcb-btn-start');

      if (mcbBar) mcbBar.classList.toggle('running', !!busy);

      if (busy) {
        if (busyBox) busyBox.style.display = 'inline-flex';
        if (busyText && taskDesc) busyText.innerText = taskDesc;
        if (produceBtn) produceBtn.disabled = true;
        if (batchBtn) batchBtn.disabled = true;
        if (quickAlterBtn) quickAlterBtn.disabled = true;
        if (customExecuteBtn) customExecuteBtn.disabled = true;
        if (batchAbortBtn) batchAbortBtn.style.display = 'inline-flex';
        if (abortBtn) abortBtn.style.display = 'inline-flex';
        if (customAbortBtn) customAbortBtn.style.display = 'inline-flex';

        if (mcbIdle) mcbIdle.style.display = 'none';
        if (mcbRunning) mcbRunning.style.display = 'flex';
        if (mcbRunningTitle) mcbRunningTitle.innerText = currentGoalFulfillmentMode ? '목표 완수 모드 진행 중...' : '작업 진행 중...';
        if (mcbRunningDesc && taskDesc) mcbRunningDesc.innerText = `> ${taskDesc}`;
        if (mcbBtnStart) mcbBtnStart.disabled = true;
      } else {
        if (busyBox) busyBox.style.display = 'none';
        if (produceBtn) produceBtn.disabled = false;
        if (batchBtn) batchBtn.disabled = false;
        if (quickAlterBtn && cachedQuickPlan) quickAlterBtn.disabled = !cachedQuickPlan.can_start;
        if (customExecuteBtn && cachedCustomPlan) customExecuteBtn.disabled = cachedCustomPlan.all_completed;
        if (batchAbortBtn) batchAbortBtn.style.display = 'none';
        if (abortBtn) abortBtn.style.display = 'none';
        if (customAbortBtn) customAbortBtn.style.display = 'none';

        if (mcbIdle) mcbIdle.style.display = 'flex';
        if (mcbRunning) mcbRunning.style.display = 'none';
        if (mcbBtnStart) mcbBtnStart.disabled = false;
        updateMasterCommandBar();
      }
    }

    let isUpdatingStatus = false;
    async function updateStatus() {
      if (isUpdatingStatus) return;
      isUpdatingStatus = true;
      const statusPill = document.getElementById('pill-status');
      const wingsPill = document.getElementById('pill-wings');
      const weightPill = document.getElementById('pill-weight');

      try {
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 12000);
        const res = await fetch('/api/status', { signal: controller.signal });
        clearTimeout(timeoutId);
        const data = await res.json();
        statusFailCount = 0;

        if (data.delivery_character) {
          const dc = data.delivery_character;
          const prevActId = lastKnownActiveCharId;
          const prevMatchId = lastKnownMatchedIngameCharId;
          renderDeliveryCharacterBar(dc);
          if ((prevActId && dc.active_character_id !== prevActId) || (prevMatchId !== dc.matched_ingame_character_id)) {
            if (!data.is_busy) {
              loadDeliveryTargets();
              loadBatchPlan();
            }
          }
        }

        if (data.homework) {
          try {
            onHomeworkStateReceived(data.homework, data.omen_barrier, data, true);
          } catch (hwErr) {
            console.error('onHomeworkStateReceived error:', hwErr);
          }
        } else if (data.omen_barrier) {
          try {
            updateOmenBarrierUI(data.omen_barrier);
          } catch (obErr) {}
        }

        const currentlyBusy = !!data.is_busy;
        if (!currentlyBusy && wasBusy) {
          // Task just finished! Play sound if enabled
          if (currentSoundAlarmEnabled) {
            playChime(true);
          }
          // Stagger refreshes to avoid CLI process queue contention
          try { loadAlteringQueue(); } catch(e) {}
          setTimeout(() => {
            try {
              if (currentMainMode === 'delivery') {
                loadDeliveryTargets();
                loadBatchPlan();
              } else if (currentMainMode === 'custom_craft') {
                loadCustomTargets();
                loadCustomPlan();
              } else {
                loadQuickAlterPlan();
              }
              updateMasterCommandBar();
            } catch(e) {}
          }, 300);
        }
        wasBusy = currentlyBusy;

        const cliAlertBanner = document.getElementById('cli-connection-alert');
        const alertTitle = document.getElementById('cli-alert-title');
        const alertDesc = document.getElementById('cli-alert-desc');
        const alertIcon = document.getElementById('cli-alert-icon');
        const alertPathBox = document.getElementById('cli-alert-path-box');

        if (data.connected && data.character) {
          const char = data.character;
          const jobName = char.EnabledCombatJobDisplayName || char.CombatJobDisplayName || char.JobDisplayName || char.CombatJob || char.Job || '캐릭터';
          const realmName = char.RealmName || '';
          const levelNum = String(char.Level ?? 0).trim();
          const charLabel = `${jobName} (Lv.<span style="font-variant-numeric: tabular-nums;">${levelNum}</span>)`;
          const fullDesc = `${jobName} (Lv.${levelNum})${realmName ? ` [${realmName} 서버]` : ''}`;

          if (statusPill) {
            if (currentlyBusy) {
              statusPill.className = "pill warning";
              statusPill.innerHTML = `<span class="cli-spinner">${getCurrentSpinnerText()}</span> ${charLabel}`;
              statusPill.title = `${fullDesc} | ${data.current_task || '작업 진행 중'}`;
            } else {
              statusPill.className = "pill online";
              statusPill.innerHTML = charLabel;
              statusPill.title = `${fullDesc} | 정상 연결됨 (클릭하여 설정 확인)`;
            }
          }
          const activityPill = document.getElementById('pill-activity');
          if (activityPill) {
            const act = data.activity || {};
            let actText = '대기 중';
            if (currentlyBusy && data.current_task) {
              if (data.current_task.includes('채집')) actText = '자동 채집 중';
              else if (data.current_task.includes('가공') || data.current_task.includes('수령')) actText = '시설 가공·수령 중';
              else if (data.current_task.includes('제작')) actText = '아이템 제작 중';
              else actText = '파이프라인 가동 중';
            } else if (act.IsGathering) {
              actText = `채집 중${act.TargetName ? ' (' + act.TargetName + ')' : ''}`;
            } else if (act.IsCrafting) {
              actText = '제작 중';
            } else if (act.IsMoving || act.IsAutoMoving) {
              actText = '이동 중';
            } else if (act.CurrentState && act.CurrentState !== 'Idle' && act.CurrentState !== 'None') {
              actText = String(act.CurrentState);
            }
            activityPill.innerHTML = `상태: <span style="color: ${currentlyBusy || actText !== '대기 중' ? 'var(--text-bright)' : 'var(--text-muted)'}; font-weight: ${currentlyBusy || actText !== '대기 중' ? '700' : '400'};">${actText}</span>`;
          }
          if (cliAlertBanner) cliAlertBanner.style.display = 'none';
          if (wingsPill) {
            wingsPill.innerHTML = `날개 <span class="mono-num">${(data.wings || 0).toLocaleString()}</span>`;
          }
          const inv = data.inventory || {};
          const curWeight = inv.CurrentInventoryWeightAsDecimal || inv.CurrentInventoryWeight || 0;
          const maxWeight = inv.MaxInventoryWeightAsDecimal || inv.MaxInventoryWeight || 0;
          const wasWeightOver = currentIsWeightOver;
          currentWeightRatio = maxWeight > 0 ? (curWeight / maxWeight) : 0;
          currentIsWeightOver = (maxWeight > 0 && currentWeightRatio >= 0.98);
          if (weightPill) {
            const ratio = currentWeightRatio;
            const gaugeHtml = maxWeight > 0 ? makeWeightGaugeHtml(curWeight, maxWeight, 5) : '';
            const weightColor = maxWeight > 0 ? getWeightColorByRatio(ratio) : 'var(--text)';
            const isOver = currentIsWeightOver;
            const glowStyle = isOver ? 'text-shadow: 0 0 6px rgba(239, 68, 68, 0.7); font-weight: 700;' : '';
            let overText = '';
            if (ratio >= 1.0) {
              overText = ` <span style="color: #EF4444; font-weight: 700; text-shadow: 0 0 6px rgba(239, 68, 68, 0.7); margin-left: 4px;">[!] 가방 무게 초과 (정리 필요)</span>`;
            } else if (ratio >= 0.98) {
              overText = ` <span style="color: #EF4444; font-weight: 700; text-shadow: 0 0 6px rgba(239, 68, 68, 0.7); margin-left: 4px;">[!] 가방 98% 도달 (작업 중단 주의)</span>`;
            }
            weightPill.innerHTML = `무게 <span class="mono-num" style="color: ${weightColor}; ${glowStyle}">${curWeight}/${maxWeight}</span> ${gaugeHtml}${overText}`;
            weightPill.title = `가방 무게: ${curWeight}/${maxWeight} (${(ratio * 100).toFixed(1)}%${ratio >= 1.0 ? ' - [!] 가방 무게 100% 초과' : (ratio >= 0.98 ? ' - [!] 98% 작업 안전 중지 기준 도달' : '')})`;
          }
          if (wasWeightOver !== currentIsWeightOver) {
            updateMasterCommandBar();
          }
          const envPill = document.getElementById('pill-env');
          if (envPill) {
            const env = data.environment || {};
            const region = char.CurrentRegionDisplayName || char.RegionDisplayName || env.RegionName || env.MapName || '';
            const chNum = char.ChannelNumber || env.ChannelNumber || '';
            const erinnTime = env.ErinnNow || env.ErinnTime || '';
            const weather = env.WeatherDisplayName || env.Weather || '';
            const satiety = char.SatietyValue ?? char.Satiety;
            let envParts = [];
            if (region) envParts.push(`${region}${chNum ? ' ' + chNum + 'ch' : ''}`);
            if (erinnTime) envParts.push(`에린 ${erinnTime}`);
            if (weather) envParts.push(weather);
            if (satiety !== undefined && satiety !== null) envParts.push(`포만감 ${satiety}`);
            envPill.innerHTML = envParts.length > 0 ? envParts.join(' · ') : '에린 환경 연동됨';
          }
        } else {
          const errCode = data.error_code || '';
          let shortStatus = '연결 끊김';

          if (errCode === 'CLI_NOT_FOUND') {
            shortStatus = 'CLI 미발견 (클릭하여 설정)';
            if (alertIcon) alertIcon.innerText = '//';
            if (alertTitle) alertTitle.innerText = '마비노기 모바일 연동 파일(MabinogiMobile_CLI.exe)을 찾을 수 없습니다';
            if (alertDesc) alertDesc.innerHTML = '<span class="sym-prefix">&gt;</span>게임이 C: 드라이브가 아닌 D: 드라이브나 다른 폴더에 설치되어 있다면 아래에 설치 폴더를 입력해주세요.<br><span class="sym-prefix">&gt;</span>또한 게임 내 [환경설정] -> [기타]에서 <strong>[MM AI 에이전트 활성화]</strong>가 ON으로 켜져 있어야 합니다.';
            if (alertPathBox) alertPathBox.style.display = 'flex';
          } else if (errCode === 'OPTION_OFF') {
            shortStatus = 'AI 에이전트 OFF';
            if (alertIcon) alertIcon.innerText = '//';
            if (alertTitle) alertTitle.innerText = '게임 설정에서 [MM AI 에이전트 활성화]가 꺼져 있습니다';
            if (alertDesc) alertDesc.innerHTML = '<span class="sym-prefix">&gt;</span>마비노기 모바일 PC 클라이언트 [환경설정] -> [기타/게임 설정]에서 <strong>[MM AI 에이전트 활성화]</strong>를 ON으로 켜주세요. 옵션을 켜면 즉시 연동됩니다.';
            if (alertPathBox) alertPathBox.style.display = 'none';
          } else if (errCode === 'GAME_OFF') {
            shortStatus = '게임 미실행';
            if (alertIcon) alertIcon.innerText = '//';
            if (alertTitle) alertTitle.innerText = '마비노기 모바일 클라이언트가 실행되어 있지 않습니다';
            if (alertDesc) alertDesc.innerHTML = '<span class="sym-prefix">&gt;</span>마비노기 모바일 PC 클라이언트를 실행하고 캐릭터로 로그인하면 자동으로 감지되어 연결됩니다.';
            if (alertPathBox) alertPathBox.style.display = 'none';
          } else {
            shortStatus = '연결 끊김';
            if (alertIcon) alertIcon.innerText = '//';
            if (alertTitle) alertTitle.innerText = '게임 클라이언트와 연결이 끊어졌습니다';
            if (alertDesc) alertDesc.innerHTML = `<span class="sym-prefix">&gt;</span>${(data.error || '게임 및 네트워크 상태를 확인해주세요.')}`;
            if (alertPathBox) alertPathBox.style.display = 'none';
          }

          if (statusPill) {
            statusPill.className = "pill offline";
            statusPill.innerHTML = `[!] ${shortStatus}`;
            statusPill.title = (data.error || '클릭하여 연동 가이드 및 설정 열기');
          }
          if (cliAlertBanner) cliAlertBanner.style.display = 'block';
        }
        
        isCurrentlyBusy = !!data.is_busy;
        applyBusyUI(isCurrentlyBusy, data.current_task || '작업 진행 중...');
        if (isCurrentlyBusy) {
          const mcbDesc = document.getElementById('mcb-running-desc');
          if (mcbDesc && data.current_task) mcbDesc.innerText = `> ${data.current_task}`;
        }

        // Execution Summary Modal Hook
        if (data.last_summary) {
          renderSummaryModal(data.last_summary);
          if (!isInitialStatusLoaded) {
            // First status fetch: sync shownSummaryId without interrupting user with auto-popup
            shownSummaryId = data.last_summary_id || 0;
            isInitialStatusLoaded = true;
          } else if (data.last_summary_id && data.last_summary_id > shownSummaryId && !data.is_busy) {
            shownSummaryId = data.last_summary_id;
            openSummaryModal();
          }
        } else {
          isInitialStatusLoaded = true;
        }
      } catch (err) {
        statusFailCount++;
        console.warn('updateStatus fetch failure (' + statusFailCount + '):', err);
        if (statusFailCount >= 3 && statusPill) {
          if (isCurrentlyBusy) {
            statusPill.className = "pill warning";
            statusPill.innerHTML = `<span class="cli-spinner">${getCurrentSpinnerText()}</span> 작업 진행 중 (응답 대기)...`;
          } else {
            statusPill.className = "pill offline";
            statusPill.innerHTML = `[!] 연결 재시도 중...`;
          }
        }
      } finally {
        isUpdatingStatus = false;
      }
    }

    async function loadQuests() {
      try {
        const res = await fetch('/api/quests');
        const data = await res.json();
        const listEl = document.getElementById('quest-list');
        if (!listEl) return;
        const tasks = data.tasks || [];
        if (tasks.length === 0) {
          listEl.innerHTML = '<p style="color: var(--text-muted); font-size: var(--fs-md);"><span class="sym-prefix">&gt;</span>현재 진행 중인 납품 목표 퀘스트가 없습니다.</p>';
          return;
        }

        let html = '';
        tasks.forEach(t => {
          const isDone = t.is_completed || t.needed === 0;
          const gaugeStr = makeTextGauge(t.current, t.goal, 5);
          html += `
            <div style="border-bottom: 1px solid var(--border-line); padding: 10px 4px; margin-bottom: 6px;">
              <div style="display: flex; justify-content: space-between; align-items: center;">
                <span style="font-weight: 700; font-size: var(--fs-md); color: var(--text-title);">// ${t.quest_title}</span>
                <span class="${isDone ? 'badge' : 'badge-dim'}">${isDone ? '완료' : '진행 중'}</span>
              </div>
              <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 6px; font-size: var(--fs-sm);">
                <span style="color: var(--text-muted);"><span class="sym-prefix">&gt;</span>납품 목표: <strong style="color: var(--text-main);">${t.item_name}</strong></span>
                <span class="mono-num"><span class="cli-gauge">${gaugeStr}</span> 보유: <strong style="color: var(--text-main);">${t.current} / ${t.goal}</strong> (${t.needed}개 부족)</span>
              </div>
            </div>
          `;
        });
        listEl.innerHTML = html;
      } catch (err) {
        console.error('loadQuests error:', err);
      }
    }

    async function startCustomProduce() {
      const itemEl = document.getElementById('manual-item');
      const countEl = document.getElementById('manual-count');
      if (!itemEl || !countEl) return;
      const item = itemEl.value.trim();
      const count = parseInt(countEl.value, 10);
      if (!item || isNaN(count) || count <= 0) {
        alert('올바른 아이템명과 수량을 입력해주세요.');
        return;
      }
      isCurrentlyBusy = true;
      if (typeof applyBusyUI === 'function') applyBusyUI(true, `[${item}] ${count}개 제작 준비 중...`);
      try {
        const res = await fetch('/api/produce', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ item_name: item, target_count: count })
        });
        const data = await res.json();
        if (data.error) {
          isCurrentlyBusy = false;
          if (typeof applyBusyUI === 'function') applyBusyUI(false);
          alert('오류: ' + data.error);
        } else {
          pollLogs();
          updateStatus();
        }
      } catch (err) {
        isCurrentlyBusy = false;
        if (typeof applyBusyUI === 'function') applyBusyUI(false);
        alert('서버 요청 실패: ' + err);
      }
    }

    let lastRenderedLogSig = '';
    async function pollLogs() {
      try {
        const res = await fetch('/api/logs');
        const data = await res.json();
        const logsEl = document.getElementById('console-logs');
        if (logsEl && data.logs && data.logs.length > 0) {
          const lastItem = data.logs[data.logs.length - 1];
          const sig = `${data.logs.length}|${lastItem.time}|${lastItem.message}`;
          if (sig === lastRenderedLogSig) return;
          const wasAtBottom = (logsEl.scrollHeight - logsEl.scrollTop - logsEl.clientHeight) < 40;
          lastRenderedLogSig = sig;
          logsEl.innerHTML = data.logs.map(l => {
            const cls = `log-${l.level || 'info'}`;
            return `<div class="log-line ${cls}"><span class="log-time">[${l.time}]</span> ${l.message}</div>`;
          }).join('');
          if (wasAtBottom) {
            logsEl.scrollTop = logsEl.scrollHeight;
          }
        }
      } catch (err) {}
    }

    // ==========================================
    // Korean Chosung (초성) & Smart Recipe/Material Search + Autocomplete
    // ==========================================
    const CHOSUNG_LIST = [
      'ㄱ', 'ㄲ', 'ㄴ', 'ㄷ', 'ㄸ', 'ㄹ', 'ㅁ', 'ㅂ', 'ㅃ', 'ㅅ',
      'ㅆ', 'ㅇ', 'ㅈ', 'ㅉ', 'ㅊ', 'ㅋ', 'ㅌ', 'ㅍ', 'ㅎ'
    ];
    window.allItemCatalog = [];
    window.allItemMap = {};
    let searchDebounceTimer = null;
    let recentSearches = [];
    try {
      recentSearches = JSON.parse(safeGetStorage('mabi_recent_searches', '[]')) || [];
    } catch (e) {
      recentSearches = [];
    }

    function extractChosung(str) {
      if (!str) return '';
      let result = '';
      for (let i = 0; i < str.length; i++) {
        const code = str.charCodeAt(i) - 0xAC00;
        if (code >= 0 && code < 11172) {
          const chosungIdx = Math.floor(code / 588);
          result += CHOSUNG_LIST[chosungIdx];
        } else {
          result += str.charAt(i).toLowerCase();
        }
      }
      return result;
    }

    function matchItemQuery(itemObj, rawQuery) {
      if (!rawQuery) return 0;
      const q = rawQuery.trim().toLowerCase();
      const qNoSpace = q.replace(/\s+/g, '');
      const nameLower = itemObj.nameLower;
      const nameNoSpace = itemObj.nameNoSpace;
      const chosung = itemObj.chosung;
      const chosungNoSpace = itemObj.chosungNoSpace;

      if (nameLower === q || nameNoSpace === qNoSpace) return 100;
      if (nameLower.startsWith(q) || nameNoSpace.startsWith(qNoSpace)) return 85;
      if (chosungNoSpace.startsWith(qNoSpace)) return 75;
      if (nameLower.includes(q) || nameNoSpace.includes(qNoSpace)) return 65;
      if (chosung.includes(q) || chosungNoSpace.includes(qNoSpace)) return 50;
      return 0;
    }

    async function loadItemCatalog() {
      try {
        const res = await fetch('/api/recipes_all');
        const data = await res.json();
        const recipes = data.recipes || {};
        const alterRecipes = data.alter_recipes || {};
        const gatherables = data.gatherables || [];
        const nonGatherables = data.non_gatherables || [];

        const catalogMap = {};

        function registerCatalogItem(name, catLabel, descText, ingredientsObj) {
          if (!name) return;
          const clean = name.trim();
          if (!clean) return;
          if (!catalogMap[clean]) {
            const nameLower = clean.toLowerCase();
            const chosung = extractChosung(clean);
            catalogMap[clean] = {
              name: clean,
              nameLower: nameLower,
              nameNoSpace: nameLower.replace(/\s+/g, ''),
              chosung: chosung,
              chosungNoSpace: chosung.replace(/\s+/g, ''),
              category: catLabel,
              desc: descText || '',
              ingredients: ingredientsObj || null
            };
          } else {
            if (catLabel && !catalogMap[clean].category.includes(catLabel)) {
              catalogMap[clean].category += '/' + catLabel;
            }
            if (descText && !catalogMap[clean].desc) {
              catalogMap[clean].desc = descText;
            }
            if (ingredientsObj && !catalogMap[clean].ingredients) {
              catalogMap[clean].ingredients = ingredientsObj;
            }
          }
        }

        Object.entries(recipes).forEach(([rName, ings]) => {
          const ingSummary = Object.entries(ings || {}).map(([k, v]) => `${k} ${v}`).join(', ');
          registerCatalogItem(rName, '제작', ingSummary, ings);
          Object.keys(ings || {}).forEach(ingName => {
            registerCatalogItem(ingName, '재료', '', null);
          });
        });

        Object.entries(alterRecipes).forEach(([aName, aInfo]) => {
          const facShort = (aInfo.facility || '가공').replace(' 가공 시설', '').replace(' 시설', '');
          const ingSummary = Object.entries(aInfo.ingredients || {}).map(([k, v]) => `${k} ${v}`).join(', ');
          registerCatalogItem(aName, `${facShort} 가공`, ingSummary, aInfo.ingredients);
          Object.keys(aInfo.ingredients || {}).forEach(ingName => {
            registerCatalogItem(ingName, '재료', '', null);
          });
        });

        gatherables.forEach(gName => {
          registerCatalogItem(gName, '채집', '필드 채집 원자재', null);
        });

        nonGatherables.forEach(sName => {
          registerCatalogItem(sName, '특수/상점', '상점 구매 또는 사냥 획득', null);
        });

        const list = Object.values(catalogMap).sort((a, b) => a.name.localeCompare(b.name, 'ko'));
        window.allItemCatalog = list;
        window.allItemMap = catalogMap;

        const dl = document.getElementById('all-items-datalist');
        if (dl) {
          dl.innerHTML = list.map(item => `<option value="${item.name.replace(/"/g, '&quot;')}">${item.category}${item.desc ? ' · ' + item.desc : ''}</option>`).join('');
        }
        const badge = document.getElementById('catalog-count-badge');
        if (badge) {
          badge.innerText = `${list.length}종 DB · 초성 검색`;
        }
        renderRecentSearchChips();
      } catch (e) {
        console.warn('loadItemCatalog error:', e);
      }
    }

    function renderRecentSearchChips() {
      const box = document.getElementById('search-quick-chips');
      if (!box) return;
      const defaults = ['론 엣지소드S', '특수강괴', '상급 목재+', '최상급 가죽', '나무 진액'];
      const combined = [...recentSearches];
      defaults.forEach(d => {
        if (!combined.includes(d) && combined.length < 7) combined.push(d);
      });
      const label = recentSearches.length > 0 ? '최근·추천:' : '빠른 조회:';
      let html = `<span style="font-size: 11px; color: var(--text-dim);">${label}</span>`;
      combined.slice(0, 6).forEach(name => {
        const safe = name.replace(/'/g, "\\'");
        html += `<span class="search-chip" onclick="quickSearchItem('${safe}')">${name}</span>`;
      });
      box.innerHTML = html;
    }

    function saveRecentSearch(q) {
      if (!q || q.length < 2) return;
      recentSearches = [q, ...recentSearches.filter(x => x !== q)].slice(0, 6);
      safeSetStorage('mabi_recent_searches', JSON.stringify(recentSearches));
      renderRecentSearchChips();
    }

    function getAutocompleteMatches(query, maxCount = 10) {
      if (!query || !query.trim()) return [];
      const scored = [];
      for (let i = 0; i < window.allItemCatalog.length; i++) {
        const it = window.allItemCatalog[i];
        const score = matchItemQuery(it, query);
        if (score > 0) {
          scored.push({ item: it, score: score });
        }
      }
      scored.sort((a, b) => {
        if (b.score !== a.score) return b.score - a.score;
        if (a.item.name.length !== b.item.name.length) return a.item.name.length - b.item.name.length;
        return a.item.name.localeCompare(b.item.name, 'ko');
      });
      return scored.slice(0, maxCount).map(s => s.item);
    }

    function closeAllAutocompleteDropdowns() {
      document.querySelectorAll('.ac-dropdown').forEach(el => {
        el.style.display = 'none';
      });
    }

    document.addEventListener('click', (e) => {
      if (!e.target.closest('.ac-wrapper')) {
        closeAllAutocompleteDropdowns();
      }
    });

    function onItemInputAutocomplete(inputEl, dropdownId) {
      const dd = document.getElementById(dropdownId);
      if (!dd || !inputEl) return;
      const q = inputEl.value.trim();
      if (!q) {
        dd.style.display = 'none';
        return;
      }
      const matches = getAutocompleteMatches(q, 8);
      if (matches.length === 0) {
        dd.style.display = 'none';
        return;
      }
      let html = '';
      matches.forEach((m, idx) => {
        const subDesc = m.desc ? `<span style="color:var(--text-dim); font-size:11px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:180px;">${m.desc}</span>` : '';
        html += `
          <div class="ac-item ${idx === 0 ? 'active' : ''}" data-value="${m.name.replace(/"/g, '&quot;')}" onmousedown="selectAutocompleteItem('${inputEl.id}', '${dropdownId}', this.getAttribute('data-value'))">
            <div style="display:flex; align-items:center; gap:6px; min-width:0;">
              <strong style="color:var(--text-bright); white-space:nowrap;">${m.name}</strong>
              ${subDesc}
            </div>
            <span class="badge-dim" style="font-size:10px; flex-shrink:0;">${m.category}</span>
          </div>
        `;
      });
      dd.innerHTML = html;
      dd.style.display = 'block';
    }

    function selectAutocompleteItem(inputId, dropdownId, value) {
      const inputEl = document.getElementById(inputId);
      const dd = document.getElementById(dropdownId);
      if (inputEl) inputEl.value = value;
      if (dd) dd.style.display = 'none';
      if (inputId === 'search-query') {
        doSearch();
      }
    }

    function handleAutocompleteKeydown(event, inputEl, dropdownId, enterActionFn) {
      const dd = document.getElementById(dropdownId);
      const isVisible = dd && dd.style.display === 'block';
      const items = isVisible ? Array.from(dd.querySelectorAll('.ac-item')) : [];

      if (event.key === 'ArrowDown' && isVisible && items.length > 0) {
        event.preventDefault();
        let idx = items.findIndex(el => el.classList.contains('active'));
        items.forEach(el => el.classList.remove('active'));
        idx = (idx + 1) % items.length;
        items[idx].classList.add('active');
        items[idx].scrollIntoView({ block: 'nearest' });
        return;
      }
      if (event.key === 'ArrowUp' && isVisible && items.length > 0) {
        event.preventDefault();
        let idx = items.findIndex(el => el.classList.contains('active'));
        items.forEach(el => el.classList.remove('active'));
        idx = (idx - 1 + items.length) % items.length;
        items[idx].classList.add('active');
        items[idx].scrollIntoView({ block: 'nearest' });
        return;
      }
      if (event.key === 'Escape' && isVisible) {
        dd.style.display = 'none';
        return;
      }
      if (event.key === 'Enter') {
        event.preventDefault();
        if (isVisible && items.length > 0) {
          const activeEl = dd.querySelector('.ac-item.active') || items[0];
          if (activeEl) {
            const val = activeEl.getAttribute('data-value');
            if (val && inputEl.value.trim() !== val) {
              inputEl.value = val;
              dd.style.display = 'none';
              if (inputEl.id === 'search-query' && typeof enterActionFn === 'function') {
                enterActionFn();
              }
              return;
            }
          }
          dd.style.display = 'none';
        }
        if (typeof enterActionFn === 'function') {
          enterActionFn();
        }
      }
    }

    function onSearchQueryInput(inputEl) {
      onItemInputAutocomplete(inputEl, 'ac-search-query');
      const q = inputEl.value.trim();
      if (!q) return;

      // Instant local preview even while typing (including Korean Chosung!)
      const matches = getAutocompleteMatches(q, 5);
      if (matches.length > 0) {
        renderInstantCatalogPreview(q, matches);
      }

      if (searchDebounceTimer) clearTimeout(searchDebounceTimer);
      searchDebounceTimer = setTimeout(() => {
        doSearch(q, false);
      }, 280);
    }

    function quickSearchItem(itemName) {
      if (searchDebounceTimer) {
        clearTimeout(searchDebounceTimer);
        searchDebounceTimer = null;
      }
      const qEl = document.getElementById('search-query');
      if (qEl) qEl.value = itemName;
      closeAllAutocompleteDropdowns();
      doSearch(itemName, true);
    }

    async function quickAddDeliveryFromSearch(itemName) {
      try {
        const res = await fetch('/api/add_delivery_target', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ item_name: itemName, goal: 1 })
        });
        const d = await res.json();
        if (d.error) {
          alert('추가 실패: ' + d.error);
        } else {
          loadDeliveryTargets();
          loadBatchPlan();
          alert(`'${itemName}' 품목이 주간 납품 목표에 추가되었습니다.`);
        }
      } catch (e) {
        alert('요청 오류: ' + e);
      }
    }

    function renderInstantCatalogPreview(q, matches) {
      const resEl = document.getElementById('search-results');
      if (!resEl) return;
      let html = `<div style="font-size:11px; color:var(--text-dim); margin-bottom:6px;"><span class="sym-prefix">&gt;</span>'${q}' 실시간 매칭 (${matches.length}건) — 항목을 클릭하면 상세 재료 및 창고 재고를 조회합니다:</div>`;
      matches.forEach(m => {
        const safe = m.name.replace(/'/g, "\\'");
        html += `
          <div style="padding: 6px 8px; border: 1px solid var(--border-line); margin-bottom: 5px; display:flex; justify-content:space-between; align-items:center; gap:8px; cursor:pointer;" onclick="quickSearchItem('${safe}')">
            <div style="min-width:0;">
              <strong style="color:var(--text-bright);">${m.name}</strong>
              <span class="badge-dim" style="font-size:10px; margin-left:6px;">${m.category}</span>
              ${m.desc ? `<div style="font-size:11px; color:var(--text-muted); margin-top:2px;">소요 재료: ${m.desc}</div>` : ''}
            </div>
            <span style="font-size:11px; color:var(--text-dim); white-space:nowrap;">상세 보기 &gt;</span>
          </div>
        `;
      });
      resEl.innerHTML = html;
    }

    async function doSearch(overrideQuery, shouldSaveRecent = true) {
      if (searchDebounceTimer) {
        clearTimeout(searchDebounceTimer);
        searchDebounceTimer = null;
      }
      const qEl = document.getElementById('search-query');
      if (!qEl && !overrideQuery) return;
      let rawQ = (overrideQuery || (qEl ? qEl.value : '')).trim();
      if (!rawQ) return;

      closeAllAutocompleteDropdowns();

      // Only replace rawQ with the top catalog match when the user typed Korean Chosung (e.g., ㄹㅇㅈ)
      // so broad substring searches (e.g., "소드", "통나무", "갑옷") still list all matching items!
      let resolvedQ = rawQ;
      if (/[ㄱ-ㅎ]/.test(rawQ) && !window.allItemMap[rawQ]) {
        const matches = getAutocompleteMatches(rawQ, 1);
        if (matches.length > 0) {
          resolvedQ = matches[0].name;
        }
      }

      if (shouldSaveRecent) {
        saveRecentSearch(resolvedQ);
      }

      const resEl = document.getElementById('search-results');
      if (!resEl) return;
      resEl.innerHTML = '<span style="color:var(--text-muted);"><span class="sym-prefix">&gt;</span>상세 레시피 및 인벤토리/창고 재고 조회 중...</span>';

      try {
        const res = await fetch(`/api/search_recipe?name=${encodeURIComponent(resolvedQ)}`);
        const d = await res.json();
        if (d.error) {
          resEl.innerHTML = `<span style="color:var(--text-main);">[!] ${d.error}</span>`;
          return;
        }

        const safeTarget = (d.item_name || resolvedQ).replace(/'/g, "\\'");
        let html = '';

        // 1. Header Card: Stock & Quick Action Buttons
        const stockLine = d.cli_live_ok
          ? `가방: <strong style="color:var(--text-bright);">${d.owned_bag}개</strong> | 개인창고: ${d.owned_char_storage || 0}개 | 공용창고: ${d.owned_account_storage || 0}개 (총 <strong style="color:var(--text-bright);">${d.owned_all || 0}개</strong>)`
          : `<span style="color:var(--text-dim);">${d.is_busy ? '작업 진행 중 (실시간 창고 조회 생략 · 로컬 도감 표시)' : '게임 미연결 (로컬 레시피 도감 표시)'}</span>`;

        html += `
          <div style="padding: 8px 10px; background: var(--bg-sub-panel); border: 1px solid var(--border-line); margin-bottom: 8px;">
            <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:6px; margin-bottom:4px;">
              <strong style="font-size:var(--fs-md); color:var(--text-bright);">// ${d.item_name}</strong>
              <div style="display:flex; gap:4px; flex-wrap:wrap;">
                <button type="button" class="btn btn-sm btn-boxed" onclick="quickAddCustomTarget('${safeTarget}', 1); switchMainMode('custom_craft');" title="지정 제작 탭에 이 품목 1개 추가">+ 지정 제작</button>
                <button type="button" class="btn btn-sm btn-boxed" onclick="quickAddDeliveryFromSearch('${safeTarget}')" title="주간 납품 목표에 이 품목 추가">+ 납품 추가</button>
                <button type="button" class="btn btn-sm btn-boxed" onclick="openRecipeModal('${safeTarget}')" title="1회 제작 레시피 확인 및 수정">레시피 수정</button>
              </div>
            </div>
            <div class="mono-num" style="font-size:12px; color:var(--text-muted);"><span class="sym-prefix">&gt;</span>${stockLine}</div>
          </div>
        `;

        // 2. Local Craft Recipes (1회 제작 재료 상세 + 클릭 시 하위 재료 연쇄 검색)
        const localCrafts = d.local_crafts || [];
        if (localCrafts.length > 0) {
          html += `<div style="margin-bottom: 8px;"><div style="font-weight:700; color:var(--text-bright); margin-bottom:4px; font-size:12px;"><span class="sym-prefix">&gt;</span>제작 레시피 (${localCrafts.length}건) <span style="font-weight:400; color:var(--text-dim); font-size:11px;">*재료 클릭 시 연쇄 조회</span></div>`;
          localCrafts.slice(0, 8).forEach(c => {
            const cSafe = c.name.replace(/'/g, "\\'");
            const ingChips = Object.entries(c.ingredients || {}).map(([k, v]) => {
              const kSafe = k.replace(/'/g, "\\'");
              return `<span class="search-chip" onclick="quickSearchItem('${kSafe}')" title="'${k}' 상세 조회">${k} <strong class="mono-num" style="color:var(--text-bright);">${v}</strong></span>`;
            }).join(' ');
            html += `
              <div style="padding: 6px 8px; border: 1px solid var(--line-subtle); margin-bottom: 4px;">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px;">
                  <strong style="color:var(--text-main); cursor:pointer;" onclick="quickSearchItem('${cSafe}')">${c.name}</strong>
                  <div style="display:flex; gap:4px;">
                    <button type="button" class="btn btn-sm btn-boxed" style="font-size:11px; padding:1px 5px !important;" onclick="quickAddCustomTarget('${cSafe}', 1)">+제작목표</button>
                    <button type="button" class="btn btn-sm btn-boxed" style="font-size:11px; padding:1px 5px !important;" onclick="openRecipeModal('${cSafe}')">편집</button>
                  </div>
                </div>
                <div style="display:flex; gap:4px; flex-wrap:wrap; align-items:center;">
                  <span style="font-size:11px; color:var(--text-dim);">1회 재료:</span>
                  ${ingChips || '<span style="font-size:11px; color:var(--text-dim);">등록된 재료 없음</span>'}
                </div>
              </div>
            `;
          });
          html += `</div>`;
        }

        // 3. Alter Recipes (시설 가공법 상세)
        const localAlters = d.local_alters || [];
        if (localAlters.length > 0) {
          html += `<div style="margin-bottom: 8px;"><div style="font-weight:700; color:var(--text-bright); margin-bottom:4px; font-size:12px;"><span class="sym-prefix">&gt;</span>시설 가공 레시피 (${localAlters.length}건)</div>`;
          localAlters.slice(0, 8).forEach(a => {
            const ingChips = Object.entries(a.ingredients || {}).map(([k, v]) => {
              const kSafe = k.replace(/'/g, "\\'");
              return `<span class="search-chip" onclick="quickSearchItem('${kSafe}')" title="'${k}' 상세 조회">${k} <strong class="mono-num" style="color:var(--text-bright);">${v}</strong></span>`;
            }).join(' ');
            html += `
              <div style="padding: 6px 8px; border: 1px solid var(--line-subtle); margin-bottom: 4px;">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px;">
                  <div>
                    <strong style="color:var(--text-main);">${a.name}</strong>
                    <span class="badge-dim mono-num" style="font-size:10px; margin-left:4px;">T${a.tier} · ${a.facility} (1회 ${a.produced}개 생산)</span>
                  </div>
                </div>
                <div style="display:flex; gap:4px; flex-wrap:wrap; align-items:center;">
                  <span style="font-size:11px; color:var(--text-dim);">1회 가공 재료:</span>
                  ${ingChips}
                </div>
              </div>
            `;
          });
          html += `</div>`;
        }

        // 4. Reverse Ingredient Lookup ("이 재료가 쓰이는 상위 제작/가공 품목")
        const usedIn = d.used_in || [];
        if (usedIn.length > 0) {
          html += `<div style="margin-bottom: 6px;"><div style="font-weight:700; color:var(--text-bright); margin-bottom:4px; font-size:12px;"><span class="sym-prefix">&gt;</span>이 재료가 사용되는 상위 품목 (${usedIn.length}건)</div>`;
          html += `<div style="display:flex; gap:4px; flex-wrap:wrap;">`;
          usedIn.slice(0, 18).forEach(u => {
            const uSafe = u.target.replace(/'/g, "\\'");
            html += `<span class="search-chip" onclick="quickSearchItem('${uSafe}')" title="클릭하여 '${u.target}' 조회 (${u.ingredient} ${u.qty}개 소요)"><strong style="color:var(--text-main);">${u.target}</strong> <span class="mono-num" style="color:var(--text-dim);">(${u.category}·${u.qty}개)</span></span>`;
          });
          html += `</div></div>`;
        }

        // 5. Gatherable / Special Notice
        const localGathers = d.local_gathers || [];
        const localSpecials = d.local_specials || [];
        if (localGathers.length > 0 || localSpecials.length > 0) {
          const tags = [
            ...localGathers.map(g => `<span class="search-chip" onclick="quickSearchItem('${g.replace(/'/g, "\\'")}')">[채집] ${g}</span>`),
            ...localSpecials.map(s => `<span class="search-chip" onclick="quickSearchItem('${s.replace(/'/g, "\\'")}')">[상점/특수] ${s}</span>`)
          ].join(' ');
          html += `<div style="margin-top:4px; font-size:11px; color:var(--text-muted);"><span class="sym-prefix">&gt;</span>채집/특수 분류: ${tags}</div>`;
        }

        if (localCrafts.length === 0 && localAlters.length === 0 && usedIn.length === 0 && localGathers.length === 0 && localSpecials.length === 0) {
          html += `<div style="font-size:12px; color:var(--text-muted); padding:4px 0;"><span class="sym-prefix">&gt;</span>인게임 검색 결과 — 제작: ${d.craft.length}건 | 가공: ${d.alter.length}건 | 채집: ${d.gather.length}건</div>`;
        }

        resEl.innerHTML = html;
      } catch (e) {
        resEl.innerHTML = `<span style="color:var(--text-main);">[!] 검색 오류: ${e}</span>`;
      }
    }

    // Heartbeat & Auto-Shutdown on Window Close
    function sendHeartbeat() {
      fetch('/api/heartbeat', { method: 'POST', keepalive: true }).catch(() => {});
    }

    // High-reliability Web Worker to prevent background throttling in Chromium
    try {
      const workerBlob = new Blob([
        "setInterval(function() { postMessage('tick'); }, 1000);"
      ], { type: 'application/javascript' });
      const workerUrl = URL.createObjectURL(workerBlob);
      const bgWorker = new Worker(workerUrl);
      let bgTickCount = 0;
      bgWorker.onmessage = function() {
        bgTickCount++;
        // 1. Alarm countdown ticker runs every 1 second accurately
        if (typeof window.tickAlarms === 'function') window.tickAlarms();
        // 2. Heartbeat to python server runs every 3 seconds
        if (bgTickCount % 3 === 0) {
          sendHeartbeat();
        }
      };
    } catch (e) {
      console.warn('Web Worker fallback to window timers:', e);
      setInterval(() => { if (typeof window.tickAlarms === 'function') window.tickAlarms(); }, 1000);
      setInterval(sendHeartbeat, 3000);
    }

    // Visibility change & focus handlers: immediately refresh when user switches back to this tab
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) {
        sendHeartbeat();
        updateStatus();
      }
    });

    window.addEventListener('focus', () => {
      sendHeartbeat();
      updateStatus();
    });

    // Detect browser window / tab closing (beforeunload only fires on actual unload)
    window.addEventListener('beforeunload', () => {
      try {
        if (navigator.sendBeacon) {
          navigator.sendBeacon('/api/dashboard_closed');
        } else {
          fetch('/api/dashboard_closed', { method: 'POST', keepalive: true }).catch(() => {});
        }
      } catch (e) {}
    });

    async function manualServerShutdown() {
      if (!confirm('웹 대시보드와 백그라운드 서버를 완전히 종료하시겠습니까?')) return;
      try {
        await fetch('/api/shutdown', { method: 'POST', keepalive: true });
        document.body.innerHTML = `
          <div style="height: 100vh; display: flex; flex-direction: column; align-items: center; justify-content: center; background: #121212; color: #E5E5E5; font-family: 'JetBrains Mono', 'Pretendard', monospace;">
            <h2 style="font-size: 16px; color: #F5F5F5; margin-bottom: 12px; font-weight: 700;">// 웹 서버가 안전하게 종료되었습니다</h2>
            <p style="color: #888888; font-size: 14px; margin-bottom: 24px;"><span class="sym-prefix">&gt;</span>이 브라우저 창이나 탭을 닫으셔도 됩니다.</p>
            <button onclick="window.close()" style="padding: 8px 20px; background: #E0E0E0; color: #121212; border: 1px solid #F5F5F5; font-weight: 700; cursor: pointer;">[ 창 닫기 ]</button>
          </div>
        `;
      } catch (e) {
        alert('종료 요청 실패: ' + e);
      }
    }

    // =========================================================================
    // [HOMEWORK SUPPORT & TOP-LEVEL MODE SWITCHER]
    // =========================================================================
    function escapeHtml(str) {
      if (str === null || str === undefined) return '';
      return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }
    window.escapeHtml = escapeHtml;

    let currentTopAppMode = localStorage.getItem('mobi_top_app_mode') || 'living';
    let currentHomeworkSubTab = localStorage.getItem('mobi_hw_sub_tab') || 'main';
    let cachedHomeworkState = null;
    let cachedLiveStatusData = null;
    let dismissedOmenHourKey = '';
    let lastOmenChimeHourKey = '';
    let isEditingMemo = false;

    function switchTopAppMode(mode) {
      if (mode !== 'living' && mode !== 'homework') mode = 'living';
      currentTopAppMode = mode;
      try { localStorage.setItem('mobi_top_app_mode', mode); } catch(e) {}
      applyTopAppModeView();
    }

    function applyTopAppModeView() {
      const livingView = document.getElementById('top-view-living');
      const hwView = document.getElementById('top-view-homework');
      const btnLiving = document.getElementById('btn-top-mode-living');
      const btnHw = document.getElementById('btn-top-mode-homework');

      if (livingView) livingView.style.display = (currentTopAppMode === 'living') ? 'block' : 'none';
      if (hwView) hwView.style.display = (currentTopAppMode === 'homework') ? 'block' : 'none';
      if (btnLiving) btnLiving.classList.toggle('active', currentTopAppMode === 'living');
      if (btnHw) btnHw.classList.toggle('active', currentTopAppMode === 'homework');

      if (currentTopAppMode === 'homework') {
        applyHomeworkSubTabView();
        if (cachedHomeworkState) {
          renderHomeworkDashboard(cachedHomeworkState);
        } else {
          fetchHomeworkState();
        }
        refreshHomeworkLiveMissions();
      } else {
        updateMasterCommandBar();
      }
      try { updateStickyHeaderOffset(); } catch(e) {}
    }

    function switchHomeworkSubTab(tab) {
      if (!['main', 'summary', 'matrix', 'settings'].includes(tab)) tab = 'main';
      currentHomeworkSubTab = tab;
      try { localStorage.setItem('mobi_hw_sub_tab', tab); } catch(e) {}
      applyHomeworkSubTabView();
      if (cachedHomeworkState) {
        renderHomeworkDashboard(cachedHomeworkState, false);
        if (tab === 'settings') {
          renderHomeworkSettings(cachedHomeworkState, true);
        }
      }
    }

    function applyHomeworkSubTabView() {
      const tabs = ['main', 'summary', 'matrix', 'settings'];
      tabs.forEach(t => {
        const btn = document.getElementById('btn-hw-tab-' + t);
        const view = document.getElementById('hw-subview-' + t);
        if (btn) btn.classList.toggle('active', currentHomeworkSubTab === t);
        if (view) view.style.display = (currentHomeworkSubTab === t) ? 'block' : 'none';
      });
    }

    async function fetchHomeworkState() {
      try {
        const res = await fetch('/api/homework/state');
        const hw = await res.json();
        if (hw && !hw.error) {
          onHomeworkStateReceived(hw, hw.omen_barrier, cachedLiveStatusData, false);
        }
      } catch (e) {
        console.warn('fetchHomeworkState error:', e);
      }
    }

    function onHomeworkStateReceived(hw, omenBarrier, fullStatusData, isPolling = false) {
      if (fullStatusData) cachedLiveStatusData = fullStatusData;
      cachedHomeworkState = hw;
      if (omenBarrier || hw.omen_barrier) {
        updateOmenBarrierUI(omenBarrier || hw.omen_barrier, hw.settings);
      }
      updateTopSwitcherPills(hw);
      renderHomeworkDashboard(hw, isPolling);
    }

    function updateTopSwitcherPills(hw) {
      const hwPill = document.getElementById('top-hw-summary-pill');
      if (hwPill && hw && hw.characters) {
        const actName = hw.active_character || '';
        const actChar = (hw.characters || []).find(c => c.name === actName) || hw.characters[0];
        if (actChar && actChar.progress) {
          const dp = actChar.progress.daily || { done: 0, total: 0 };
          const wp = actChar.progress.weekly || { done: 0, total: 0 };
          hwPill.innerText = `${actChar.name} · 일일 ${dp.done}/${dp.total} · 주간 ${wp.done}/${wp.total}`;
        }
      }
      const livPill = document.getElementById('top-living-summary-pill');
      const dcInfo = window.deliveryCharactersInfo;
      if (livPill && dcInfo) {
        const actId = dcInfo.active_character_id;
        const c = (dcInfo.characters || []).find(x => x.id === actId);
        if (c) {
          livPill.innerText = c.weekly_delivered ? `${c.name} · 납품완료✓` : `${c.name} · 납품 ${c.completed_count || 0}/${c.target_count || c.total_count || 18}`;
        }
      }
    }

    function updateOmenBarrierUI(omen, hwSettings) {
      if (!omen) return;
      const pill = document.getElementById('pill-omen-barrier');
      const banner = document.getElementById('omen-barrier-banner');
      const bannerText = document.getElementById('omen-barrier-banner-text');
      const alertEnabled = hwSettings ? (hwSettings.omen_barrier_alert !== false) : true;

      if (pill) {
        if (omen.is_active_window) {
          pill.innerHTML = `<span style="color: var(--text-bright); font-weight: 700;">[결계] ${omen.label}</span>`;
        } else if (omen.is_pre_alert) {
          pill.innerHTML = `<span style="color: var(--text-main); font-weight: 700;">⚡ ${omen.label}</span>`;
        } else {
          pill.innerHTML = `<span style="color: var(--text-muted);">⚡ ${omen.label}</span>`;
        }
      }

      if (banner) {
        if (alertEnabled && (omen.is_active_window || omen.is_pre_alert) && dismissedOmenHourKey !== omen.hour_key) {
          banner.style.display = 'block';
          if (bannerText) {
            bannerText.innerText = omen.is_active_window
              ? `현재 정각 불길한 소환의 결계가 진행 중입니다! (${omen.label})`
              : `곧 정각 불길한 소환의 결계가 시작됩니다! (${omen.label})`;
          }
          if (omen.is_active_window && lastOmenChimeHourKey !== omen.hour_key) {
            lastOmenChimeHourKey = omen.hour_key;
            if (currentSoundAlarmEnabled && typeof playChime === 'function') {
              try { playChime(false); } catch(e) {}
            }
          }
        } else {
          banner.style.display = 'none';
        }
      }
    }

    function dismissOmenBarrierBanner() {
      if (cachedHomeworkState && cachedHomeworkState.omen_barrier) {
        dismissedOmenHourKey = cachedHomeworkState.omen_barrier.hour_key || 'dismissed';
      }
      const banner = document.getElementById('omen-barrier-banner');
      if (banner) banner.style.display = 'none';
    }

    function renderHomeworkDashboard(hw, isPolling = false) {
      if (!hw) return;
      try { renderHomeworkMasterCommandBar(hw); } catch(e) { console.error(e); }
      try { renderHomeworkCharacterBar(hw); } catch(e) { console.error(e); }
      try { renderHomeworkChecklists(hw); } catch(e) { console.error(e); }
      try { renderHomeworkSummaryTable(hw); } catch(e) { console.error(e); }
      try { renderHomeworkMatrixTable(hw); } catch(e) { console.error(e); }
      if (!isPolling && currentHomeworkSubTab === 'settings') {
        try { renderHomeworkSettings(hw, false); } catch(e) { console.error(e); }
      }
    }

    const _RE_KEYCAP = /^(.*?)\s*\[([^\]]+)\]$/;
    function formatTaskNameHtml(name) {
      if (!name) return '';
      const m = String(name).match(_RE_KEYCAP);
      if (!m) return `<span>${escapeHtml(name)}</span>`;
      const title = m[1].trim();
      const key = m[2].trim();
      return `<span>${escapeHtml(title)}</span><kbd class="hw-keycap">${escapeHtml(key)}</kbd>`;
    }

    function renderHomeworkMasterCommandBar(hw) {
      const actName = hw.active_character || '';
      const actChar = (hw.characters || []).find(c => c.name === actName) || (hw.characters || [])[0];
      const step = hw.active_step;
      const focusMode = !!(hw.focus_mode_enabled || (hw.settings && hw.settings.focus_mode_enabled));
      const briefingEl = document.getElementById('hw-mcb-briefing');
      const btnComplete = document.getElementById('btn-hw-complete-step');
      const btnRollback = document.getElementById('btn-hw-rollback');
      const btnFocusMode = document.getElementById('btn-hw-focus-mode');
      const iconFocusMode = document.getElementById('icon-hw-focus-mode');

      if (btnFocusMode) {
        btnFocusMode.classList.toggle('active', focusMode);
      }
      if (iconFocusMode) {
        iconFocusMode.innerText = focusMode ? '■' : '□';
      }

      if (briefingEl && actChar) {
        const clsText = actChar.class ? `(${actChar.class})` : '';
        const modeTag = focusMode ? `<span class="badge" style="margin-right: 4px;">집중 모드</span>` : '';
        if (step && step.task_name) {
          const gaugeStr = typeof makeTextGauge === 'function' ? makeTextGauge(step.cat_done, step.cat_total, 5) : '';
          briefingEl.innerHTML = `<span class="sym-prefix">&gt;</span>${modeTag}<strong>[${escapeHtml(actChar.name)}${escapeHtml(clsText)}]</strong> 현재 추천 순서: <span class="badge-dim" style="margin: 0 4px;">${escapeHtml(step.cat_title)}</span> <strong style="color: var(--text-bright); display: inline-flex; align-items: center;">${formatTaskNameHtml(step.task_name)}</strong> <span class="cli-gauge" style="margin-left: 4px;">${gaugeStr}</span> <span class="mono-num" style="color: var(--text-muted);">(${step.cat_done}/${step.cat_total})</span>`;
        } else {
          briefingEl.innerHTML = `<span class="sym-prefix">&gt;</span>${modeTag}<strong>[${escapeHtml(actChar.name)}${escapeHtml(clsText)}]</strong> <span class="badge">${focusMode ? '=== 집중 숙제 100% 달성 ===' : '=== 일일·주간 숙제 100% 달성 ==='}</span>`;
        }
      }

      if (btnComplete) {
        btnComplete.disabled = !(step && step.task_name);
        if (step && step.task_name) {
          const m = String(step.task_name).match(_RE_KEYCAP);
          if (m) {
            btnComplete.innerHTML = `✓ ${escapeHtml(m[1].trim())} <kbd class="hw-keycap">${escapeHtml(m[2].trim())}</kbd> 완료`;
          } else {
            btnComplete.innerHTML = `✓ [${escapeHtml(step.task_name)}] 완료`;
          }
        } else {
          btnComplete.innerText = focusMode ? '✓ 집중 숙제 완료됨' : '✓ 모든 숙제 완료됨';
        }
      }
      if (btnRollback) {
        btnRollback.disabled = !hw.can_rollback;
      }

      // Pills on Homework sub-tabs
      const pillMain = document.getElementById('hw-tab-pill-main');
      if (pillMain && actChar && actChar.progress) {
        const d = actChar.progress.daily || { done: 0, total: 0 };
        pillMain.innerText = `${actChar.name} ${d.done}/${d.total}`;
      }
      const pillSummary = document.getElementById('hw-tab-pill-summary');
      if (pillSummary && hw.characters) {
        const allDailyDone = hw.characters.filter(c => c.progress && c.progress.daily && c.progress.daily.pct >= 100).length;
        pillSummary.innerText = `일일완 ${allDailyDone}/${hw.characters.length}`;
      }
      const pillMatrix = document.getElementById('hw-tab-pill-matrix');
      if (pillMatrix && hw.characters) {
        let wDone = 0, wTotal = 0;
        hw.characters.forEach(c => {
          if (c.progress && c.progress.weekly) {
            wDone += c.progress.weekly.done;
            wTotal += c.progress.weekly.total;
          }
        });
        pillMatrix.innerText = `${wDone}/${wTotal}`;
      }

      // Option check icons
      const st = hw.settings || {};
      const setIcon = (id, val) => {
        const el = document.getElementById(id);
        if (el) el.innerText = val ? '■' : '□';
      };
      setIcon('icon-hw-opt-reset', st.auto_reset_enabled !== false);
      setIcon('icon-hw-opt-autoswitch', st.auto_detect_character !== false);
      setIcon('icon-hw-opt-autocheck', st.auto_check_missions !== false);
      setIcon('icon-hw-opt-omen', st.omen_barrier_alert !== false);
    }

    function renderHomeworkCharacterBar(hw) {
      const container = document.getElementById('hw-character-tabs-list');
      const recBadge = document.getElementById('hw-recommended-focus-badge');
      const matchBadge = document.getElementById('hw-ingame-match-badge');
      if (!container || !hw) return;

      if (recBadge) {
        if (hw.recommended_focus_character) {
          recBadge.innerText = `집중 추천: ${hw.recommended_focus_character}`;
          recBadge.style.display = 'inline-block';
        } else {
          recBadge.style.display = 'none';
        }
      }
      if (matchBadge) {
        if (hw.matched_ingame_character) {
          matchBadge.innerHTML = `<span class="char-ingame-dot" style="margin-right: 4px; vertical-align: middle;"></span>인게임 접속: ${escapeHtml(hw.matched_ingame_character)}`;
          matchBadge.style.display = 'inline-block';
        } else {
          matchBadge.style.display = 'none';
          matchBadge.innerHTML = '';
        }
      }

      let html = '';
      (hw.characters || []).forEach(c => {
        const isActive = !!c.is_active;
        const isIngame = !!(hw.matched_ingame_character && hw.matched_ingame_character === c.name);
        const dp = (c.progress && c.progress.daily) || { done: 0, total: 0, pct: 0 };
        const fp = (c.progress && c.progress.focus) || { done: 0, total: 0, pct: 0 };
        const wp = (c.progress && c.progress.weekly) || { done: 0, total: 0, pct: 0 };
        const safeName = c.name.replace(/'/g, "\\'");
        const ingameDot = isIngame
          ? `<span class="char-ingame-dot" title="현재 인게임 접속 중인 캐릭터"></span>`
          : '';
        const subLabel = c.class
          ? `<span class="char-tab-sub" style="font-size:11px; color:var(--text-dim); white-space:nowrap;">${escapeHtml(c.class)}</span>`
          : '';
        const focusDoneTag = c.weekly_focus_done
          ? `<span class="char-delivered-badge" title="이번 주 집중 숙제 완료됨 (매주 월요일 오전 6시 초기화)">✓집중</span>`
          : '';
        const isDailyDone = dp.total > 0 && dp.done >= dp.total;
        const doneTag = `<span class="char-tab-progress mono-num" style="font-size:11px; ${isDailyDone ? 'font-weight:700; color:var(--text-bright);' : 'color:var(--text-muted);'}">일${dp.done}/${dp.total}·주${wp.done}/${wp.total}</span>`;

        html += `
          <button type="button" class="char-tab-btn ${isActive ? 'active' : ''}" onclick="selectHomeworkCharacter('${safeName}')" title="${escapeHtml(c.name)} (${escapeHtml(c.class || '미지정')}) — 일일 ${dp.done}/${dp.total} · 집중 ${fp.done}/${fp.total}${c.weekly_focus_done ? '(이번주 완료)' : ''} · 주간 ${wp.done}/${wp.total}">
            <span class="char-tab-left">
              ${ingameDot}
              <span style="white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${escapeHtml(c.name)}</span>
              ${subLabel}
            </span>
            <span class="char-tab-right">
              ${focusDoneTag}
              ${doneTag}
            </span>
          </button>
        `;
      });
      container.innerHTML = html;
    }

    function toggleHwCategoryView(catKey) {
      const storageKey = `mobi_hw_collapsed_${catKey}`;
      const curCollapsed = localStorage.getItem(storageKey) === '1';
      localStorage.setItem(storageKey, curCollapsed ? '0' : '1');
      applyHwCategoryCollapsedView(catKey);
    }

    function applyHwCategoryCollapsedView(catKey) {
      const storageKey = `mobi_hw_collapsed_${catKey}`;
      const isCollapsed = localStorage.getItem(storageKey) === '1';
      const bodyEl = document.getElementById(`hw-cat-body-${catKey}`);
      const colBarEl = document.getElementById(`hw-cat-collapsed-${catKey}`);
      const btnEl = document.getElementById(`btn-toggle-hw-cat-${catKey}`);
      if (bodyEl) bodyEl.style.display = isCollapsed ? 'none' : 'flex';
      if (colBarEl) colBarEl.style.display = isCollapsed ? 'flex' : 'none';
      if (btnEl) btnEl.innerText = isCollapsed ? '펼치기 v' : '접기 ^';
    }

    async function toggleActiveCharWeeklyFocus() {
      if (!cachedHomeworkState) return;
      const actName = cachedHomeworkState.active_character || '';
      try {
        const res = await fetch('/api/homework/toggle_weekly_focus', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ char_name: actName })
        });
        const d = await res.json();
        if (d.state) onHomeworkStateReceived(d.state);
      } catch (e) {
        console.error('toggleActiveCharWeeklyFocus error:', e);
      }
    }

    function renderHomeworkChecklists(hw) {
      const actName = hw.active_character || '';
      const actChar = (hw.characters || []).find(c => c.name === actName) || (hw.characters || [])[0];
      if (!actChar) return;

      const step = hw.active_step || null;
      const cats = [
        { key: 'daily', title: '일일 숙제', progTextId: 'hw-daily-prog-text', progBarId: 'hw-daily-prog-bar', listId: 'hw-daily-task-list', colTextId: 'hw-cat-collapsed-text-daily' },
        { key: 'focus', title: '집중 숙제', progTextId: 'hw-focus-prog-text', progBarId: 'hw-focus-prog-bar', listId: 'hw-focus-task-list', colTextId: 'hw-cat-collapsed-text-focus' },
        { key: 'weekly', title: '주간 숙제', progTextId: 'hw-weekly-prog-text', progBarId: 'hw-weekly-prog-bar', listId: 'hw-weekly-task-list', colTextId: 'hw-cat-collapsed-text-weekly' },
      ];

      const focusDateBadge = document.getElementById('hw-focus-last-date');
      if (focusDateBadge) {
        focusDateBadge.innerText = actChar.last_focus_completed ? `최근 완료: ${actChar.last_focus_completed}` : '기록 없음';
      }
      const btnWkFocus = document.getElementById('btn-hw-toggle-weekly-focus');
      if (btnWkFocus) {
        if (actChar.weekly_focus_done) {
          btnWkFocus.innerText = '✓집중완료(해제)';
          btnWkFocus.style.color = '#34D399';
        } else {
          btnWkFocus.innerText = '✓집중체크';
          btnWkFocus.style.color = '';
        }
      }

      cats.forEach(cat => {
        applyHwCategoryCollapsedView(cat.key);
        const prog = (actChar.progress && actChar.progress[cat.key]) || { done: 0, total: 0, pct: 0, items: [] };
        const txtEl = document.getElementById(cat.progTextId);
        const barEl = document.getElementById(cat.progBarId);
        const listEl = document.getElementById(cat.listId);
        const colTxtEl = document.getElementById(cat.colTextId);
        const gaugeStr = typeof makeTextGauge === 'function' ? makeTextGauge(prog.done, prog.total, 5) : '';

        if (txtEl) {
          txtEl.innerHTML = `<span class="cli-gauge" style="margin-right: 4px;">${gaugeStr}</span>${prog.done}/${prog.total}`;
        }
        if (barEl) barEl.style.width = `${prog.pct}%`;
        if (colTxtEl) {
          const nextTodo = (prog.items || []).find(it => !it.done);
          const nextStr = nextTodo ? `다음: ${formatTaskNameHtml(nextTodo.name)}` : '모두 완료됨 ✓';
          colTxtEl.innerHTML = `<span class="sym-prefix">&gt;</span><span class="cli-gauge" style="margin-right:4px;">${gaugeStr}</span><strong class="mono-num" style="color:var(--text-bright);">${prog.done}/${prog.total}</strong> 완료 · <span>${nextStr}</span>`;
        }
        if (!listEl) return;

        let html = '';
        (prog.items || []).forEach(item => {
          const isStep = step && step.cat_key === cat.key && step.task_name === item.name;
          const safeChar = actChar.name.replace(/'/g, "\\'");
          const safeTask = item.name.replace(/'/g, "\\'");
          html += `
            <div class="hw-task-row ${item.done ? 'done' : ''} ${isStep ? 'current-step' : ''}" onclick="toggleHomeworkTask('${safeChar}', '${cat.key}', '${safeTask}')">
              <div class="hw-task-left">
                <span class="hw-task-check">${item.done ? '■' : '□'}</span>
                <span class="hw-task-name">${formatTaskNameHtml(item.name)}</span>
              </div>
              <div style="display: flex; align-items: center; gap: 6px;">
                ${isStep ? '<span class="badge" style="font-size: 10px; padding: 0 4px;">▶ 현재 순서</span>' : ''}
                <span class="mono-num" style="font-size: 11px; color: ${item.done ? 'var(--text-dim)' : 'var(--text-muted)'};">${item.done ? '완료' : '대기'}</span>
              </div>
            </div>
          `;
        });

        // In Weekly column, also show unified Weekly Delivery status row!
        if (cat.key === 'weekly') {
          const delivDone = !!actChar.weekly_delivered;
          const delivCnt = actChar.delivery_completed_count || 0;
          const delivTot = actChar.delivery_target_count || 18;
          html += `
            <div class="hw-task-row ${delivDone ? 'done' : ''}" style="border-top: 1px dashed var(--line); margin-top: 2px;" onclick="switchTopAppMode('living')" title="클릭하면 생활 지원(주간 납품) 화면으로 이동합니다">
              <div class="hw-task-left">
                <span class="hw-task-check">${delivDone ? '■' : '□'}</span>
                <span class="hw-task-name">⚒️ 주간 납품 (${delivCnt}/${delivTot} 품목)</span>
              </div>
              <span class="badge-dim" style="font-size: 10px; padding: 0 4px;">생활 지원 &gt;</span>
            </div>
          `;
        }

        if (!html) {
          html = `<div style="padding: 8px; color: var(--text-muted); font-size: var(--fs-sm); text-align: center;">표시할 숙제 항목이 없습니다.</div>`;
        }
        listEl.innerHTML = html;
      });

      // Memo & Delivery quick box
      const memoTitle = document.getElementById('hw-memo-char-title');
      if (memoTitle) memoTitle.innerText = `${actChar.name}${actChar.class ? '(' + actChar.class + ')' : ''}`;
      const memoInput = document.getElementById('hw-char-memo-input');
      if (memoInput && document.activeElement !== memoInput) {
        memoInput.value = actChar.memo || '';
        memoInput.dataset.charName = actChar.name;
      }
      const delivQuick = document.getElementById('hw-delivery-quick-status');
      if (delivQuick) {
        if (actChar.weekly_delivered) {
          delivQuick.innerHTML = `<span class="badge">이번 주 주간 납품 완료 ✓</span>`;
        } else {
          const gaugeStr = typeof makeTextGauge === 'function' ? makeTextGauge(actChar.delivery_completed_count || 0, actChar.delivery_target_count || 18, 5) : '';
          delivQuick.innerHTML = `<span class="cli-gauge" style="margin-right: 4px;">${gaugeStr}</span><span style="color: var(--text-bright);">${actChar.delivery_completed_count || 0}/${actChar.delivery_target_count || 18} 품목 보유</span>`;
        }
      }
    }

    function renderHomeworkTelemetry(hw) {
      const grid = document.getElementById('hw-telemetry-grid');
      const badge = document.getElementById('hw-telemetry-updated-badge');
      if (!grid || !hw) return;

      const actName = hw.active_character || '';
      const actChar = (hw.characters || []).find(c => c.name === actName) || (hw.characters || [])[0];
      if (!actChar) return;

      const st = actChar.stats || {};
      const liveChar = (cachedLiveStatusData && cachedLiveStatusData.character) || {};
      const isLiveMatched = (hw.matched_ingame_character === actChar.name);

      if (badge) {
        badge.className = isLiveMatched ? 'badge mono-num' : 'badge-dim mono-num';
        badge.innerText = isLiveMatched
          ? `현재 접속 중 (${st.updated_at || '실시간'})`
          : (st.updated_at ? `최근 기록: ${st.updated_at}` : '인게임 접속 시 자동 수집');
      }

      const cp = st.combat_power || actChar.combat_power || 0;
      const lvScore = st.living_score || 0;
      const attrScore = st.attractiveness_score || 0;
      const decorScore = st.decor_score || 0;
      const title = st.title || actChar.title || '-';
      const level = st.level || (isLiveMatched ? liveChar.Level : '-') || '-';
      const silver = st.silver_coin !== undefined ? st.silver_coin : '-';
      const tribute = st.tribute !== undefined ? st.tribute : '-';
      const gold = st.gold ? Number(st.gold).toLocaleString() : '-';
      const wings = st.wings !== undefined ? st.wings : '-';
      const bagText = st.max_weight > 0 ? `${st.bag_weight}/${st.max_weight}` : '-';
      const vitals = (isLiveMatched && liveChar.Vitals) ? liveChar.Vitals : {};
      const hpCur = vitals.HealthCurrent ?? liveChar.Hp;
      const hpMax = vitals.HealthMax ?? liveChar.MaxHp;
      const hpText = (isLiveMatched && hpMax) ? `${Number(hpCur || 0).toLocaleString()}/${Number(hpMax).toLocaleString()}` : '-';
      const satVal = vitals.SatietyValue ?? liveChar.SatietyValue ?? liveChar.Satiety;
      const buffCnt = vitals.ActiveBuffCount;
      const satietyText = (isLiveMatched && satVal !== undefined) ? `${satVal}${buffCnt !== undefined ? ` (버프 ${buffCnt})` : ''}` : '-';

      const statsItems = [
        { label: '캐릭터 / 레벨', val: `${actChar.name} (Lv.${level})` },
        { label: '직업 / 칭호', val: `${actChar.class || '-'} · ${title}` },
        { label: '전투력 (Combat)', val: cp ? Number(cp).toLocaleString() : '-' },
        { label: '생활력 / 매력', val: `${lvScore ? Number(lvScore).toLocaleString() : '-'} / ${attrScore ? Number(attrScore).toLocaleString() : '-'}` },
        { label: '은동전 / 마족 공물', val: `${silver} / ${tribute}` },
        { label: '보유 골드 / 날개', val: `${gold} / ${wings}` },
        { label: '가방 무게 / 인테리어', val: `${bagText} / ${decorScore || '-'}` },
        { label: '체력(HP) / 포만감', val: `${hpText} / ${satietyText}` },
      ];

      grid.innerHTML = statsItems.map(item => `
        <div class="hw-stat-box">
          <span class="hw-stat-label">${escapeHtml(item.label)}</span>
          <span class="hw-stat-val">${escapeHtml(String(item.val))}</span>
        </div>
      `).join('');
    }

    function renderHomeworkSummaryTable(hw) {
      const tbody = document.getElementById('hw-summary-tbody');
      if (!tbody || !hw) return;

      let html = '';
      (hw.characters || []).forEach(c => {
        const st = c.stats || {};
        const dp = (c.progress && c.progress.daily) || { done: 0, total: 0, pct: 0 };
        const fp = (c.progress && c.progress.focus) || { done: 0, total: 0, pct: 0 };
        const wp = (c.progress && c.progress.weekly) || { done: 0, total: 0, pct: 0 };
        const cp = st.combat_power || c.combat_power || 0;
        const lv = st.living_score || 0;
        const attr = st.attractiveness_score || 0;
        const weight = st.max_weight > 0 ? `${st.bag_weight}/${st.max_weight}` : '-';
        const coinTrib = (st.silver_coin !== undefined || st.tribute !== undefined) ? `은동전 ${st.silver_coin ?? '-'} · 공물 ${st.tribute ?? '-'}` : '-';
        const safeName = c.name.replace(/'/g, "\\'");
        const recStar = c.is_recommended_focus ? `<span class="badge-dim" style="font-size: 10px; margin-right: 4px;">집중추천</span>` : '';
        const delivHtml = c.weekly_delivered
          ? `<span class="badge">완료 ✓</span>`
          : `<span class="badge-dim mono-num">${c.delivery_completed_count || 0}/${c.delivery_target_count || 18}</span>`;
        const dGauge = typeof makeTextGauge === 'function' ? makeTextGauge(dp.done, dp.total, 5) : '';
        const fGauge = typeof makeTextGauge === 'function' ? makeTextGauge(fp.done, fp.total, 5) : '';
        const wGauge = typeof makeTextGauge === 'function' ? makeTextGauge(wp.done, wp.total, 5) : '';

        html += `
          <tr style="${c.is_active ? 'background: var(--table-hover-bg);' : ''}">
            <td>
              <div style="font-weight: 700; color: var(--text-bright); cursor: pointer;" onclick="selectHomeworkCharacter('${safeName}'); switchHomeworkSubTab('main');">
                ${recStar}${escapeHtml(c.name)} <span style="font-weight: 400; color: var(--text-muted); font-size: 12px;">(${escapeHtml(c.class || '-')})</span>
              </div>
              ${(st.title || c.title) ? `<div style="font-size: 11px; color: var(--text-muted);">${escapeHtml(st.title || c.title)}</div>` : ''}
            </td>
            <td class="mono-num">
              <div style="font-weight: 700; color: var(--text-bright);">전투 ${cp ? Number(cp).toLocaleString() : '-'}</div>
              <div style="font-size: 11px; color: var(--text-muted);">생활 ${lv || '-'} · 매력 ${attr || '-'}</div>
            </td>
            <td class="mono-num" style="text-align: center;">
              <div>${escapeHtml(weight)}</div>
              <div style="font-size: 11px; color: var(--text-muted);">${escapeHtml(coinTrib)}</div>
            </td>
            <td class="mono-num" style="text-align: center;">
              <div><span class="cli-gauge">${dGauge}</span></div>
              <div style="font-size: 11px; color: ${dp.pct >= 100 ? 'var(--text-bright)' : 'var(--text-muted)'}; font-weight: 700;">${dp.done}/${dp.total} (${dp.pct}%)</div>
            </td>
            <td class="mono-num" style="text-align: center;">
              <div><span class="cli-gauge">${fGauge}</span> <span style="font-size: 11px; color: ${fp.pct >= 100 ? 'var(--text-bright)' : 'var(--text-muted)'}; font-weight: 700;">${fp.done}/${fp.total}</span></div>
              <div style="font-size: 11px; color: var(--text-muted);">${c.last_focus_completed || '기록 없음'}</div>
            </td>
            <td class="mono-num" style="text-align: center;">
              <div><span class="cli-gauge">${wGauge}</span></div>
              <div style="font-size: 11px; color: ${wp.pct >= 100 ? 'var(--text-bright)' : 'var(--text-muted)'}; font-weight: 700;">${wp.done}/${wp.total} (${wp.pct}%)</div>
            </td>
            <td style="text-align: center;">${delivHtml}</td>
            <td style="max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-muted); font-size: var(--fs-sm);" title="${escapeHtml(c.memo || '')}">
              ${escapeHtml(c.memo || '-')}
            </td>
            <td style="text-align: center; white-space: nowrap;">
              <button type="button" class="btn btn-sm btn-boxed" onclick="selectHomeworkCharacter('${safeName}'); switchHomeworkSubTab('main');">열기</button>
            </td>
          </tr>
        `;
      });
      tbody.innerHTML = html;
    }

    function renderHomeworkMatrixTable(hw) {
      const thead = document.getElementById('hw-matrix-thead');
      const tbody = document.getElementById('hw-matrix-tbody');
      if (!thead || !tbody || !hw) return;

      const chars = hw.characters || [];
      const weeklyTasks = (hw.categories && hw.categories.weekly && hw.categories.weekly.tasks) || [];
      const targetMap = (hw.task_character_mapping && hw.task_character_mapping.weekly) || {};

      let headHtml = `<tr><th style="min-width: 170px;">주간 숙제 항목</th>`;
      chars.forEach(c => {
        const safeName = c.name.replace(/'/g, "\\'");
        headHtml += `<th style="text-align: center; min-width: 90px; cursor: pointer;" onclick="selectHomeworkCharacter('${safeName}'); switchHomeworkSubTab('main');" title="클릭하여 ${escapeHtml(c.name)} 상세 보기">
          <div>${escapeHtml(c.name)}</div>
          <div style="font-size: 10px; color: var(--text-muted); font-weight: 400;">${escapeHtml(c.class || '')}</div>
        </th>`;
      });
      headHtml += `<th style="text-align: center; min-width: 110px;">일괄 처리</th></tr>`;
      thead.innerHTML = headHtml;

      let bodyHtml = '';
      weeklyTasks.forEach(taskName => {
        const safeTask = taskName.replace(/'/g, "\\'");
        const allowedList = targetMap[taskName];
        bodyHtml += `<tr><td style="font-weight: 700; color: var(--text-bright);">${formatTaskNameHtml(taskName)}</td>`;
        chars.forEach(c => {
          const safeChar = c.name.replace(/'/g, "\\'");
          const isAllowed = !Array.isArray(allowedList) || allowedList.includes(c.name);
          if (!isAllowed) {
            bodyHtml += `<td class="hw-matrix-cell excluded" title="대상 제외 캐릭터">-</td>`;
          } else {
            const wItems = (c.progress && c.progress.weekly && c.progress.weekly.items) || [];
            const found = wItems.find(x => x.name === taskName);
            const done = !!(found && found.done);
            bodyHtml += `<td class="hw-matrix-cell ${done ? 'done' : 'todo'}" onclick="toggleHomeworkTask('${safeChar}', 'weekly', '${safeTask}')">${done ? '■ 완료' : '□ 대기'}</td>`;
          }
        });
        bodyHtml += `<td style="text-align: center; white-space: nowrap;">
          <button type="button" class="btn btn-sm btn-boxed" onclick="batchWeeklyTaskAllChars('${safeTask}', true)">전체완료</button>
          <button type="button" class="btn btn-sm btn-boxed" onclick="batchWeeklyTaskAllChars('${safeTask}', false)">초기화</button>
        </td></tr>`;
      });

      // Add Weekly Delivery row at the bottom of the Matrix
      bodyHtml += `<tr style="border-top: 2px solid var(--line);"><td style="font-weight: 700; color: var(--text-bright);">⚒️ 주간 납품 (18품목)</td>`;
      chars.forEach(c => {
        const done = !!c.weekly_delivered;
        bodyHtml += `<td class="hw-matrix-cell ${done ? 'done' : 'todo'}" onclick="switchTopAppMode('living')" title="생활 지원 탭에서 관리">${done ? '■ 완료' : `□ ${c.delivery_completed_count || 0}/${c.delivery_target_count || 18}`}</td>`;
      });
      bodyHtml += `<td style="text-align: center;"><button type="button" class="btn btn-sm btn-boxed" onclick="switchTopAppMode('living')">납품 탭 이동</button></td></tr>`;

      tbody.innerHTML = bodyHtml;
    }

    function renderHomeworkSettings(hw, force = false) {
      if (!hw) return;
      const settingsView = document.getElementById('hw-subview-settings');
      if (!force && settingsView && settingsView.contains(document.activeElement)) {
        return;
      }
      // 1. Characters CRUD table
      const charTbody = document.getElementById('hw-settings-char-tbody');
      if (charTbody) {
        if (!force && charTbody.contains(document.activeElement)) {
          // Keep active editing intact
        } else {
          let html = '';
          (hw.characters || []).forEach((c, idx) => {
            const safeName = c.name.replace(/'/g, "\\'");
            html += `
              <tr>
                <td><input type="text" id="hw-edit-char-name-${idx}" class="input-text" value="${escapeHtml(c.name)}" style="width: 100px;" onkeydown="if(event.key==='Enter'){event.preventDefault();saveHomeworkCharacterRow('${safeName}', ${idx});}"></td>
                <td><input type="text" id="hw-edit-char-class-${idx}" class="input-text" value="${escapeHtml(c.class || '')}" style="width: 95px;" onkeydown="if(event.key==='Enter'){event.preventDefault();saveHomeworkCharacterRow('${safeName}', ${idx});}"></td>
                <td><input type="number" id="hw-edit-char-cp-${idx}" class="input-text" value="${c.combat_power || 0}" style="width: 95px;" onkeydown="if(event.key==='Enter'){event.preventDefault();saveHomeworkCharacterRow('${safeName}', ${idx});}"></td>
                <td><input type="text" id="hw-edit-char-title-${idx}" class="input-text" value="${escapeHtml(c.title || '')}" style="width: 110px;" onkeydown="if(event.key==='Enter'){event.preventDefault();saveHomeworkCharacterRow('${safeName}', ${idx});}"></td>
                <td style="text-align: center; white-space: nowrap;">
                  <button type="button" class="btn btn-sm" onclick="saveHomeworkCharacterRow('${safeName}', ${idx})">저장</button>
                  <button type="button" class="btn btn-sm" onclick="moveHomeworkCharacter('${safeName}', -1)">▲</button>
                  <button type="button" class="btn btn-sm" onclick="moveHomeworkCharacter('${safeName}', 1)">▼</button>
                  <button type="button" class="btn btn-sm" onclick="deleteHomeworkCharacter('${safeName}')">삭제</button>
                </td>
              </tr>
            `;
          });
          charTbody.innerHTML = html;
        }
      }

      // 2. Tasks & per-character target filter
      renderHomeworkSettingsTasks(force);

      // 3. Discord webhook settings
      const st = hw.settings || {};
      const whUrl = document.getElementById('hw-discord-webhook-input');
      if (whUrl && (force || document.activeElement !== whUrl)) whUrl.value = st.discord_webhook_url || '';
      const whEvents = st.webhook_events || {};
      const setChk = (id, val) => {
        const el = document.getElementById(id);
        if (el && (force || document.activeElement !== el)) el.checked = (val !== false);
      };
      setChk('hw-wh-dungeon', whEvents.dungeon);
      setChk('hw-wh-abyss', whEvents.abyss);
      setChk('hw-wh-hunting', whEvents.hunting_ground);
      setChk('hw-wh-gathering', whEvents.gathering);
      setChk('hw-wh-crafting', whEvents.crafting);
    }

    function renderHomeworkSettingsTasks(force = false) {
      const container = document.getElementById('hw-settings-tasks-container');
      const catSel = document.getElementById('hw-settings-cat-select');
      if (!container || !catSel || !cachedHomeworkState) return;

      if (!force && container.contains(document.activeElement)) {
        return;
      }

      const catKey = catSel.value || 'daily';
      const tasks = (cachedHomeworkState.categories && cachedHomeworkState.categories[catKey] && cachedHomeworkState.categories[catKey].tasks) || [];
      const targetMap = (cachedHomeworkState.task_character_mapping && cachedHomeworkState.task_character_mapping[catKey]) || {};
      const allChars = (cachedHomeworkState.characters || []).map(c => c.name);

      let html = '';
      tasks.forEach((tName, idx) => {
        const safeTask = tName.replace(/'/g, "\\'");
        const allowed = targetMap[tName]; // null/undefined means all characters
        const charChecks = allChars.map(cName => {
          const checked = !Array.isArray(allowed) || allowed.includes(cName);
          const safeChar = cName.replace(/'/g, "\\'");
          return `<label style="display: inline-flex; align-items: center; gap: 3px; font-size: 11px; cursor: pointer;">
            <input type="checkbox" ${checked ? 'checked' : ''} onchange="toggleHomeworkTaskTargetChar('${catKey}', '${safeTask}', '${safeChar}', this.checked)">
            <span>${escapeHtml(cName)}</span>
          </label>`;
        }).join(' ');

        html += `
          <div style="padding: 8px 10px; background: var(--bg-sub-panel); border: 1px solid var(--line-subtle);">
            <div style="display: flex; justify-content: space-between; align-items: center; gap: 6px; margin-bottom: 6px;">
              <input type="text" id="hw-edit-task-${idx}" class="input-text" value="${escapeHtml(tName)}" style="flex: 1;" onkeydown="if(event.key==='Enter'){event.preventDefault();renameHomeworkTaskItem('${catKey}', '${safeTask}', ${idx});}">
              <div style="display: flex; gap: 4px;">
                <button type="button" class="btn btn-sm" onclick="renameHomeworkTaskItem('${catKey}', '${safeTask}', ${idx})">이름변경</button>
                <button type="button" class="btn btn-sm" onclick="moveHomeworkTaskItem('${catKey}', '${safeTask}', -1)">▲</button>
                <button type="button" class="btn btn-sm" onclick="moveHomeworkTaskItem('${catKey}', '${safeTask}', 1)">▼</button>
                <button type="button" class="btn btn-sm" onclick="deleteHomeworkTaskItem('${catKey}', '${safeTask}')">삭제</button>
              </div>
            </div>
            <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">
              <span style="font-size: 11px; color: var(--text-muted);">대상 캐릭터:</span>
              ${charChecks}
            </div>
          </div>
        `;
      });
      container.innerHTML = html || '<div style="color: var(--text-muted); font-size: var(--fs-sm);">등록된 숙제가 없습니다.</div>';
    }

    async function selectHomeworkCharacter(charName) {
      try {
        const res = await fetch('/api/homework/select_character', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ char_name: charName })
        });
        const d = await res.json();
        if (d.characters_info) renderDeliveryCharacterBar(d.characters_info);
        if (d.homework) onHomeworkStateReceived(d.homework);
      } catch (e) {
        console.error('selectHomeworkCharacter error:', e);
      }
    }

    async function toggleHomeworkTask(charName, category, taskName) {
      try {
        const res = await fetch('/api/homework/toggle_task', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ char_name: charName, category: category, task_name: taskName })
        });
        const d = await res.json();
        if (d.homework) onHomeworkStateReceived(d.homework);
      } catch (e) {
        console.error('toggleHomeworkTask error:', e);
      }
    }

    async function completeCurrentHomeworkStep() {
      try {
        const res = await fetch('/api/homework/complete_step', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({})
        });
        const d = await res.json();
        if (d.state) onHomeworkStateReceived(d.state);
      } catch (e) {
        console.error('completeCurrentHomeworkStep error:', e);
      }
    }

    async function rollbackHomeworkStep() {
      try {
        const res = await fetch('/api/homework/rollback_step', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({})
        });
        const d = await res.json();
        if (d.state) onHomeworkStateReceived(d.state);
      } catch (e) {
        console.error('rollbackHomeworkStep error:', e);
      }
    }

    async function batchHomeworkCategory(category, completed) {
      if (!cachedHomeworkState) return;
      const charName = cachedHomeworkState.active_character;
      try {
        const res = await fetch('/api/homework/batch_category', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ char_name: charName, category: category, completed: !!completed })
        });
        const d = await res.json();
        if (d.homework) onHomeworkStateReceived(d.homework);
      } catch (e) {
        console.error('batchHomeworkCategory error:', e);
      }
    }

    async function batchWeeklyTaskAllChars(taskName, completed) {
      try {
        const res = await fetch('/api/homework/batch_weekly_task', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ task_name: taskName, completed: !!completed })
        });
        const d = await res.json();
        if (d.homework) onHomeworkStateReceived(d.homework);
      } catch (e) {
        console.error('batchWeeklyTaskAllChars error:', e);
      }
    }

    async function saveActiveCharacterMemo() {
      const memoInput = document.getElementById('hw-char-memo-input');
      const statusEl = document.getElementById('hw-memo-save-status');
      if (!memoInput || !cachedHomeworkState) return;
      const charName = memoInput.dataset.charName || cachedHomeworkState.active_character;
      const memoText = memoInput.value || '';
      try {
        if (statusEl) statusEl.innerText = '저장 중...';
        const res = await fetch('/api/homework/save_memo', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ char_name: charName, memo: memoText })
        });
        const d = await res.json();
        if (d.homework) onHomeworkStateReceived(d.homework);
        if (statusEl) statusEl.innerText = '✓ 저장 완료';
      } catch (e) {
        if (statusEl) statusEl.innerText = '저장 실패';
      }
    }

    async function toggleHomeworkSetting(key) {
      if (!cachedHomeworkState || !cachedHomeworkState.settings) return;
      const currentVal = key === 'focus_mode_enabled'
        ? !!cachedHomeworkState.settings[key]
        : (cachedHomeworkState.settings[key] !== false);
      const payload = {};
      payload[key] = !currentVal;
      try {
        const res = await fetch('/api/homework/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const d = await res.json();
        if (d.state) onHomeworkStateReceived(d.state);
      } catch (e) {
        console.error('toggleHomeworkSetting error:', e);
      }
    }

    async function saveHomeworkDiscordSettings() {
      const urlEl = document.getElementById('hw-discord-webhook-input');
      const payload = {
        discord_webhook_url: urlEl ? urlEl.value.trim() : '',
        webhook_events: {
          dungeon: !!(document.getElementById('hw-wh-dungeon') && document.getElementById('hw-wh-dungeon').checked),
          abyss: !!(document.getElementById('hw-wh-abyss') && document.getElementById('hw-wh-abyss').checked),
          hunting_ground: !!(document.getElementById('hw-wh-hunting') && document.getElementById('hw-wh-hunting').checked),
          gathering: !!(document.getElementById('hw-wh-gathering') && document.getElementById('hw-wh-gathering').checked),
          crafting: !!(document.getElementById('hw-wh-crafting') && document.getElementById('hw-wh-crafting').checked),
        }
      };
      try {
        const res = await fetch('/api/homework/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const d = await res.json();
        if (d.state) onHomeworkStateReceived(d.state);
      } catch (e) {
        console.error('saveHomeworkDiscordSettings error:', e);
      }
    }

    async function addHomeworkCharacter() {
      const nameEl = document.getElementById('hw-new-char-name');
      const clsEl = document.getElementById('hw-new-char-class');
      const cpEl = document.getElementById('hw-new-char-cp');
      const titleEl = document.getElementById('hw-new-char-title');
      const name = nameEl ? nameEl.value.trim() : '';
      if (!name) { alert('캐릭터 이름을 입력해주세요.'); return; }
      try {
        const res = await fetch('/api/homework/character_crud', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            action: 'add',
            name: name,
            char_class: clsEl ? clsEl.value.trim() : '',
            combat_power: cpEl ? Number(cpEl.value || 0) : 0,
            title: titleEl ? titleEl.value.trim() : ''
          })
        });
        const d = await res.json();
        if (!d.ok) { alert(d.message || '추가 실패'); return; }
        if (nameEl) nameEl.value = '';
        if (clsEl) clsEl.value = '';
        if (cpEl) cpEl.value = '';
        if (titleEl) titleEl.value = '';
        if (d.characters_info) renderDeliveryCharacterBar(d.characters_info);
        if (d.state) {
          onHomeworkStateReceived(d.state, null, null, false);
          renderHomeworkSettings(d.state, true);
        }
      } catch (e) {
        alert('캐릭터 추가 오류: ' + e);
      }
    }

    async function saveHomeworkCharacterRow(oldName, idx) {
      const newName = (document.getElementById(`hw-edit-char-name-${idx}`) || {}).value || oldName;
      const charClass = (document.getElementById(`hw-edit-char-class-${idx}`) || {}).value || '';
      const cp = Number((document.getElementById(`hw-edit-char-cp-${idx}`) || {}).value || 0);
      const title = (document.getElementById(`hw-edit-char-title-${idx}`) || {}).value || '';
      try {
        const res = await fetch('/api/homework/character_crud', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            action: 'update',
            old_name: oldName,
            new_name: newName.trim(),
            char_class: charClass.trim(),
            combat_power: cp,
            title: title.trim()
          })
        });
        const d = await res.json();
        if (!d.ok) { alert(d.message || '수정 실패'); return; }
        if (d.characters_info) renderDeliveryCharacterBar(d.characters_info);
        if (d.state) {
          onHomeworkStateReceived(d.state, null, null, false);
          renderHomeworkSettings(d.state, true);
        }
      } catch (e) {
        alert('캐릭터 수정 오류: ' + e);
      }
    }

    async function moveHomeworkCharacter(name, direction) {
      try {
        const res = await fetch('/api/homework/character_crud', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'move', name: name, direction: direction })
        });
        const d = await res.json();
        if (d.characters_info) renderDeliveryCharacterBar(d.characters_info);
        if (d.state) {
          onHomeworkStateReceived(d.state, null, null, false);
          renderHomeworkSettings(d.state, true);
        }
      } catch (e) {}
    }

    async function deleteHomeworkCharacter(name) {
      if (!confirm(`'${name}' 캐릭터 프로필을 삭제하시겠습니까? (숙제 및 주간 납품 탭에서 함께 삭제됩니다)`)) return;
      try {
        const res = await fetch('/api/homework/character_crud', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'delete', name: name })
        });
        const d = await res.json();
        if (!d.ok) { alert(d.message || '삭제 실패'); return; }
        if (d.characters_info) renderDeliveryCharacterBar(d.characters_info);
        if (d.state) {
          onHomeworkStateReceived(d.state, null, null, false);
          renderHomeworkSettings(d.state, true);
        }
      } catch (e) {}
    }

    async function addHomeworkTaskItem() {
      const catSel = document.getElementById('hw-settings-cat-select');
      const inputEl = document.getElementById('hw-new-task-input');
      const catKey = catSel ? catSel.value : 'daily';
      const taskName = inputEl ? inputEl.value.trim() : '';
      if (!taskName) { alert('추가할 숙제 이름을 입력해주세요.'); return; }
      try {
        const res = await fetch('/api/homework/task_crud', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'add', category: catKey, task_name: taskName })
        });
        const d = await res.json();
        if (!d.ok) { alert(d.message || '추가 실패'); return; }
        if (inputEl) inputEl.value = '';
        if (d.state) {
          onHomeworkStateReceived(d.state, null, null, false);
          renderHomeworkSettings(d.state, true);
        }
      } catch (e) {}
    }

    async function renameHomeworkTaskItem(catKey, oldName, idx) {
      const el = document.getElementById(`hw-edit-task-${idx}`);
      const newName = el ? el.value.trim() : '';
      if (!newName) {
        alert('변경할 숙제 이름을 입력해주세요.');
        if (el) el.focus();
        return;
      }
      if (newName === oldName) return;
      try {
        const res = await fetch('/api/homework/task_crud', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'rename', category: catKey, old_name: oldName, new_name: newName })
        });
        const d = await res.json();
        if (!d.ok) { alert(d.message || '이름 변경 실패'); return; }
        if (d.state) {
          onHomeworkStateReceived(d.state, null, null, false);
          renderHomeworkSettings(d.state, true);
        }
      } catch (e) {
        alert('이름 변경 오류: ' + e);
      }
    }

    async function moveHomeworkTaskItem(catKey, taskName, direction) {
      try {
        const res = await fetch('/api/homework/task_crud', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'move', category: catKey, task_name: taskName, direction: direction })
        });
        const d = await res.json();
        if (d.state) {
          onHomeworkStateReceived(d.state, null, null, false);
          renderHomeworkSettings(d.state, true);
        }
      } catch (e) {}
    }

    async function deleteHomeworkTaskItem(catKey, taskName) {
      if (!confirm(`'${taskName}' 숙제 항목을 삭제하시겠습니까?`)) return;
      try {
        const res = await fetch('/api/homework/task_crud', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'delete', category: catKey, task_name: taskName })
        });
        const d = await res.json();
        if (d.state) {
          onHomeworkStateReceived(d.state, null, null, false);
          renderHomeworkSettings(d.state, true);
        }
      } catch (e) {}
    }

    async function toggleHomeworkTaskTargetChar(catKey, taskName, charName, checked) {
      if (!cachedHomeworkState) return;
      const allChars = (cachedHomeworkState.characters || []).map(c => c.name);
      const targetMap = (cachedHomeworkState.task_character_mapping && cachedHomeworkState.task_character_mapping[catKey]) || {};
      let currentList = Array.isArray(targetMap[taskName]) ? [...targetMap[taskName]] : [...allChars];
      if (checked) {
        if (!currentList.includes(charName)) currentList.push(charName);
      } else {
        currentList = currentList.filter(x => x !== charName);
      }
      const targetPayload = (currentList.length >= allChars.length) ? null : currentList;
      try {
        const res = await fetch('/api/homework/task_crud', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'set_targets', category: catKey, task_name: taskName, target_chars: targetPayload })
        });
        const d = await res.json();
        if (d.state) {
          onHomeworkStateReceived(d.state, null, null, false);
          renderHomeworkSettings(d.state, true);
        }
      } catch (e) {}
    }

    async function resetHomeworkCategoryDefaults() {
      const catSel = document.getElementById('hw-settings-cat-select');
      const catKey = catSel ? catSel.value : 'daily';
      if (!confirm('선택한 카테고리의 숙제 목록을 기본값으로 복원하시겠습니까?')) return;
      try {
        const res = await fetch('/api/homework/task_crud', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'reset_defaults', category: catKey })
        });
        const d = await res.json();
        if (d.state) {
          onHomeworkStateReceived(d.state, null, null, false);
          renderHomeworkSettings(d.state, true);
        }
      } catch (e) {}
    }

    async function exportHomeworkBackup() {
      try {
        const res = await fetch('/api/homework/export_backup');
        const bundle = await res.json();
        const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `mobi_supporter_homework_backup_${new Date().toISOString().slice(0,10)}.json`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
      } catch (e) {
        alert('백업 내보내기 실패: ' + e);
      }
    }

    async function importHomeworkBackupFile(event) {
      const file = event.target.files && event.target.files[0];
      if (!file) return;
      try {
        const text = await file.text();
        const bundle = JSON.parse(text);
        const res = await fetch('/api/homework/import_backup', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(bundle)
        });
        const d = await res.json();
        if (!d.ok) { alert(d.message || '백업 불러오기 실패'); return; }
        if (d.characters_info) renderDeliveryCharacterBar(d.characters_info);
        if (d.state) {
          onHomeworkStateReceived(d.state, null, null, false);
          renderHomeworkSettings(d.state, true);
        }
        alert(d.message || '백업 데이터를 성공적으로 복원했습니다.');
      } catch (e) {
        alert('백업 파일 파싱 오류: ' + e);
      } finally {
        event.target.value = '';
      }
    }

    async function reimportLegacyOverlayData() {
      if (!confirm("기존 '모비 숙제 오버레이' 폴더의 캐릭터 및 숙제 데이터를 다시 가져와 병합하시겠습니까?")) return;
      try {
        const res = await fetch('/api/homework/reimport_legacy', { method: 'POST' });
        const d = await res.json();
        if (!d.ok) { alert(d.message || '가져오기 실패'); return; }
        if (d.characters_info) renderDeliveryCharacterBar(d.characters_info);
        if (d.state) {
          onHomeworkStateReceived(d.state, null, null, false);
          renderHomeworkSettings(d.state, true);
        }
        alert(d.message);
      } catch (e) {
        alert('레거시 데이터 가져오기 오류: ' + e);
      }
    }

    async function refreshHomeworkLiveMissions() {
      if (isCurrentlyBusy) return;
      const dailyEl = document.getElementById('hw-live-daily-missions-list');
      const weeklyEl = document.getElementById('hw-live-weekly-missions-list');
      const questsEl = document.getElementById('hw-live-quests-list');
      const dailyCnt = document.getElementById('hw-live-daily-count');
      const weeklyCnt = document.getElementById('hw-live-weekly-count');
      const questsCnt = document.getElementById('hw-live-quests-count');

      try {
        const [mRes, qRes] = await Promise.all([
          fetch('/api/missions'),
          fetch('/api/quests')
        ]);
        const mData = await mRes.json();
        const qData = await qRes.json();

        // 1. Daily Missions
        const dList = (mData.daily && (mData.daily.Missions || mData.daily.DailyMissions)) || (Array.isArray(mData.daily) ? mData.daily : []);
        if (dailyCnt) dailyCnt.innerText = `${dList.length}건`;
        if (dailyEl) {
          if (dList.length === 0) {
            dailyEl.innerHTML = `<div style="color: var(--text-muted);">표시할 인게임 일일 미션이 없거나 게임 미연결 상태입니다.</div>`;
          } else {
            dailyEl.innerHTML = dList.map(m => {
              const title = m.Title || m.Name || m.Description || ('일일 미션 #' + (m.MissionId || ''));
              const desc = (m.Title && m.Description && m.Title !== m.Description) ? ` (${m.Description})` : '';
              const cur = m.CurrentCount ?? m.Progress ?? 0;
              const max = m.GoalCount ?? m.TargetCount ?? m.MaxCount ?? 1;
              const claimed = !!(m.IsRewardReceived || m.IsRewardClaimed || m.RewardClaimed);
              const done = claimed || !!(m.IsCompleted || (max > 0 && cur >= max));
              return `<div style="display: flex; justify-content: space-between; align-items: center; padding: 4px 6px; border-bottom: 1px dashed var(--line-subtle);">
                <span style="color: ${done ? 'var(--text-dim)' : 'var(--text-bright)'}; ${claimed ? 'text-decoration: line-through;' : ''}">${done ? '■' : '□'} ${escapeHtml(String(title + desc))}</span>
                <span class="mono-num" style="font-size: 11px; color: ${claimed ? 'var(--text-dim)' : (done ? 'var(--text-bright)' : 'var(--text-muted)')}; font-weight: ${done && !claimed ? '700' : '400'};">${claimed ? '보상수령✓' : `${cur}/${max}`}</span>
              </div>`;
            }).join('');
          }
        }

        // 2. Weekly Missions
        const wObj = mData.weekly || {};
        const wList = wObj.Missions || wObj.WeeklyMissions || (Array.isArray(wObj) ? wObj : []);
        const abyssCnt = wObj.AbyssClearRewardClaimCount ?? null;
        const guildReg = wObj.GuildRegularTask || null;
        let wHtml = '';
        if (abyssCnt !== null || guildReg) {
          wHtml += `<div style="padding: 6px 8px; background: var(--bg-panel); border: 1px solid var(--line-subtle); margin-bottom: 4px; font-size: 11px;">
            ${abyssCnt !== null ? `<div><span class="sym-prefix">&gt;</span>어비스 주간 클리어 보상: <strong class="mono-num" style="color: var(--text-bright);">${abyssCnt}회</strong></div>` : ''}
            ${guildReg ? `<div><span class="sym-prefix">&gt;</span>길드 정기 협동(던전/심층/사냥터): <strong class="mono-num" style="color: var(--text-bright);">${guildReg.Dungeon || 0} / ${guildReg.DeepDungeon || 0} / ${guildReg.HuntingGround || 0}</strong></div>` : ''}
          </div>`;
        }
        if (Array.isArray(wList) && wList.length > 0) {
          wHtml += wList.map(m => {
            const title = m.Title || m.Name || m.Description || ('주간 미션 #' + (m.MissionId || ''));
            const desc = (m.Title && m.Description && m.Title !== m.Description) ? ` (${m.Description})` : '';
            const cur = m.CurrentCount ?? m.Progress ?? 0;
            const max = m.GoalCount ?? m.TargetCount ?? m.MaxCount ?? 1;
            const claimed = !!(m.IsRewardReceived || m.IsRewardClaimed || m.RewardClaimed);
            const done = claimed || !!(m.IsCompleted || (max > 0 && cur >= max));
            return `<div style="display: flex; justify-content: space-between; align-items: center; padding: 4px 6px; border-bottom: 1px dashed var(--line-subtle);">
              <span style="color: ${done ? 'var(--text-dim)' : 'var(--text-bright)'}; ${claimed ? 'text-decoration: line-through;' : ''}">${done ? '■' : '□'} ${escapeHtml(String(title + desc))}</span>
              <span class="mono-num" style="font-size: 11px; color: ${claimed ? 'var(--text-dim)' : (done ? 'var(--text-bright)' : 'var(--text-muted)')}; font-weight: ${done && !claimed ? '700' : '400'};">${claimed ? '보상수령✓' : `${cur}/${max}`}</span>
            </div>`;
          }).join('');
        }
        if (weeklyCnt) weeklyCnt.innerText = `${Array.isArray(wList) ? wList.length : 0}건`;
        if (weeklyEl) weeklyEl.innerHTML = wHtml || `<div style="color: var(--text-muted);">표시할 인게임 주간 미션이 없거나 게임 미연결 상태입니다.</div>`;

        // 3. Active Quests & Dungeon state
        const qList = (qData && qData.quests) || [];
        if (questsCnt) questsCnt.innerText = `${qList.length}건`;
        if (questsEl) {
          const act = (cachedLiveStatusData && cachedLiveStatusData.activity) || {};
          const dg = act.Dungeon || {};
          let qHtml = '';
          if (dg && dg.Name) {
            qHtml += `<div style="padding: 6px 8px; background: var(--bg-panel); border-left: 3px solid var(--invert-bg); margin-bottom: 4px;">
              <strong>[던전/필드 진행]</strong> ${escapeHtml(dg.Name)} <span class="mono-num">(${escapeHtml(dg.State || '진행 중')})</span>
            </div>`;
          }
          if (qList.length > 0) {
            qHtml += qList.slice(0, 12).map(q => {
              const qTitle = q.Title || q.Name || '퀘스트';
              const cat = q.CategoryDisplayName || q.Category || '';
              const objs = (q.Objectives || []).map(o => `${o.Description || ''} (${o.CurrentCount ?? 0}/${o.TargetCount ?? 1})`).join(', ');
              return `<div style="padding: 4px 6px; border-bottom: 1px dashed var(--line-subtle);">
                <div style="font-weight: 600; color: var(--text-bright);">${cat ? `<span class="badge-dim" style="font-size: 10px; margin-right: 4px;">${escapeHtml(cat)}</span>` : ''}${escapeHtml(qTitle)}</div>
                ${objs ? `<div style="font-size: 11px; color: var(--text-muted); margin-top: 2px;">${escapeHtml(objs)}</div>` : ''}
              </div>`;
            }).join('');
          }
          questsEl.innerHTML = qHtml || `<div style="color: var(--text-muted);">진행 중인 퀘스트 정보가 없습니다.</div>`;
        }
      } catch (e) {
        console.warn('refreshHomeworkLiveMissions error:', e);
      }
    }

    // Safe Init View (each call isolated in try/catch)
    function initAll() {
      try { applyTopAppModeView(); } catch(e) { console.error('applyTopAppModeView error:', e); }
      try { updateStickyHeaderOffset(); } catch(e) {}
      try { applyAlteringView(); } catch(e) { console.error('applyAlteringView error:', e); }
      try { applyQuickAlterView(); } catch(e) { console.error('applyQuickAlterView error:', e); }
      try { applyDeliveryTargetsView(); } catch(e) { console.error('applyDeliveryTargetsView error:', e); }
      try { applyMainModeView(); } catch(e) { console.error('applyMainModeView error:', e); }
      try { loadSettings(); } catch(e) { console.error('loadSettings error:', e); }
      try { updateMasterCommandBar(); } catch(e) { console.error('updateMasterCommandBar error:', e); }
      try { loadPresets(); } catch(e) { console.error('loadPresets error:', e); }
      try { loadItemCatalog(); } catch(e) { console.error('loadItemCatalog error:', e); }
      try { applyHideSatisfiedBomUI(); } catch(e) {}
      try { fetchUserDataInfo(); } catch(e) { console.error('fetchUserDataInfo error:', e); }
      try { pollLogs(); } catch(e) { console.error('pollLogs error:', e); }

      // Status executes first immediately
      try { updateStatus(); } catch(e) { console.error('updateStatus error:', e); }

      // Stagger background queries so active mode renders first without duplicate requests
      setTimeout(() => {
        try {
          if (currentMainMode !== 'delivery') loadDeliveryTargets();
        } catch(e) {}
      }, 250);
      setTimeout(() => {
        try {
          if (currentMainMode !== 'custom_craft') loadCustomTargets();
        } catch(e) {}
      }, 450);
      setTimeout(() => { try { loadAlteringQueue(); } catch(e) {} }, 600);
      setTimeout(() => {
        try {
          if (currentMainMode !== 'quick_alter') loadQuickAlterPlan();
        } catch(e) {}
      }, 950);

      setInterval(() => { try { updateStatus(); } catch(e) {} }, 3000);
      setInterval(() => {
        if (document.hidden || isCurrentlyBusy) return;
        try {
          if (currentMainMode === 'delivery') {
            loadDeliveryTargets();
            loadBatchPlan();
          } else if (currentMainMode === 'custom_craft') {
            loadCustomTargets();
            loadCustomPlan();
          } else if (currentMainMode === 'quick_alter') {
            loadQuickAlterPlan();
          }
        } catch(e) {}
      }, 12000);
      setInterval(() => { if (document.hidden) return; try { loadAlteringQueue(); } catch(e) {} }, 4000);
      setInterval(() => { if (document.hidden) return; try { pollLogs(); } catch(e) {} }, 2500);
    }

    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', initAll);
    } else {
      initAll();
    }
  </script>
</body>
</html>
"""

if __name__ == "__main__":
    try:
        start_server(8080)
    except Exception:
        import traceback
        base_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
        crash_path = os.path.join(base_dir, "crash.log")
        with open(crash_path, "w", encoding="utf-8") as f:
            traceback.print_exc(file=f)

