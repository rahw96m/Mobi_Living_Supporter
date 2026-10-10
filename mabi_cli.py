import os
import sys
import json
import base64
import shutil
import ctypes
import subprocess
import threading
import time
import copy
from typing import Any, Dict, List, Optional, Tuple

_CLI_LOCK = threading.RLock()

READ_CACHE_TTL = {
    "get_inventory": 4.0,
    "get_altering_works": 4.0,
    "get_character_storage": 10.0,
    "get_account_storage": 10.0,
    "get_activity": 3.0,
    "get_wings_count": 5.0,
    "get_currencies": 10.0,
    "get_items": 4.0,
    "get_quests": 5.0,
    "get_my_info": 30.0,
    "get_craftable_items": 15.0,
    "get_alterable_items": 15.0,
    "get_gatherable_items": 15.0,
}

ACTION_COMMANDS = {
    "execute_altering",
    "execute_gathering",
    "execute_crafting",
    "complete_altering_work",
    "stop_action"
}

CLI_DEFAULT_PATH = r"C:\Nexon\MabinogiMobile\MabinogiMobile_CLI.exe"
LAST_RESPONSE_PATH = os.path.expandvars(r"%LOCALAPPDATA%\MabinogiMobileCLI\last-response.json")

if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(APP_DIR, "mabi_config.json")
LEGACY_CONFIG_FILE = os.path.join(APP_DIR, "mabi_helper_config.json")


def _normalize_cli_path(p: Optional[str]) -> Optional[str]:
    """Given a candidate file or directory path, resolves and returns the absolute MabinogiMobile_CLI.exe path if valid."""
    if not p:
        return None
    try:
        p = os.path.abspath(os.path.expandvars(p.strip().strip('"').strip("'")))
        if os.path.isdir(p):
            cand = os.path.join(p, "MabinogiMobile_CLI.exe")
            if os.path.isfile(cand):
                return cand
        elif os.path.isfile(p):
            base = os.path.basename(p).lower()
            if base == "mabinogimobile_cli.exe":
                return p
            if base == "mabinogimobile.exe":
                cand = os.path.join(os.path.dirname(p), "MabinogiMobile_CLI.exe")
                if os.path.isfile(cand):
                    return cand
    except Exception:
        pass
    return None


def get_saved_cli_path() -> Optional[str]:
    """Reads configured cli_path from mabi_config.json."""
    for cfg_path in (CONFIG_FILE, LEGACY_CONFIG_FILE):
        if os.path.isfile(cfg_path):
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    cand = _normalize_cli_path(data.get("cli_path"))
                    if cand:
                        return cand
            except Exception:
                pass
    return None


def save_configured_cli_path(path: str) -> bool:
    """Saves the given path to mabi_config.json."""
    cand = _normalize_cli_path(path)
    if not cand:
        return False
    try:
        cfg = {}
        if os.path.isfile(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception:
                cfg = {}
        elif os.path.isfile(LEGACY_CONFIG_FILE):
            try:
                with open(LEGACY_CONFIG_FILE, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception:
                cfg = {}
        cfg["cli_path"] = cand
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def _get_available_drives() -> List[str]:
    drives = []
    if sys.platform == "win32":
        try:
            bitmask = ctypes.windll.kernel32.GetLogicalDrives()
            for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                if bitmask & 1:
                    drives.append(letter)
                bitmask >>= 1
        except Exception:
            drives = ["C", "D", "E", "F", "G"]
    else:
        drives = ["C"]
    return drives


def _find_from_running_processes() -> Optional[str]:
    """Checks running processes for MabinogiMobile.exe or MabinogiMobile_CLI.exe and extracts directory."""
    if sys.platform != "win32":
        return None
    try:
        kernel32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi
        arr = (ctypes.c_ulong * 1024)()
        cb_needed = ctypes.c_ulong()
        if psapi.EnumProcesses(ctypes.byref(arr), ctypes.sizeof(arr), ctypes.byref(cb_needed)):
            count = cb_needed.value // ctypes.sizeof(ctypes.c_ulong)
            for i in range(count):
                pid = arr[i]
                h_proc = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
                if h_proc:
                    buf = ctypes.create_unicode_buffer(1024)
                    size = ctypes.c_ulong(1024)
                    if kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size)):
                        exe_path = buf.value
                        lower = os.path.basename(exe_path).lower()
                        if lower in ("mabinogimobile.exe", "mabinogimobile_cli.exe", "mabinogimobile_controller.exe"):
                            cand = _normalize_cli_path(os.path.dirname(exe_path))
                            if cand:
                                kernel32.CloseHandle(h_proc)
                                return cand
                    kernel32.CloseHandle(h_proc)
    except Exception:
        pass
    return None


def _is_game_running() -> bool:
    """Fast check whether MabinogiMobile.exe is currently active in the process list."""
    if sys.platform != "win32":
        return False
    # Method 1: CreateToolhelp32Snapshot (Works reliably even when process is elevated or protected by NGS)
    try:
        import ctypes.wintypes
        kernel32 = ctypes.windll.kernel32
        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ('dwSize', ctypes.wintypes.DWORD),
                ('cntUsage', ctypes.wintypes.DWORD),
                ('th32ProcessID', ctypes.wintypes.DWORD),
                ('th32DefaultHeapID', ctypes.c_size_t),
                ('th32ModuleID', ctypes.wintypes.DWORD),
                ('cntThreads', ctypes.wintypes.DWORD),
                ('th32ParentProcessID', ctypes.wintypes.DWORD),
                ('pcPriClassBase', ctypes.c_long),
                ('dwFlags', ctypes.wintypes.DWORD),
                ('szExeFile', ctypes.c_wchar * 260)
            ]
        h_snap = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
        if h_snap and h_snap != -1:
            try:
                pe = PROCESSENTRY32W()
                pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
                target_names = ("mabinogimobile.exe", "mabinogimobile_cli.exe", "mabinogimobile_controller.exe")
                if kernel32.Process32FirstW(h_snap, ctypes.byref(pe)):
                    while True:
                        if pe.szExeFile.lower() in target_names:
                            return True
                        if not kernel32.Process32NextW(h_snap, ctypes.byref(pe)):
                            break
            finally:
                kernel32.CloseHandle(h_snap)
    except Exception:
        pass

    # Method 2: Fallback EnumProcesses
    try:
        kernel32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi
        arr = (ctypes.c_ulong * 1024)()
        cb_needed = ctypes.c_ulong()
        if psapi.EnumProcesses(ctypes.byref(arr), ctypes.sizeof(arr), ctypes.byref(cb_needed)):
            count = cb_needed.value // ctypes.sizeof(ctypes.c_ulong)
            for i in range(count):
                pid = arr[i]
                h_proc = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
                if h_proc:
                    buf = ctypes.create_unicode_buffer(1024)
                    size = ctypes.c_ulong(1024)
                    if kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size)):
                        exe_path = buf.value
                        lower = os.path.basename(exe_path).lower()
                        if lower in ("mabinogimobile.exe", "mabinogimobile_cli.exe", "mabinogimobile_controller.exe"):
                            kernel32.CloseHandle(h_proc)
                            return True
                    kernel32.CloseHandle(h_proc)
    except Exception:
        pass
    return False


def _find_from_registry() -> Optional[str]:
    """Inspects Windows Registry for Nexon & Mabinogi Mobile install paths."""
    if sys.platform != "win32":
        return None
    import winreg
    reg_candidates = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Nexon\MabinogiM", "RootPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Nexon\MabinogiM", "RootPath"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\WOW6432Node\Nexon\MabinogiM", "RootPath"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Nexon\MabinogiM", "RootPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\MabinogiM", "InstallLocation"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\MabinogiM", "InstallLocation"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\MabinogiM", "InstallLocation"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\MabinogiM", "DisplayIcon"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\MabinogiM", "DisplayIcon"),
    ]
    for hkey, subkey, val_name in reg_candidates:
        try:
            with winreg.OpenKey(hkey, subkey) as k:
                val, _ = winreg.QueryValueEx(k, val_name)
                if val:
                    cand = _normalize_cli_path(val)
                    if cand:
                        return cand
        except Exception:
            pass
    return None


def _find_from_drives() -> Optional[str]:
    """Scans all available drive letters for typical Nexon / Mabinogi Mobile install paths."""
    drives = _get_available_drives()
    sub_paths = [
        r"Nexon\MabinogiMobile\MabinogiMobile_CLI.exe",
        r"Program Files\Nexon\MabinogiMobile\MabinogiMobile_CLI.exe",
        r"Program Files (x86)\Nexon\MabinogiMobile\MabinogiMobile_CLI.exe",
        r"Games\Nexon\MabinogiMobile\MabinogiMobile_CLI.exe",
        r"Games\MabinogiMobile\MabinogiMobile_CLI.exe",
        r"Nexon\MabinogiM\MabinogiMobile_CLI.exe",
        r"MabinogiMobile\MabinogiMobile_CLI.exe",
    ]
    for d in drives:
        for sub in sub_paths:
            full = f"{d}:\\{sub}"
            if os.path.isfile(full):
                return full
    return None


def _find_from_env_path() -> Optional[str]:
    """Checks system PATH and user environment registry PATH."""
    w = shutil.which("MabinogiMobile_CLI.exe") or shutil.which("MabinogiMobile_CLI")
    if w and os.path.isfile(w):
        return os.path.abspath(w)

    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as k:
                val, _ = winreg.QueryValueEx(k, "Path")
                for part in val.split(";"):
                    cand = _normalize_cli_path(part)
                    if cand:
                        return cand
        except Exception:
            pass
    return None


def find_cli_path(custom_hint: Optional[str] = None) -> Tuple[Optional[str], str]:
    """
    Intelligently finds MabinogiMobile_CLI.exe across all potential locations.
    Returns: (resolved_path, source_description)
    """
    # 1. Custom hint if passed
    if custom_hint:
        cand = _normalize_cli_path(custom_hint)
        if cand:
            return cand, "사용자 지정 경로"

    # 2. Saved configuration file
    saved = get_saved_cli_path()
    if saved:
        return saved, "저장된 설정 파일"

    # 3. Active running game process (100% accurate when game is open)
    proc_cli = _find_from_running_processes()
    if proc_cli:
        return proc_cli, "실행 중인 게임 프로세스"

    # 4. Windows Registry
    reg_cli = _find_from_registry()
    if reg_cli:
        return reg_cli, "Windows 레지스트리"

    # 5. Environment PATH
    env_cli = _find_from_env_path()
    if env_cli:
        return env_cli, "시스템 환경변수 (PATH)"

    # 6. Default hardcoded standard path
    if os.path.isfile(CLI_DEFAULT_PATH):
        return CLI_DEFAULT_PATH, "기본 설치 경로 (C:\\Nexon\\MabinogiMobile)"

    # 7. Drive scan across all partitions (C, D, E, F...)
    drive_cli = _find_from_drives()
    if drive_cli:
        return drive_cli, "드라이브 자동 검색"

    # 8. LocalAppData / AppData
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        cand = _normalize_cli_path(os.path.join(local_app_data, "Programs", "Nexon", "MabinogiMobile"))
        if cand:
            return cand, "LocalAppData"

    return None, "미발견"


class MabinogiCLIError(Exception):
    def __init__(self, message: str, exit_code: int = 0, raw_output: str = "", response_data: Any = None, error_code: str = ""):
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code
        self.raw_output = raw_output
        self.response_data = response_data
        self.error_code = error_code

    def __str__(self) -> str:
        return self.message


class MabinogiCLI:
    def __init__(self, cli_path: Optional[str] = None):
        self.cli_path: Optional[str] = None
        self.discovery_source: str = ""
        self._read_cache: Dict[Tuple[str, Optional[str]], Tuple[float, Any]] = {}
        self._cache_lock = threading.Lock()
        self.discover_cli_path(cli_path)

    def invalidate_cache(self) -> None:
        """Clears all cached read responses (called after mutations or inventory changes)."""
        with self._cache_lock:
            self._read_cache.clear()

    def check_game_running(self) -> bool:
        """Checks if Mabinogi Mobile game process is running."""
        return _is_game_running()

    def discover_cli_path(self, hint: Optional[str] = None) -> Optional[str]:
        """Runs the intelligent path resolver and records discovery source."""
        path, source = find_cli_path(hint)
        if path:
            self.cli_path = path
            self.discovery_source = source
        return self.cli_path

    def set_cli_path(self, new_path: str) -> bool:
        """Sets and persists a custom CLI path."""
        cand = _normalize_cli_path(new_path)
        if cand:
            self.cli_path = cand
            self.discovery_source = "사용자 직접 입력"
            save_configured_cli_path(cand)
            return True
        return False

    @staticmethod
    def encode_body(body: Optional[str]) -> Optional[str]:
        """Base64 encodes body if it contains non-ASCII characters, following SKILL.md specification."""
        if not body:
            return None
        if any(ord(c) > 127 for c in body):
            b64_str = base64.b64encode(body.encode("utf-8")).decode("ascii")
            return f"base64:{b64_str}"
        return body

    def run_raw(self, command: str, body: Optional[str] = None, timeout: int = 600, bypass_cache: bool = False) -> Tuple[int, Any]:
        """
        Executes a CLI command and returns (exit_code, parsed_json_or_text).
        Automatically resolves CLI path, manages encoding, and transforms errors into user-friendly messages.
        Includes fast thread-safe TTL caching for read-only queries to prevent subprocess saturation.
        """
        # Invalidate read cache on mutation commands
        if command in ACTION_COMMANDS:
            self.invalidate_cache()

        cache_key = (command, body)
        if not bypass_cache and command in READ_CACHE_TTL:
            ttl = READ_CACHE_TTL[command]
            with self._cache_lock:
                if cache_key in self._read_cache:
                    cached_time, cached_val = self._read_cache[cache_key]
                    if (time.time() - cached_time) < ttl:
                        return 0, copy.deepcopy(cached_val)
        # If CLI path is missing or invalid, attempt re-discovery
        if not self.cli_path or not os.path.isfile(self.cli_path):
            discovered = self.discover_cli_path()
            if not discovered:
                raise MabinogiCLIError(
                    "MabinogiMobile_CLI.exe 실행 파일을 찾을 수 없습니다.\n"
                    "1. 마비노기 모바일 PC 클라이언트를 실행하고 캐릭터로 로그인해주세요.\n"
                    "2. 인게임 [환경설정]에서 [MM AI 에이전트 활성화]가 ON으로 켜져 있는지 확인하세요.\n"
                    "3. 게임이 C드라이브가 아닌 다른 폴더에 설치되어 있다면 상단 [연결 설정]에서 설치 폴더를 지정해주세요.",
                    error_code="CLI_NOT_FOUND"
                )

        args = [self.cli_path, command]
        encoded = self.encode_body(body)
        if encoded is not None:
            args.append(encoded)

        # Commands that must not block the shared _CLI_LOCK:
        # - stop_action: emergency interrupt of in-progress actions
        # - execute_gathering: long-running background action monitored concurrently by worker loop
        if command in ("stop_action", "execute_gathering"):
            try:
                kwargs = {
                    "capture_output": True,
                    "text": True,
                    "timeout": timeout,
                    "encoding": "utf-8",
                    "errors": "replace"
                }
                if sys.platform == "win32":
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    startupinfo.wShowWindow = subprocess.SW_HIDE
                    kwargs["startupinfo"] = startupinfo
                    kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

                res = subprocess.run(args, **kwargs)
            except subprocess.TimeoutExpired as te:
                raise MabinogiCLIError(f"명령어 '{command}' 실행 시간이 초과되었습니다 ({timeout}초)", exit_code=3, error_code="TIMEOUT") from te
            except FileNotFoundError as fnf:
                raise MabinogiCLIError(
                    f"MabinogiMobile_CLI.exe 실행 파일을 찾을 수 없습니다: {str(fnf)}\n"
                    f"설정된 경로: {self.cli_path}\n"
                    f"게임 설치 폴더 및 MM AI 에이전트 활성화 상태를 확인해주세요.",
                    exit_code=1,
                    error_code="CLI_NOT_FOUND"
                ) from fnf
            except Exception as ex:
                raise MabinogiCLIError(f"CLI 실행 중 오류가 발생했습니다 ('{command}'): {str(ex)}", error_code="EXEC_ERROR") from ex
        else:
            with _CLI_LOCK:
                try:
                    kwargs = {
                        "capture_output": True,
                        "text": True,
                        "timeout": timeout,
                        "encoding": "utf-8",
                        "errors": "replace"
                    }
                    if sys.platform == "win32":
                        startupinfo = subprocess.STARTUPINFO()
                        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                        startupinfo.wShowWindow = subprocess.SW_HIDE
                        kwargs["startupinfo"] = startupinfo
                        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

                    res = subprocess.run(args, **kwargs)
                except subprocess.TimeoutExpired as te:
                    raise MabinogiCLIError(f"명령어 '{command}' 실행 시간이 초과되었습니다 ({timeout}초)", exit_code=3, error_code="TIMEOUT") from te
                except FileNotFoundError as fnf:
                    raise MabinogiCLIError(
                        f"MabinogiMobile_CLI.exe 실행 파일을 찾을 수 없습니다: {str(fnf)}\n"
                        f"설정된 경로: {self.cli_path}\n"
                        f"게임 설치 폴더 및 MM AI 에이전트 활성화 상태를 확인해주세요.",
                        exit_code=1,
                        error_code="CLI_NOT_FOUND"
                    ) from fnf
                except Exception as ex:
                    raise MabinogiCLIError(f"CLI 실행 중 오류가 발생했습니다 ('{command}'): {str(ex)}", error_code="EXEC_ERROR") from ex

        exit_code = res.returncode
        stdout_str = res.stdout.strip()

        # Parse JSON
        parsed = None
        if stdout_str:
            try:
                parsed = json.loads(stdout_str)
            except json.JSONDecodeError:
                # Try reading last-response.json as fallback
                if os.path.exists(LAST_RESPONSE_PATH):
                    try:
                        with open(LAST_RESPONSE_PATH, "r", encoding="utf-8") as f:
                            parsed = json.load(f)
                    except Exception:
                        parsed = stdout_str
                else:
                    parsed = stdout_str

        if exit_code != 0:
            reason = ""
            msg = ""
            if isinstance(parsed, dict):
                reason = str(parsed.get("reason") or "")
                msg = str(parsed.get("message") or parsed.get("error") or reason or "")

            if exit_code == 5:
                if reason == "game_off":
                    raise MabinogiCLIError(
                        "마비노기 모바일 클라이언트가 실행되어 있지 않습니다. 게임을 실행하고 로그인해주세요.",
                        exit_code=5,
                        error_code="GAME_OFF",
                        raw_output=stdout_str,
                        response_data=parsed
                    )
                elif reason == "option_off":
                    raise MabinogiCLIError(
                        "게임 설정에서 [MM AI 에이전트 활성화] 옵션이 꺼져 있습니다. 게임 내 환경설정에서 활성화해주세요.",
                        exit_code=5,
                        error_code="OPTION_OFF",
                        raw_output=stdout_str,
                        response_data=parsed
                    )
                else:
                    detail = f" ({msg})" if msg else ""
                    raise MabinogiCLIError(
                        f"게임 클라이언트와 연결되지 않았습니다{detail}",
                        exit_code=5,
                        error_code="DISCONNECTED",
                        raw_output=stdout_str,
                        response_data=parsed
                    )
            elif exit_code == 3:
                raise MabinogiCLIError("게임 내 대기 또는 작업이 취소되었습니다.", exit_code=3, error_code="CANCELED", raw_output=stdout_str, response_data=parsed)
            elif exit_code == 4:
                raise MabinogiCLIError("게임 클라이언트에서 인식할 수 없는 명령어입니다.", exit_code=4, error_code="UNKNOWN_COMMAND", raw_output=stdout_str, response_data=parsed)
            else:
                detail = f": {msg}" if msg else ""
                raise MabinogiCLIError(
                    f"CLI 프로세스 오류 (종료 코드 {exit_code}){detail}",
                    exit_code=exit_code,
                    error_code=f"EXIT_{exit_code}",
                    raw_output=stdout_str,
                    response_data=parsed
                )

        # A response that reaches game client may exit with 0 but contain error body
        if isinstance(parsed, dict) and "error" in parsed:
            error_msg = parsed.get("message") or parsed.get("error")
            kind = parsed.get("kind")
            if kind:
                error_msg = f"[{kind}] {error_msg}"
            raise MabinogiCLIError(str(error_msg), exit_code=exit_code, raw_output=stdout_str, response_data=parsed, error_code="GAME_ERROR")

        # Cache successful read-only response
        if exit_code == 0 and command in READ_CACHE_TTL:
            with self._cache_lock:
                self._read_cache[cache_key] = (time.time(), copy.deepcopy(parsed))

        return exit_code, parsed

    # --- Core Status & Info ---

    def status(self) -> Dict[str, Any]:
        _, data = self.run_raw("status", timeout=10)
        return data if isinstance(data, dict) else {}

    def get_my_info(self) -> Dict[str, Any]:
        _, data = self.run_raw("get_my_info", timeout=15)
        return data if isinstance(data, dict) else {}

    def get_current_environment(self) -> Dict[str, Any]:
        _, data = self.run_raw("get_current_environment", timeout=15)
        return data if isinstance(data, dict) else {}

    def get_activity(self) -> Dict[str, Any]:
        _, data = self.run_raw("get_activity", timeout=15)
        return data if isinstance(data, dict) else {}

    def get_inventory(self) -> Dict[str, Any]:
        _, data = self.run_raw("get_inventory", timeout=15)
        return data if isinstance(data, dict) else {}

    def get_quests(self) -> List[Dict[str, Any]]:
        _, data = self.run_raw("get_quests", timeout=20)
        return data if isinstance(data, list) else []

    def get_currencies(self) -> List[Dict[str, Any]]:
        _, data = self.run_raw("get_currencies", timeout=15)
        return data if isinstance(data, list) else []

    def get_wings_count(self) -> int:
        """Returns the number of '정령의 날개'."""
        currencies = self.get_currencies()
        for cur in currencies:
            if cur.get("DisplayName") == "정령의 날개":
                return int(cur.get("Amount", 0))
        return 0

    # --- Items & Inventory ---

    def get_items(self, name: Optional[str] = None, category: Optional[str] = None, bypass_cache: bool = False) -> List[Dict[str, Any]]:
        body_dict = {}
        if name:
            body_dict["name"] = name
        if category:
            body_dict["category"] = category

        body_str = json.dumps(body_dict, ensure_ascii=False) if body_dict else None
        _, data = self.run_raw("get_items", body_str, timeout=25, bypass_cache=bypass_cache)
        return data if isinstance(data, list) else []

    def get_all_item_locations_map(self, bypass_cache: bool = False) -> Dict[str, Dict[str, int]]:
        """
        Returns a high-speed cached mapping of all owned items:
        item_name -> {inventory, character_storage, account_storage, storage_total, total}.
        Queries get_items() once without filters (benefiting from 4.0s READ_CACHE_TTL when bypass_cache is False).
        """
        items = self.get_items(bypass_cache=bypass_cache)
        mapping: Dict[str, Dict[str, int]] = {}
        for it in items:
            name = it.get("DisplayName", "")
            if not name:
                continue
            loc = it.get("Location", "")
            cnt = int(it.get("Count", 0) or 0)
            if name not in mapping:
                mapping[name] = {
                    "inventory": 0,
                    "character_storage": 0,
                    "account_storage": 0,
                    "storage_total": 0,
                    "total": 0
                }
            if loc == "inventory":
                mapping[name]["inventory"] += cnt
            elif loc == "character_storage":
                mapping[name]["character_storage"] += cnt
                mapping[name]["storage_total"] += cnt
            elif loc == "account_storage":
                mapping[name]["account_storage"] += cnt
                mapping[name]["storage_total"] += cnt
            mapping[name]["total"] += cnt
        return mapping

    def count_item(self, item_name: str, include_storage: bool = True, bypass_cache: bool = False) -> int:
        """Counts how many of item_name the player owns (in inventory and optionally storage)."""
        breakdown = self.get_item_location_breakdown(item_name, bypass_cache=bypass_cache)
        return breakdown["total"] if include_storage else breakdown["inventory"]

    def get_item_location_breakdown(self, item_name: str, bypass_cache: bool = False) -> Dict[str, int]:
        """
        Returns a breakdown of where item_name is stored:
        inventory (가방), character_storage (개인 창고), account_storage (공용 창고),
        storage_total (창고 합계), and total (전체 총합).
        Uses fast batch-cached item locations map to prevent subprocess flood.
        """
        mapping = self.get_all_item_locations_map(bypass_cache=bypass_cache)
        if item_name in mapping:
            return copy.deepcopy(mapping[item_name])
        return {
            "inventory": 0,
            "character_storage": 0,
            "account_storage": 0,
            "storage_total": 0,
            "total": 0
        }

    # --- Crafting ---

    def get_craftable_items(self, filter_name: str = "") -> Dict[str, Any]:
        _, data = self.run_raw("get_craftable_items", filter_name if filter_name else None, timeout=25)
        return data if isinstance(data, dict) else {"craftingUnlocked": False, "items": []}

    def execute_crafting(self, display_name: str, craft_count: int = 1) -> Dict[str, Any]:
        body = json.dumps({"displayName": display_name, "craftCount": craft_count}, ensure_ascii=False)
        _, data = self.run_raw("execute_crafting", body, timeout=600)
        return data

    # --- Altering (가공) ---

    def get_alterable_items(self, filter_name: str = "") -> Dict[str, Any]:
        _, data = self.run_raw("get_alterable_items", filter_name if filter_name else None, timeout=25)
        return data if isinstance(data, dict) else {"items": []}

    def execute_altering(self, display_name: str) -> Dict[str, Any]:
        body = json.dumps({"displayName": display_name}, ensure_ascii=False)
        _, data = self.run_raw("execute_altering", body, timeout=600)
        return data

    def get_altering_works(self) -> Dict[str, Any]:
        _, data = self.run_raw("get_altering_works", timeout=20)
        return data if isinstance(data, dict) else {"completedCount": 0, "works": []}

    def complete_altering_work(self, display_name: str) -> Dict[str, Any]:
        body = json.dumps({"displayName": display_name}, ensure_ascii=False)
        _, data = self.run_raw("complete_altering_work", body, timeout=600)
        return data

    # --- Gathering (채집) ---

    def get_gatherable_items(self, filter_name: str = "") -> Dict[str, Any]:
        _, data = self.run_raw("get_gatherable_items", filter_name if filter_name else None, timeout=25)
        return data if isinstance(data, dict) else {"items": []}

    def execute_gathering(self, display_name: str) -> Dict[str, Any]:
        body = json.dumps({"displayName": display_name}, ensure_ascii=False)
        _, data = self.run_raw("execute_gathering", body, timeout=600)
        return data

    def stop_action(self) -> Dict[str, Any]:
        _, data = self.run_raw("stop_action", timeout=15)
        return data
