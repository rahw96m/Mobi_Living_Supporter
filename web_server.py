import os
import sys
import json
import threading
import time
import copy
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
from mabi_cli import MabinogiCLI, MabinogiCLIError, get_saved_cli_path, find_cli_path, get_user_data_dir, get_user_data_path
from delivery_manager import (
    DeliveryManager, DeliveryTask, alarm_store, delivery_target_store,
    delivery_preset_store, custom_target_store, settings_store, get_facility_for_material,
    send_discord_test_message, notify_discord_goal_completion
)

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
    global is_shutting_down, server_instance
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


cached_status_data = None
cached_status_time = 0
cached_character_info = None
cached_character_time = 0
cached_altering_works = None
cached_delivery_targets = None
cached_custom_targets = None
cached_quests = None
status_lock = threading.Lock()

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
        finally:
            self.close_connection = True

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

        if path == "/api/network_info":
            try:
                ip = get_local_ip()
                port = active_server_port
                mobile_url = f"http://{ip}:{port}"
                self._send_json({
                    "local_ip": ip,
                    "port": port,
                    "mobile_url": mobile_url
                })
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
                global cached_status_time, cached_character_time, cached_status_data
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
                    add_log("success", f"[디스코드] 테스트 웹훅 메시지가 성공적으로 전송되었습니다.")
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
                add_log("info", f"[보유 수량 초기화] 등록된 {reset_count}개 납품 품목의 보유 수량이 모두 0개로 초기화되었습니다.")
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
                    add_log("info", f"[납품 목표 삭제] '{item_name}' 항목이 목표 목록에서 제거되었습니다.")
                self._send_json({"status": "success"})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/clear_delivery_targets":
            try:
                delivery_target_store.clear()
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
                    add_log("info", f"[개별 제작 삭제] '{item_name}' 항목이 목표 목록에서 제거되었습니다.")
                self._send_json({"status": "success"})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/clear_custom_targets":
            try:
                custom_target_store.clear()
                add_log("info", "[개별 제작 초기화] 모든 등록된 개별 제작 목표가 삭제되었습니다.")
                self._send_json({"status": "success"})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/reset_custom_targets_current":
            try:
                reset_count = custom_target_store.reset_all_current()
                add_log("info", f"[개별 제작 수량 초기화] 등록된 {reset_count}개 목표의 보유 수량이 모두 0개로 초기화되었습니다.")
                self._send_json({"status": "success", "reset_count": reset_count})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/sync_custom_targets_inventory":
            try:
                sync_count = custom_target_store.clear_all_overrides()
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
                add_log("success", f"[프리셋 불러오기] '{preset_name}' ({loaded_count}개 항목) 프리셋이 등록되었습니다.{mode_str}")
                self._send_json({"status": "success", "loaded_count": loaded_count, "reset_current": reset_current})
            except Exception as e:
                self._send_error(e)
            return

        if path == "/api/collect_all_completed_altering":
            if is_busy:
                self._send_error("이미 다른 작업이 진행 중입니다.", 409)
                return

            is_busy = True
            current_task_info = "완료 가공품 순차 수령 중..."

            def collect_worker():
                global is_busy, current_task_info
                try:
                    collected = manager_instance.collect_completed_altering_works(callback=add_log)
                    if not collected:
                        add_log("info", "수령할 완료된 가공품이 없습니다.")
                except Exception as err:
                    add_log("error", f"가공품 수령 실패: {str(err)}")
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
  <title>모비노기 생활 지원도구</title>
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
      display: inline-block;
      width: 3ch;
      text-align: center;
      font-family: var(--font-mono);
      font-weight: 700;
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
      display: inline-flex;
      align-items: center;
      gap: 4px;
      margin-left: auto;
      flex-shrink: 0;
      flex-wrap: wrap;
    }
    .header-action-btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 3px 4px;
      font-family: var(--font-sans);
      font-size: var(--fs-md);
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
      content: "[ ";
      font-family: var(--font-mono);
      opacity: 0;
      color: var(--text-bright);
    }
    .header-action-btn::after {
      content: " ]";
      font-family: var(--font-mono);
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
      content: "[ ";
      font-family: var(--font-mono);
      opacity: 0;
      color: var(--text-bright);
      transition: opacity 0.1s ease;
    }
    .btn::after {
      content: " ]";
      font-family: var(--font-mono);
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
      min-height: 110px;
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
      text-align: right;
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
      max-width: 280px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
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
  </style>
</head>
<body>
  <div class="container">
    <header>
      <!-- 1층: 좌측(타이틀) / 우측(보조 메뉴) -->
      <div class="header-top">
        <div class="logo-area">
          <div class="title">
            <h1>모비노기 생활 지원도구 <span class="badge">v1.2.0</span></h1>
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
        <div class="pill wings" id="pill-wings">날개 -</div>
        <span class="status-delim">|</span>
        <div class="pill weight" id="pill-weight">무게 -/-</div>
        <span class="status-delim">|</span>
        <div class="pill sound" id="pill-sound" onclick="toggleSoundAlarmSetting()" style="cursor: pointer;" title="클릭하여 작업 완료 알림음 ON/OFF 전환">알람 소리: 꺼짐</div>
        <span class="status-delim">|</span>
        <div class="pill discord" id="pill-discord" onclick="openDiscordModal()" style="cursor: pointer;" title="클릭하여 디스코드 웹훅 알림 설정 열기">디스코드: 꺼짐</div>
      </div>
    </header>

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
            <button class="mcb-pill-btn" id="mcb-btn-discord" onclick="handleMcbDiscordClick()" title="목표 완수 모드 완료 시 디스코드 웹훅 알림 (클릭하여 ON/OFF 전환 또는 설정)">
              <span class="cli-check-box" id="mcb-discord-icon">□</span>
              <span id="mcb-discord-text">디스코드 알림</span>
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
            <span class="cli-spinner">[/]</span>
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
            <button class="btn btn-sm" onclick="addCurrentQuestTarget()" title="게임 내 추적 중인 납품 퀘스트를 읽어 목록에 추가합니다">현재 퀘스트 등록</button>
            <button class="btn btn-sm" onclick="resetAllTargetsCurrent()" title="새 주간 퀘스트를 시작할 때 모든 등록 목표의 현재 보유 수량을 0개로 일괄 초기화합니다">수량 0개로 초기화</button>
            <button class="btn btn-sm" onclick="clearDeliveryTargets()">전체 비우기</button>
            <button class="btn btn-sm" onclick="loadDeliveryTargets()">새로고침</button>
            <button class="btn btn-sm" id="btn-toggle-delivery-targets" onclick="toggleDeliveryTargetsView()">접어두기 ^</button>
          </div>
        </div>

        <!-- Collapsed Compact Bar (shown when collapsed) -->
        <div id="delivery-targets-collapsed-bar" style="display: none; margin-top: 10px; padding: 8px 0; border-top: 1px solid var(--line-subtle); font-size: var(--fs-md); color: var(--text-muted); cursor: pointer;" onclick="toggleDeliveryTargetsView()">
          <span style="flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;"><span class="sym-prefix">&gt;</span><span id="delivery-targets-quick-summary">등록된 주간 납품 목표 요약 정보...</span></span>
        </div>

        <!-- Expanded Content (shown by default) -->
        <div id="delivery-targets-expanded-content" style="margin-top: 14px;">
          <!-- Quick Manual Add Input Form -->
          <div style="display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; margin-bottom: 14px; padding-bottom: 14px; border-bottom: 1px solid var(--line);">
            <div style="flex: 2; min-width: 170px;">
              <label style="font-size: var(--fs-sm); color: var(--text-muted); font-weight: 600; margin-bottom: 4px; display: block;">납품 아이템명 (직접 입력)</label>
              <input type="text" id="target-input-item" class="input-text" placeholder="예: 론 엣지소드S, 비늘 갑옷 장갑S" style="width: 100%;" onkeydown="if(event.key==='Enter') addManualTarget()">
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
              <button class="btn" onclick="addManualTarget()">+ 직접 추가</button>
            </div>
            <div style="width: 100%; font-size: var(--fs-sm); color: var(--text-muted); margin-top: 4px;">
              <span><span class="sym-prefix">&gt;</span>인게임에서 주간 퀘스트를 추적하며 [현재 퀘스트 등록]을 누르거나, 아이템명과 수량을 직접 입력해 목록에 등록하세요.</span>
            </div>
          </div>

          <!-- Preset Save/Load Section -->
          <div style="margin-bottom: 14px; padding-bottom: 14px; border-bottom: 1px solid var(--line);">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; flex-wrap: wrap; gap: 8px;">
              <div style="display: flex; align-items: center; gap: 8px;">
                <span style="font-weight: 700; font-size: var(--fs-md); color: var(--text-bright);"><span class="sym-prefix">&gt;</span>주간 납품 프리셋 (퀘스트 조합 저장)</span>
                <span id="preset-count-badge" class="badge-dim">0개 저장됨</span>
              </div>
              <div style="display: flex; gap: 6px; align-items: center;">
                <input type="text" id="preset-save-name" class="input-text" placeholder="프리셋 이름 (예: 대장간 세트)" style="width: 180px; padding: 4px 8px; font-size: var(--fs-sm);">
                <button class="btn btn-sm" onclick="saveCurrentAsPreset()">현재 목록 저장</button>
              </div>
            </div>
            <div id="preset-list-container" style="display: flex; flex-wrap: wrap; gap: 8px; min-height: 24px;">
              <span style="color: var(--text-muted); font-size: var(--fs-sm);"><span class="sym-prefix">&gt;</span>저장된 프리셋이 없습니다. 납품 목표를 등록한 뒤 이름을 지정해 저장하세요.</span>
            </div>
          </div>

          <!-- Registered Delivery Targets Grid / List -->
          <div id="delivery-targets-container" style="display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px;">
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
            <div id="batch-tasks-container" style="display: none;"></div>
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
              <input type="text" id="custom-input-item" class="input-text" placeholder="아이템명 (예: 론 엣지소드S, 은 펜던트, 실크 로브 등)" style="flex: 2; min-width: 200px;" onkeydown="if(event.key==='Enter') addManualCustomTarget()">
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
          </div>
          <div class="bom-table-container">
            <table class="bom-table">
              <thead>
                <tr>
                  <th style="min-width: 130px;">재료명</th>
                  <th style="min-width: 70px; text-align: center;">구분/시설</th>
                  <th style="min-width: 75px; text-align: right;">총 소요</th>
                  <th style="min-width: 105px; text-align: center;">가방(창고)</th>
                  <th style="min-width: 80px; text-align: right;">부족 수량</th>
                  <th style="min-width: 90px; text-align: center;">가공 필요</th>
                  <th style="min-width: 130px;">가공 상태</th>
                </tr>
              </thead>
              <tbody id="custom-batch-materials-tbody">
                <tr>
                  <td colspan="7" style="text-align: center; color: var(--text-muted); padding: 20px;">개별 제작 목표를 등록하면 전체 소요 자재가 분석됩니다.</td>
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

    <!-- Bottom 2-Column Grid: Live Console & Recipe Search -->
    <div class="grid">
      <!-- Left Column: Live Console Section -->
      <div>
        <div class="card">
          <div class="card-title">
            <span><span class="sym-prefix">//</span>실시간 실행 콘솔</span>
            <div id="busy-box" style="display: none;" class="busy-indicator">
              <span class="cli-spinner">[/]</span>
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
            <span><span class="sym-prefix">//</span>레시피 &amp; 재료 즉시 조회</span>
          </div>
          <div class="form-row">
            <input type="text" id="search-query" class="input-text" style="flex: 1;" placeholder="아이템명 또는 재료명 입력 (예: 목재, 가죽, 론)" onkeydown="if(event.key==='Enter') doSearch()">
            <button class="btn btn-sm" onclick="doSearch()">검색</button>
          </div>
          <div id="search-results" style="margin-top: 12px; max-height: 180px; overflow-y: auto; font-size: var(--fs-md);"></div>
        </div>
      </div>
    </div>
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
          <input type="text" id="recipe-modal-item-name" class="input-text" style="flex: 1;" placeholder="예: 삶은 달걀">
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
          <div style="font-size: var(--fs-sm); color: var(--text-muted); margin-top: 6px;">
            * 디스코드 채널 설정 &gt; 연동 &gt; 웹훅 만들기 &gt; [웹훅 URL 복사]를 붙여넣으세요.
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
    const CLI_SPINNER_FRAMES = ['[/]', '[-]', '[\\]', '[|]'];
    let cliSpinnerFrameIdx = 0;

    function getCurrentSpinnerText() {
      return CLI_SPINNER_FRAMES[cliSpinnerFrameIdx % CLI_SPINNER_FRAMES.length];
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
      const isOver = ratio >= 1.0;
      const glowStyle = isOver ? 'text-shadow: 0 0 6px rgba(239, 68, 68, 0.7); font-weight: 700;' : '';
      const percentStr = (ratio * 100).toFixed(1);
      const remain = Math.max(0, m - c);

      return `<span class="cli-gauge" style="color: ${color}; ${glowStyle}" title="무게 ${percentStr}% (남은 여유: ${remain})">${gaugeInner}</span>`;
    }

    setInterval(function() {
      cliSpinnerFrameIdx = (cliSpinnerFrameIdx + 1) % CLI_SPINNER_FRAMES.length;
      const frame = CLI_SPINNER_FRAMES[cliSpinnerFrameIdx];
      const spinners = document.querySelectorAll('.cli-spinner');
      for (let i = 0; i < spinners.length; i++) {
        spinners[i].textContent = frame;
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
    let currentIsWeightOver = false;
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
              briefing.innerHTML = parts.join(' | ') || '주간 납품 계획 준비 완료';
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
              briefing.innerHTML = parts.join(' | ') || '지정 제작 계획 준비 완료';
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
        briefing.innerHTML += ` <span style="color: #EF4444; font-weight: 700; text-shadow: 0 0 6px rgba(239, 68, 68, 0.7); margin-left: 6px;">[!] 가방 무게 초과 (정리 필요)</span>`;
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
      if (!currentDiscordWebhookUrl) {
        openDiscordModal();
        return;
      }
      toggleDiscordAlarmSetting();
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

    function openDiscordModal() {
      const overlay = document.getElementById('discord-modal-overlay');
      const urlInput = document.getElementById('discord-webhook-url-input');
      const enabledCheck = document.getElementById('discord-alarm-enabled-input');
      const checkSym = document.getElementById('discord-enabled-check-sym');
      const badge = document.getElementById('discord-modal-status-badge');
      const testResult = document.getElementById('discord-test-result');

      if (urlInput) urlInput.value = currentDiscordWebhookUrl;
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
      if (overlay) overlay.style.display = 'flex';
    }

    function closeDiscordModal() {
      const overlay = document.getElementById('discord-modal-overlay');
      if (overlay) overlay.style.display = 'none';
    }

    function handleDiscordModalOverlayClick(e) {
      if (e.target && e.target.id === 'discord-modal-overlay') {
        closeDiscordModal();
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
      if (currentIsWeightOver) {
        if (!confirm('[!] 가방 무게 초과 (정리 필요)\n\n현재 가방 무게가 최대치를 초과한 상태입니다.\n이대로 진행하면 채집/가공 완료품을 획득하지 못할 수 있습니다.\n\n정리하지 않고 그래도 계속 실행하시겠습니까?')) {
          return;
        }
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
        const hasCompleted = completedAtFac.length > 0;
        const firstCompletedName = hasCompleted ? completedAtFac[0].DisplayName : '';

        const collectBtn = hasCompleted
          ? `<button class="btn-facility-collect" onclick="collectAltering('${firstCompletedName}')">완료 가공품 수령 (<span class="mono-num">${completedAtFac.length}</span>)</button>`
          : `<button class="btn-facility-idle" disabled>수령 대기</button>`;

        let itemsHtml = '';
        if (itemsAtFac.length === 0) {
          itemsHtml = '<div style="color: var(--text-dim); font-size: var(--fs-sm); padding: 8px 0; text-align: center;">작업 없음</div>';
        } else {
          itemsAtFac.forEach(w => {
            const remSec = getWorkRemainingSeconds(w);
            let timeStr = '';
            if (w.IsCompleted || remSec === 0) {
              timeStr = '<span class="badge mono-num">완료</span>';
            } else if (w.State === 'InProgress' || remSec > 0) {
              timeStr = `<span class="mono-num" style="color: var(--text-main); font-weight: 700;">${formatRemaining(remSec)}</span>`;
            } else {
              timeStr = `<span class="mono-num" style="color: var(--text-muted); font-size: var(--fs-sm);">대기 (${formatRemaining(remSec)})</span>`;
            }

            const bellBtn = (!w.IsCompleted && remSec > 0)
              ? `<button class="btn-manual-bell" title="이 작업에 알람 추가" onclick="addManualAlarm('${fac.name}', '${w.DisplayName}', ${remSec})">+알람</button>`
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
              <span class="facility-title">${formatFacilityShort(fac.name)}</span>
              ${collectBtn}
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
          showPushNotification(`[${al.facility}] 마지막 가공 완료`, `[${al.facility}] 모든 가공 작업이 완료되었습니다. (${al.item}) 지금 바로 수령하세요.`);
          document.title = `[${al.facility} 가공 완료] 모비노기 생활 지원도구`;
          setTimeout(() => { document.title = '모비노기 생활 지원도구'; }, 8000);
        }
      });
      if (changed) renderAlarms();
      if (window.cachedAlteringWorks && window.cachedAlteringWorks.length > 0) {
        renderAlteringQueue();
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
          if (gatherSum) gatherSum.innerHTML = `> <span>사전 부족분 맞춤 채집: ${rawText} <span style="color: var(--text-muted); font-size: var(--fs-sm);">(부족한 수량만 채집 후 즉시 자동 중단)</span></span>`;
        } else {
          if (gatherSum) gatherSum.innerHTML = `> <span style="color: var(--text-main);">사전 채집이 필요한 원자재 없음 (보유량 충분)</span>`;
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
          quickSummaryEl.innerHTML = `> <strong>7슬롯 가공 빠른 실행:</strong> 가공 예정 <span class="mono-num" style="color: var(--text-main); font-weight:700;">${totalPlanned}건</span> :: 완료 수령 대기 <span class="mono-num" style="color: var(--text-main); font-weight:700;">${totalCompleted}건</span> :: 상태: ${canStartTag}`;
        }
        updateMasterCommandBar();
      } catch (e) {
        console.error('loadQuickAlterPlan error:', e);
        if (container) container.innerHTML = `<p style="color: var(--text-main); font-size: var(--fs-md);">[!] 계획 불러오기 실패: ${e}</p>`;
      }
    }

    async function executeQuickAlter() {
      if (currentIsWeightOver) {
        if (!confirm('[!] 가방 무게 초과 (정리 필요)\n\n현재 가방 무게가 최대치를 초과한 상태입니다.\n이대로 진행하면 채집/가공 완료품을 획득하지 못할 수 있습니다.\n\n정리하지 않고 그래도 계속 실행하시겠습니까?')) {
          return;
        }
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
    async function loadDeliveryTargets() {
      if (isDeliveryTargetsLoading) return;
      isDeliveryTargetsLoading = true;
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
            quickSummaryEl.innerHTML = '<span style="color: var(--text-muted);"><span class="sym-prefix">&gt;</span>등록된 주간 납품 목표가 없습니다. 클릭하여 펼친 뒤 등록하세요.</span>';
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
        targets.forEach(t => {
          const isDone = t.is_completed || (t.current >= t.goal);
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
          }

          html += `
            <div class="target-card ${isDone ? 'target-done' : ''}">
              <div style="display: flex; justify-content: space-between; align-items: flex-start;">
                <div>
                  <div style="font-weight: 700; font-size: var(--fs-md); color: var(--text-title);">${t.item_name}</div>
                  <div style="font-size: var(--fs-sm); color: var(--text-muted);"><span class="sym-prefix">&gt;</span>${t.quest_title || '주간 납품'}</div>
                </div>
                <div style="display: flex; align-items: center; gap: 6px;">
                  <span class="${isDone ? 'badge' : 'badge-dim'}">${isDone ? '완료' : '진행 중'}</span>
                  <button class="btn btn-sm" onclick="deleteDeliveryTarget('${safeName}')" title="삭제">삭제</button>
                </div>
              </div>

              ${recHtml}

              <div style="display: flex; justify-content: space-between; align-items: center; font-size: var(--fs-sm); flex-wrap: wrap; gap: 6px;">
                <div style="display: flex; align-items: center; gap: 4px; flex-wrap: wrap;">
                  <span class="cli-gauge">${gaugeStr}</span>
                  <span style="color: var(--text-muted);">보유:</span>
                  <button class="btn btn-sm" onclick="adjustTargetCurrent('${safeName}', ${t.current}, -1)" title="1개 감소">-</button>
                  <strong class="mono-num" style="color: var(--text-main); cursor: pointer; text-decoration: underline dotted;" onclick="promptEditCurrent('${safeName}', ${t.current})" title="수량 클릭하여 직접 입력">${t.current}</strong>
                  <button class="btn btn-sm" onclick="adjustTargetCurrent('${safeName}', ${t.current}, 1)" title="1개 증가">+</button>
                  <span class="mono-num" style="color: var(--text-muted);">/ ${t.goal}개</span>
                  <button class="btn btn-sm" onclick="promptEditCurrent('${safeName}', ${t.current})" title="보유 수량 직접 수정">수정</button>
                  <button class="btn btn-sm" onclick="updateTargetCurrent('${safeName}', 0)" title="현재 수량 0개로 리셋">0개로</button>
                  ${t.storage_count > 0 ? `<small class="mono-num" style="color: var(--text-muted); font-size: var(--fs-sm);">(가방 ${t.inventory_count || 0} + 창고 ${t.storage_count})</small>` : ''}
                </div>
                <span class="mono-num" style="color: ${isDone ? 'var(--text-dim)' : 'var(--text-bright)'}; font-weight: 700;">${isDone ? '=== 납품 준비 완료 ===' : `--- ${t.needed}개 부족 ---`}</span>
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
      if (!confirm('등록된 모든 주간 납품 목표의 현재 보유 수량을 0개로 초기화할까요?\n\n(새로운 주간 퀘스트를 시작하거나 목표 품목들을 처음부터 제작하고자 할 때 유용합니다)')) return;
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

              rowsHtml += `
                <tr class="row-alter ${isSatisfied ? 'row-satisfied' : ''}">
                  <td style="font-weight:700; color:var(--text-main); white-space:nowrap;"><span class="bom-dot dot-alter"></span>${r.item_name}</td>
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
      if (currentIsWeightOver) {
        if (!confirm('[!] 가방 무게 초과 (정리 필요)\n\n현재 가방 무게가 최대치를 초과한 상태입니다.\n이대로 진행하면 채집/가공 완료품을 획득하지 못할 수 있습니다.\n\n정리하지 않고 그래도 계속 실행하시겠습니까?')) {
          return;
        }
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
                  <strong class="mono-num" style="color: var(--text-main); cursor: pointer; text-decoration: underline dotted;" onclick="promptEditCustomCurrent('${safeName}', ${t.current})" title="수량 클릭하여 직접 입력">${t.current}</strong>
                  <button class="btn btn-sm" onclick="adjustCustomTargetCurrent('${safeName}', ${t.current}, 1)" title="1개 증가">+</button>
                  <button class="btn btn-sm" onclick="promptEditCustomCurrent('${safeName}', ${t.current})" title="보유 수량 직접 수정">수정</button>
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
        <input type="text" class="recipe-row-name" value="${(name || '').replace(/"/g, '&quot;')}" placeholder="재료명 (예: 달걀, 물이 든 병)" style="flex: 2; padding: 6px 10px;">
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
          tbody.innerHTML = `<tr><td colspan="7" style="text-align: center; color: var(--text-main); padding: 20px;">[!] 오류: ${plan.error}</td></tr>`;
          if (statusText) statusText.innerText = '계획 로드 실패';
          return;
        }

        const materials = plan.intermediate_requirements || plan.materials || [];
        const rawMaterials = plan.raw_materials || [];

        if (materials.length === 0 && rawMaterials.length === 0) {
          tbody.innerHTML = `<tr><td colspan="7" style="text-align: center; color: var(--text-muted); padding: 24px;"><span class="sym-prefix">&gt;</span>등록된 지정 제작 목표가 없습니다. 위에서 목표를 추가하세요.</td></tr>`;
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
            statusBadge = '<span style="font-weight:700;">=== 재료 준비 완료 ===</span>' + compBadge;
          } else if (m.status === 'ready_to_collect' || (compCnt > 0 && worksNeeded === 0)) {
            statusBadge = `<span class="badge mono-num">수령 가능 (${compCnt}회)</span>`;
          } else if (m.status === 'in_queue' || (queuedCnt > 0 && worksNeeded === 0)) {
            statusBadge = `<span class="mono-num" style="color:var(--text-main); font-weight:700;">대기열 (${queuedCnt}회)</span>` + compBadge;
          } else if (worksNeeded > 0) {
            statusBadge = `<span class="mono-num" style="color:var(--text-main); font-weight:700;">> ${shortFac} ${worksNeeded}회 등록 필요</span>` + queuedBadge + compBadge;
          } else {
            statusBadge = '<span style="font-weight:700;">=== 준비 완료 ===</span>';
          }

          const invCnt = m.inventory_count || 0;
          const storeCnt = m.storage_count || 0;
          const bagStorageText = `<span class="mono-num">${invCnt}개</span><span class="mono-num" style="color:var(--text-muted); font-size:11px;">(${storeCnt}개)</span>`;

          rowsHtml += `
            <tr class="row-alter ${isSatisfied ? 'row-satisfied' : ''}">
              <td style="font-weight:700; color:var(--text-main); white-space:nowrap;"><span class="bom-dot dot-alter"></span>${m.item_name}</td>
              <td style="text-align:center; white-space:nowrap;"><span class="badge-dim">${shortFac}</span></td>
              <td class="mono-num" style="font-weight:700; color:var(--text-main); text-align:right; white-space:nowrap;">${m.total_needed}개</td>
              <td style="text-align:center; white-space:nowrap;">${bagStorageText}</td>
              <td class="mono-num" style="font-weight:700; color:${deficit > 0 ? 'var(--text-main)' : 'var(--text-muted)'}; text-align:right; white-space:nowrap;">
                ${deficit > 0 ? `-${deficit}개` : '0개'}
              </td>
              <td class="mono-num" style="text-align:center; font-weight:700; color:${worksNeeded > 0 ? 'var(--text-main)' : 'var(--text-dim)'}; white-space:nowrap;">
                ${worksNeeded > 0 ? `+${worksNeeded}회` : '-'}
              </td>
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
                <td class="mono-num" style="font-weight:700; color:${rm.deficit > 0 ? 'var(--text-main)' : 'var(--text-muted)'}; text-align:right; white-space:nowrap;">
                  ${rm.deficit > 0 ? `-${rm.deficit}개` : '0개'}
                </td>
                <td style="text-align:center; color:var(--text-dim); white-space:nowrap;">-</td>
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
      if (currentIsWeightOver) {
        if (!confirm('[!] 가방 무게 초과 (정리 필요)\n\n현재 가방 무게가 최대치를 초과한 상태입니다.\n이대로 진행하면 채집/가공 완료품을 획득하지 못할 수 있습니다.\n\n정리하지 않고 그래도 계속 실행하시겠습니까?')) {
          return;
        }
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

      const mcbIdle = document.getElementById('mcb-box-idle');
      const mcbRunning = document.getElementById('mcb-box-running');
      const mcbRunningTitle = document.getElementById('mcb-running-title');
      const mcbRunningDesc = document.getElementById('mcb-running-desc');
      const mcbBtnStart = document.getElementById('mcb-btn-start');

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
          const realmName = char.RealmName || '서버';
          const levelNum = char.Level || 0;
          if (statusPill) {
            if (currentlyBusy) {
              statusPill.className = "pill warning";
              statusPill.innerHTML = `<span class="cli-spinner">${getCurrentSpinnerText()}</span> ${realmName} (Lv.<span class="mono-num">${levelNum}</span>)`;
              statusPill.title = data.current_task || '작업 진행 중';
            } else {
              statusPill.className = "pill online";
              statusPill.innerHTML = `${realmName} (Lv.<span class="mono-num">${levelNum}</span>)`;
              statusPill.title = "정상 연결됨 (클릭하여 설정 확인)";
            }
          }
          if (cliAlertBanner) cliAlertBanner.style.display = 'none';
          if (wingsPill) {
            wingsPill.innerHTML = `날개 <span class="mono-num">${(data.wings || 0).toLocaleString()}</span>`;
          }
          const inv = data.inventory || {};
          const curWeight = inv.CurrentInventoryWeightAsDecimal || 0;
          const maxWeight = inv.MaxInventoryWeightAsDecimal || 0;
          const wasWeightOver = currentIsWeightOver;
          currentIsWeightOver = (maxWeight > 0 && curWeight > maxWeight);
          if (weightPill) {
            const ratio = maxWeight > 0 ? (curWeight / maxWeight) : 0;
            const gaugeHtml = maxWeight > 0 ? makeWeightGaugeHtml(curWeight, maxWeight, 5) : '';
            const weightColor = maxWeight > 0 ? getWeightColorByRatio(ratio) : 'var(--text)';
            const isOver = currentIsWeightOver;
            const glowStyle = isOver ? 'text-shadow: 0 0 6px rgba(239, 68, 68, 0.7); font-weight: 700;' : '';
            const overText = isOver ? ` <span style="color: #EF4444; font-weight: 700; text-shadow: 0 0 6px rgba(239, 68, 68, 0.7); margin-left: 4px;">[!] 가방 무게 초과 (정리 필요)</span>` : '';
            weightPill.innerHTML = `무게 <span class="mono-num" style="color: ${weightColor}; ${glowStyle}">${curWeight}/${maxWeight}</span> ${gaugeHtml}${overText}`;
            weightPill.title = `가방 무게: ${curWeight}/${maxWeight} (${(ratio * 100).toFixed(1)}%${isOver ? ' - [!] 가방 무게 초과 (정리 필요)' : ''})`;
          }
          if (wasWeightOver !== currentIsWeightOver) {
            updateMasterCommandBar();
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
      resEl.innerHTML = '<span style="color:var(--text-muted);"><span class="sym-prefix">&gt;</span>검색 중...</span>';
      try {
        const res = await fetch(`/api/search_recipe?name=${encodeURIComponent(q)}`);
        const d = await res.json();
        let html = `<div style="padding: 6px; border: 1px solid var(--border-line); margin-bottom: 8px;">`;
        html += `<strong>'${d.item_name}'</strong> - <span class="mono-num">가방: ${d.owned_bag}개 | 개인창고: ${d.owned_char_storage || 0}개 | 공용창고: ${d.owned_account_storage || 0}개 (총 ${d.owned_all || 0}개)</span><br>`;
        html += `<span class="mono-num"><span class="sym-prefix">&gt;</span>제작: ${d.craft.length}건 | 가공: ${d.alter.length}건 | 채집: ${d.gather.length}건</span></div>`;
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

    // Safe Init View (each call isolated in try/catch)
    function initAll() {
      try { updateStickyHeaderOffset(); } catch(e) {}
      try { applyAlteringView(); } catch(e) { console.error('applyAlteringView error:', e); }
      try { applyQuickAlterView(); } catch(e) { console.error('applyQuickAlterView error:', e); }
      try { applyDeliveryTargetsView(); } catch(e) { console.error('applyDeliveryTargetsView error:', e); }
      try { applyMainModeView(); } catch(e) { console.error('applyMainModeView error:', e); }
      try { loadSettings(); } catch(e) { console.error('loadSettings error:', e); }
      try { updateMasterCommandBar(); } catch(e) { console.error('updateMasterCommandBar error:', e); }
      try { loadPresets(); } catch(e) { console.error('loadPresets error:', e); }
      try { fetchUserDataInfo(); } catch(e) { console.error('fetchUserDataInfo error:', e); }
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
    except Exception as e:
        import traceback
        base_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
        crash_path = os.path.join(base_dir, "crash.log")
        with open(crash_path, "w", encoding="utf-8") as f:
            traceback.print_exc(file=f)

