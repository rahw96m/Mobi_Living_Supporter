import os
import sys
import json
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from typing import Dict, Any, List

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
from mabi_cli import MabinogiCLI, MabinogiCLIError, get_saved_cli_path, find_cli_path
from delivery_manager import DeliveryManager, DeliveryTask, alarm_store, delivery_target_store, delivery_preset_store, custom_target_store, settings_store, get_facility_for_material

BASE_DIR = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
EXE_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))

def get_icon_bytes(format_type="png"):
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
                    return f.read()
            except Exception:
                pass
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

def schedule_shutdown(delay: float = 60.0, reason: str = "웹 대시보드가 닫혔습니다."):
    global shutdown_timer
    # Ignore spurious pagehide/disconnect within first 10 seconds of server start
    if (time.time() - server_start_time) < 10.0:
        return
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
            print(f"\n🔌 [자동 종료] {reason} 마비노기 모바일도 실행 중이 아니므로 서버를 안전하게 종료합니다.")
            shutdown_server()
        shutdown_timer = threading.Timer(delay, _trigger)
        shutdown_timer.daemon = True
        shutdown_timer.start()

def shutdown_server():
    global is_shutting_down, server_instance
    if is_shutting_down:
        return
    is_shutting_down = True
    print("\n=========================================================")
    print("🔌 [서버 자동 종료] 웹 대시보드가 종료되어 서버와 프로세스를 안전하게 정리합니다.")
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
        if not has_client_connected:
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
        # CRITICAL 3: Only shut down if no heartbeat for more than 30 minutes (1800s) AND game is closed
        if (now - last_heartbeat_time) > 1800.0:
            print("\n🔌 [장시간 미사용 감지] 마비노기 모바일이 종료되었고 30분 이상 대시보드 신호가 없어 안전하게 종료합니다...")
            shutdown_server()
            break


cached_status_data = None
cached_status_time = 0
cached_character_info = None
cached_character_time = 0
cached_altering_works = None
cached_delivery_targets = None
cached_custom_targets = None
cached_quests = None
status_lock = threading.Lock()
last_execution_summary: Optional[Dict[str, Any]] = None
last_summary_id: int = 0

def add_log(level: str, message: str):
    timestamp = time.strftime("%H:%M:%S")
    with log_lock:
        console_logs.append({"time": timestamp, "level": level, "message": message})
        if len(console_logs) > 300:
            console_logs.pop(0)

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
                return copy.deepcopy(cached_status_data)
            return {
                "connected": True,
                "activity": {},
                "character": copy.deepcopy(cached_character_info or {}),
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
            return copy.deepcopy(cached_status_data)

        need_char_refresh = (not cached_character_info) or ((now - cached_character_time) > 30.0)
        prev_char_info = copy.deepcopy(cached_character_info) if cached_character_info else {}

    # Invoke CLI outside status_lock to avoid blocking other requests
    try:
        new_char_info = None
        if need_char_refresh:
            try:
                new_char_info = cli_instance.get_my_info()
            except Exception:
                new_char_info = None

        activity = cli_instance.get_activity()
        wings = cli_instance.get_wings_count()
        inv = cli_instance.get_inventory()

        data = {
            "connected": True,
            "activity": activity,
            "character": new_char_info or prev_char_info,
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

    def _send_json(self, obj, status=200):
        body_bytes = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send_data(status, body_bytes, "application/json; charset=utf-8")

    def _send_html(self, html_text, status=200):
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
                    targets = cached_delivery_targets if cached_delivery_targets is not None else []
                    self._send_json({"targets": targets, "is_busy": True})
                    return
            try:
                targets = manager_instance.get_registered_deliveries_with_status()
                with status_lock:
                    cached_delivery_targets = targets
                self._send_json({"targets": targets})
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

        if path == "/api/batch_plan":
            if is_busy:
                self._send_json({"is_busy": True, "can_start": False, "items": [], "steps": []})
                return
            query_params = parse_qs(parsed.query)
            alter_order = query_params.get("alter_order", [None])[0]
            try:
                plan = manager_instance.analyze_batch_plan(alter_order=alter_order)
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
            if is_busy:
                self._send_json({"is_busy": True, "can_start": False, "items": [], "steps": []})
                return
            query_params = parse_qs(parsed.query)
            alter_order = query_params.get("alter_order", [None])[0]
            try:
                plan = manager_instance.analyze_custom_plan(alter_order=alter_order)
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
            if is_busy:
                self._send_error("작업 진행 중에는 실시간 레시피 검색을 수행할 수 없습니다.", 409)
                return
            query_params = parse_qs(parsed.query)
            item_name = query_params.get("name", [""])[0].strip()
            if not item_name:
                self._send_error("검색할 이름을 입력해주세요.", 400)
                return
            try:
                craft_res = cli_instance.get_craftable_items(item_name)
                alter_res = cli_instance.get_alterable_items(item_name)
                gath_res = cli_instance.get_gatherable_items(item_name)
                breakdown = cli_instance.get_item_location_breakdown(item_name)

                self._send_json({
                    "item_name": item_name,
                    "owned_bag": breakdown["inventory"],
                    "owned_char_storage": breakdown["character_storage"],
                    "owned_account_storage": breakdown["account_storage"],
                    "owned_storage": breakdown["storage_total"],
                    "owned_all": breakdown["total"],
                    "craft": craft_res.get("items", []),
                    "alter": alter_res.get("items", []),
                    "gather": gath_res.get("items", [])
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
            if is_busy:
                self._send_json({"is_busy": True, "can_start": False, "facilities": []})
                return
            query_params = parse_qs(parsed.query)
            category = query_params.get("category", ["all"])[0].strip()
            alter_order = query_params.get("alter_order", [None])[0]
            try:
                plan = manager_instance.analyze_quick_alter(category if category != "all" else None, alter_order=alter_order)
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
                self._send_json({"recipes": manager_instance.recipes})
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
            schedule_shutdown(delay=45.0, reason="웹 대시보드 창이 닫혀")
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
                global cached_status_time, cached_character_time, cached_status_data
                with status_lock:
                    cached_status_time = 0.0
                    cached_character_time = 0.0
                    cached_status_data = None
                add_log("success", f"⚙️ [연결 설정] 게임 CLI 경로가 갱신되었습니다: {cli_instance.cli_path}")
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
                    add_log("success", f"🔍 [경로 자동 탐색] CLI 경로를 감지했습니다: {found} ({cli_instance.discovery_source})")
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
                    except Exception as err:
                        add_log("error", f"❌ 작업 실패: {str(err)}")
                        with status_lock:
                            last_execution_summary = {
                                "title": f"'{item_name}' 제작 작업 리포트 (오류 중단)",
                                "status": "aborted",
                                "status_text": "오류 중단 ❌",
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
                add_log("info", f"⚙️ 설정이 업데이트되었습니다: {req_json}")
                self._send_json({"status": "success", "settings": settings_store.get_all()})
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

            manager_instance.reset_abort()
            is_busy = True
            order_name = "상위 티어 우선" if (alter_order or settings_store.get("alter_order")) != "low_tier" else "하위 티어 우선"
            current_task_info = f"⚡ 전체 주간 납품 일괄 최적화 제작 진행 중 ({order_name})..."

            def batch_worker():
                global is_busy, current_task_info, last_execution_summary, last_summary_id
                try:
                    plan = manager_instance.analyze_batch_plan(alter_order=alter_order)
                    res = manager_instance.execute_batch_deliveries(plan, callback=add_log, alter_order=alter_order)
                    if isinstance(res, dict) and "summary" in res:
                        with status_lock:
                            last_execution_summary = res["summary"]
                            last_summary_id += 1
                except Exception as err:
                    add_log("error", f"❌ 일괄 제작 실패: {str(err)}")
                    with status_lock:
                        last_execution_summary = {
                            "title": "주간 납품 작업 리포트 (오류 중단)",
                            "status": "aborted",
                            "status_text": "오류 중단 ❌",
                            "duration_text": "0초",
                            "wings": {"initial": 0, "final": 0, "used": 0},
                            "gather": {"count": 0, "items": []},
                            "alter": {"count": 0, "items": []},
                            "craft": {"count": 0, "items": []},
                            "collected_facilities": [],
                            "deferred_works": [{"recipe": "일괄 납품", "reason": str(err)}]
                        }
                        last_summary_id += 1
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

            manager_instance.reset_abort()
            is_busy = True
            order_name = "상위 티어 우선" if (alter_order or settings_store.get("alter_order")) != "low_tier" else "하위 티어 우선"
            current_task_info = f"🔨 개별 지정 아이템 일괄 최적화 제작 진행 중 ({order_name})..."

            def custom_batch_worker():
                global is_busy, current_task_info, last_execution_summary, last_summary_id
                try:
                    plan = manager_instance.analyze_custom_plan(alter_order=alter_order)
                    res = manager_instance.execute_batch_deliveries(plan, callback=add_log, alter_order=alter_order)
                    if isinstance(res, dict) and "summary" in res:
                        res["summary"]["title"] = "개별 지정 아이템 제작 완료 리포트"
                        with status_lock:
                            last_execution_summary = res["summary"]
                            last_summary_id += 1
                except Exception as err:
                    add_log("error", f"❌ 개별 지정 일괄 제작 실패: {str(err)}")
                    with status_lock:
                        last_execution_summary = {
                            "title": "개별 지정 제작 리포트 (오류 중단)",
                            "status": "aborted",
                            "status_text": "오류 중단 ❌",
                            "duration_text": "0초",
                            "wings": {"initial": 0, "final": 0, "used": 0},
                            "gather": {"count": 0, "items": []},
                            "alter": {"count": 0, "items": []},
                            "craft": {"count": 0, "items": []},
                            "collected_facilities": [],
                            "deferred_works": [{"recipe": "개별 지정 제작", "reason": str(err)}]
                        }
                        last_summary_id += 1
                finally:
                    is_busy = False
                    current_task_info = ""

            threading.Thread(target=custom_batch_worker, daemon=True).start()
            self._send_json({"status": "started"})
            return

        if path in ("/api/abort_quick_alter", "/api/abort_pipeline"):
            add_log("warn", "🛑 [중지 요청 접수] 사용자가 진행 중인 작업의 중단 명령을 전송했습니다.")
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
                current_task_info = f"⚡ 7슬롯 최고 레벨 {cat_disp} 빠른 실행 진행 중 ({order_name})..."

                def quick_alter_worker():
                    global is_busy, current_task_info, last_execution_summary, last_summary_id
                    try:
                        plan = manager_instance.analyze_quick_alter(category if category != "all" else None, alter_order=alter_order)
                        res = manager_instance.execute_quick_alter(plan, callback=add_log, alter_order=alter_order)
                        if isinstance(res, dict) and "summary" in res:
                            with status_lock:
                                last_execution_summary = res["summary"]
                                last_summary_id += 1
                    except Exception as err:
                        add_log("error", f"❌ 빠른 가공 실행 실패: {str(err)}")
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
                current_task_info = f"🎁 '{item_name}' 가공품 수령 중..."
                try:
                    res = cli_instance.complete_altering_work(item_name)
                    add_log("success", f"🎁 '{item_name}' 가공품 수령 완료: {res}")
                    self._send_json({"status": "success", "result": res})
                finally:
                    is_busy = False
                    current_task_info = ""
            except Exception as e:
                add_log("error", f"❌ 가공품 수령 실패: {str(e)}")
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
                f_works = [w for w in works_info.get("works", []) if w.get("FacilityName") == facility and not w.get("IsCompleted")]
                if f_works:
                    total_sec = sum(int(w.get("RemainingSeconds", 0)) for w in f_works)
                    summary = f"{facility} 전체 작업 ({len(f_works)}슬롯)"
                    al = alarm_store.add_facility_alarm(facility, summary, total_sec, source="수동 등록", works_count=len(f_works))
                else:
                    al = alarm_store.add_facility_alarm(facility, item_name, remaining_sec, source="수동 등록", works_count=1)

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
                if added:
                    add_log("success", f"🎯 [주간 납품 등록] 현재 퀘스트 {len(added)}건이 납품 목표 목록에 등록되었습니다.")
                else:
                    add_log("warn", "⚠️ 현재 게임 내 퀘스트 추적창에서 감지된 주간 납품 퀘스트가 없습니다.")
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
                add_log("success", f"🎯 [납품 목표 등록] '{item_name}' (목표: {goal}개 / 현재: {cur}개) 등록 완료")
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
                if entry:
                    add_log("info", f"✏️ [보유 수량 수정] '{item_name}' 보유 수량이 {entry['current']}/{entry['goal']}개로 갱신되었습니다.")
                    self._send_json({"status": "success", "target": entry})
                else:
                    self._send_error("해당 아이템을 찾을 수 없습니다.", 404)
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/reset_delivery_targets_current":
            try:
                reset_count = delivery_target_store.reset_all_current()
                add_log("info", f"🔄 [보유 수량 초기화] 등록된 {reset_count}개 납품 품목의 보유 수량이 모두 0개로 초기화되었습니다.")
                self._send_json({"status": "success", "reset_count": reset_count})
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
                    add_log("info", f"🗑️ [납품 목표 삭제] '{item_name}' 항목이 목표 목록에서 제거되었습니다.")
                self._send_json({"status": "success"})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/clear_delivery_targets":
            try:
                delivery_target_store.clear()
                add_log("info", "🗑️ [납품 목표 초기화] 모든 등록된 납품 목표가 삭제되었습니다.")
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
                add_log("success", f"🔨 [개별 제작 등록] '{item_name}' (목표: {goal}개 / 현재: {cur}개) 등록 완료")
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
                if entry:
                    add_log("info", f"✏️ [개별 보유 수량 수정] '{item_name}' 보유 수량이 {entry['current']}/{entry['goal']}개로 갱신되었습니다.")
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
                    add_log("info", f"🗑️ [개별 제작 삭제] '{item_name}' 항목이 목표 목록에서 제거되었습니다.")
                self._send_json({"status": "success"})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/clear_custom_targets":
            try:
                custom_target_store.clear()
                add_log("info", "🗑️ [개별 제작 초기화] 모든 등록된 개별 제작 목표가 삭제되었습니다.")
                self._send_json({"status": "success"})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/reset_custom_targets_current":
            try:
                reset_count = custom_target_store.reset_all_current()
                add_log("info", f"🔄 [개별 제작 수량 초기화] 등록된 {reset_count}개 목표의 보유 수량이 모두 0개로 초기화되었습니다.")
                self._send_json({"status": "success", "reset_count": reset_count})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/sync_custom_targets_inventory":
            try:
                sync_count = custom_target_store.clear_all_overrides()
                add_log("info", f"🎒 [인벤토리 동기화] 등록된 {sync_count}개 개별 제작 목표의 수동 수량이 해제되어 실제 인벤토리와 다시 동기화됩니다.")
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
                add_log("success", f"📖 [레시피 갱신] '{item_name}' 제작 레시피 저장 완료: {clean_ingredients}")
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
                add_log("success", f"💾 [프리셋 저장] '{preset_name}' ({len(items)}개 항목) 프리셋이 저장되었습니다.")
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
                    add_log("info", f"🗑️ [프리셋 삭제] '{preset_name}' 프리셋이 삭제되었습니다.")
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
                add_log("success", f"📂 [프리셋 불러오기] '{preset_name}' ({loaded_count}개 항목) 프리셋이 등록되었습니다.{mode_str}")
                self._send_json({"status": "success", "loaded_count": loaded_count, "reset_current": reset_current})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/collect_all_completed_altering":
            if is_busy:
                self._send_error("이미 다른 작업이 진행 중입니다.", 409)
                return

            is_busy = True
            current_task_info = "🎁 완료 가공품 순차 수령 중..."

            def collect_worker():
                global is_busy, current_task_info
                try:
                    collected = manager_instance.collect_completed_altering_works(callback=add_log)
                    if not collected:
                        add_log("info", "ℹ️ 수령할 완료된 가공품이 없습니다.")
                except Exception as err:
                    add_log("error", f"❌ 가공품 수령 실패: {str(err)}")
                finally:
                    is_busy = False
                    current_task_info = ""

            threading.Thread(target=collect_worker, daemon=True).start()
            self._send_json({"status": "started"})
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

def start_server(port=8080):
    global server_instance, server_start_time
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

    # 2. Try to bind starting from port up to port+10
    server = None
    active_port = port
    for p in range(port, port + 10):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), RequestHandler)
            active_port = p
            break
        except OSError:
            continue

    if server is None:
        time.sleep(1.0)
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), RequestHandler)
            active_port = port
        except OSError:
            launch_app_window(f"http://127.0.0.1:{port}")
            return

    server_instance = server

    # Start auto-shutdown watchdog thread
    threading.Thread(target=watchdog_loop, daemon=True).start()

    # Automatically launch standalone desktop app window once server is confirmed listening
    def _open_app_when_ready():
        time.sleep(0.4)
        launch_app_window(f"http://127.0.0.1:{active_port}")

    threading.Thread(target=_open_app_when_ready, daemon=True).start()

    try:
        server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass

HTML_PAGE = """<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>모비노기 생활 지원도구</title>
  <link rel="icon" type="image/x-icon" href="/favicon.ico">
  <link rel="apple-touch-icon" href="/api/icon">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Pretendard:wght@300;400;600;700;800&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #090d16;
      --card-bg: rgba(23, 31, 51, 0.7);
      --card-border: rgba(99, 102, 241, 0.2);
      --accent: #6366f1;
      --accent-glow: rgba(99, 102, 241, 0.4);
      --emerald: #10b981;
      --amber: #f59e0b;
      --cyan: #38bdf8;
      --rose: #f43f5e;
      --text: #f1f5f9;
      --text-muted: #94a3b8;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg);
      background-image: 
        radial-gradient(at 10% 20%, rgba(99, 102, 241, 0.15) 0px, transparent 50%),
        radial-gradient(at 90% 80%, rgba(16, 185, 129, 0.12) 0px, transparent 50%);
      color: var(--text);
      font-family: 'Pretendard', -apple-system, sans-serif;
      word-break: keep-all;
      min-height: 100vh;
      padding: 24px;
    }
    .container { max-width: 1320px; margin: 0 auto; }
    
    /* Header (Sticky Floating Bar) */
    header {
      position: sticky;
      top: 12px;
      z-index: 1000;
      display: flex;
      flex-direction: column;
      padding: 14px 22px;
      background: rgba(15, 23, 42, 0.92);
      backdrop-filter: blur(20px);
      -webkit-backdrop-filter: blur(20px);
      border: 1px solid var(--card-border);
      border-radius: 18px;
      margin-bottom: 24px;
      box-shadow: 0 10px 30px rgba(0,0,0,0.6);
      gap: 10px;
    }
    .header-top {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap-reverse;
      gap: 10px;
    }
    .logo-area { display: flex; align-items: center; gap: 13px; }
    .logo-icon {
      width: 42px; height: 42px; border-radius: 12px;
      background: linear-gradient(135deg, #6366f1, #38bdf8);
      display: flex; align-items: center; justify-content: center;
      font-size: 21px; box-shadow: 0 4px 15px var(--accent-glow);
      flex-shrink: 0;
    }
    .title h1 {
      font-size: 18.5px;
      font-weight: 800;
      letter-spacing: -0.4px;
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 4px;
    }
    .title p { font-size: 12px; color: var(--text-muted); margin-top: 2px; }

    /* Header Action Buttons (연결설정, 최근 작업 리포트, 서버 종료) */
    .header-actions {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      margin-left: auto;
      flex-shrink: 0;
    }
    .header-action-btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 5px;
      padding: 5px 11px;
      font-size: 11.5px;
      font-weight: 700;
      border-radius: 9999px;
      cursor: pointer;
      border: 1px solid transparent;
      white-space: nowrap;
      transition: all 0.2s ease;
      line-height: 1.3;
      user-select: none;
    }
    .header-action-btn .btn-icon {
      font-size: 12px;
      line-height: 1;
    }
    .header-action-btn.btn-settings {
      background: rgba(99, 102, 241, 0.16);
      color: #a5b4fc;
      border-color: rgba(99, 102, 241, 0.35);
    }
    .header-action-btn.btn-settings:hover {
      background: rgba(99, 102, 241, 0.3);
      color: #e0e7ff;
      transform: translateY(-1px);
    }
    .header-action-btn.btn-summary {
      background: rgba(59, 130, 246, 0.16);
      color: #60a5fa;
      border-color: rgba(59, 130, 246, 0.35);
    }
    .header-action-btn.btn-summary:hover {
      background: rgba(59, 130, 246, 0.3);
      color: #93c5fd;
      transform: translateY(-1px);
    }
    .header-action-btn.btn-shutdown {
      background: rgba(239, 68, 68, 0.14);
      color: #f87171;
      border-color: rgba(239, 68, 68, 0.3);
    }
    .header-action-btn.btn-shutdown:hover {
      background: rgba(239, 68, 68, 0.25);
      color: #fca5a5;
      transform: translateY(-1px);
    }

    /* 상단/하단 영역 구분선 */
    .header-divider {
      height: 1px;
      width: 100%;
      background: linear-gradient(90deg, rgba(99, 102, 241, 0.3) 0%, rgba(255, 255, 255, 0.08) 50%, rgba(99, 102, 241, 0.05) 100%);
      margin: 1px 0;
    }

    /* 하단 상태 바 (서버 연결 상태, 날개, 무게) */
    .header-status-bar {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }
    .status-pills { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    .pill {
      padding: 5px 12px; border-radius: 20px; font-size: 12px; font-weight: 600;
      display: flex; align-items: center; gap: 6px; transition: all 0.3s;
      white-space: nowrap;
    }
    .pill.online { background: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.3); }
    .pill.offline { background: rgba(244, 63, 94, 0.15); color: #f43f5e; border: 1px solid rgba(244, 63, 94, 0.3); }
    .pill.wings { background: rgba(56, 189, 248, 0.15); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.3); }
    .pill.weight { background: rgba(245, 158, 11, 0.15); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.3); }

    /* 반응형: 가로폭이 좁아질 때 아이콘만 남겨 공간 확보 */
    @media (max-width: 860px) {
      .header-action-btn {
        padding: 5px 9px;
      }
      .header-action-btn .btn-label {
        display: none;
      }
    }
    @media (max-width: 640px) {
      .header-actions {
        width: 100%;
        justify-content: flex-end;
      }
    }

    /* Layout */
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
    @media (max-width: 1024px) { .grid { grid-template-columns: 1fr; } }

    .card {
      background: var(--card-bg);
      backdrop-filter: blur(16px);
      border: 1px solid var(--card-border);
      border-radius: 18px;
      padding: 20px;
      margin-bottom: 24px;
      box-shadow: 0 10px 25px rgba(0,0,0,0.3);
    }
    .card-title {
      font-size: 15.5px; font-weight: 700; margin-bottom: 14px;
      display: flex; justify-content: space-between; align-items: center;
      flex-wrap: wrap; gap: 10px;
    }
    .badge {
      font-size: 11px; padding: 3px 8px; border-radius: 6px; font-weight: 700;
      background: rgba(99, 102, 241, 0.2); color: #a5b4fc;
      white-space: nowrap; flex-shrink: 0; display: inline-flex; align-items: center;
    }
    .badge.delivery {
      background: linear-gradient(135deg, rgba(99,102,241,0.3), rgba(245,158,11,0.3));
      color: #fbbf24; border: 1px solid rgba(245,158,11,0.4);
    }
    .badge.manual {
      background: rgba(56, 189, 248, 0.2); color: #38bdf8;
    }

    /* Buttons */
    .btn {
      padding: 8px 14px; border-radius: 9px; border: none; cursor: pointer;
      font-weight: 600; font-size: 12.5px; transition: all 0.2s;
      display: inline-flex; align-items: center; gap: 6px;
      background: #1e293b; color: #f1f5f9; border: 1px solid rgba(255,255,255,0.1);
      white-space: nowrap; flex-shrink: 0; user-select: none;
    }
    .btn:hover { background: #334155; transform: translateY(-1px); }
    .btn-sm { padding: 4px 10px; font-size: 11.5px; border-radius: 7px; }
    .btn-primary { background: linear-gradient(135deg, #6366f1, #4f46e5); color: #fff; border: none; }
    .btn-primary:hover { opacity: 0.9; box-shadow: 0 4px 12px var(--accent-glow); }
    .btn-emerald { background: linear-gradient(135deg, #10b981, #059669); color: #fff; border: none; }
    .btn-emerald:hover { opacity: 0.9; box-shadow: 0 4px 12px rgba(16,185,129,0.4); }

    /* Inputs */
    .form-row { display: flex; gap: 10px; margin-bottom: 12px; }
    .form-group { display: flex; flex-direction: column; gap: 4px; }
    .form-group label { font-size: 11.5px; color: var(--text-muted); font-weight: 600; }
    .input-text, .input-number {
      background: rgba(0, 0, 0, 0.35); border: 1px solid rgba(255, 255, 255, 0.12);
      color: #fff; padding: 9px 12px; border-radius: 9px; font-size: 13px;
      outline: none; transition: border-color 0.2s;
    }
    .input-text:focus, .input-number:focus { border-color: var(--accent); }

    /* Console */
    .console {
      background: #050810; border: 1px solid rgba(255,255,255,0.08);
      border-radius: 12px; padding: 14px; height: 350px; overflow-y: auto;
      font-family: 'JetBrains Mono', monospace; font-size: 12.5px;
      display: flex; flex-direction: column; gap: 6px;
    }
    .log-line { line-height: 1.5; word-break: break-all; }
    .log-time { color: #64748b; margin-right: 6px; }
    .log-info { color: #cbd5e1; }
    .log-success { color: #34d399; font-weight: 600; }
    .log-warn { color: #fbbf24; }
    .log-error { color: #f43f5e; font-weight: 600; }
    .log-action { color: #38bdf8; font-weight: 600; }

    .busy-indicator {
      display: inline-flex; align-items: center; gap: 8px;
      font-size: 13px; color: #fbbf24; font-weight: 600;
    }
    .spinner {
      width: 14px; height: 14px; border: 2px solid rgba(251, 191, 36, 0.3);
      border-top-color: #fbbf24; border-radius: 50%;
      animation: spin 0.8s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }

    /* Facility 3x2 Grid */
    .facility-grid {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 12px;
    }
    @media(max-width: 960px) { .facility-grid { grid-template-columns: repeat(2, 1fr); } }
    @media(max-width: 600px) { .facility-grid { grid-template-columns: 1fr; } }

    .facility-card {
      background: rgba(0, 0, 0, 0.28);
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 12px;
      padding: 12px 14px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      min-height: 125px;
      transition: all 0.2s ease;
    }
    .facility-card:hover { border-color: rgba(99, 102, 241, 0.4); }
    .facility-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 8px;
      padding-bottom: 6px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    }
    .facility-title {
      font-weight: 700;
      font-size: 13.5px;
      color: #e0e7ff;
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .facility-items {
      display: flex;
      flex-direction: column;
      gap: 4px;
      max-height: 100px;
      overflow-y: auto;
    }
    .facility-item-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 12px;
      padding: 3px 0;
      border-bottom: 1px dashed rgba(255, 255, 255, 0.05);
    }
    .facility-item-row:last-child { border-bottom: none; }
    .item-name-text {
      color: #cbd5e1;
      font-weight: 500;
      max-width: 130px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .btn-manual-bell {
      background: none; border: none; color: #64748b; cursor: pointer;
      font-size: 11px; margin-left: 4px; padding: 1px 3px; border-radius: 4px;
    }
    .btn-manual-bell:hover { color: #fbbf24; background: rgba(251, 191, 36, 0.1); }

    /* BOM Table */
    .bom-table {
      width: 100%; border-collapse: collapse; font-size: 12.5px; margin-top: 10px;
    }
    .bom-table th {
      background: rgba(0,0,0,0.3); color: var(--text-muted); text-align: left;
      padding: 9px 12px; font-weight: 600; border-bottom: 1px solid rgba(255,255,255,0.08);
      white-space: nowrap;
    }
    .bom-table td {
      padding: 9px 12px; border-bottom: 1px solid rgba(255,255,255,0.04);
      white-space: nowrap;
    }
    .bom-table tr:hover { background: rgba(255,255,255,0.02); }

    /* 7-Slot Quick Alter Styling */
    .quick-tab {
      background: rgba(0, 0, 0, 0.35); border: 1px solid rgba(255, 255, 255, 0.1);
      color: #94a3b8; font-weight: 600;
    }
    .quick-tab:hover { background: rgba(255, 255, 255, 0.08); color: #fff; }
    .quick-tab.active {
      background: linear-gradient(135deg, #0284c7, #2563eb);
      color: #fff;
      border-color: transparent;
      box-shadow: 0 2px 12px rgba(2, 132, 199, 0.4);
    }
    .quick-cat-card {
      background: rgba(0, 0, 0, 0.28);
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 12px;
      padding: 14px;
    }
    .quick-tier-table {
      width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 8px;
    }
    .quick-tier-table th {
      background: rgba(0,0,0,0.3); color: var(--text-muted); text-align: left;
      padding: 6px 8px; font-weight: 600; border-bottom: 1px solid rgba(255,255,255,0.08);
    }
    .quick-tier-table td {
      padding: 6px 8px; border-bottom: 1px solid rgba(255,255,255,0.04);
    }

    /* Main Execution Mode Switcher Tabs */
    .main-mode-switcher-container {
      background: rgba(15, 23, 42, 0.75);
      backdrop-filter: blur(16px);
      -webkit-backdrop-filter: blur(16px);
      border: 1px solid rgba(255, 255, 255, 0.09);
      border-radius: 16px;
      padding: 6px;
      margin-bottom: 22px;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
      box-sizing: border-box;
      width: 100%;
      overflow: hidden;
    }
    .main-mode-tabs {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
      width: 100%;
      box-sizing: border-box;
    }
    @media(max-width: 960px) {
      .main-mode-tabs {
        grid-template-columns: 1fr;
      }
      .main-mode-tab {
        padding: 10px 14px;
      }
    }
    .main-mode-tab {
      background: transparent;
      border: 1px solid transparent;
      border-radius: 12px;
      padding: 12px 18px;
      display: flex;
      align-items: center;
      gap: 14px;
      cursor: pointer;
      text-align: left;
      transition: all 0.25s cubic-bezier(0.16, 1, 0.3, 1);
      color: #94a3b8;
      position: relative;
      user-select: none;
      min-width: 0;
      width: 100%;
      box-sizing: border-box;
      overflow: hidden;
    }
    .main-mode-tab:hover {
      background: rgba(255, 255, 255, 0.04);
      color: #cbd5e1;
      transform: translateY(-1px);
    }
    .main-mode-tab .tab-icon-wrap {
      width: 44px;
      height: 44px;
      border-radius: 11px;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 22px;
      flex-shrink: 0;
      transition: transform 0.25s ease;
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
    }
    .main-mode-tab:hover .tab-icon-wrap {
      transform: scale(1.05);
    }
    .main-mode-tab .tab-text-group {
      display: flex;
      flex-direction: column;
      gap: 3px;
      min-width: 0;
      flex: 1;
      overflow: hidden;
    }
    .main-mode-tab .tab-main-title {
      font-size: 15px;
      font-weight: 700;
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px;
      color: #cbd5e1;
      transition: color 0.2s ease;
      min-width: 0;
    }
    .main-mode-tab .tab-sub-desc {
      font-size: 11.5px;
      color: #64748b;
      line-height: 1.4;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      transition: color 0.2s ease;
      min-width: 0;
    }

    /* Active Tab: Delivery Mode */
    .main-mode-tab.active#btn-mode-delivery {
      background: linear-gradient(135deg, rgba(168, 85, 247, 0.18) 0%, rgba(99, 102, 241, 0.14) 100%);
      border: 1px solid rgba(168, 85, 247, 0.45);
      color: #fff;
      box-shadow: 0 4px 20px rgba(168, 85, 247, 0.22);
    }
    .main-mode-tab.active#btn-mode-delivery .tab-main-title {
      color: #f3e8ff;
    }
    .main-mode-tab.active#btn-mode-delivery .tab-sub-desc {
      color: #c4b5fd;
    }
    .main-mode-tab.active#btn-mode-delivery .tab-icon-wrap {
      box-shadow: 0 4px 16px rgba(168, 85, 247, 0.4);
    }

    /* Active Tab: Quick Alter Mode */
    .main-mode-tab.active#btn-mode-quick-alter {
      background: linear-gradient(135deg, rgba(2, 132, 199, 0.2) 0%, rgba(37, 99, 235, 0.14) 100%);
      border: 1px solid rgba(56, 189, 248, 0.45);
      color: #fff;
      box-shadow: 0 4px 20px rgba(2, 132, 199, 0.22);
    }
    .main-mode-tab.active#btn-mode-quick-alter .tab-main-title {
      color: #e0f2fe;
    }
    .main-mode-tab.active#btn-mode-quick-alter .tab-sub-desc {
      color: #7dd3fc;
    }
    .main-mode-tab.active#btn-mode-quick-alter .tab-icon-wrap {
      box-shadow: 0 4px 16px rgba(2, 132, 199, 0.4);
    }

    /* Active Tab: Custom Craft Mode */
    .main-mode-tab.active#btn-mode-custom-craft {
      background: linear-gradient(135deg, rgba(16, 185, 129, 0.2) 0%, rgba(5, 150, 105, 0.14) 100%);
      border: 1px solid rgba(16, 185, 129, 0.45);
      color: #fff;
      box-shadow: 0 4px 20px rgba(16, 185, 129, 0.22);
    }
    .main-mode-tab.active#btn-mode-custom-craft .tab-main-title {
      color: #a7f3d0;
    }
    .main-mode-tab.active#btn-mode-custom-craft .tab-sub-desc {
      color: #6ee7b7;
    }
    .main-mode-tab.active#btn-mode-custom-craft .tab-icon-wrap {
      box-shadow: 0 4px 16px rgba(16, 185, 129, 0.4);
    }

    /* Alteration Order Segmented Control */
    .alter-order-container {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      background: rgba(15, 23, 42, 0.65);
      border: 1px solid rgba(255, 255, 255, 0.1);
      padding: 4px 6px;
      border-radius: 10px;
    }
    .alter-order-label {
      font-size: 11.5px;
      font-weight: 700;
      color: #94a3b8;
      display: flex;
      align-items: center;
      gap: 4px;
      padding-left: 4px;
    }
    .segmented-control {
      display: inline-flex;
      background: rgba(0, 0, 0, 0.4);
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 8px;
      padding: 2px;
      gap: 2px;
    }
    .seg-btn {
      background: transparent;
      border: 1px solid transparent;
      color: #94a3b8;
      font-size: 11.5px;
      font-weight: 600;
      padding: 4px 10px;
      border-radius: 6px;
      cursor: pointer;
      transition: all 0.2s ease;
      white-space: nowrap;
      display: inline-flex;
      align-items: center;
      gap: 5px;
    }
    .seg-btn:hover {
      color: #f1f5f9;
      background: rgba(255, 255, 255, 0.06);
    }
    .seg-btn.active {
      background: linear-gradient(135deg, rgba(56, 189, 248, 0.25), rgba(99, 102, 241, 0.25));
      color: #38bdf8;
      border-color: rgba(56, 189, 248, 0.45);
      box-shadow: 0 2px 8px rgba(0, 0, 0, 0.35);
      font-weight: 700;
    }

    /* Tab Content Animation */
    .mode-tab-content {
      animation: tabFadeIn 0.25s cubic-bezier(0.16, 1, 0.3, 1);
    }
    @keyframes tabFadeIn {
      from { opacity: 0; transform: translateY(6px); }
      to { opacity: 1; transform: translateY(0); }
    }

    /* Execution Summary Modal Overlay */
    .modal-overlay {
      position: fixed;
      top: 0; left: 0; right: 0; bottom: 0;
      background: rgba(4, 7, 15, 0.75);
      backdrop-filter: blur(8px);
      display: flex;
      align-items: center;
      justify-content: center;
      z-index: 9999;
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.25s ease;
    }
    .modal-overlay.active {
      opacity: 1;
      pointer-events: auto;
    }
    .modal-dialog {
      background: #0f172a;
      border: 1px solid rgba(99, 102, 241, 0.35);
      border-radius: 20px;
      box-shadow: 0 25px 60px rgba(0, 0, 0, 0.7), 0 0 35px rgba(99, 102, 241, 0.2);
      width: 90%;
      max-width: 680px;
      max-height: 85vh;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      transform: scale(0.95);
      transition: transform 0.25s cubic-bezier(0.16, 1, 0.3, 1);
    }
    .modal-overlay.active .modal-dialog {
      transform: scale(1);
    }
    .modal-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 18px 24px;
      background: rgba(30, 41, 59, 0.6);
      border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    }
    .modal-body {
      padding: 20px 24px;
      overflow-y: auto;
      font-size: 13.5px;
      color: var(--text);
    }
    .modal-footer {
      display: flex;
      justify-content: flex-end;
      gap: 10px;
      padding: 14px 24px;
      background: rgba(30, 41, 59, 0.6);
      border-top: 1px solid rgba(255, 255, 255, 0.08);
    }
    .summary-stat-grid {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 12px;
      margin-bottom: 20px;
    }
    .summary-stat-card {
      background: rgba(255, 255, 255, 0.04);
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 12px;
      padding: 12px;
      text-align: center;
    }
    .summary-stat-card .label {
      font-size: 11.5px;
      color: var(--text-muted);
      margin-bottom: 4px;
    }
    .summary-stat-card .val {
      font-size: 17px;
      font-weight: 800;
      color: #fff;
    }
    .summary-section {
      background: rgba(0, 0, 0, 0.25);
      border: 1px solid rgba(255, 255, 255, 0.06);
      border-radius: 12px;
      padding: 14px;
      margin-bottom: 14px;
    }
    .summary-section-title {
      font-size: 13px;
      font-weight: 700;
      margin-bottom: 10px;
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .summary-item-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 6px 0;
      border-bottom: 1px dashed rgba(255, 255, 255, 0.07);
      font-size: 12.5px;
    }
    .summary-item-row:last-child {
      border-bottom: none;
    }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <!-- 1층: 좌측(로고, 제목) / 우측(연결설정, 최근 작업 리포트, 서버 종료) -->
      <div class="header-top">
        <div class="logo-area">
          <div class="logo-icon" style="padding: 2px; overflow: hidden; background: rgba(99, 102, 241, 0.15); border: 1px solid rgba(99, 102, 241, 0.35); box-shadow: 0 4px 15px rgba(99, 102, 241, 0.35);">
            <img src="/api/icon" alt="아이콘" style="width: 100%; height: 100%; object-fit: contain; border-radius: 9px;" onerror="this.style.display='none'; this.parentElement.innerText='⚔️';">
          </div>
          <div class="title">
            <h1>모비노기 생활 지원도구 <span style="font-size: 11px; background: rgba(99, 102, 241, 0.25); color: #c7d2fe; padding: 2px 7px; border-radius: 6px; font-weight: 700; margin-left: 6px; border: 1px solid rgba(99, 102, 241, 0.4); vertical-align: middle;">v0.5.6</span></h1>
            <p>마비노기 모바일 AI 커넥터 연동</p>
          </div>
        </div>
        <div class="header-actions">
          <button class="header-action-btn btn-settings" onclick="openCliModal()" title="게임 CLI 설치 경로 및 연결 설정">
            <span class="btn-icon">⚙️</span><span class="btn-label">연결 설정</span>
          </button>
          <button id="btn-show-summary" class="header-action-btn btn-summary" style="display: none;" onclick="openSummaryModal()" title="최근 완료된 작업의 채집, 가공, 제작, 날개 소모 결과를 확인합니다">
            <span class="btn-icon">📊</span><span class="btn-label">최근 작업 리포트</span>
          </button>
          <button class="header-action-btn btn-shutdown" onclick="manualServerShutdown()" title="웹 대시보드와 백그라운드 서버를 종료합니다">
            <span class="btn-icon">🔌</span><span class="btn-label">서버 종료</span>
          </button>
        </div>
      </div>

      <!-- 구분 선 -->
      <div class="header-divider"></div>

      <!-- 2층: 서버 연결 상태, 날개, 무게만 깔끔하게 표시 -->
      <div class="header-status-bar status-pills" id="status-pills">
        <div class="pill online" id="pill-status" onclick="openCliModal()" style="cursor: pointer;" title="클릭하여 연결 상태 및 설정 확인">● 커넥터 연결 확인 중</div>
        <div class="pill wings" id="pill-wings">🪽 날개: -</div>
        <div class="pill weight" id="pill-weight">📦 무게: -</div>
      </div>
    </header>

    <!-- Connection Alert Banner (shown when disconnected) -->
    <div id="cli-connection-alert" style="display: none; margin-bottom: 24px; padding: 18px 22px; background: rgba(245, 158, 11, 0.08); border: 1px solid rgba(245, 158, 11, 0.35); border-radius: 14px; backdrop-filter: blur(8px);">
      <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; flex-wrap: wrap;">
        <div style="display: flex; gap: 14px; align-items: flex-start; flex: 1; min-width: 300px;">
          <div id="cli-alert-icon" style="font-size: 26px; line-height: 1;">⚠️</div>
          <div style="flex: 1;">
            <div id="cli-alert-title" style="font-size: 15px; font-weight: 700; color: #fbbf24; margin-bottom: 6px;">게임 연결 끊김</div>
            <div id="cli-alert-desc" style="font-size: 13px; color: var(--text-muted); line-height: 1.6;">게임 클라이언트 상태를 확인해주세요.</div>
            <!-- Quick path setter form inside alert banner -->
            <div id="cli-alert-path-box" style="display: none; margin-top: 12px; gap: 8px; align-items: center; flex-wrap: wrap;">
              <input type="text" id="quick-cli-path-input" placeholder="예: D:\\Nexon\\MabinogiMobile 또는 C:\\Nexon\\MabinogiMobile" style="flex: 1; min-width: 280px; padding: 8px 12px; background: rgba(0,0,0,0.4); border: 1px solid rgba(245, 158, 11, 0.4); border-radius: 8px; color: #fff; font-size: 13px;">
              <button class="btn btn-sm btn-amber" onclick="saveQuickCliPath()">경로 저장 & 연결</button>
              <button class="btn btn-sm" onclick="triggerCliScan()">🔍 자동 다시 검색</button>
            </div>
          </div>
        </div>
        <div>
          <button class="btn btn-sm" onclick="openCliModal()" style="white-space: nowrap; background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.15);">연결 가이드 열기</button>
        </div>
      </div>
    </div>

    <!-- Collapsible 3x2 Facility Altering Queue & Delivery-Only Alarm Card -->
    <div class="card" id="altering-main-card" style="margin-bottom: 24px;">
      <div class="card-title" style="margin-bottom: 0;">
        <div style="display: flex; align-items: center; gap: 10px;">
          <span>⏳ 가공 시설별 대기열 현황</span>
          <span id="altering-summary-pill" class="badge" style="background: rgba(99,102,241,0.25); color: #c7d2fe;">확인 중...</span>
        </div>
        <div style="display: flex; gap: 8px;">
          <button class="btn btn-sm btn-emerald" onclick="collectAllCompletedAltering()" title="모든 시설의 완료된 가공 슬롯을 즉시 순차 수령합니다">🎁 완료 가공품 수령</button>
          <button class="btn btn-sm" onclick="loadAlteringQueue()">대기열 새로고침</button>
          <button class="btn btn-sm" id="btn-toggle-altering" onclick="toggleAlteringView()">접어두기 ▲</button>
        </div>
      </div>

      <!-- Collapsed Compact Bar (shown when collapsed) -->
      <div id="altering-collapsed-bar" style="display: none; margin-top: 12px; padding: 10px 14px; background: rgba(0,0,0,0.25); border-radius: 10px; font-size: 13px; color: var(--text-muted); cursor: pointer;" onclick="toggleAlteringView()">
        <span id="altering-quick-summary">가공 시설 상태 요약 정보...</span>
        <span style="float: right; color: #a5b4fc; font-weight: 600;">펼치기 ▼</span>
      </div>

      <!-- Expanded Content (shown by default) -->
      <div id="altering-expanded-content" style="margin-top: 16px;">
        <!-- 3x2 Grid for 6 Facilities -->
        <div class="facility-grid" id="facility-grid-container">
          <!-- Dynamically populated 6 facility cards -->
        </div>

        <!-- Delivery-Exclusive Alarm Section -->
        <div style="margin-top: 16px; padding-top: 14px; border-top: 1px solid rgba(255,255,255,0.08);">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
            <div style="display: flex; align-items: center; gap: 8px;">
              <span style="font-weight: 700; font-size: 13.5px; color: #a5b4fc;">🔔 주간 납품 전용 가공 알람 목록</span>
              <span id="alarm-count-badge" class="badge delivery">0개 활성</span>
            </div>
            <div style="display: flex; gap: 6px;">
              <button class="btn btn-sm" style="padding: 3px 8px; font-size: 11.5px;" onclick="testAlarmSound()">🔔 소리 테스트</button>
              <button class="btn btn-sm" style="padding: 3px 8px; font-size: 11.5px;" onclick="requestPushPermission()">📢 브라우저 알림 허용</button>
              <button class="btn btn-sm" style="padding: 3px 8px; font-size: 11.5px;" onclick="clearAllAlarms()">✕ 알람 비우기</button>
            </div>
          </div>
          <div id="alarm-list-container" style="max-height: 120px; overflow-y: auto; font-size: 12px; display: flex; flex-direction: column; gap: 4px;">
            <p style="color: #64748b; font-size: 12px;">현재 등록된 주간 납품 알람이 없습니다. (납품 파이프라인 가동 시 자동 등록됩니다)</p>
          </div>
        </div>
      </div>
    </div>

    <!-- Main Execution Mode Selector Tab Bar -->
    <div class="main-mode-switcher-container">
      <div class="main-mode-tabs" role="tablist">
        <button class="main-mode-tab active" id="btn-mode-delivery" role="tab" aria-selected="true" onclick="switchMainMode('delivery')">
          <div class="tab-icon-wrap" style="background: linear-gradient(135deg, rgba(168, 85, 247, 0.35), rgba(99, 102, 241, 0.35));">
            🎯
          </div>
          <div class="tab-text-group">
            <div class="tab-main-title">
              <span>주간 납품 관리 & 일괄 실행</span>
              <span id="tab-delivery-pill" class="badge delivery" style="font-size: 11px; padding: 2px 8px;">0개 등록</span>
            </div>
            <div class="tab-sub-desc">주간 퀘스트 목표 누적 등록 · 자재 소요 분석(BOM) · 부족 재료 일괄 제작</div>
          </div>
        </button>

        <button class="main-mode-tab" id="btn-mode-quick-alter" role="tab" aria-selected="false" onclick="switchMainMode('quick_alter')">
          <div class="tab-icon-wrap" style="background: linear-gradient(135deg, rgba(56, 189, 248, 0.35), rgba(37, 99, 235, 0.35));">
            ⚡
          </div>
          <div class="tab-text-group">
            <div class="tab-main-title">
              <span>7슬롯 가공대 빠른 실행</span>
              <span id="tab-quick-alter-pill" class="badge" style="font-size: 11px; padding: 2px 8px; background: rgba(56, 189, 248, 0.2); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.35);">7슬롯 최적화</span>
            </div>
            <div class="tab-sub-desc">완료 가공품 일괄 수령 · 부족 원자재 자동 사전 채집 · 최고 티어 1슬롯씩 대기열 가공</div>
          </div>
        </button>

        <button class="main-mode-tab" id="btn-mode-custom-craft" role="tab" aria-selected="false" onclick="switchMainMode('custom_craft')">
          <div class="tab-icon-wrap" style="background: linear-gradient(135deg, rgba(16, 185, 129, 0.35), rgba(5, 150, 105, 0.35));">
            🔨
          </div>
          <div class="tab-text-group">
            <div class="tab-main-title">
              <span>개별 아이템 지정 제작</span>
              <span id="tab-custom-craft-pill" class="badge" style="font-size: 11px; padding: 2px 8px; background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.35);">0개 등록</span>
            </div>
            <div class="tab-sub-desc">원하는 아이템 직접 입력 · 자재 소요 일괄 분석(BOM) · 부족 원자재 채집/가공/제작 원스톱 완료</div>
          </div>
        </button>
      </div>
    </div>

    <!-- Mode 1: 주간 납품 관리 & 일괄 실행 탭 컨텐츠 (기본 활성) -->
    <div id="tab-content-delivery" class="mode-tab-content">
      <!-- Weekly Delivery Targets Manager Card (주간 납품 목표 누적 등록 및 관리) -->
      <div class="card" id="delivery-targets-main-card" style="margin-bottom: 24px; border-color: rgba(168, 85, 247, 0.45); background: linear-gradient(180deg, rgba(30, 27, 75, 0.35) 0%, rgba(15, 23, 42, 0.6) 100%);">
        <div class="card-title" style="margin-bottom: 0; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px;">
          <div style="display: flex; align-items: center; gap: 10px; flex-shrink: 0;">
            <span style="font-size: 15.5px; font-weight: 700; color: #f1f5f9; white-space: nowrap;">🎯 주간 납품 목표 등록 & 관리</span>
            <span id="target-count-badge" class="badge delivery">0개 등록됨</span>
          </div>
          <div style="display: flex; flex-wrap: wrap; gap: 8px; align-items: center;">
            <button class="btn btn-sm btn-primary" onclick="addCurrentQuestTarget()" title="게임 내 추적 중인 납품 퀘스트를 읽어 목록에 추가합니다" style="padding: 6px 12px;">
              ➕ 현재 퀘스트 등록
            </button>
            <button class="btn btn-sm" onclick="resetAllTargetsCurrent()" style="color: #fbbf24; border-color: rgba(251,191,36,0.4); background: rgba(251,191,36,0.1); padding: 6px 12px;" title="새 주간 퀘스트를 시작할 때 모든 등록 목표의 현재 보유 수량을 0개로 일괄 초기화합니다">
              🔄 수량 0개로 초기화
            </button>
            <button class="btn btn-sm" onclick="clearDeliveryTargets()" style="color: #f87171; border-color: rgba(248,113,113,0.3); padding: 6px 12px;">
              🗑️ 전체 비우기
            </button>
            <button class="btn btn-sm" onclick="loadDeliveryTargets()" style="padding: 6px 12px;">
              새로고침
            </button>
            <button class="btn btn-sm" id="btn-toggle-delivery-targets" onclick="toggleDeliveryTargetsView()" style="background: rgba(168, 85, 247, 0.2); border-color: rgba(168, 85, 247, 0.4); color: #e9d5ff; padding: 6px 12px;">
              접어두기 ▲
            </button>
          </div>
        </div>

        <!-- Collapsed Compact Bar (shown when collapsed) -->
        <div id="delivery-targets-collapsed-bar" style="display: none; margin-top: 12px; padding: 10px 14px; background: rgba(0,0,0,0.25); border-radius: 10px; font-size: 13px; color: var(--text-muted); cursor: pointer; align-items: center; justify-content: space-between; gap: 12px; word-break: keep-all;" onclick="toggleDeliveryTargetsView()">
          <span id="delivery-targets-quick-summary" style="flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">등록된 주간 납품 목표 요약 정보...</span>
          <span style="color: #c4b5fd; font-weight: 600; white-space: nowrap; flex-shrink: 0;">펼치기 ▼</span>
        </div>

        <!-- Expanded Content (shown by default) -->
        <div id="delivery-targets-expanded-content" style="margin-top: 14px;">
          <!-- Quick Manual Add Input Form -->
          <div style="background: rgba(0,0,0,0.3); border-radius: 10px; padding: 12px 14px; margin-bottom: 14px; border: 1px solid rgba(255,255,255,0.06); display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end;">
            <div style="flex: 2; min-width: 170px;">
              <label style="font-size: 11.5px; color: #a5b4fc; font-weight: 600; margin-bottom: 4px; display: block;">납품 아이템명 (직접 입력)</label>
              <input type="text" id="target-input-item" class="input-text" placeholder="예: 론 엣지소드S, 비늘 갑옷 장갑S" style="height: 38px; padding: 6px 12px; font-size: 13px;" onkeydown="if(event.key==='Enter') addManualTarget()">
            </div>
            <div style="flex: 1; min-width: 80px;">
              <label style="font-size: 11.5px; color: #a5b4fc; font-weight: 600; margin-bottom: 4px; display: block;">목표 수량</label>
              <input type="number" id="target-input-goal" class="input-number" min="1" max="100" value="6" style="height: 38px; padding: 6px 12px; font-size: 13px;" onkeydown="if(event.key==='Enter') addManualTarget()">
            </div>
            <div style="flex: 1; min-width: 90px;">
              <label style="font-size: 11.5px; color: #38bdf8; font-weight: 600; margin-bottom: 4px; display: block;">현재 보유 (선택)</label>
              <input type="number" id="target-input-current" class="input-number" min="0" max="100" placeholder="자동 감지" style="height: 38px; padding: 6px 12px; font-size: 13px;" onkeydown="if(event.key==='Enter') addManualTarget()">
            </div>
            <div>
              <button class="btn btn-emerald" style="height: 38px; padding: 0 18px; font-size: 13px; font-weight: 700;" onclick="addManualTarget()">
                ➕ 직접 추가
              </button>
            </div>
            <div style="width: 100%; font-size: 11.5px; color: #94a3b8; display: flex; align-items: center; gap: 6px;">
              <span>💡 <strong>사용 방법:</strong> 인게임에서 주간 퀘스트를 하나씩 추적하면서 <strong style="color: #c7d2fe;">[➕ 현재 퀘스트 등록]</strong>을 누르거나, 여기서 아이템명과 수량을 직접 입력해 목록에 등록하세요. 아래 통합 플래너가 모든 목표를 한 번에 합산해 최적화 제작합니다!</span>
            </div>
          </div>

          <!-- Preset Save/Load Section -->
          <div style="background: rgba(168, 85, 247, 0.08); border: 1px solid rgba(168, 85, 247, 0.25); border-radius: 10px; padding: 12px 14px; margin-bottom: 14px;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
              <div style="display: flex; align-items: center; gap: 8px;">
                <span style="font-weight: 700; font-size: 13px; color: #c4b5fd;">💾 주간 납품 프리셋 (퀘스트 조합 저장)</span>
                <span id="preset-count-badge" class="badge" style="background: rgba(168, 85, 247, 0.2); color: #c4b5fd;">0개 저장됨</span>
              </div>
              <div style="display: flex; gap: 6px; align-items: center;">
                <input type="text" id="preset-save-name" class="input-text" placeholder="프리셋 이름 (예: 대장간 세트)" style="height: 32px; padding: 4px 10px; font-size: 12px; width: 160px;">
                <button class="btn btn-sm" style="padding: 4px 12px; font-size: 11.5px; background: rgba(168, 85, 247, 0.2); color: #c4b5fd; border: 1px solid rgba(168, 85, 247, 0.35);" onclick="saveCurrentAsPreset()">💾 현재 목록 저장</button>
              </div>
            </div>
            <div id="preset-list-container" style="display: flex; flex-wrap: wrap; gap: 8px; min-height: 28px;">
              <span style="color: #64748b; font-size: 12px;">저장된 프리셋이 없습니다. 납품 목표를 등록한 뒤 이름을 지정해 저장하세요.</span>
            </div>
          </div>

          <!-- Registered Delivery Targets Grid / List -->
          <div id="delivery-targets-container" style="display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 10px;">
            <p style="color: #64748b; font-size: 12.5px; grid-column: 1/-1;">등록된 주간 납품 목표가 없습니다.</p>
          </div>
        </div>
      </div>

      <!-- Unified Batch Planner Card (Global MRP for All Delivery Quests) -->
      <div class="card" style="margin-bottom: 24px; border-color: rgba(99,102,241,0.35);">
        <div class="card-title">
          <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap;">
            <span>⚡ 주간 납품 통합 플래너 (일괄 자재 소요 분석 & 원스톱 제작)</span>
            <span class="badge delivery">최적화 일괄 모드</span>
          </div>
          <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap;">
            <div class="alter-order-container" title="가공 등록 순서 설정: 장시간 소요 가공부터 먼저 등록하거나, 기초 재료부터 순차 등록합니다">
              <span class="alter-order-label">가공:</span>
              <div class="segmented-control" id="seg-delivery-alter-order">
                <button type="button" class="seg-btn active" id="btn-delivery-alter-high" onclick="setAlterOrder('high_tier')" title="오래 걸리는 고티어 가공품(T7~T1)부터 시설 슬롯에 우선 등록합니다">
                  <span>⏱️ 상위 티어</span>
                </button>
                <button type="button" class="seg-btn" id="btn-delivery-alter-low" onclick="setAlterOrder('low_tier')" title="기초 재료(T1~T7)부터 시설 슬롯에 순차 등록합니다">
                  <span>⚙️ 하위 티어</span>
                </button>
              </div>
            </div>
            <button class="btn btn-sm" onclick="loadBatchPlan()">재료 분석 새로고침</button>
          </div>
        </div>

        <!-- Full-Width Aggregated Materials Table Container -->
        <div style="background: rgba(0,0,0,0.25); border-radius: 12px; padding: 16px; border: 1px solid rgba(255,255,255,0.06); overflow-x: auto;">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; flex-wrap: wrap; gap: 8px;">
            <div style="display: flex; align-items: center; gap: 8px;">
              <h4 style="font-size: 14px; font-weight: 700; color: #34d399; margin: 0;">📊 전체 통합 필요 1차 가공품 분석표</h4>
              <span id="batch-status-text" style="font-size: 12px; color: var(--text-muted);">계산 중...</span>
            </div>
            <!-- Hidden compatibility container for batch-tasks-container -->
            <div id="batch-tasks-container" style="display: none;"></div>
          </div>
          <table class="bom-table">
            <thead>
              <tr>
                <th style="min-width: 120px;">재료명</th>
                <th style="min-width: 65px; text-align: center;">시설</th>
                <th style="min-width: 75px; text-align: right;">총 소요</th>
                <th style="min-width: 100px; text-align: center;">가방(창고)</th>
                <th style="min-width: 130px;">상태</th>
              </tr>
            </thead>
            <tbody id="bom-table-body">
              <tr><td colspan="5" style="text-align: center; color: #64748b; padding: 14px;">분석 데이터를 불러오는 중...</td></tr>
            </tbody>
          </table>
        </div>

        <!-- Facility Slot Status & Warning -->
        <div id="facility-slots-section" style="margin-top: 14px; display: none;">
          <div style="background: rgba(0,0,0,0.25); border-radius: 10px; padding: 12px 14px; border: 1px solid rgba(255,255,255,0.06);">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
              <span style="font-size: 12.5px; font-weight: 700; color: #a5b4fc;">🏭 시설별 가공 슬롯 현황 (시설당 7슬롯)</span>
              <span id="slot-summary-badge" style="font-size: 11.5px; color: #94a3b8;"></span>
            </div>
            <div id="facility-slots-container" style="display: flex; flex-wrap: wrap; gap: 8px;"></div>
          </div>
        </div>

        <div id="slot-warnings-banner" style="margin-top: 10px; display: none; background: rgba(251, 191, 36, 0.08); border: 1px solid rgba(251, 191, 36, 0.3); border-radius: 10px; padding: 12px 16px;">
          <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 6px;">
            <span style="font-size: 16px;">⚠️</span>
            <span style="font-size: 13px; font-weight: 700; color: #fbbf24;">가공 슬롯 부족 사전 안내</span>
          </div>
          <div id="slot-warnings-list" style="font-size: 12px; color: #fde68a; line-height: 1.7;"></div>
        </div>

        <!-- Action Button for Batch Pipeline -->
        <div style="margin-top: 16px; display: flex; justify-content: flex-end; gap: 10px;">
          <button id="btn-batch-execute" class="btn btn-emerald" style="padding: 12px 24px; font-size: 14px; font-weight: 700;" onclick="executeBatchPipeline()">
            ⚡ 전체 납품 재료 일괄 가공 & 제작 시작
          </button>
        </div>
      </div>
    </div>

    <!-- Mode 2: 7슬롯 가공대 빠른 실행 탭 컨텐츠 -->
    <div id="tab-content-quick-alter" class="mode-tab-content" style="display: none;">
      <!-- 7-Slot Max Level Alteration Bench Quick Run Card -->
      <div class="card" id="quick-alter-main-card" style="margin-bottom: 24px; border-color: rgba(56, 189, 248, 0.4); background: linear-gradient(180deg, rgba(23, 31, 51, 0.85) 0%, rgba(15, 23, 42, 0.95) 100%);">
        <div class="card-title" style="margin-bottom: 0;">
          <div style="display: flex; align-items: center; gap: 10px;">
            <span>⚡ 7슬롯 최고 레벨 가공대 빠른 실행</span>
            <span class="badge" style="background: rgba(56, 189, 248, 0.25); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.35);">7슬롯 최적화</span>
          </div>
          <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap;">
            <div class="alter-order-container" title="가공 우선순위 설정">
              <span class="alter-order-label">가공:</span>
              <div class="segmented-control" id="seg-quick-alter-order">
                <button type="button" class="seg-btn active" id="btn-quick-alter-high" onclick="setAlterOrder('high_tier')" title="오래 걸리는 최고 티어(T7 ➔ T1)부터 1슬롯씩 대기열에 등록합니다">
                  <span>⏱️ 상위 티어</span>
                </button>
                <button type="button" class="seg-btn" id="btn-quick-alter-low" onclick="setAlterOrder('low_tier')" title="기초 하위 티어(T1 ➔ T7)부터 1슬롯씩 대기열에 등록합니다">
                  <span>⚙️ 하위 티어</span>
                </button>
              </div>
            </div>
            <button class="btn btn-sm" onclick="loadQuickAlterPlan()">계획 새로고침</button>
            <button class="btn btn-sm" id="btn-toggle-quick-alter" onclick="toggleQuickAlterView()">접어두기 ▲</button>
          </div>
        </div>

        <!-- Collapsed Compact Bar (shown when collapsed) -->
        <div id="quick-alter-collapsed-bar" style="display: none; margin-top: 12px; padding: 10px 14px; background: rgba(0,0,0,0.25); border-radius: 10px; font-size: 13px; color: var(--text-muted); cursor: pointer;" onclick="toggleQuickAlterView()">
          <span id="quick-alter-quick-summary">7슬롯 가공대 빠른 실행 계획 요약...</span>
          <span style="float: right; color: #38bdf8; font-weight: 600;">펼치기 ▼</span>
        </div>

        <!-- Expanded Content -->
        <div id="quick-alter-expanded-content" style="margin-top: 12px;">
          <p id="quick-alter-desc-text" style="font-size: 13px; color: var(--text-muted); margin-bottom: 14px; line-height: 1.6;">
            완료된 가공품이 있으면 일괄 수령하여 슬롯(최대 7개)을 확보하고, 부족한 원자재를 사전에 모두 채집한 뒤 최고 티어 재료부터 순서대로 1슬롯씩 대기열에 등록합니다. <span style="color: #fbbf24;">(가공 전용 재료가 부족한 티어는 자동으로 건너뜁니다)</span>
          </p>

          <!-- Category Filter Tabs -->
          <div style="display: flex; gap: 8px; margin-bottom: 14px; flex-wrap: wrap;">
            <button class="btn btn-sm quick-tab active" id="tab-cat-all" onclick="selectQuickCategory('all')">⚡ 전체 가공대 일괄</button>
            <button class="btn btn-sm quick-tab" id="tab-cat-금속" onclick="selectQuickCategory('금속')">🪙 금속 (백금강괴 ~ 철괴)</button>
            <button class="btn btn-sm quick-tab" id="tab-cat-목재" onclick="selectQuickCategory('목재')">🪵 목재 (특급목재 ~ 목재)</button>
            <button class="btn btn-sm quick-tab" id="tab-cat-가죽" onclick="selectQuickCategory('가죽')">🦊 가죽 (특급가죽 ~ 가죽)</button>
            <button class="btn btn-sm quick-tab" id="tab-cat-옷감" onclick="selectQuickCategory('옷감')">🧶 옷감 (특급옷감 ~ 옷감)</button>
          </div>

          <!-- Quick Alter Plan Container -->
          <div id="quick-alter-plan-container" style="display: flex; flex-direction: column; gap: 14px;">
            <p style="color: #64748b; font-size: 13px;">가공대 상태 및 고티어 계획을 분석 중입니다...</p>
          </div>

          <!-- Bottom Action Bar -->
          <div style="margin-top: 16px; display: flex; justify-content: space-between; align-items: center; border-top: 1px solid rgba(255,255,255,0.08); padding-top: 14px; flex-wrap: wrap; gap: 10px;">
            <div id="quick-gather-summary" style="font-size: 12.5px; color: var(--cyan); display: flex; align-items: center; gap: 6px;">
              <!-- Gathering summary populated by JS -->
            </div>
            <div style="display: flex; gap: 8px; align-items: center;">
              <button id="btn-quick-alter-abort" class="btn" style="display: none; padding: 12px 20px; font-size: 13.5px; font-weight: 700; background: linear-gradient(135deg, #ef4444, #dc2626); color: white; border: none; border-radius: 8px; box-shadow: 0 4px 15px rgba(239, 68, 68, 0.4); cursor: pointer;" onclick="abortQuickAlter()">
                🛑 작업 즉시 중지
              </button>
              <button id="btn-quick-alter-execute" class="btn btn-primary" style="padding: 12px 24px; font-size: 13.5px; font-weight: 700; background: linear-gradient(135deg, #0284c7, #2563eb); box-shadow: 0 4px 15px rgba(2, 132, 199, 0.4);" onclick="executeQuickAlter()">
                🚀 7슬롯 빠른 가공 시작 (수령 ➔ 채집 ➔ 가공)
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Mode 3: 개별 아이템 지정 제작 탭 컨텐츠 -->
    <div id="tab-content-custom-craft" class="mode-tab-content" style="display: none;">
      <!-- Custom Targets Manager Card (개별 제작 목표 누적 등록 및 관리) -->
      <div class="card" id="custom-targets-main-card" style="margin-bottom: 24px; border-color: rgba(16, 185, 129, 0.45); background: linear-gradient(180deg, rgba(6, 78, 59, 0.25) 0%, rgba(15, 23, 42, 0.6) 100%);">
        <div class="card-title" style="margin-bottom: 0; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px;">
          <div style="display: flex; align-items: center; gap: 10px; flex-shrink: 0;">
            <span style="font-size: 15.5px; font-weight: 700; color: #f1f5f9; white-space: nowrap;">🎯 개별 제작 목표 품목 등록 & 관리</span>
            <span id="custom-target-count-badge" class="badge" style="background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.35);">0개 등록됨</span>
          </div>
          <div style="display: flex; flex-wrap: wrap; gap: 8px; align-items: center;">
            <button class="btn btn-sm" onclick="resetAllCustomTargetsCurrent()" style="color: #fbbf24; border-color: rgba(251,191,36,0.4); background: rgba(251,191,36,0.1); padding: 6px 12px;" title="모든 개별 제작 목표의 현재 보유 수량을 0개로 일괄 초기화합니다">
              🔄 수량 0개로 초기화
            </button>
            <button class="btn btn-sm" onclick="syncCustomTargetsInventory()" style="color: #38bdf8; border-color: rgba(56,189,248,0.35); background: rgba(56,189,248,0.1); padding: 6px 12px;" title="게임 내 인벤토리/창고 실제 보유량으로 수량을 다시 동기화합니다">
              🎒 인벤 동기화
            </button>
            <button class="btn btn-sm" onclick="openRecipeManagerModal()" style="background: rgba(16,185,129,0.15); border-color: rgba(16,185,129,0.35); color: #6ee7b7; padding: 6px 12px;" title="아이템별 1회 제작 소요 재료를 확인하고 수정합니다">
              📖 레시피 도감/수정
            </button>
            <button class="btn btn-sm" onclick="clearCustomTargets()" style="color: #f87171; border-color: rgba(248,113,113,0.3); padding: 6px 12px;">
              🗑️ 전체 비우기
            </button>
            <button class="btn btn-sm" onclick="loadCustomTargets()" style="padding: 6px 12px;">
              새로고침
            </button>
          </div>
        </div>

        <div style="margin-top: 14px;">
          <!-- Quick Manual Add Input Form -->
          <div style="background: rgba(0,0,0,0.35); padding: 12px 14px; border-radius: 10px; border: 1px solid rgba(255,255,255,0.08); margin-bottom: 14px;">
            <div style="font-size: 12px; font-weight: 700; color: #a7f3d0; margin-bottom: 8px; display: flex; align-items: center; gap: 6px;">
              <span>✍️ 제작할 아이템 직접 등록</span>
              <span style="font-size: 11px; color: var(--text-muted); font-weight: normal;">(원하는 품목과 수량을 등록하면 아래 플래너가 모든 하위 재료를 일괄 계산합니다)</span>
            </div>
            <div style="display: flex; gap: 10px; align-items: center; flex-wrap: wrap;">
              <input type="text" id="custom-input-item" placeholder="아이템명 (예: 론 엣지소드S, 은 펜던트, 실크 로브 등)" style="flex: 2; min-width: 200px; padding: 9px 13px; background: rgba(0,0,0,0.4); border: 1px solid rgba(16, 185, 129, 0.4); border-radius: 8px; color: #fff; font-size: 13px;" onkeydown="if(event.key==='Enter') addManualCustomTarget()">
              <div style="display: flex; align-items: center; gap: 6px;">
                <span style="font-size: 12px; color: #cbd5e1; white-space: nowrap;">목표 수량:</span>
                <input type="number" id="custom-input-count" value="1" min="1" max="999" style="width: 75px; padding: 9px 10px; background: rgba(0,0,0,0.4); border: 1px solid rgba(16, 185, 129, 0.4); border-radius: 8px; color: #fff; font-size: 13px; text-align: center;" onkeydown="if(event.key==='Enter') addManualCustomTarget()">
                <span style="font-size: 12px; color: #cbd5e1;">개</span>
              </div>
              <button class="btn btn-sm btn-emerald" onclick="addManualCustomTarget()" style="padding: 9px 18px; font-size: 13px; font-weight: 700;">
                ➕ 목표 추가
              </button>
            </div>
            <div style="display: flex; gap: 6px; align-items: center; margin-top: 10px; flex-wrap: wrap;">
              <span style="font-size: 11.5px; color: var(--text-muted);">빠른 추천:</span>
              <button type="button" class="btn btn-sm" style="padding: 2px 8px; font-size: 11px; background: rgba(255,255,255,0.06);" onclick="quickAddCustomTarget('론 엣지소드S', 4)">론 엣지소드S x4</button>
              <button type="button" class="btn btn-sm" style="padding: 2px 8px; font-size: 11px; background: rgba(255,255,255,0.06);" onclick="quickAddCustomTarget('은 펜던트', 4)">은 펜던트 x4</button>
              <button type="button" class="btn btn-sm" style="padding: 2px 8px; font-size: 11px; background: rgba(255,255,255,0.06);" onclick="quickAddCustomTarget('실크 로브', 2)">실크 로브 x2</button>
              <button type="button" class="btn btn-sm" style="padding: 2px 8px; font-size: 11px; background: rgba(255,255,255,0.06);" onclick="quickAddCustomTarget('가죽 아머', 2)">가죽 아머 x2</button>
            </div>
          </div>

          <!-- Registered Custom Targets Grid / List -->
          <div id="custom-targets-container" style="display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 10px;">
            <p style="color: #64748b; font-size: 12.5px; grid-column: 1/-1;">등록된 개별 제작 목표가 없습니다. 위 입력창에서 제작할 아이템을 등록하세요.</p>
          </div>
        </div>
      </div>

      <!-- Unified Custom Batch Planner Card -->
      <div class="card" id="custom-plan-main-card" style="margin-bottom: 24px; border-color: rgba(16, 185, 129, 0.35);">
        <div class="card-title">
          <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap;">
            <span>⚡ 개별 지정 제작 통합 플래너 (일괄 자재 소요 분석 & 원스톱 제작)</span>
            <span class="badge" style="background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.35);">개별 지정 일괄 모드</span>
          </div>
          <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap;">
            <div class="alter-order-container" title="가공 등록 순서 설정: 장시간 소요 가공부터 먼저 등록하거나, 기초 재료부터 순차 등록합니다">
              <span class="alter-order-label">가공:</span>
              <div class="segmented-control" id="seg-custom-alter-order">
                <button type="button" class="seg-btn active" id="btn-custom-alter-high" onclick="setAlterOrder('high_tier')" title="오래 걸리는 고티어 가공품(T7~T1)부터 시설 슬롯에 우선 등록합니다">
                  <span>⏱️ 상위 티어</span>
                </button>
                <button type="button" class="seg-btn" id="btn-custom-alter-low" onclick="setAlterOrder('low_tier')" title="기초 재료(T1~T7)부터 시설 슬롯에 순차 등록합니다">
                  <span>⚙️ 하위 티어</span>
                </button>
              </div>
            </div>
            <button class="btn btn-sm" onclick="loadCustomPlan()">재료 분석 새로고침</button>
          </div>
        </div>

        <!-- Full-Width Aggregated Materials Table Container -->
        <div style="background: rgba(0,0,0,0.25); border-radius: 12px; padding: 16px; border: 1px solid rgba(255,255,255,0.06); overflow-x: auto;">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; flex-wrap: wrap; gap: 8px;">
            <div style="display: flex; align-items: center; gap: 8px;">
              <h4 style="font-size: 14px; font-weight: 700; color: #34d399; margin: 0;">📊 개별 제작 통합 필요 1차 가공품 분석표</h4>
              <span id="custom-batch-status-text" style="font-size: 12px; color: var(--text-muted);">계산 중...</span>
            </div>
          </div>
          <table class="bom-table">
            <thead>
              <tr>
                <th style="min-width: 120px;">재료명</th>
                <th style="min-width: 65px; text-align: center;">시설</th>
                <th style="min-width: 75px; text-align: right;">총 소요</th>
                <th style="min-width: 100px; text-align: center;">가방(창고)</th>
                <th style="min-width: 80px; text-align: right;">부족 수량</th>
                <th style="min-width: 90px; text-align: center;">가공 필요</th>
                <th style="min-width: 130px; text-align: center;">가공 상태</th>
              </tr>
            </thead>
            <tbody id="custom-batch-materials-tbody">
              <tr>
                <td colspan="7" style="text-align: center; color: #64748b; padding: 24px;">개별 제작 목표를 등록하면 전체 소요 자재가 분석됩니다.</td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- Facility Slots Status & Overflow Warnings Section -->
        <div id="custom-facility-slots-section" style="margin-top: 16px; display: none;">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
            <span style="font-size: 12.5px; font-weight: 700; color: #cbd5e1;">🏭 가공 시설별 슬롯 현황 & 수용 가능 여부</span>
            <span id="custom-slot-summary-badge" style="font-size: 11.5px;"></span>
          </div>
          <div id="custom-facility-slots-container" style="display: flex; gap: 8px; flex-wrap: wrap;"></div>
        </div>

        <!-- Overflow Warning Banner -->
        <div id="custom-slot-warnings-banner" style="display: none; margin-top: 12px; padding: 10px 14px; background: rgba(244, 63, 94, 0.1); border: 1px solid rgba(244, 63, 94, 0.3); border-radius: 8px; font-size: 12px; color: #fda4af;">
          <div style="font-weight: 700; margin-bottom: 4px;">⚠️ 시설 슬롯 부족 안내</div>
          <div id="custom-slot-warnings-list"></div>
        </div>

        <!-- Bottom Execution Action Bar -->
        <div style="margin-top: 16px; display: flex; justify-content: space-between; align-items: center; border-top: 1px solid rgba(255,255,255,0.08); padding-top: 14px; flex-wrap: wrap; gap: 10px;">
          <div id="custom-plan-summary-text" style="font-size: 12.5px; color: var(--text-muted); display: flex; align-items: center; gap: 6px;">
            <!-- Summary populated by JS -->
          </div>
          <div style="display: flex; gap: 8px; align-items: center;">
            <button id="btn-custom-abort" class="btn" style="display: none; padding: 12px 20px; font-size: 13.5px; font-weight: 700; background: linear-gradient(135deg, #ef4444, #dc2626); color: white; border: none; border-radius: 8px; box-shadow: 0 4px 15px rgba(239, 68, 68, 0.4); cursor: pointer;" onclick="abortQuickAlter()">
              🛑 작업 즉시 중지
            </button>
            <button id="btn-custom-execute" class="btn btn-emerald" style="padding: 12px 24px; font-size: 13.5px; font-weight: 700; background: linear-gradient(135deg, #059669, #10b981); box-shadow: 0 4px 15px rgba(16, 185, 129, 0.4);" onclick="executeCustomPipeline()">
              ⚡ 개별 지정 재료 일괄 가공 & 제작 시작
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- Bottom 2-Column Grid: Live Console & Recipe Search -->
    <div class="grid">
      <!-- Left Column: Live Console Card -->
      <div>
        <div class="card">
          <div class="card-title">
            <span>💻 실시간 실행 콘솔</span>
            <div id="busy-box" style="display: none;" class="busy-indicator">
              <div class="spinner"></div>
              <span id="busy-text">작업 진행 중...</span>
              <button class="btn btn-sm" style="margin-left: 8px; padding: 2px 8px; font-size: 11px; background: #dc2626; color: white; border: none; border-radius: 4px; cursor: pointer;" onclick="abortQuickAlter()">🛑 중지</button>
            </div>
          </div>
          <div class="console" id="console-logs">
            <div class="log-line"><span class="log-time">[SYSTEM]</span> <span class="log-info">대시보드가 준비되었습니다.</span></div>
          </div>
        </div>
      </div>

      <!-- Right Column: Recipe Quick Search Card -->
      <div>
        <div class="card">
          <div class="card-title">
            <span>🔍 레시피 & 재료 즉시 조회</span>
          </div>
          <div class="form-row">
            <input type="text" id="search-query" class="input-text" placeholder="아이템명 또는 재료명 입력 (예: 목재, 가죽, 론)" onkeydown="if(event.key==='Enter') doSearch()">
            <button class="btn btn-sm" onclick="doSearch()">검색</button>
          </div>
          <div id="search-results" style="margin-top: 14px; max-height: 180px; overflow-y: auto; font-size: 13px;"></div>
        </div>
      </div>
    </div>
  </div>

  <!-- Execution Summary Modal -->
  <div id="summary-modal-overlay" class="modal-overlay" onclick="handleModalOverlayClick(event)">
    <div class="modal-dialog" role="dialog" aria-modal="true" aria-labelledby="modal-summary-title">
      <div class="modal-header">
        <div style="display: flex; align-items: center; gap: 10px;">
          <span style="font-size: 22px;">📊</span>
          <div>
            <h3 id="modal-summary-title" style="font-size: 16px; font-weight: 800; color: #fff;">작업 진행 종합 결과</h3>
            <div id="modal-summary-subtitle" style="font-size: 11.5px; color: var(--text-muted); margin-top: 2px;">소요 시간: -</div>
          </div>
        </div>
        <button class="btn btn-sm" onclick="closeSummaryModal()" style="padding: 4px 10px; background: rgba(255,255,255,0.08); border-radius: 8px;">✕</button>
      </div>
      <div class="modal-body" id="modal-summary-body">
        <!-- 4 Metric Cards -->
        <div class="summary-stat-grid">
          <div class="summary-stat-card" style="border-color: rgba(56, 189, 248, 0.3);">
            <div class="label">🪽 날개 소모</div>
            <div class="val" id="sum-card-wings" style="color: #38bdf8;">0개</div>
            <div id="sum-card-wings-sub" style="font-size: 10px; color: var(--text-muted); margin-top: 2px;">(0 ➔ 0)</div>
          </div>
          <div class="summary-stat-card" style="border-color: rgba(16, 185, 129, 0.3);">
            <div class="label">🌿 필드 채집</div>
            <div class="val" id="sum-card-gather" style="color: #34d399;">0품목</div>
            <div id="sum-card-gather-sub" style="font-size: 10px; color: var(--text-muted); margin-top: 2px;">총 0개 획득</div>
          </div>
          <div class="summary-stat-card" style="border-color: rgba(99, 102, 241, 0.3);">
            <div class="label">⏳ 가공 대기열</div>
            <div class="val" id="sum-card-alter" style="color: #818cf8;">0슬롯</div>
            <div id="sum-card-alter-sub" style="font-size: 10px; color: var(--text-muted); margin-top: 2px;">0품목 등록</div>
          </div>
          <div class="summary-stat-card" style="border-color: rgba(245, 158, 11, 0.3);">
            <div class="label">✨ 최종 제작</div>
            <div class="val" id="sum-card-craft" style="color: #fbbf24;">0개</div>
            <div id="sum-card-craft-sub" style="font-size: 10px; color: var(--text-muted); margin-top: 2px;">0품목 완료</div>
          </div>
        </div>

        <!-- Detail Sections -->
        <div id="sum-detail-craft" class="summary-section" style="display: none;">
          <div class="summary-section-title" style="color: #fbbf24;">
            <span>✨</span> <span>납품/최종 아이템 제작 내역</span>
          </div>
          <div id="sum-list-craft"></div>
        </div>

        <div id="sum-detail-alter" class="summary-section" style="display: none;">
          <div class="summary-section-title" style="color: #818cf8;">
            <span>⏳</span> <span>시설 가공 대기열 등록 내역</span>
          </div>
          <div id="sum-list-alter"></div>
        </div>

        <div id="sum-detail-gather" class="summary-section" style="display: none;">
          <div class="summary-section-title" style="color: #34d399;">
            <span>🌿</span> <span>원자재 필드 채집 내역</span>
          </div>
          <div id="sum-list-gather"></div>
        </div>

        <div id="sum-detail-extras" class="summary-section" style="display: none; background: rgba(245, 158, 11, 0.08); border-color: rgba(245, 158, 11, 0.2);">
          <div class="summary-section-title" style="color: #f59e0b;">
            <span>📌</span> <span>추가 수령 및 알림 사항</span>
          </div>
          <div id="sum-list-extras" style="font-size: 12px; line-height: 1.6; color: #cbd5e1;"></div>
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn btn-sm" onclick="copySummaryText()" style="background: rgba(255,255,255,0.08);">📋 텍스트 복사</button>
        <button class="btn btn-sm btn-primary" onclick="closeSummaryModal()">확인</button>
      </div>
    </div>
  </div>

  <!-- CLI Connection & Setup Modal -->
  <div class="modal-overlay" id="cli-modal-overlay" onclick="handleCliModalOverlayClick(event)">
    <div class="modal-dialog" style="max-width: 580px;">
      <div class="modal-header">
        <div style="display: flex; align-items: center; gap: 10px;">
          <span style="font-size: 22px;">⚙️</span>
          <div>
            <h3 style="font-size: 16px; font-weight: 800; color: #fff;">마비노기 모바일 연동 설정 & 연결 가이드</h3>
            <div style="font-size: 11.5px; color: var(--text-muted); margin-top: 2px;">게임 클라이언트 연동 상태 및 설치 폴더 관리</div>
          </div>
        </div>
        <button class="btn btn-sm" onclick="closeCliModal()" style="padding: 4px 10px; background: rgba(255,255,255,0.08); border-radius: 8px;">✕</button>
      </div>
      <div class="modal-body" style="padding: 20px 24px; font-size: 13.5px; line-height: 1.6;">
        <!-- Status Box -->
        <div id="cli-modal-status-box" style="margin-bottom: 18px; padding: 14px 16px; background: rgba(0,0,0,0.3); border: 1px solid rgba(255,255,255,0.08); border-radius: 10px;">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
            <span style="font-weight: 700; color: #f8fafc;">연결 상태</span>
            <span id="cli-modal-status-badge" class="badge" style="background: rgba(239,68,68,0.2); color: #f87171;">연결 확인 중...</span>
          </div>
          <div style="font-size: 12px; color: var(--text-muted); word-break: break-all; display: flex; flex-direction: column; gap: 4px;">
            <div><strong>현재 인식된 경로:</strong> <span id="cli-modal-path-text" style="color: #cbd5e1;">-</span></div>
            <div><strong>탐색 경로 출처:</strong> <span id="cli-modal-source-text" style="color: #94a3b8;">-</span></div>
          </div>
        </div>

        <!-- Path Input Form -->
        <div style="margin-bottom: 20px;">
          <label style="display: block; font-weight: 700; color: #e2e8f0; margin-bottom: 6px;">📁 마비노기 모바일 설치 폴더 (또는 CLI 경로)</label>
          <div style="display: flex; gap: 8px;">
            <input type="text" id="cli-modal-input" placeholder="예: D:\\Nexon\\MabinogiMobile 또는 C:\\Nexon\\MabinogiMobile" style="flex: 1; padding: 10px 14px; background: rgba(0,0,0,0.4); border: 1px solid rgba(99,102,241,0.4); border-radius: 8px; color: #fff; font-size: 13px;">
            <button class="btn btn-primary" onclick="saveCliPathFromModal()" style="white-space: nowrap; padding: 0 16px;">저장 & 연결</button>
          </div>
          <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 8px; flex-wrap: wrap; gap: 6px; margin-bottom: 20px;">
            <span style="font-size: 11.5px; color: var(--text-muted);">* 설치 폴더만 입력하셔도 MabinogiMobile_CLI.exe를 자동 감지합니다.</span>
            <button class="btn btn-sm" onclick="triggerCliScan()" style="font-size: 11.5px; padding: 3px 10px; background: rgba(255,255,255,0.06);">🔍 자동 다시 검색</button>
          </div>
        </div>

        <!-- 3-Step Setup Checklist -->
        <div style="padding: 14px 16px; background: rgba(99, 102, 241, 0.06); border: 1px solid rgba(99, 102, 241, 0.2); border-radius: 10px;">
          <div style="font-weight: 700; color: #818cf8; margin-bottom: 10px; display: flex; align-items: center; gap: 6px;">
            <span>📋</span> <span>연결 필수 체크리스트 (3단계)</span>
          </div>
          <ol style="margin: 0; padding-left: 20px; font-size: 12.5px; color: #cbd5e1; display: flex; flex-direction: column; gap: 8px;">
            <li><strong>PC 클라이언트 실행:</strong> 마비노기 모바일 PC 클라이언트를 켜고 원하는 캐릭터로 로그인합니다.</li>
            <li><strong>인게임 AI 옵션 활성화:</strong> 게임 내 [환경설정] → [기타] (또는 게임 설정)에서 <strong>[MM AI 에이전트 활성화]</strong>를 ON으로 켜주세요. (이 옵션을 켜야 넥슨에서 연동 CLI를 자동으로 설치합니다)</li>
            <li><strong>설치 드라이브 확인:</strong> 게임이 D:, E: 드라이브 등 기본 경로와 다른 곳에 설치된 경우 위의 폴더 경로를 입력해주세요.</li>
          </ol>
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn btn-sm btn-primary" onclick="closeCliModal()">닫기</button>
      </div>
    </div>
  </div>

  <!-- Recipe Manager & Edit Modal -->
  <div class="modal-overlay" id="recipe-modal-overlay" onclick="handleRecipeModalOverlayClick(event)">
    <div class="modal-dialog" style="max-width: 560px;">
      <div class="modal-header">
        <div style="display: flex; align-items: center; gap: 10px;">
          <span style="font-size: 20px;">📖</span>
          <div>
            <h3 id="recipe-modal-title" style="font-size: 16px; font-weight: 800; color: #fff; margin: 0;">제작 레시피 설정 / 수정</h3>
            <div id="recipe-modal-subtitle" style="font-size: 11.5px; color: var(--text-muted); margin-top: 2px;">아이템 1회 제작에 필요한 소요 재료를 설정합니다</div>
          </div>
        </div>
        <button class="btn btn-sm" onclick="closeRecipeModal()" style="padding: 4px 10px; background: rgba(255,255,255,0.08); border-radius: 8px;">✕</button>
      </div>
      <div class="modal-body" style="padding: 18px 20px;">
        <div style="background: rgba(16, 185, 129, 0.08); border: 1px solid rgba(16, 185, 129, 0.25); border-radius: 8px; padding: 10px 14px; margin-bottom: 14px; font-size: 12px; color: #a7f3d0; line-height: 1.5;">
          💡 <strong>레시피 안내:</strong> 게임 내 인벤토리에 이미 보유 중인 재료는 게임 클라이언트가 누락된 것으로 파악하지 못할 수 있습니다. 여기서 올바른 1회 제작 필요 재료와 수량을 등록해두면 모든 하위 재료/채집이 정확히 계산됩니다.
        </div>

        <div style="margin-bottom: 14px; display: flex; align-items: center; gap: 8px;">
          <span style="font-size: 12.5px; font-weight: 700; color: #e2e8f0; white-space: nowrap;">대상 아이템:</span>
          <input type="text" id="recipe-modal-item-name" style="flex: 1; padding: 8px 12px; background: rgba(0,0,0,0.35); border: 1px solid rgba(255,255,255,0.15); border-radius: 8px; color: #fff; font-size: 13px;" placeholder="예: 삶은 달걀">
          <button type="button" class="btn btn-sm" onclick="loadRecipeForCurrentInput()" style="padding: 8px 12px; font-size: 12px;">조회</button>
        </div>

        <div style="margin-bottom: 10px; display: flex; justify-content: space-between; align-items: center;">
          <span style="font-size: 12.5px; font-weight: 700; color: #cbd5e1;">필요 재료 목록 (1회 제작당)</span>
          <button type="button" class="btn btn-sm" onclick="addRecipeModalRow('', 1)" style="padding: 4px 10px; font-size: 11.5px; background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.35);">
            ➕ 재료 추가
          </button>
        </div>

        <div id="recipe-modal-rows-container" style="display: flex; flex-direction: column; gap: 8px; max-height: 260px; overflow-y: auto; padding-right: 4px;">
          <!-- Dynamically generated rows -->
        </div>
      </div>
      <div class="modal-footer" style="display: flex; justify-content: space-between; align-items: center;">
        <span id="recipe-modal-msg" style="font-size: 12px; color: #94a3b8;"></span>
        <div style="display: flex; gap: 8px;">
          <button class="btn btn-sm" onclick="closeRecipeModal()">취소</button>
          <button class="btn btn-sm btn-primary" onclick="saveRecipeFromModal()" style="padding: 6px 16px; font-weight: 700; background: linear-gradient(135deg, #059669, #10b981);">💾 레시피 저장</button>
        </div>
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
        cleanTitle = cleanTitle.replace(/\\s*\\(백그라운드\\s*가공\\s*진행\\s*중\\)/g, '').trim();
        if (sum.status === 'aborted') {
          titleEl.innerText = `${cleanTitle} [사용자 중지 🛑]`;
        } else if (sum.status === 'error') {
          titleEl.innerText = `${cleanTitle} [오류 중단 ❌]`;
        } else {
          titleEl.innerText = cleanTitle;
        }
      }
      const durText = sum.duration_text || (sum.duration_seconds ? sum.duration_seconds + '초' : '0초');
      let statText = sum.status_text || '완료 ✅';
      statText = statText.replace(/\\s*\\(백그라운드\\s*가공\\s*진행\\s*중\\)\\s*⏳?/g, '').trim();
      if (!statText.includes('✅') && !statText.includes('🛑') && !statText.includes('❌')) {
        statText += ' ✅';
      }
      if (subtitleEl) subtitleEl.innerText = `소요 시간: ${durText} | 상태: ${statText}`;

      // 1. Wings
      const wings = sum.wings || {};
      const cardWings = document.getElementById('sum-card-wings');
      const cardWingsSub = document.getElementById('sum-card-wings-sub');
      if (cardWings) cardWings.innerText = `${(wings.used || 0).toLocaleString()}개 소모`;
      if (cardWingsSub) cardWingsSub.innerText = `(${wings.initial || 0} ➔ ${wings.final || 0}개)`;

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
                <span style="font-weight: 600; color: #f1f5f9;">✨ ${itemName}</span>
                <span class="badge" style="background: rgba(245, 158, 11, 0.2); color: #fbbf24;">${craftCount}개 제작</span>
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
                  <span style="font-weight: 600; color: #f1f5f9;">⏳ ${recipeName}</span>
                  <span style="font-size: 11px; color: var(--text-muted); margin-left: 6px;">(${facilityName})</span>
                </div>
                <span class="badge" style="background: rgba(99, 102, 241, 0.2); color: #a5b4fc;">${slotsCount}슬롯 (산출: +${expectedYield}개)</span>
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
                <span style="font-weight: 600; color: #f1f5f9;">🌿 ${itemName}</span>
                <span class="badge" style="background: rgba(16, 185, 129, 0.2); color: #34d399;">+${gainedCount}개 획득${reqText}</span>
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
          extraLines.push(`🎁 <strong>사전 완료 가공품 즉시 수령</strong>: ${collectedList.join(', ')}`);
        }
        const deferredList = sum.deferred_works || sum.deferred || [];
        if (deferredList.length > 0) {
          const defNames = deferredList.map(d => {
            const rName = d.recipe || d.item_name || '가공';
            const reason = d.reason || (d.deferred ? `${d.deferred}회 보류` : '보류');
            const fac = d.facility ? `[${d.facility}] ` : '';
            return `${fac}${rName} (${reason})`;
          }).join(', ');
          extraLines.push(`⚠️ <strong>슬롯/재료 대기로 유예된 가공</strong>: ${defNames}`);
        }
        if (extraLines.length > 0) {
          secExtras.style.display = 'block';
          listExtras.innerHTML = extraLines.map(line => `<div style="margin-bottom: 4px;">• ${line}</div>`).join('');
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
        let cleanTitle = (s.title || '작업 진행 종합 리포트').replace(/\\s*\\(백그라운드\\s*가공\\s*진행\\s*중\\)/g, '').trim();
        let cleanStatus = (s.status_text || '완료 ✅').replace(/\\s*\\(백그라운드\\s*가공\\s*진행\\s*중\\)\\s*⏳?/g, '').trim();
        if (!cleanStatus.includes('✅') && !cleanStatus.includes('🛑') && !cleanStatus.includes('❌')) {
          cleanStatus += ' ✅';
        }
        const lines = [
          '============================================================',
          `📊 ${cleanTitle}`,
          '============================================================',
          `• 진행 상태: ${cleanStatus}`,
          `• 소요 시간: ${s.duration_text || (s.duration_seconds ? s.duration_seconds + '초' : '0초')}`,
          `• 정령의 날개: ${(s.wings ? s.wings.used : 0)}개 소모 (${s.wings ? s.wings.initial : 0} ➔ ${s.wings ? s.wings.final : 0}개)`,
          ''
        ];
        const gItems = (s.gather && s.gather.items) ? s.gather.items : (s.gathered || []);
        lines.push(`[🌿 원자재 채집] 총 ${gItems.length}품목`);
        gItems.forEach(it => {
          lines.push(`  - ${it.name || it.item}: +${it.gained}개 (목표: ${it.required}개)`);
        });
        lines.push('');
        const aItems = (s.alter && s.alter.items) ? s.alter.items : (s.altered || []);
        lines.push(`[⏳ 가공 대기열 등록] 총 ${(s.alter ? s.alter.count : s.total_altered_slots || 0)}슬롯`);
        aItems.forEach(it => {
          lines.push(`  - ${it.recipe || it.item} (${it.facility}): ${it.slots || it.registered}슬롯 (예상: +${it.expected_yield || it.yield_est}개)`);
        });
        lines.push('');
        const cItems = (s.craft && s.craft.items) ? s.craft.items : (s.crafted || []);
        lines.push(`[✨ 최종 제작 완료] 총 ${(s.craft ? s.craft.count : s.total_crafted_count || 0)}개`);
        cItems.forEach(it => {
          lines.push(`  - ${it.item || it.name}: ${it.count !== undefined ? it.count : it.crafted}개`);
        });
        const coll = s.collected_facilities || s.collected;
        if (coll && coll.length > 0) {
          lines.push('');
          lines.push(`[🎁 사전 수령] ${coll.join(', ')}`);
        }
        lines.push('============================================================');
        text = lines.join('\\n');
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
      const modal = document.getElementById('cli-modal-overlay');
      if (modal) modal.classList.add('active');
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
            badge.style.background = 'rgba(16,185,129,0.2)';
            badge.style.color = '#34d399';
            badge.innerText = 'CLI 파일 정상 확인';
          } else {
            badge.style.background = 'rgba(239,68,68,0.2)';
            badge.style.color = '#f87171';
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
          alert('✅ CLI 경로가 성공적으로 저장되었습니다! 즉시 재연결을 시도합니다.');
          fetchCliConfig();
          updateStatus();
        } else {
          alert('❌ ' + (data.error || '경로가 올바르지 않습니다.'));
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
          alert('✅ 경로가 저장되었습니다! 잠시 후 서버 상태가 갱신됩니다.');
          updateStatus();
        } else {
          alert('❌ ' + (data.error || '경로가 올바르지 않습니다.'));
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
          alert('🎉 CLI 실행 파일을 성공적으로 찾았습니다!\\n경로: ' + data.cli_path + '\\n(' + data.source + ')');
          fetchCliConfig();
          updateStatus();
        } else {
          alert('⚠️ 자동 검색으로 찾지 못했습니다.\\n게임이 켜져 있는지 확인하거나 직접 폴더 경로를 입력해주세요.');
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

    let currentAlterOrder = safeGetStorage('mabi_alter_order', 'high_tier');

    async function loadSettings() {
      applyAlterOrderUI();
      try {
        const res = await fetch('/api/settings');
        const d = await res.json();
        if (d) {
          if (d.alter_order) {
            currentAlterOrder = d.alter_order;
            safeSetStorage('mabi_alter_order', currentAlterOrder);
            applyAlterOrderUI();
          }
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
          descText.innerHTML = `완료된 가공품이 있으면 일괄 수령하여 슬롯(최대 7개)을 확보하고, 부족한 원자재를 사전에 모두 채집한 뒤 <strong style="color: #38bdf8;">오래 걸리는 최고 티어 재료(T7 ➔ T1)부터 순서대로</strong> 1슬롯씩 대기열에 등록합니다. <span style="color: #fbbf24;">(가공 전용 재료가 부족한 티어는 자동으로 건너뜁니다)</span>`;
        } else {
          descText.innerHTML = `완료된 가공품이 있으면 일괄 수령하여 슬롯(최대 7개)을 확보하고, 부족한 원자재를 사전에 모두 채집한 뒤 <strong style="color: #38bdf8;">기초 하위 티어 재료(T1 ➔ T7)부터 순서대로</strong> 1슬롯씩 대기열에 등록합니다. <span style="color: #fbbf24;">(가공 전용 재료가 부족한 티어는 자동으로 건너뜁니다)</span>`;
        }
      }
    }

    async function setAlterOrder(order) {
      currentAlterOrder = order;
      safeSetStorage('mabi_alter_order', order);
      applyAlterOrderUI();
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

    let isCurrentlyBusy = false;
    let activeAlarms = {}; // Synchronized strictly from /api/alarms

    const ALL_FACILITIES = [
      { name: '목재 가공 시설', icon: '🪵' },
      { name: '금속 가공 시설', icon: '⚒️' },
      { name: '옷감 가공 시설', icon: '🧵' },
      { name: '가죽 가공 시설', icon: '🛡️' },
      { name: '약품 가공 시설', icon: '🧪' },
      { name: '식품 가공 시설', icon: '🍖' }
    ];

    function formatFacilityShort(fac) {
      if (!fac) return '-';
      if (fac.includes('채집')) return '채집';
      return fac.replace(/\\s*가공\\s*시설|\\s*시설/g, '').trim();
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
        btn.innerText = '펼치기 ▼';
        safeSetStorage('mabi_altering_collapsed', 'true');
      } else {
        exp.style.display = 'block';
        bar.style.display = 'none';
        btn.innerText = '접어두기 ▲';
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
        btn.innerText = '펼치기 ▼';
      } else {
        exp.style.display = 'block';
        bar.style.display = 'none';
        btn.innerText = '접어두기 ▲';
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
          btn.innerText = '접어두기 ▲';
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
        btn.innerText = '펼치기 ▼';
        safeSetStorage('mabi_quick_alter_collapsed', 'true');
      } else {
        exp.style.display = 'block';
        bar.style.display = 'none';
        btn.innerText = '접어두기 ▲';
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
        btn.innerText = '펼치기 ▼';
      } else {
        exp.style.display = 'block';
        bar.style.display = 'none';
        btn.innerText = '접어두기 ▲';
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
        btn.innerText = '펼치기 ▼';
        safeSetStorage('mabi_delivery_targets_collapsed', 'true');
      } else {
        exp.style.display = 'block';
        bar.style.display = 'none';
        btn.innerText = '접어두기 ▲';
        safeSetStorage('mabi_delivery_targets_collapsed', 'false');
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
        btn.innerText = '펼치기 ▼';
      } else {
        exp.style.display = 'block';
        bar.style.display = 'none';
        btn.innerText = '접어두기 ▲';
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
    function playChime() {
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
      playChime();
      alert('🔔 주간 납품 알람음 테스트가 정상 작동합니다!');
    }

    function requestPushPermission() {
      if (!('Notification' in window)) {
        alert('이 브라우저는 데스크톱 알림을 지원하지 않습니다.');
        return;
      }
      Notification.requestPermission().then(permission => {
        if (permission === 'granted') {
          alert('📢 브라우저 푸시 알림이 허용되었습니다! 가공이 완료되면 알림창이 뜹니다.');
          new Notification('모비노기 생활 지원도구', { body: '주간 납품 가공 완료 알림이 활성화되었습니다!' });
        } else {
          alert('알림 권한이 거부되었거나 설정되지 않았습니다.');
        }
      });
    }

    function showPushNotification(title, body) {
      if ('Notification' in window && Notification.permission === 'granted') {
        new Notification(title, { body: body, icon: '⚔️' });
      }
    }

    // 1. Load Altering Facility Grid
    async function loadAlteringQueue() {
      try {
        const res = await fetch('/api/altering');
        const data = await res.json();
        const works = data.works || [];
        const completedTotal = data.completedCount || 0;

        const grouped = {};
        ALL_FACILITIES.forEach(f => { grouped[f.name] = []; });
        works.forEach(w => {
          const fn = w.FacilityName || '';
          if (!grouped[fn]) grouped[fn] = [];
          grouped[fn].push(w);
        });

        // Summary Pill & Quick Bar
        const inProgressTotal = works.filter(w => !w.IsCompleted).length;
        const sumPill = document.getElementById('altering-summary-pill');
        if (sumPill) sumPill.innerText = `진행 ${inProgressTotal}건 | 완료 ${completedTotal}건`;
        
        let quickSummaryParts = [];
        ALL_FACILITIES.forEach(f => {
          const list = grouped[f.name] || [];
          if (list.length > 0) {
            const incomplete = list.filter(w => !w.IsCompleted && (Number(w.RemainingSeconds) || 0) > 0);
            const totalSec = incomplete.reduce((sum, w) => sum + (Number(w.RemainingSeconds) || 0), 0);
            const hasComp = list.some(w => w.IsCompleted);
            const facShort = f.name.replace(' 가공 시설', '');

            if (totalSec > 0) {
              const compTag = hasComp ? ' <span style="color:#34d399;font-size:11px;">(일부완료)</span>' : '';
              quickSummaryParts.push(`${f.icon} ${facShort}: <span style="color:#fbbf24;font-weight:600;">${formatRemaining(totalSec)}</span>${compTag}`);
            } else if (hasComp) {
              quickSummaryParts.push(`${f.icon} ${facShort}: <span style="color:#34d399;font-weight:700;">완료!</span>`);
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
          const completedAtFac = itemsAtFac.filter(x => x.IsCompleted);
          const hasCompleted = completedAtFac.length > 0;
          const firstCompletedName = hasCompleted ? completedAtFac[0].DisplayName : '';

          const collectBtn = hasCompleted
            ? `<button class="btn btn-sm btn-emerald" style="padding: 3px 8px; font-size: 11.5px;" onclick="collectAltering('${firstCompletedName}')">🎁 수령(${completedAtFac.length})</button>`
            : `<button class="btn btn-sm" disabled style="opacity: 0.25; padding: 3px 8px; font-size: 11.5px;">수령</button>`;

          let itemsHtml = '';
          if (itemsAtFac.length === 0) {
            itemsHtml = '<div style="color: #64748b; font-size: 12px; padding: 8px 0; text-align: center;">작업 없음</div>';
          } else {
            itemsAtFac.forEach(w => {
              let timeStr = '';
              if (w.IsCompleted) {
                timeStr = '<span style="color: #34d399; font-weight: 700;">완료</span>';
              } else if (w.State === 'InProgress') {
                timeStr = `<span style="color: #fbbf24; font-weight: 600;">⏳ ${formatRemaining(w.RemainingSeconds)}</span>`;
              } else {
                timeStr = `<span style="color: #94a3b8; font-size: 11px;">대기 (${formatRemaining(w.RemainingSeconds)})</span>`;
              }

              const bellBtn = (!w.IsCompleted && w.RemainingSeconds > 0)
                ? `<button class="btn-manual-bell" title="이 작업에 알람 추가" onclick="addManualAlarm('${fac.name}', '${w.DisplayName}', ${w.RemainingSeconds})">+🔔</button>`
                : '';

              itemsHtml += `
                <div class="facility-item-row">
                  <div style="display:flex; align-items:center;">
                    <span class="item-name-text" title="${w.DisplayName}">${w.DisplayName}</span>
                    ${bellBtn}
                  </div>
                  <span>${timeStr}</span>
                </div>
              `;
            });
          }

          gridHtml += `
            <div class="facility-card">
              <div class="facility-header">
                <span class="facility-title">${fac.icon} ${formatFacilityShort(fac.name)}</span>
                ${collectBtn}
              </div>
              <div class="facility-items">
                ${itemsHtml}
              </div>
            </div>
          `;
        });
        gridEl.innerHTML = gridHtml;
        loadDeliveryAlarms();
      } catch (err) {
        console.error('Error loading altering queue:', err);
      }
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
        container.innerHTML = '<p style="color: #64748b; font-size: 12px;">현재 등록된 주간 납품 알람이 없습니다. (납품 파이프라인 가동 시 자동 등록됩니다)</p>';
        return;
      }

      let html = '';
      keys.forEach(k => {
        const al = activeAlarms[k];
        const remMs = al.targetTimestamp - Date.now();
        const remSec = Math.max(0, Math.ceil(remMs / 1000));
        const statusHtml = al.notified 
          ? '<span style="color:#34d399; font-weight:700;">🔔 전체 가공 완료! (수령 가능)</span>'
          : `<span style="color:#fbbf24; font-weight:600;">⏳ ${formatRemaining(remSec)} 남음 <small style="color:#94a3b8; font-size:10px;">(마지막 가공 완료)</small></span>`;

        const badgeClass = al.isDeliveryTarget ? 'badge delivery' : 'badge manual';
        const badgeText = al.source || (al.isDeliveryTarget ? '주간 납품' : '가공 시설');

        html += `
          <div style="display:flex; justify-content:space-between; align-items:center; padding:6px 12px; background:rgba(255,255,255,0.03); border:1px solid rgba(255,255,255,0.06); border-radius:6px;">
            <div style="display:flex; align-items:center; gap:8px;">
              <span class="${badgeClass}">🏭 ${formatFacilityShort(al.facility)}</span>
              <span style="font-weight:700; color:#e0e7ff; font-size:13px;">${al.item}</span>
            </div>
            <div style="display:flex; align-items:center; gap:10px;">
              ${statusHtml}
              <button onclick="deleteAlarmServer('${k}')" style="background:none; border:none; color:#ef4444; cursor:pointer; font-size:12px; font-weight:bold;" title="알람 삭제">✕</button>
            </div>
          </div>
        `;
      });
      container.innerHTML = html;
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

    // 1-second Alarm Ticker (triggers strictly for registered alarms)
    window.tickAlarms = function tickAlarms() {
      const now = Date.now();
      let changed = false;
      Object.keys(activeAlarms).forEach(k => {
        const al = activeAlarms[k];
        if (!al.notified && now >= al.targetTimestamp) {
          al.notified = true;
          changed = true;
          playChime();
          showPushNotification(`🔔 [${al.facility}] 마지막 가공 완료!`, `[${al.facility}] 모든 가공 작업이 완료되었습니다! (${al.item}) 지금 바로 수령하세요.`);
          document.title = `🔔 [${al.facility} 가공 완료!] 모비노기 생활 지원도구`;
          setTimeout(() => { document.title = '모비노기 생활 지원도구'; }, 8000);
        }
      });
      if (changed) renderAlarms();
    };

    // 2.5. 7-Slot Max Level Alteration Quick Routine
    let currentQuickCategory = 'all';
    let cachedQuickPlan = null;

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
          container.innerHTML = `<p style="color: #f87171; font-size: 13px;">⚠️ 계획 분석 오류: ${data.error}</p>`;
          if (execBtn) execBtn.disabled = true;
          return;
        }

        const categories = data.categories || {};
        const catKeys = Object.keys(categories);
        if (catKeys.length === 0) {
          container.innerHTML = '<p style="color: #64748b; font-size: 13px;">선택된 가공대 정보가 없습니다.</p>';
          if (execBtn) execBtn.disabled = true;
          return;
        }

        let html = '';
        catKeys.forEach(catName => {
          const c = categories[catName];
          const hasCompleted = c.completed_count > 0;
          const slotsBadge = `<span class="badge" style="background: rgba(99,102,241,0.2); color: #c7d2fe;">가용 슬롯: ${c.available_slots}/7</span>`;
          const completedBadge = hasCompleted 
            ? `<span class="badge" style="background: rgba(16,185,129,0.25); color: #34d399; border: 1px solid rgba(16,185,129,0.4);">🎁 완료 ${c.completed_count}건 수령 대기</span>`
            : '';

          html += `
            <div class="quick-cat-card">
              <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; flex-wrap: wrap; gap: 6px;">
                <div style="display: flex; align-items: center; gap: 8px;">
                  <strong style="font-size: 14px; color: #e0e7ff;">${catName} (${c.facility})</strong>
                  ${slotsBadge}
                  ${completedBadge}
                </div>
                <span style="font-size: 11.5px; color: var(--text-muted);">
                  고티어 가공 예정: <strong style="color: #38bdf8;">${c.planned_tiers.length}건</strong> / 건너뜀: ${c.skipped_tiers.length}건
                </span>
              </div>

              <table class="quick-tier-table">
                <thead>
                  <tr>
                    <th style="width: 55px;">티어</th>
                    <th style="width: 130px;">아이템명</th>
                    <th style="width: 140px;">레시피</th>
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
              statusHtml = `<span class="badge" style="background: rgba(56,189,248,0.2); color: #38bdf8;">🌿 사전 채집 후 가공 [${reqs}]</span>`;
            } else {
              statusHtml = `<span class="badge" style="background: rgba(16,185,129,0.2); color: #34d399;">✅ 즉시 가공 가능</span>`;
            }

            html += `
              <tr>
                <td><span class="badge" style="background: rgba(99,102,241,0.25); color: #a5b4fc;">T${p.tier}</span></td>
                <td><strong style="color: #f1f5f9;">${p.item_name}</strong></td>
                <td><span style="color: #94a3b8; font-size: 11.5px;">${p.recipe_name}</span></td>
                <td>${statusHtml}</td>
              </tr>
            `;
          });

          // Skipped items
          c.skipped_tiers.forEach(s => {
            html += `
              <tr style="opacity: 0.65;">
                <td><span class="badge" style="background: rgba(100,116,139,0.2); color: #94a3b8;">T${s.tier}</span></td>
                <td><span style="color: #94a3b8;">${s.item_name}</span></td>
                <td><span style="color: #64748b; font-size: 11.5px;">-</span></td>
                <td><span style="color: #f59e0b; font-size: 11.5px;">⏭️ 건너뜀 (${s.reason})</span></td>
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
          const rawText = rawEntries.map(([k, v]) => `<strong>${k}</strong> ${v}개`).join(', ');
          if (gatherSum) gatherSum.innerHTML = `🌿 <span>사전 부족분 맞춤 채집: ${rawText} <span style="color: #38bdf8; font-size: 11.5px;">(부족한 수량만 채집 후 즉시 자동 중단)</span></span>`;
        } else {
          if (gatherSum) gatherSum.innerHTML = `✨ <span style="color: #34d399;">사전 채집이 필요한 원자재 없음 (보유량 충분)</span>`;
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
            quickTabPill.style.background = 'rgba(16, 185, 129, 0.2)';
            quickTabPill.style.color = '#34d399';
            quickTabPill.style.borderColor = 'rgba(16, 185, 129, 0.4)';
          } else {
            quickTabPill.innerText = '7슬롯 최적화';
            quickTabPill.style.background = 'rgba(56, 189, 248, 0.2)';
            quickTabPill.style.color = '#38bdf8';
            quickTabPill.style.borderColor = 'rgba(56, 189, 248, 0.35)';
          }
        }

        // Update collapsed summary bar
        const quickSummaryEl = document.getElementById('quick-alter-quick-summary');
        if (quickSummaryEl) {
          const canStartTag = data.can_start ? '<span style="color:#34d399; font-weight:600;">실행 가능</span>' : '<span style="color:#94a3b8;">가공 예정 없음</span>';
          quickSummaryEl.innerHTML = `⚡ <strong>7슬롯 가공 빠른 실행:</strong> 가공 예정 <span style="color: #38bdf8; font-weight:600;">${totalPlanned}건</span> | 완료 수령 대기 <span style="color: #34d399; font-weight:600;">${totalCompleted}건</span> | 상태: ${canStartTag}`;
        }
      } catch (e) {
        console.error('loadQuickAlterPlan error:', e);
        if (container) container.innerHTML = `<p style="color: #f43f5e; font-size: 13px;">계획 불러오기 실패: ${e}</p>`;
      }
    }

    async function executeQuickAlter() {
      const catText = currentQuickCategory === 'all' ? '전체 가공대' : `'${currentQuickCategory}' 가공대`;
      const orderDesc = currentAlterOrder === 'low_tier' ? '하위 티어(T1~T7)부터 순차 가공 등록' : '최고 티어(T7~T1)부터 순차 가공 등록 (오래 걸리는 가공 우선)';
      if (!confirm(`${catText} 7슬롯 빠른 가공 루틴을 실행할까요?\\n\\n1. 완료 가공품 일괄 수령 (슬롯 확보)\\n2. 부족한 원자재 사전 일괄 채집\\n3. ${orderDesc}`)) {
        return;
      }

      isCurrentlyBusy = true;
      if (typeof applyBusyUI === 'function') applyBusyUI(true, `⚡ 7슬롯 최고 레벨 ${catText} 빠른 실행 준비 중...`);
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
      try {
        const btn = document.getElementById('btn-quick-alter-abort');
        if (btn) {
          btn.innerText = '중지 신호 전송 중...';
          btn.disabled = true;
        }
        const res = await fetch('/api/abort_quick_alter', { method: 'POST' });
        const d = await res.json();
        pollLogs();
        updateStatus();
        setTimeout(loadQuickAlterPlan, 1000);
        setTimeout(loadAlteringQueue, 1000);
      } catch (e) {
        alert('중지 요청 실패: ' + e);
      } finally {
        const btn = document.getElementById('btn-quick-alter-abort');
        if (btn) {
          btn.innerText = '🛑 작업 즉시 중지';
          btn.disabled = false;
        }
      }
    }

    // Delivery Targets Manager
    async function loadDeliveryTargets() {
      try {
        const res = await fetch('/api/delivery_targets');
        const data = await res.json();
        const targets = data.targets || [];
        const container = document.getElementById('delivery-targets-container');
        const badge = document.getElementById('target-count-badge');
        if (badge) {
          badge.innerText = `${targets.length}개 등록됨`;
        }
        const tabDeliveryPill = document.getElementById('tab-delivery-pill');
        if (tabDeliveryPill) {
          tabDeliveryPill.innerText = `${targets.length}개 등록`;
        }

        // Update Collapsed Bar Quick Summary
        const quickSummaryEl = document.getElementById('delivery-targets-quick-summary');
        if (quickSummaryEl) {
          if (targets.length === 0) {
            quickSummaryEl.innerHTML = '<span style="color: #64748b;">등록된 주간 납품 목표가 없습니다. 클릭하여 펼친 뒤 등록하세요.</span>';
          } else {
            const completedCount = targets.filter(t => t.is_completed || t.current >= t.goal).length;
            const itemTexts = targets.slice(0, 5).map(t => {
              const done = t.is_completed || t.current >= t.goal;
              const color = done ? '#34d399' : '#fbbf24';
              return `<span style="color:#f1f5f9;">${t.item_name}</span> (<span style="color:${color};font-weight:600;">${t.current}/${t.goal}</span>)`;
            });
            const more = targets.length > 5 ? ` <span style="color:#94a3b8;">외 ${targets.length - 5}건</span>` : '';
            const statusBadge = completedCount === targets.length
              ? '<span style="color:#34d399;font-weight:700;">[전체 달성]</span>'
              : `<span style="color:#c4b5fd;font-weight:600;">[${completedCount}/${targets.length} 완료]</span>`;
            quickSummaryEl.innerHTML = `${statusBadge} ${itemTexts.join(', ')}${more}`;
          }
        }

        if (!container) return;
        if (targets.length === 0) {
          container.innerHTML = '<p style="color: #64748b; font-size: 12.5px; grid-column: 1/-1;">등록된 주간 납품 목표가 없습니다. 인게임에서 퀘스트를 선택하고 [➕ 현재 퀘스트 등록]을 누르거나 직접 추가하세요.</p>';
          return;
        }

        let html = '';
        targets.forEach(t => {
          const isDone = t.is_completed || (t.current >= t.goal);
          const percent = Math.min(100, Math.round((t.current / (t.goal || 1)) * 100));
          const safeName = (t.item_name || '').replace(/'/g, "\\'");

          let recHtml = '';
          const rec = t.recipe || {};
          const ingKeys = Object.keys(rec);
          if (ingKeys.length > 0) {
            const ingListStr = ingKeys.map(k => `${k} ${rec[k]}개`).join(', ');
            recHtml = `
              <div style="background: rgba(168, 85, 247, 0.08); border: 1px solid rgba(168, 85, 247, 0.2); border-radius: 6px; padding: 4px 8px; font-size: 11px; display: flex; justify-content: space-between; align-items: center; gap: 6px;">
                <span style="color: #e9d5ff; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="1회 제작 재료: ${ingListStr}">
                  📦 1회 재료: <strong>${ingListStr}</strong>
                </span>
                <button class="btn btn-sm" onclick="openRecipeModal('${safeName}')" style="padding: 1px 6px; font-size: 10px; white-space: nowrap; background: rgba(168, 85, 247, 0.2); color: #d8b4fe; border-color: rgba(168, 85, 247, 0.35);">
                  ✏️ 레시피
                </button>
              </div>
            `;
          }

          html += `
            <div style="background: rgba(0,0,0,0.3); border: 1px solid ${isDone ? 'rgba(52,211,153,0.35)' : 'rgba(255,255,255,0.08)'}; border-radius: 10px; padding: 12px; display: flex; flex-direction: column; justify-content: space-between; gap: 8px;">
              <div style="display: flex; justify-content: space-between; align-items: flex-start;">
                <div>
                  <div style="font-weight: 700; font-size: 13.5px; color: #f1f5f9;">${t.item_name}</div>
                  <div style="font-size: 11px; color: #94a3b8;">${t.quest_title || '주간 납품'}</div>
                </div>
                <div style="display: flex; align-items: center; gap: 6px;">
                  <span class="badge ${isDone ? '' : 'delivery'}" style="${isDone ? 'background: rgba(52,211,153,0.2); color: #34d399;' : ''}">${isDone ? '완료' : '진행 중'}</span>
                  <button class="btn btn-sm" style="padding: 2px 6px; font-size: 11px; color: #f87171; border-color: rgba(248,113,113,0.3);" onclick="deleteDeliveryTarget('${safeName}')" title="삭제">✕</button>
                </div>
              </div>

              ${recHtml}

              <!-- Progress bar -->
              <div style="background: rgba(255,255,255,0.08); border-radius: 4px; height: 6px; overflow: hidden; width: 100%;">
                <div style="background: ${isDone ? '#34d399' : '#fbbf24'}; height: 100%; width: ${percent}%;"></div>
              </div>

              <div style="display: flex; justify-content: space-between; align-items: center; font-size: 12px; flex-wrap: wrap; gap: 6px;">
                <div style="display: flex; align-items: center; gap: 4px;">
                  <span style="color: #cbd5e1;">보유:</span>
                  <button class="btn btn-sm" style="padding: 1px 6px; font-size: 11px; min-height: 20px; line-height: 18px;" onclick="adjustTargetCurrent('${safeName}', ${t.current}, -1)" title="1개 감소">-</button>
                  <strong style="color: ${isDone ? '#34d399' : '#fbbf24'}; cursor: pointer; text-decoration: underline dotted;" onclick="promptEditCurrent('${safeName}', ${t.current})" title="수량 클릭하여 직접 입력">${t.current}</strong>
                  <button class="btn btn-sm" style="padding: 1px 6px; font-size: 11px; min-height: 20px; line-height: 18px;" onclick="adjustTargetCurrent('${safeName}', ${t.current}, 1)" title="1개 증가">+</button>
                  <span style="color: #94a3b8;">/ ${t.goal}개</span>
                  <button class="btn btn-sm" style="padding: 1px 5px; font-size: 10px; min-height: 20px; line-height: 18px; color: #a5b4fc; border-color: rgba(165,180,252,0.3);" onclick="promptEditCurrent('${safeName}', ${t.current})" title="보유 수량 직접 수정">✏️</button>
                  <button class="btn btn-sm" style="padding: 1px 5px; font-size: 10px; min-height: 20px; line-height: 18px; color: #fbbf24; border-color: rgba(251,191,36,0.3);" onclick="updateTargetCurrent('${safeName}', 0)" title="현재 수량 0개로 리셋">🔄 0개로</button>
                  ${t.storage_count > 0 ? `<small style="color: #38bdf8; font-size: 10.5px; margin-left: 2px;">(가방 ${t.inventory_count || 0} + 창고 ${t.storage_count})</small>` : ''}
                </div>
                <span style="color: ${isDone ? '#34d399' : '#f43f5e'}; font-weight: 600;">${isDone ? '납품 준비 완료' : `${t.needed}개 부족`}</span>
              </div>
            </div>
          `;
        });
        container.innerHTML = html;
      } catch (e) {
        console.error('loadDeliveryTargets error:', e);
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
          container.innerHTML = '<span style="color: #64748b; font-size: 12px;">저장된 프리셋이 없습니다. 원하는 납품 목표를 등록한 뒤 이름을 입력하여 저장하세요.</span>';
          return;
        }

        let html = '';
        presets.forEach(p => {
          const itemCount = (p.items || []).length;
          const escapedName = (p.name || '').replace(/'/g, "\\'");
          html += `
            <div style="background: rgba(168, 85, 247, 0.12); border: 1px solid rgba(168, 85, 247, 0.3); border-radius: 8px; padding: 6px 12px; display: inline-flex; align-items: center; gap: 8px;">
              <span style="font-weight: 600; font-size: 12.5px; color: #e9d5ff;">${p.name}</span>
              <span class="badge" style="background: rgba(168, 85, 247, 0.25); color: #c4b5fd; font-size: 11px;">${itemCount}개 항목</span>
              <button class="btn btn-sm" style="padding: 2px 8px; font-size: 11px; background: rgba(56, 189, 248, 0.2); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.35);" onclick="loadPreset('${escapedName}')" title="이 프리셋으로 목표 등록">📂 불러오기</button>
              <button class="btn btn-sm" style="padding: 2px 6px; font-size: 11px; color: #f87171; border-color: rgba(248,113,113,0.3);" onclick="deletePreset('${escapedName}')" title="프리셋 삭제">✕</button>
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
      if (!confirm('등록된 모든 주간 납품 목표의 현재 보유 수량을 0개로 초기화할까요?\\n\\n(새로운 주간 퀘스트를 시작하거나 목표 품목들을 처음부터 제작하고자 할 때 유용합니다)')) return;
      try {
        const res = await fetch('/api/reset_delivery_targets_current', { method: 'POST' });
        const d = await res.json();
        if (d.status === 'success') {
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
        "'" + name + "' 프리셋의 납품 목표를 불러옵니다.\\n\\n" +
        "• [확인] 누름: 보유 수량을 0개로 초기화하여 새로 시작 (추천: 새 주간 퀘스트)\\n" +
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
      if (typeof applyBusyUI === 'function') applyBusyUI(true, '🎁 모든 시설 완료 가공품 순차 수령 중...');
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

        const tasksContainer = document.getElementById('batch-tasks-container');
        const tasks = plan.tasks || [];
        if (tasksContainer) {
          if (tasks.length === 0) {
            tasksContainer.innerHTML = '<p style="color: #64748b; font-size: 12px;">등록되거나 진행 중인 주간 납품 퀘스트가 없습니다.</p>';
          } else {
            let tasksHtml = '';
            tasks.forEach(t => {
              const isDone = t.is_completed || (t.needed === 0) || (t.current >= t.goal);
              const badge = isDone 
                ? '<span style="color:#34d399; font-weight:700; font-size:11.5px;">✓ 달성 완료</span>' 
                : `<span style="color:#fbbf24; font-weight:700; font-size:11.5px;">${t.needed}개 필요</span>`;
              tasksHtml += `
                <div style="display:flex; justify-content:space-between; align-items:center; padding: 6px 8px; background: rgba(255,255,255,0.04); border-radius: 6px; border: 1px solid ${isDone ? 'rgba(52,211,153,0.2)' : 'rgba(255,255,255,0.05)'};">
                  <div>
                    <span style="font-weight:600; font-size:12.5px; color:${isDone ? '#34d399' : '#e0e7ff'};">${t.item_name}</span>
                    <span style="font-size:11px; color:#94a3b8; margin-left:6px;">(${t.current}/${t.goal}개)</span>
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
          if (reqs.length === 0) {
            tbody.innerHTML = '<tr><td colspan="5" style="text-align: center; color: #64748b; padding: 12px;">필요한 1차 가공품이 없습니다.</td></tr>';
            if (btnExec) btnExec.disabled = true;
            if (statusText) statusText.innerText = plan.all_completed ? '모든 목표 달성 완료' : '납품 퀘스트 없음';
          } else {
            let rowsHtml = '';
            reqs.forEach(r => {
              const queuedBadge = r.queued_works_count > 0 ? `<span style="color:#818cf8; font-size:11px; margin-left:4px;">(대기 ${r.queued_works_count}회)</span>` : '';
              const completedBadge = r.completed_works_count > 0 ? `<span style="color:#38bdf8; font-size:11px; margin-left:4px;">(수령가능 ${r.completed_works_count}회)</span>` : '';

              let statusBadge = '';
              if (r.status === 'satisfied') {
                statusBadge = '<span style="color:#34d399; font-weight:700; white-space:nowrap;">● 준비 완료</span>' + completedBadge;
              } else if (r.status === 'needs_alter') {
                statusBadge = `<span style="color:#f43f5e; font-weight:700; white-space:nowrap;">▲ ${r.works_needed}회 가공 필요</span>` + queuedBadge + completedBadge;
              } else if (r.status === 'in_queue') {
                statusBadge = `<span style="color:#fbbf24; font-weight:700; white-space:nowrap;">⏳ 대기열 (${r.queued_works_count}회)</span>` + completedBadge;
              } else if (r.status === 'ready_to_collect') {
                statusBadge = `<span style="color:#38bdf8; font-weight:700; white-space:nowrap;">🎁 수령 가능 (${r.completed_works_count}회)</span>`;
              }

              const invCnt = r.inventory_count || 0;
              const storeCnt = r.storage_count || 0;
              const bagStorageText = `${invCnt}개<span style="color:${storeCnt > 0 ? '#38bdf8' : '#64748b'};">(${storeCnt}개)</span>`;
              const facShort = formatFacilityShort(r.facility);

              rowsHtml += `
                <tr>
                  <td style="font-weight:700; color:#e0e7ff; white-space:nowrap;">${r.item_name}</td>
                  <td style="text-align:center; white-space:nowrap;"><span class="badge" style="background:rgba(255,255,255,0.06); color:#cbd5e1; padding:2px 7px; font-size:11px;">${facShort}</span></td>
                  <td style="font-weight:700; color:#fbbf24; text-align:right; white-space:nowrap;">${r.total_needed}개</td>
                  <td style="text-align:center; white-space:nowrap;">${bagStorageText}</td>
                  <td style="white-space:nowrap;">${statusBadge}</td>
                </tr>
              `;
            });
            if (plan.raw_materials && plan.raw_materials.length > 0) {
              plan.raw_materials.forEach(rm => {
                let statusBadge = '';
                if (rm.deficit === 0) {
                  statusBadge = '<span style="color:#34d399; font-weight:700; white-space:nowrap;">● 준비 완료</span>';
                } else if (rm.tool_ok) {
                  statusBadge = `<span style="color:#34d399; font-weight:700; white-space:nowrap;">🌿 ${rm.deficit}개 자동 채집</span>`;
                } else {
                  statusBadge = `<span style="color:#f43f5e; font-weight:700; white-space:nowrap;">⚠️ 도구 필요 (${rm.deficit}개 부족)</span>`;
                }

                const rmInvCnt = rm.inventory_count || 0;
                const rmStoreCnt = rm.storage_count || 0;
                const rmBagStorageText = `${rmInvCnt}개<span style="color:${rmStoreCnt > 0 ? '#38bdf8' : '#64748b'};">(${rmStoreCnt}개)</span>`;

                rowsHtml += `
                  <tr style="background: rgba(16, 185, 129, 0.05); border-left: 2px solid #34d399;">
                    <td style="font-weight:700; color:#a7f3d0; white-space:nowrap;">🌿 ${rm.item_name}</td>
                    <td style="text-align:center; white-space:nowrap;"><span class="badge" style="background:rgba(16,185,129,0.15); color:#34d399; padding:2px 7px; font-size:11px;">채집</span></td>
                    <td style="font-weight:700; color:#fbbf24; text-align:right; white-space:nowrap;">${rm.total_needed}개</td>
                    <td style="text-align:center; white-space:nowrap;">${rmBagStorageText}</td>
                    <td style="white-space:nowrap;">${statusBadge}</td>
                  </tr>
                `;
              });
            }

            tbody.innerHTML = rowsHtml;

            if (btnExec && statusText) {
              const craftableNote = (plan.craftable_tasks_count > 0) ? ` (즉시 제작 가능: ${plan.craftable_tasks_count}건)` : '';
              const neededRawGather = (plan.raw_materials || []).filter(r => r.deficit > 0);
              const gatherNote = (neededRawGather.length > 0) ? ` / 채집 ${neededRawGather.length}종` : '';

              if (plan.all_completed) {
                btnExec.innerText = '🎉 모든 납품 목표 달성 완료!';
                btnExec.className = 'btn';
                btnExec.disabled = true;
                statusText.innerHTML = '<span style="color:#34d399; font-weight:700;">모든 목표 달성 완료</span>';
              } else if (plan.can_craft_immediately) {
                btnExec.innerText = '🚀 재료 준비 완료! 즉시 일괄 최종 제작';
                btnExec.className = 'btn btn-emerald';
                btnExec.disabled = false;
                statusText.innerHTML = '<span style="color:#34d399; font-weight:700;">모든 재료 준비 완료</span>';
              } else if (plan.total_works_to_queue > 0) {
                btnExec.innerText = `⚡ 일괄 가공(${plan.total_works_to_queue}회)${gatherNote} & 즉시 제작${craftableNote}`;
                btnExec.className = 'btn btn-primary';
                btnExec.disabled = false;
                statusText.innerHTML = `<span style="color:#fbbf24; font-weight:700;">가공 ${plan.total_works_to_queue}회 등록 필요${gatherNote}${craftableNote}</span>`;
              } else if (neededRawGather.length > 0) {
                btnExec.innerText = `🌿 부족 채집물 (${neededRawGather.length}종) 맞춤 채집 & 납품 준비${craftableNote}`;
                btnExec.className = 'btn btn-emerald';
                btnExec.disabled = false;
                statusText.innerHTML = `<span style="color:#34d399; font-weight:700;">필드 채집 ${neededRawGather.length}종 필요${craftableNote}</span>`;
              } else if (plan.craftable_tasks_count > 0) {
                btnExec.innerText = `🔨 준비된 완제품 (${plan.craftable_tasks_count}건) 즉시 제작`;
                btnExec.className = 'btn btn-emerald';
                btnExec.disabled = false;
                statusText.innerHTML = `<span style="color:#34d399; font-weight:700;">${plan.craftable_tasks_count}건 즉시 제작 가능</span>`;
              } else {
                btnExec.innerText = '⏳ 가공 진행 중 (완료 시 제작 가능)';
                btnExec.className = 'btn';
                btnExec.disabled = false;
                statusText.innerHTML = '<span style="color:#38bdf8; font-weight:700;">대기열 가공 완료 대기 중</span>';
              }
            }
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
              const used = f.in_progress + f.completed;
              const max = f.max_slots || 7;
              const avail = f.available_now;
              const needed = f.new_works_needed || 0;
              const isOver = f.overflow > 0;
              const cardBorder = isOver ? 'rgba(244, 63, 94, 0.4)' : (needed > 0 ? 'rgba(99, 102, 241, 0.3)' : 'rgba(255, 255, 255, 0.06)');
              const cardBg = isOver ? 'rgba(244, 63, 94, 0.08)' : 'rgba(255, 255, 255, 0.03)';
              
              const progDetail = f.in_progress_detail ? ` (${f.in_progress_detail})` : '';
              const compDetail = f.completed_detail ? ` (${f.completed_detail})` : '';

              html += `
                <div style="flex: 1 1 calc(33.333% - 8px); min-width: 185px; background: ${cardBg}; border: 1px solid ${cardBorder}; border-radius: 8px; padding: 8px 10px;">
                  <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                    <span style="font-weight: 700; font-size: 12px; color: #e2e8f0;">${f.facility}</span>
                    <span style="font-size: 11px; font-weight: 600; color: ${avail > 0 ? '#34d399' : '#f43f5e'};">
                      여유 ${avail} / ${max}
                    </span>
                  </div>
                  <div style="font-size: 11px; color: #94a3b8; display: flex; justify-content: space-between; flex-wrap: wrap; gap: 4px;">
                    <span>진행: ${f.in_progress}${progDetail ? `<span style="color:#c7d2fe; font-size:10px;">${progDetail}</span>` : ''} | 완료: ${f.completed}${compDetail ? `<span style="color:#6ee7b7; font-size:10px;">${compDetail}</span>` : ''}</span>
                    ${needed > 0 ? `<span style="font-weight: 700; color: ${isOver ? '#f43f5e' : '#fbbf24'};">필요 +${needed}</span>` : '<span style="color: #64748b;">필요 없음</span>'}
                  </div>
                  ${f.overflow > 0 ? `<div style="font-size: 10.5px; color: #fb7185; margin-top: 3px; font-weight: 600;">⚠️ ${f.overflow}회 초과 (단계적 진행)</div>` : ''}
                </div>
              `;
            });
            slotsContainer.innerHTML = html;
            if (summaryBadge) {
              summaryBadge.innerHTML = plan.has_slot_issue 
                ? '<span style="color: #fbbf24; font-weight: 700;">⚠️ 일부 시설 슬롯 초과 예상</span>' 
                : '<span style="color: #34d399; font-weight: 600;">✓ 전 시설 슬롯 여유 충분</span>';
            }
          } else {
            slotsSec.style.display = 'none';
          }
        }

        if (warnBanner && warnList) {
          if (warnings.length > 0) {
            warnBanner.style.display = 'block';
            warnList.innerHTML = warnings.map(w => `<div style="margin-bottom: 3px;">• ${w}</div>`).join('');
          } else {
            warnBanner.style.display = 'none';
          }
        }
      } catch (e) {
        console.error('Error loading batch plan:', e);
      }
    }

    async function executeBatchPipeline() {
      const orderDesc = currentAlterOrder === 'low_tier' ? '하위 티어 우선 (기초 재료부터)' : '상위 티어 우선 (오래 걸리는 가공부터)';
      if (confirm(`전체 주간 납품 퀘스트를 위한 일괄 재료 수급 및 제작 파이프라인을 가동할까요?\\n[가공 우선순위: ${orderDesc}]\\n\\n(부족한 가공품이 해당 시설에 일괄 등록되며 주간 납품 전용 알람이 설정됩니다)`)) {
        isCurrentlyBusy = true;
        if (typeof applyBusyUI === 'function') applyBusyUI(true, '⚡ 전체 주간 납품 일괄 최적화 제작 준비 중...');
        try {
          const res = await fetch('/api/execute_batch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ alter_order: currentAlterOrder })
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
        if (typeof applyBusyUI === 'function') applyBusyUI(true, `🎁 '${displayName}' 가공품 수령 이동 중...`);
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
    let cachedCustomPlan = null;

    async function loadCustomTargets() {
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
          container.innerHTML = '<p style="color: #64748b; font-size: 12.5px; grid-column: 1/-1;">등록된 개별 제작 목표가 없습니다. 위 입력창에서 제작할 아이템을 등록하세요.</p>';
          return;
        }

        let html = '';
        targets.forEach(t => {
          const isDone = t.is_completed || (t.current >= t.goal);
          const percent = Math.min(100, Math.round((t.current / (t.goal || 1)) * 100));
          const safeName = (t.item_name || '').replace(/'/g, "\\'");

          let recHtml = '';
          const rec = t.recipe || {};
          const ingKeys = Object.keys(rec);
          if (ingKeys.length > 0) {
            const ingListStr = ingKeys.map(k => `${k} ${rec[k]}개`).join(', ');
            recHtml = `
              <div style="background: rgba(16, 185, 129, 0.08); border: 1px solid rgba(16, 185, 129, 0.2); border-radius: 6px; padding: 4px 8px; font-size: 11px; display: flex; justify-content: space-between; align-items: center; gap: 6px;">
                <span style="color: #a7f3d0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="1회 제작 재료: ${ingListStr}">
                  📦 1회 재료: <strong>${ingListStr}</strong>
                </span>
                <button class="btn btn-sm" onclick="openRecipeModal('${safeName}')" style="padding: 1px 6px; font-size: 10.5px; white-space: nowrap; background: rgba(16, 185, 129, 0.2); color: #6ee7b7; border-color: rgba(16, 185, 129, 0.35);">
                  ✏️ 레시피
                </button>
              </div>
            `;
          } else {
            recHtml = `
              <div style="background: rgba(245, 158, 11, 0.08); border: 1px solid rgba(245, 158, 11, 0.25); border-radius: 6px; padding: 4px 8px; font-size: 11px; display: flex; justify-content: space-between; align-items: center; gap: 6px;">
                <span style="color: #fcd34d;">⚠️ 재료 미확인 (클릭하여 직접 입력)</span>
                <button class="btn btn-sm" onclick="openRecipeModal('${safeName}')" style="padding: 1px 6px; font-size: 10.5px; white-space: nowrap; background: rgba(245, 158, 11, 0.2); color: #fbbf24; border-color: rgba(245, 158, 11, 0.35);">
                  ➕ 재료 등록
                </button>
              </div>
            `;
          }

          html += `
            <div style="background: rgba(0,0,0,0.3); border: 1px solid ${isDone ? 'rgba(52,211,153,0.35)' : 'rgba(255,255,255,0.08)'}; border-radius: 10px; padding: 12px; display: flex; flex-direction: column; justify-content: space-between; gap: 8px;">
              <div style="display: flex; justify-content: space-between; align-items: flex-start;">
                <div>
                  <div style="font-weight: 700; font-size: 13.5px; color: #f1f5f9;">${t.item_name}</div>
                  <div style="font-size: 11px; color: #94a3b8;">가방: ${t.inventory_count || 0}개 | 개인창고: ${t.char_storage_count || 0}개 | 공용창고: ${t.account_storage_count || 0}개 (총 ${t.current || 0}개)</div>
                </div>
                <div style="display: flex; align-items: center; gap: 6px;">
                  <span class="badge" style="background: ${isDone ? 'rgba(16,185,129,0.2)' : 'rgba(245,158,11,0.2)'}; color: ${isDone ? '#34d399' : '#fbbf24'}; font-size: 11px;">
                    ${isDone ? '달성 완료' : (t.needed || 0) + '개 부족'}
                  </span>
                  <button class="btn btn-sm" onclick="deleteCustomTarget('${safeName}')" style="padding: 2px 6px; font-size: 11px; color: #f87171; background: rgba(248,113,113,0.1); border-color: rgba(248,113,113,0.2);" title="삭제">✕</button>
                </div>
              </div>

              ${recHtml}

              <div>
                <div style="display: flex; justify-content: space-between; align-items: center; font-size: 12px; margin-bottom: 4px;">
                  <span style="color: #94a3b8;">진행도</span>
                  <span style="font-weight: 700; color: ${isDone ? '#34d399' : '#e2e8f0'};">${t.current} / ${t.goal}개 (${percent}%)</span>
                </div>
                <div style="width: 100%; height: 6px; background: rgba(255,255,255,0.08); border-radius: 3px; overflow: hidden;">
                  <div style="width: ${percent}%; height: 100%; background: ${isDone ? 'linear-gradient(90deg, #10b981, #34d399)' : 'linear-gradient(90deg, #f59e0b, #fbbf24)'}; border-radius: 3px;"></div>
                </div>
              </div>

              <div style="display: flex; justify-content: space-between; align-items: center; font-size: 12px; flex-wrap: wrap; gap: 6px; padding-top: 6px; border-top: 1px solid rgba(255,255,255,0.04);">
                <div style="display: flex; align-items: center; gap: 4px;">
                  <span style="color: #cbd5e1;">보유:</span>
                  <button class="btn btn-sm" style="padding: 1px 6px; font-size: 11px; min-height: 20px; line-height: 18px;" onclick="adjustCustomTargetCurrent('${safeName}', ${t.current}, -1)" title="1개 감소">-</button>
                  <strong style="color: ${isDone ? '#34d399' : '#fbbf24'}; cursor: pointer; text-decoration: underline dotted;" onclick="promptEditCustomCurrent('${safeName}', ${t.current})" title="수량 클릭하여 직접 입력">${t.current}</strong>
                  <button class="btn btn-sm" style="padding: 1px 6px; font-size: 11px; min-height: 20px; line-height: 18px;" onclick="adjustCustomTargetCurrent('${safeName}', ${t.current}, 1)" title="1개 증가">+</button>
                  <button class="btn btn-sm" style="padding: 1px 5px; font-size: 10px; min-height: 20px; line-height: 18px; color: #a5b4fc; border-color: rgba(165,180,252,0.3);" onclick="promptEditCustomCurrent('${safeName}', ${t.current})" title="보유 수량 직접 수정">✏️</button>
                  <button class="btn btn-sm" style="padding: 1px 5px; font-size: 10px; min-height: 20px; line-height: 18px; color: #fbbf24; border-color: rgba(251,191,36,0.3);" onclick="updateCustomTargetCurrent('${safeName}', 0)" title="현재 수량 0개로 리셋">🔄 0개로</button>
                </div>
                <div style="display: flex; align-items: center; gap: 4px;">
                  <span style="color: #94a3b8;">목표:</span>
                  <strong style="color: #e2e8f0;">${t.goal}개</strong>
                  <button class="btn btn-sm" onclick="updateCustomTargetGoal('${safeName}', -1)" style="padding: 1px 7px; font-size: 11px; background: rgba(255,255,255,0.06);">-1</button>
                  <button class="btn btn-sm" onclick="updateCustomTargetGoal('${safeName}', 1)" style="padding: 1px 7px; font-size: 11px; background: rgba(255,255,255,0.06);">+1</button>
                </div>
              </div>
            </div>
          `;
        });
        container.innerHTML = html;
      } catch (e) {
        console.error('loadCustomTargets error:', e);
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
      if (!confirm('등록된 모든 개별 제작 목표의 현재 보유 수량을 0개로 초기화할까요?\\n\\n(모든 목표 아이템을 처음부터 다시 제작하고자 할 때 유용합니다)')) return;
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
      if (!confirm('등록된 모든 개별 제작 목표를 비우시겠습니까?')) return;
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
      if (titleEl) titleEl.innerText = currentRecipeModalItem ? `📖 제작 레시피 설정: '${currentRecipeModalItem}'` : '📖 제작 레시피 설정 / 수정';
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

      container.innerHTML = '<div style="color:#94a3b8; font-size:12px; padding:8px;">레시피 조회 중...</div>';
      try {
        const res = await fetch(`/api/recipe?name=${encodeURIComponent(itemName)}`);
        const data = await res.json();
        container.innerHTML = '';
        const ings = data.ingredients || {};
        const keys = Object.keys(ings);
        if (keys.length === 0) {
          if (msgEl) msgEl.innerText = '⚠️ 등록된 레시피가 없습니다. 아래에 재료를 추가해주세요.';
          addRecipeModalRow('', 1);
        } else {
          if (msgEl) msgEl.innerText = `총 ${keys.length}개 재료 로드됨`;
          keys.forEach(k => {
            addRecipeModalRow(k, ings[k]);
          });
        }
      } catch (e) {
        container.innerHTML = `<div style="color:#f87171; font-size:12px; padding:8px;">조회 실패: ${e}</div>`;
      }
    }

    function addRecipeModalRow(name, count) {
      const container = document.getElementById('recipe-modal-rows-container');
      if (!container) return;
      const row = document.createElement('div');
      row.className = 'recipe-modal-row';
      row.style.cssText = 'display: flex; gap: 8px; align-items: center; background: rgba(0,0,0,0.25); padding: 6px 10px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.06);';
      row.innerHTML = `
        <input type="text" class="recipe-row-name" value="${(name || '').replace(/"/g, '&quot;')}" placeholder="재료명 (예: 달걀, 물이 든 병)" style="flex: 2; padding: 7px 10px; background: rgba(0,0,0,0.4); border: 1px solid rgba(255,255,255,0.12); border-radius: 6px; color: #fff; font-size: 12.5px;">
        <div style="display: flex; align-items: center; gap: 4px;">
          <input type="number" class="recipe-row-qty" value="${count || 1}" min="1" max="999" style="width: 65px; padding: 7px 8px; background: rgba(0,0,0,0.4); border: 1px solid rgba(255,255,255,0.12); border-radius: 6px; color: #fff; font-size: 12.5px; text-align: center;">
          <span style="font-size: 11.5px; color: #94a3b8;">개</span>
        </div>
        <button type="button" class="btn btn-sm" onclick="this.closest('.recipe-modal-row').remove()" style="padding: 4px 8px; font-size: 11px; color: #f87171; border-color: rgba(248,113,113,0.3); background: rgba(248,113,113,0.1);" title="이 재료 삭제">✕</button>
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
          if (msgEl) msgEl.innerText = '✅ 저장 완료!';
          closeRecipeModal();
          pollLogs();
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

        if (plan.error) {
          tbody.innerHTML = `<tr><td colspan="7" style="text-align: center; color: #f43f5e; padding: 20px;">오류: ${plan.error}</td></tr>`;
          if (statusText) statusText.innerText = '계획 로드 실패';
          return;
        }

        const materials = plan.intermediate_requirements || plan.materials || [];
        const rawMaterials = plan.raw_materials || [];

        if (materials.length === 0 && rawMaterials.length === 0) {
          tbody.innerHTML = `<tr><td colspan="7" style="text-align: center; color: #64748b; padding: 24px;">등록된 개별 제작 목표가 없습니다. 위에서 목표를 추가하세요.</td></tr>`;
          if (statusText) statusText.innerText = '등록된 목표 없음';
          if (btnExec) {
            btnExec.disabled = true;
            btnExec.innerText = '⚡ 개별 지정 재료 일괄 가공 & 제작 시작';
            btnExec.className = 'btn';
          }
          if (summaryText) summaryText.innerHTML = '';
          return;
        }

        let rowsHtml = '';
        materials.forEach(m => {
          let statusBadge = '';
          if (m.is_ready) {
            statusBadge = '<span style="color:#34d399; font-weight:700;">● 재료 준비 완료</span>';
          } else if (m.in_progress_count > 0 && m.in_progress_count >= m.works_needed) {
            statusBadge = `<span style="color:#38bdf8; font-weight:700;">⏳ 가공 진행 중 (${m.in_progress_count}/${m.works_needed}회)</span>`;
          } else {
            const shortFac = formatFacilityShort(m.facility);
            statusBadge = `<span style="color:#fbbf24; font-weight:700;">⚙️ ${shortFac} ${m.works_to_queue}회 등록 필요</span>`;
          }

          const invCnt = m.inventory_count || 0;
          const storeCnt = m.storage_count || 0;
          const bagStorageText = `${invCnt}개<span style="color:${storeCnt > 0 ? '#38bdf8' : '#64748b'};">(${storeCnt}개)</span>`;
          const shortFac = formatFacilityShort(m.facility);

          rowsHtml += `
            <tr style="${m.deficit > 0 ? 'background: rgba(245, 158, 11, 0.05);' : ''}">
              <td style="font-weight:700; color:#f1f5f9; white-space:nowrap;">${m.item_name}</td>
              <td style="text-align:center; white-space:nowrap;"><span class="badge" style="background:rgba(99,102,241,0.15); color:#a5b4fc; padding:2px 7px; font-size:11px;">${shortFac}</span></td>
              <td style="font-weight:700; color:#fbbf24; text-align:right; white-space:nowrap;">${m.total_needed}개</td>
              <td style="text-align:center; white-space:nowrap;">${bagStorageText}</td>
              <td style="font-weight:700; color:${m.deficit > 0 ? '#f43f5e' : '#34d399'}; text-align:right; white-space:nowrap;">
                ${m.deficit > 0 ? `-${m.deficit}개` : '0개'}
              </td>
              <td style="text-align:center; font-weight:700; color:${m.works_to_queue > 0 ? '#fbbf24' : '#64748b'}; white-space:nowrap;">
                ${m.works_to_queue > 0 ? `+${m.works_to_queue}회` : '-'}
              </td>
              <td style="white-space:nowrap;">${statusBadge}</td>
            </tr>
          `;
        });

        // Raw materials
        if (rawMaterials.length > 0) {
          rawMaterials.forEach(rm => {
            let statusBadge = '';
            if (rm.deficit === 0) {
              statusBadge = '<span style="color:#34d399; font-weight:700; white-space:nowrap;">● 준비 완료</span>';
            } else if (rm.tool_ok) {
              statusBadge = `<span style="color:#34d399; font-weight:700; white-space:nowrap;">🌿 ${rm.deficit}개 자동 채집</span>`;
            } else {
              statusBadge = `<span style="color:#f43f5e; font-weight:700; white-space:nowrap;">⚠️ 도구 필요 (${rm.deficit}개 부족)</span>`;
            }

            const rmInvCnt = rm.inventory_count || 0;
            const rmStoreCnt = rm.storage_count || 0;
            const rmBagStorageText = `${rmInvCnt}개<span style="color:${rmStoreCnt > 0 ? '#38bdf8' : '#64748b'};">(${rmStoreCnt}개)</span>`;

            rowsHtml += `
              <tr style="background: rgba(16, 185, 129, 0.05); border-left: 2px solid #34d399;">
                <td style="font-weight:700; color:#a7f3d0; white-space:nowrap;">🌿 ${rm.item_name}</td>
                <td style="text-align:center; white-space:nowrap;"><span class="badge" style="background:rgba(16,185,129,0.15); color:#34d399; padding:2px 7px; font-size:11px;">채집</span></td>
                <td style="font-weight:700; color:#fbbf24; text-align:right; white-space:nowrap;">${rm.total_needed}개</td>
                <td style="text-align:center; white-space:nowrap;">${rmBagStorageText}</td>
                <td style="font-weight:700; color:${rm.deficit > 0 ? '#f43f5e' : '#34d399'}; text-align:right; white-space:nowrap;">
                  ${rm.deficit > 0 ? `-${rm.deficit}개` : '0개'}
                </td>
                <td style="text-align:center; color:#64748b; white-space:nowrap;">-</td>
                <td style="white-space:nowrap;">${statusBadge}</td>
              </tr>
            `;
          });
        }

        tbody.innerHTML = rowsHtml;

        // Button state and text
        if (btnExec && statusText) {
          const craftableNote = (plan.craftable_tasks_count > 0) ? ` (즉시 제작 가능: ${plan.craftable_tasks_count}건)` : '';
          const neededRawGather = (plan.raw_materials || []).filter(r => r.deficit > 0);
          const gatherNote = (neededRawGather.length > 0) ? ` / 채집 ${neededRawGather.length}종` : '';

          if (plan.all_completed) {
            btnExec.innerText = '🎉 모든 개별 목표 제작 완료!';
            btnExec.className = 'btn';
            btnExec.disabled = true;
            statusText.innerHTML = '<span style="color:#34d399; font-weight:700;">모든 목표 달성 완료</span>';
          } else if (plan.can_craft_immediately) {
            btnExec.innerText = '🚀 재료 준비 완료! 즉시 일괄 최종 제작';
            btnExec.className = 'btn btn-emerald';
            btnExec.disabled = isCurrentlyBusy;
            statusText.innerHTML = '<span style="color:#34d399; font-weight:700;">모든 재료 준비 완료</span>';
          } else if (plan.total_works_to_queue > 0) {
            btnExec.innerText = `⚡ 일괄 가공(${plan.total_works_to_queue}회)${gatherNote} & 즉시 제작${craftableNote}`;
            btnExec.className = 'btn btn-primary';
            btnExec.disabled = isCurrentlyBusy;
            statusText.innerHTML = `<span style="color:#fbbf24; font-weight:700;">가공 ${plan.total_works_to_queue}회 등록 필요${gatherNote}${craftableNote}</span>`;
          } else if (neededRawGather.length > 0) {
            btnExec.innerText = `🌿 부족 채집물 (${neededRawGather.length}종) 맞춤 채집 & 제작 준비${craftableNote}`;
            btnExec.className = 'btn btn-emerald';
            btnExec.disabled = isCurrentlyBusy;
            statusText.innerHTML = `<span style="color:#34d399; font-weight:700;">필드 채집 ${neededRawGather.length}종 필요${craftableNote}</span>`;
          } else if (plan.craftable_tasks_count > 0) {
            btnExec.innerText = `🔨 준비된 완제품 (${plan.craftable_tasks_count}건) 즉시 제작`;
            btnExec.className = 'btn btn-emerald';
            btnExec.disabled = isCurrentlyBusy;
            statusText.innerHTML = `<span style="color:#34d399; font-weight:700;">${plan.craftable_tasks_count}건 즉시 제작 가능</span>`;
          } else {
            btnExec.innerText = '⏳ 가공 진행 중 (완료 시 제작 가능)';
            btnExec.className = 'btn';
            btnExec.disabled = isCurrentlyBusy;
            statusText.innerHTML = '<span style="color:#38bdf8; font-weight:700;">대기열 가공 완료 대기 중</span>';
          }
        }

        // Summary Text
        if (summaryText) {
          const neededRawGather = (plan.raw_materials || []).filter(r => r.deficit > 0);
          const rawText = neededRawGather.length > 0 ? `필드 채집 <strong>${neededRawGather.length}종</strong>` : '채집 불필요';
          const alterText = plan.total_works_to_queue > 0 ? `가공 <strong>${plan.total_works_to_queue}회</strong>` : '가공 불필요';
          summaryText.innerHTML = `✨ <span>총 분석 결과: ${rawText} · ${alterText} · 제작 <strong>${(plan.custom_tasks || []).length}건</strong></span>`;
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
              const cardBorder = isOver ? 'rgba(244, 63, 94, 0.4)' : (needed > 0 ? 'rgba(16, 185, 129, 0.3)' : 'rgba(255, 255, 255, 0.06)');
              const cardBg = isOver ? 'rgba(244, 63, 94, 0.08)' : 'rgba(255, 255, 255, 0.03)';
              
              html += `
                <div style="flex: 1 1 calc(33.333% - 8px); min-width: 185px; background: ${cardBg}; border: 1px solid ${cardBorder}; border-radius: 8px; padding: 8px 10px;">
                  <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                    <span style="font-weight: 700; font-size: 12px; color: #e2e8f0;">${f.facility}</span>
                    <span style="font-size: 11px; font-weight: 600; color: ${avail > 0 ? '#34d399' : '#f43f5e'};">
                      여유 ${avail} / ${max}
                    </span>
                  </div>
                  <div style="font-size: 11px; color: #94a3b8; display: flex; justify-content: space-between; flex-wrap: wrap; gap: 4px;">
                    <span>진행: ${f.in_progress} | 완료: ${f.completed}</span>
                    ${needed > 0 ? `<span style="font-weight: 700; color: ${isOver ? '#f43f5e' : '#fbbf24'};">필요 +${needed}</span>` : '<span style="color: #64748b;">필요 없음</span>'}
                  </div>
                  ${f.overflow > 0 ? `<div style="font-size: 10.5px; color: #fb7185; margin-top: 3px; font-weight: 600;">⚠️ ${f.overflow}회 초과 (단계적 진행)</div>` : ''}
                </div>
              `;
            });
            slotsContainer.innerHTML = html;
            if (summaryBadge) {
              summaryBadge.innerHTML = plan.has_slot_issue 
                ? '<span style="color: #fbbf24; font-weight: 700;">⚠️ 일부 시설 슬롯 초과 예상</span>' 
                : '<span style="color: #34d399; font-weight: 600;">✓ 전 시설 슬롯 여유 충분</span>';
            }
          } else {
            slotsSec.style.display = 'none';
          }
        }

        if (warnBanner && warnList) {
          if (warnings.length > 0) {
            warnBanner.style.display = 'block';
            warnList.innerHTML = warnings.map(w => `<div style="margin-bottom: 3px;">• ${w}</div>`).join('');
          } else {
            warnBanner.style.display = 'none';
          }
        }
      } catch (e) {
        console.error('loadCustomPlan error:', e);
      }
    }

    async function executeCustomPipeline() {
      const orderDesc = currentAlterOrder === 'low_tier' ? '하위 티어 우선 (기초 재료부터)' : '상위 티어 우선 (오래 걸리는 가공부터)';
      if (confirm(`등록된 개별 아이템 목표들을 위한 일괄 재료 수급 및 제작 파이프라인을 가동할까요?\\n[가공 우선순위: ${orderDesc}]\\n\\n(원자재 자동 채집 ➔ 시설 가공 일괄 등록 ➔ 최종 아이템 제작 원스톱 수행)`)) {
        isCurrentlyBusy = true;
        if (typeof applyBusyUI === 'function') applyBusyUI(true, '🔨 개별 지정 아이템 일괄 최적화 제작 준비 중...');
        try {
          const res = await fetch('/api/execute_custom', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ alter_order: currentAlterOrder })
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
      const quickAlterBtn = document.getElementById('btn-quick-alter-execute');
      const customExecuteBtn = document.getElementById('btn-custom-execute');
      const abortBtn = document.getElementById('btn-quick-alter-abort');
      const customAbortBtn = document.getElementById('btn-custom-abort');

      if (busy) {
        if (busyBox) busyBox.style.display = 'inline-flex';
        if (busyText && taskDesc) busyText.innerText = taskDesc;
        if (produceBtn) produceBtn.disabled = true;
        if (batchBtn) batchBtn.disabled = true;
        if (quickAlterBtn) quickAlterBtn.disabled = true;
        if (customExecuteBtn) customExecuteBtn.disabled = true;
        if (abortBtn) abortBtn.style.display = 'inline-flex';
        if (customAbortBtn) customAbortBtn.style.display = 'inline-flex';
      } else {
        if (busyBox) busyBox.style.display = 'none';
        if (produceBtn) produceBtn.disabled = false;
        if (batchBtn) batchBtn.disabled = false;
        if (quickAlterBtn && cachedQuickPlan) quickAlterBtn.disabled = !cachedQuickPlan.can_start;
        if (customExecuteBtn && cachedCustomPlan) customExecuteBtn.disabled = cachedCustomPlan.all_completed;
        if (abortBtn) abortBtn.style.display = 'none';
        if (customAbortBtn) customAbortBtn.style.display = 'none';
      }
    }

    async function updateStatus() {
      const statusPill = document.getElementById('pill-status');
      const wingsPill = document.getElementById('pill-wings');
      const weightPill = document.getElementById('pill-weight');

      try {
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 30000);
        const res = await fetch('/api/status', { signal: controller.signal });
        clearTimeout(timeoutId);
        const data = await res.json();
        statusFailCount = 0;

        const currentlyBusy = !!data.is_busy;
        if (!currentlyBusy && wasBusy) {
          // Task just finished! Stagger refreshes to avoid CLI process queue contention
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
          if (statusPill) {
            if (currentlyBusy) {
              statusPill.className = "pill warning";
              statusPill.innerHTML = `● 작업 진행 중...`;
              statusPill.title = data.current_task || '작업 진행 중';
            } else {
              statusPill.className = "pill online";
              statusPill.innerHTML = `● ${char.RealmName || '서버'} | Lv.${char.Level || 0} ${char.EnabledCombatJobDisplayName || ''}`;
              statusPill.title = "정상 연결됨 (클릭하여 설정 확인)";
            }
          }
          if (cliAlertBanner) cliAlertBanner.style.display = 'none';
          if (wingsPill) {
            wingsPill.innerHTML = `🪽 날개: ${(data.wings || 0).toLocaleString()}개`;
          }
          const inv = data.inventory || {};
          if (weightPill) {
            weightPill.innerHTML = `📦 무게: ${inv.CurrentInventoryWeightAsDecimal || 0}/${inv.MaxInventoryWeightAsDecimal || 0}`;
          }
        } else {
          const errCode = data.error_code || '';
          let shortStatus = '연결 끊김';

          if (errCode === 'CLI_NOT_FOUND') {
            shortStatus = 'CLI 미발견 (클릭하여 설정)';
            if (alertIcon) alertIcon.innerText = '📁';
            if (alertTitle) alertTitle.innerText = '마비노기 모바일 연동 파일(MabinogiMobile_CLI.exe)을 찾을 수 없습니다';
            if (alertDesc) alertDesc.innerHTML = '게임이 C: 드라이브가 아닌 D: 드라이브나 다른 폴더에 설치되어 있다면 아래에 설치 폴더를 입력해주세요.<br>또한 게임 내 [환경설정] → [기타]에서 <strong>[MM AI 에이전트 활성화]</strong>가 ON으로 켜져 있어야 합니다.';
            if (alertPathBox) alertPathBox.style.display = 'flex';
          } else if (errCode === 'OPTION_OFF') {
            shortStatus = 'AI 에이전트 OFF';
            if (alertIcon) alertIcon.innerText = '⚙️';
            if (alertTitle) alertTitle.innerText = '게임 설정에서 [MM AI 에이전트 활성화]가 꺼져 있습니다';
            if (alertDesc) alertDesc.innerHTML = '마비노기 모바일 PC 클라이언트 [환경설정] → [기타/게임 설정]에서 <strong>[MM AI 에이전트 활성화]</strong>를 ON으로 켜주세요. 옵션을 켜면 즉시 연동됩니다.';
            if (alertPathBox) alertPathBox.style.display = 'none';
          } else if (errCode === 'GAME_OFF') {
            shortStatus = '게임 미실행';
            if (alertIcon) alertIcon.innerText = '🎮';
            if (alertTitle) alertTitle.innerText = '마비노기 모바일 클라이언트가 실행되어 있지 않습니다';
            if (alertDesc) alertDesc.innerHTML = '마비노기 모바일 PC 클라이언트를 실행하고 캐릭터로 로그인하면 자동으로 감지되어 연결됩니다.';
            if (alertPathBox) alertPathBox.style.display = 'none';
          } else {
            shortStatus = '연결 끊김';
            if (alertIcon) alertIcon.innerText = '🔌';
            if (alertTitle) alertTitle.innerText = '게임 클라이언트와 연결이 끊어졌습니다';
            if (alertDesc) alertDesc.innerText = data.error || '게임 및 네트워크 상태를 확인해주세요.';
            if (alertPathBox) alertPathBox.style.display = 'none';
          }

          if (statusPill) {
            statusPill.className = "pill offline";
            statusPill.innerHTML = `● ${shortStatus}`;
            statusPill.title = (data.error || '클릭하여 연동 가이드 및 설정 열기');
          }
          if (cliAlertBanner) cliAlertBanner.style.display = 'block';
        }
        
        isCurrentlyBusy = !!data.is_busy;
        applyBusyUI(isCurrentlyBusy, data.current_task || '작업 진행 중...');

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
            statusPill.innerHTML = `● 작업 진행 중 (응답 대기)...`;
          } else {
            statusPill.className = "pill offline";
            statusPill.innerHTML = `● 연결 재시도 중...`;
          }
        }
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
          listEl.innerHTML = '<p style="color: #64748b; font-size: 13px;">현재 진행 중인 납품 목표 퀘스트가 없습니다.</p>';
          return;
        }

        let html = '';
        tasks.forEach(t => {
          const isDone = t.is_completed || t.needed === 0;
          html += `
            <div style="background: rgba(0,0,0,0.25); border: 1px solid rgba(255,255,255,0.06); border-radius: 12px; padding: 12px; margin-bottom: 8px;">
              <div style="display: flex; justify-content: space-between; align-items: center;">
                <span style="font-weight: 700; font-size: 13.5px; color: #e0e7ff;">${t.quest_title}</span>
                <span class="badge ${isDone ? '' : 'delivery'}">${isDone ? '완료' : '진행 중'}</span>
              </div>
              <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 6px; font-size: 12.5px;">
                <span style="color: #cbd5e1;">🎯 납품 목표: <strong>${t.item_name}</strong></span>
                <span>보유: <strong style="color: ${isDone ? '#34d399' : '#fbbf24'}">${t.current} / ${t.goal}</strong> (${t.needed}개 부족)</span>
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
      if (typeof applyBusyUI === 'function') applyBusyUI(true, `🔨 [${item}] ${count}개 제작 준비 중...`);
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

    async function pollLogs() {
      try {
        const res = await fetch('/api/logs');
        const data = await res.json();
        const logsEl = document.getElementById('console-logs');
        if (logsEl && data.logs && data.logs.length > 0) {
          logsEl.innerHTML = data.logs.map(l => {
            const cls = `log-${l.level || 'info'}`;
            return `<div class="log-line ${cls}"><span class="log-time">[${l.time}]</span> ${l.message}</div>`;
          }).join('');
          logsEl.scrollTop = logsEl.scrollHeight;
        }
      } catch (err) {}
    }

    async function doSearch() {
      const qEl = document.getElementById('search-query');
      if (!qEl) return;
      const q = qEl.value.trim();
      if (!q) return;
      const resEl = document.getElementById('search-results');
      if (!resEl) return;
      resEl.innerHTML = '<span style="color:#64748b;">검색 중...</span>';
      try {
        const res = await fetch(`/api/search_recipe?name=${encodeURIComponent(q)}`);
        const d = await res.json();
        let html = `<div style="padding: 6px; background: rgba(0,0,0,0.3); border-radius: 8px; margin-bottom: 8px;">`;
        html += `<strong>'${d.item_name}'</strong> - 가방: ${d.owned_bag}개 / 개인창고: ${d.owned_char_storage || 0}개 / 공용창고: ${d.owned_account_storage || 0}개 (총 ${d.owned_all || 0}개)<br>`;
        html += `제작: ${d.craft.length}건 | 가공: ${d.alter.length}건 | 채집: ${d.gather.length}건</div>`;
        resEl.innerHTML = html;
      } catch (e) {
        resEl.innerHTML = `<span style="color:#f43f5e;">검색 오류: ${e}</span>`;
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

    // Visibility change handler: immediately refresh when user switches back to this tab
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) {
        sendHeartbeat();
        updateStatus();
      }
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
          <div style="height: 100vh; display: flex; flex-direction: column; align-items: center; justify-content: center; background: #090d16; color: #f1f5f9; font-family: Pretendard, sans-serif;">
            <div style="font-size: 56px; margin-bottom: 20px;">🔌</div>
            <h2 style="font-size: 24px; color: #a5b4fc; margin-bottom: 12px; font-weight: 700;">웹 서버가 안전하게 종료되었습니다</h2>
            <p style="color: #94a3b8; font-size: 15px; margin-bottom: 24px;">이 브라우저 창이나 탭을 닫으셔도 됩니다.</p>
            <button onclick="window.close()" style="padding: 10px 24px; background: #2563eb; color: white; border: none; border-radius: 8px; font-weight: 700; cursor: pointer;">창 닫기</button>
          </div>
        `;
      } catch (e) {
        alert('종료 요청 실패: ' + e);
      }
    }

    // Safe Init View (each call isolated in try/catch)
    function initAll() {
      try { applyAlteringView(); } catch(e) { console.error('applyAlteringView error:', e); }
      try { applyQuickAlterView(); } catch(e) { console.error('applyQuickAlterView error:', e); }
      try { applyDeliveryTargetsView(); } catch(e) { console.error('applyDeliveryTargetsView error:', e); }
      try { applyMainModeView(); } catch(e) { console.error('applyMainModeView error:', e); }
      try { loadSettings(); } catch(e) { console.error('loadSettings error:', e); }
      try { loadPresets(); } catch(e) { console.error('loadPresets error:', e); }
      try { pollLogs(); } catch(e) { console.error('pollLogs error:', e); }

      // Status executes first immediately
      try { updateStatus(); } catch(e) { console.error('updateStatus error:', e); }

      // Stagger heavy queries to prevent simultaneous CLI subprocess flood on boot
      setTimeout(() => { try { loadDeliveryTargets(); } catch(e) {} }, 150);
      setTimeout(() => { try { loadCustomTargets(); } catch(e) {} }, 300);
      setTimeout(() => { try { loadAlteringQueue(); } catch(e) {} }, 500);
      setTimeout(() => { try { loadQuickAlterPlan(); } catch(e) {} }, 750);
      setTimeout(() => { try { loadBatchPlan(); } catch(e) {} }, 1000);
      setTimeout(() => { try { loadCustomPlan(); } catch(e) {} }, 1300);

      setInterval(() => { try { updateStatus(); } catch(e) {} }, 3000);
      setInterval(() => { if (document.hidden || isCurrentlyBusy) return; try { loadDeliveryTargets(); } catch(e) {} }, 8000);
      setInterval(() => { if (document.hidden || isCurrentlyBusy) return; try { loadCustomTargets(); } catch(e) {} }, 8000);
      setInterval(() => {
        if (document.hidden || isCurrentlyBusy) return;
        try {
          if (currentMainMode === 'delivery') loadBatchPlan();
          else if (currentMainMode === 'custom_craft') loadCustomPlan();
          else if (currentMainMode === 'quick_alter') loadQuickAlterPlan();
        } catch(e) {}
      }, 8000);
      setInterval(() => { if (document.hidden || isCurrentlyBusy) return; try { loadAlteringQueue(); } catch(e) {} }, 6000);
      setInterval(() => { if (document.hidden) return; try { pollLogs(); } catch(e) {} }, 1500);
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
    except Exception as e:
        import traceback
        base_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
        crash_path = os.path.join(base_dir, "crash.log")
        with open(crash_path, "w", encoding="utf-8") as f:
            traceback.print_exc(file=f)

