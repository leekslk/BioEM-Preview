import json
import os
import re
import shutil
import subprocess
import sys

APP_NAME = "BioEM_Preview"

# Default configuration matching the original #CONFIGURATION values
DEFAULT_CONFIG = {
    "destination_dir": "D:/",
    "nas_dir": "I:/",
    "app_path": r"C:\Users\User\Desktop\Fiji\fiji-windows-x64.exe",
    "auto_start_indexer": True,
    "overlap_pct": 10.0,
}

CACHE_FILENAME = "fiji_latest_cache.json"
HISTORY_FILENAME = "fiji_sync_history.json"
CONFIG_FILENAME = "config.json"
TRIGGER_FILENAME = "fiji_trigger.txt"
INDEXER_PID_FILENAME = "indexer.pid"
STARTUP_SCRIPT_NAME = "BioEM_NAS_Indexer.vbs"


def get_app_data_dir():
    """Returns the writable per-user BioEM Preview configuration/state directory."""
    override = os.environ.get("BIOEM_CONFIG_DIR")
    if override:
        os.makedirs(override, exist_ok=True)
        return override

    if sys.platform == "win32":
        base = os.getenv("APPDATA") or os.path.expanduser("~")
    else:
        base = os.path.join(os.path.expanduser("~"), ".config")

    app_dir = os.path.join(base, APP_NAME)
    os.makedirs(app_dir, exist_ok=True)
    return app_dir


def get_config_path():
    return os.path.join(get_app_data_dir(), CONFIG_FILENAME)


def get_cache_file_path():
    """
    Returns the path to fiji_latest_cache.json.
    Maintains migration from legacy %APPDATA%/fiji_latest_cache.json on first use.
    """
    target = os.path.join(get_app_data_dir(), CACHE_FILENAME)
    if not os.path.exists(target) and not os.environ.get("BIOEM_CONFIG_DIR"):
        legacy_base = os.getenv("APPDATA")
        if legacy_base:
            legacy = os.path.join(legacy_base, CACHE_FILENAME)
            if os.path.exists(legacy):
                try:
                    shutil.copy2(legacy, target)
                except Exception:
                    pass
    return target


def get_history_file_path():
    """
    Returns the path to fiji_sync_history.json, migrating legacy Alpha history if present.
    """
    target = os.path.join(get_app_data_dir(), HISTORY_FILENAME)
    if not os.path.exists(target) and not os.environ.get("BIOEM_CONFIG_DIR"):
        legacy_base = os.getenv("APPDATA")
        if legacy_base:
            legacy = os.path.join(legacy_base, HISTORY_FILENAME)
            if os.path.exists(legacy):
                try:
                    shutil.copy2(legacy, target)
                except Exception:
                    pass
    return target


def get_trigger_file_path():
    """Returns the shared trigger file path read by BioEM_Watcher.ijm and pull_from_NAS.py."""
    return os.path.join(get_app_data_dir(), TRIGGER_FILENAME).replace("\\", "/")


def get_sync_cache_root(destination_dir):
    """
    Stores synced acquisition folders inside a dedicated 'BioEM_Cache' subfolder
    under destination_dir so 30-day housekeeping never touches unrelated drive data.
    """
    dest = (destination_dir or DEFAULT_CONFIG["destination_dir"]).strip()
    return os.path.join(dest, "BioEM_Cache")


def resource_path(relative_path):
    """Resolves bundled resource files for both PyInstaller frozen builds and source runs."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        candidate = os.path.join(sys._MEIPASS, relative_path)
        if os.path.exists(candidate):
            return candidate
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, relative_path)


def config_exists():
    return os.path.exists(get_config_path())


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    cfg_path = get_config_path()
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                cfg.update(loaded)
        except Exception:
            pass
    return cfg


def save_config(cfg):
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg)
    cfg_path = get_config_path()
    tmp_path = cfg_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=4)
    os.replace(tmp_path, cfg_path)
    return merged


def detect_fiji_executable(current_path=None):
    """Returns the first existing Fiji executable from current_path or common install paths."""
    candidates = []
    if current_path:
        candidates.append(current_path)
    env_path = os.environ.get("FIJI_EXE")
    if env_path:
        candidates.append(env_path)

    user_home = os.path.expanduser("~")
    candidates.extend([
        DEFAULT_CONFIG["app_path"],
        os.path.join(user_home, "Desktop", "Fiji", "fiji-windows-x64.exe"),
        os.path.join(user_home, "Desktop", "Fiji.app", "ImageJ-win64.exe"),
        os.path.join(user_home, "Desktop", "Fiji.app", "fiji-windows-x64.exe"),
        r"C:\Fiji.app\ImageJ-win64.exe",
        r"C:\Fiji.app\fiji-windows-x64.exe",
        r"D:\Fiji.app\ImageJ-win64.exe",
        r"D:\Fiji.app\fiji-windows-x64.exe",
        os.path.join(user_home, "Fiji.app", "ImageJ-win64.exe"),
        "/Applications/Fiji.app/Contents/MacOS/ImageJ-macosx",
    ])

    for path in candidates:
        if path and os.path.exists(path):
            return path
    return current_path or DEFAULT_CONFIG["app_path"]


def deploy_fiji_watcher(fiji_exe_path):
    """
    Installs BioEM_Watcher.ijm into <Fiji_Dir>/macros/AutoRun/BioEM_Watcher.ijm,
    configuring triggerFile to the shared per-user trigger path.
    Returns (success: bool, message: str).
    """
    if not fiji_exe_path:
        return False, "Fiji executable path is empty."

    # On macOS, Fiji executable lives inside Fiji.app/Contents/MacOS/ImageJ-macosx
    exe_dir = os.path.dirname(os.path.abspath(fiji_exe_path))
    if sys.platform == "darwin" and exe_dir.endswith(os.path.join("Contents", "MacOS")):
        fiji_root = os.path.dirname(os.path.dirname(exe_dir))
    else:
        fiji_root = exe_dir

    if not os.path.exists(fiji_root):
        return False, "Fiji directory does not exist: " + fiji_root

    src_watcher = resource_path("BioEM_Watcher.ijm")
    if not os.path.exists(src_watcher):
        return False, "Bundled BioEM_Watcher.ijm not found at: " + src_watcher

    autorun_dir = os.path.join(fiji_root, "macros", "AutoRun")
    dest_watcher = os.path.join(autorun_dir, "BioEM_Watcher.ijm")

    try:
        os.makedirs(autorun_dir, exist_ok=True)
        with open(src_watcher, "r", encoding="utf-8") as f:
            watcher_content = f.read()

        trigger_path = get_trigger_file_path()
        updated_content = re.sub(
            r'triggerFile\s*=\s*"[^"]*";',
            'triggerFile = "%s";' % trigger_path,
            watcher_content,
            count=1,
        )

        with open(dest_watcher, "w", encoding="utf-8") as f:
            f.write(updated_content)

        return True, "Deployed BioEM_Watcher.ijm to " + dest_watcher
    except PermissionError:
        return False, (
            "Permission denied writing to %s. "
            "If Fiji is installed in Program Files, move Fiji to Desktop or a user-writable folder."
            % autorun_dir
        )
    except Exception as exc:
        return False, "Failed to deploy BioEM_Watcher.ijm: %s" % exc


def _get_startup_script_path():
    if sys.platform != "win32":
        return None
    appdata = os.getenv("APPDATA")
    if not appdata:
        return None
    startup_dir = os.path.join(
        appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup"
    )
    return os.path.join(startup_dir, STARTUP_SCRIPT_NAME)


def configure_indexer_autostart(enable=True):
    """
    Registers or removes a zero-console Windows Startup launcher (.vbs)
    that runs `<App>.exe --indexer` (or `pythonw nas_indexer.py` from source) at login.
    Returns (success: bool, message: str).
    """
    if sys.platform != "win32":
        return True, "Startup registration skipped on non-Windows platform."

    vbs_path = _get_startup_script_path()
    if not vbs_path:
        return False, "Could not locate Windows Startup folder."

    if not enable:
        try:
            if os.path.exists(vbs_path):
                os.remove(vbs_path)
            return True, "Removed indexer auto-start from Windows Startup."
        except Exception as exc:
            return False, "Could not remove Startup script: %s" % exc

    try:
        os.makedirs(os.path.dirname(vbs_path), exist_ok=True)
        if getattr(sys, "frozen", False):
            cmd = '"%s" --indexer' % sys.executable
        else:
            indexer_script = os.path.abspath(
                os.path.join(os.path.dirname(__file__), "nas_indexer.py")
            )
            py_exe = sys.executable
            pyw_exe = py_exe.replace("python.exe", "pythonw.exe")
            if os.path.exists(pyw_exe):
                py_exe = pyw_exe
            cmd = '"%s" "%s"' % (py_exe, indexer_script)

        # WScript.Shell.Run(..., 0, False) launches with no visible console window
        escaped_cmd = cmd.replace('"', '""')
        vbs_body = (
            'Set WshShell = CreateObject("WScript.Shell")\n'
            'WshShell.Run "%s", 0, False\n' % escaped_cmd
        )
        with open(vbs_path, "w", encoding="utf-8") as f:
            f.write(vbs_body)
        return True, "Registered background indexer in Windows Startup."
    except Exception as exc:
        return False, "Failed to register Startup launcher: %s" % exc


def is_indexer_running():
    """Checks if a BioEM indexer process is currently active via PID file."""
    pid_file = os.path.join(get_app_data_dir(), INDEXER_PID_FILENAME)
    if not os.path.exists(pid_file):
        return False
    try:
        with open(pid_file, "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
        if pid <= 0:
            return False
        import psutil
        if psutil.pid_exists(pid):
            proc = psutil.Process(pid)
            if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                return True
    except Exception:
        pass
    return False


def ensure_indexer_running():
    """Launches the background indexer silently if not already running."""
    if is_indexer_running():
        return True, "Background indexer already running."

    try:
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--indexer"]
        else:
            indexer_script = os.path.abspath(
                os.path.join(os.path.dirname(__file__), "nas_indexer.py")
            )
            cmd = [sys.executable, indexer_script]

        kwargs = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "stdin": subprocess.DEVNULL,
            "close_fds": True,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP

        subprocess.Popen(cmd, **kwargs)
        return True, "Started background NAS indexer."
    except Exception as exc:
        return False, "Failed to start background indexer: %s" % exc
