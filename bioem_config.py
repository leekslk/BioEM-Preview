import json
import os
import re
import shutil
import struct
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
    "engine_python": "",
    "engine_variant": "auto",
}

CACHE_FILENAME = "fiji_latest_cache.json"
HISTORY_FILENAME = "fiji_sync_history.json"
CONFIG_FILENAME = "config.json"
TRIGGER_FILENAME = "fiji_trigger.txt"
INDEXER_PID_FILENAME = "indexer.pid"
LAST_LOG_FILENAME = "last_action.log"
ALIGNED_DATASETS_FILENAME = "aligned_datasets.json"
DATASET_LOGS_DIRNAME = "dataset_logs"
STARTUP_SCRIPT_NAME = "BioEM_NAS_Indexer.vbs"
INDEXER_PROTOCOL_VERSION = 2


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


def get_last_log_file_path():
    """Returns the file path storing the log lines of the last performed action."""
    return os.path.join(get_app_data_dir(), LAST_LOG_FILENAME)


def get_aligned_datasets_path():
    """Returns the registry file tracking datasets that have completed alignment and XML creation."""
    return os.path.join(get_app_data_dir(), ALIGNED_DATASETS_FILENAME)


def get_dataset_log_path(safe_folder_name):
    """Returns the persistent per-dataset log file path inside app data."""
    safe = "".join(c for c in (safe_folder_name or "dataset") if c.isalnum() or c in ["_", "-"]) or "dataset"
    logs_dir = os.path.join(get_app_data_dir(), DATASET_LOGS_DIRNAME)
    os.makedirs(logs_dir, exist_ok=True)
    return os.path.join(logs_dir, f"{safe}.log")


def _normalize_dataset_key(source_folder):
    return (source_folder or "").replace("\\", "/").rstrip("/").lower()


def load_aligned_datasets():
    path = get_aligned_datasets_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_aligned_datasets(records):
    path = get_aligned_datasets_path()
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2)
    os.replace(tmp_path, path)


def record_aligned_dataset(source_folder, record):
    key = _normalize_dataset_key(source_folder)
    if not key:
        return
    records = load_aligned_datasets()
    records[key] = record
    _save_aligned_datasets(records)


def get_aligned_dataset_record(source_folder):
    key = _normalize_dataset_key(source_folder)
    if not key:
        return None
    return load_aligned_datasets().get(key)


def remove_aligned_dataset_record(source_folder):
    key = _normalize_dataset_key(source_folder)
    if not key:
        return
    records = load_aligned_datasets()
    if key in records:
        del records[key]
        try:
            _save_aligned_datasets(records)
        except Exception:
            pass


def get_sync_cache_root(destination_dir):
    """Returns the root directory on the local scratch drive where datasets are synced."""
    dest = (destination_dir or DEFAULT_CONFIG["destination_dir"]).strip()
    return dest or DEFAULT_CONFIG["destination_dir"]


def _parse_fei_ini_text(raw_bytes):
    text = raw_bytes.decode("latin-1", errors="replace").replace("\x00", "")
    result = {}
    section = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = {}
            result[line[1:-1].strip()] = section
            continue
        if "=" in line:
            key, val = line.split("=", 1)
            section[key.strip()] = val.strip()
    return result


def read_fei_tiff_metadata(tiff_path):
    """
    Lightweight pure-stdlib reader for FEI/Thermo Fisher TIFF metadata tags
    (34680 FEI_SFEG, 34682 FEI_HELIOS) and PhotometricInterpretation (262).
    Returns (fei_dict, photometric_int_or_None).
    """
    fei_dict = {}
    photometric = None
    if not tiff_path or not os.path.isfile(tiff_path):
        return fei_dict, photometric

    try:
        with open(tiff_path, "rb") as f:
            header = f.read(16)
            if len(header) < 8:
                return fei_dict, photometric
            bo = header[:2]
            if bo == b"II":
                endian = "<"
            elif bo == b"MM":
                endian = ">"
            else:
                return fei_dict, photometric

            version = struct.unpack(endian + "H", header[2:4])[0]
            type_sizes = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 16: 8, 17: 8, 18: 8}

            if version == 42:
                ifd_offset = struct.unpack(endian + "I", header[4:8])[0]
                if ifd_offset <= 0:
                    return fei_dict, photometric
                f.seek(ifd_offset)
                count_bytes = f.read(2)
                if len(count_bytes) < 2:
                    return fei_dict, photometric
                n_entries = struct.unpack(endian + "H", count_bytes)[0]
                for _ in range(n_entries):
                    entry = f.read(12)
                    if len(entry) < 12:
                        break
                    tag, dtype, count = struct.unpack(endian + "HHI", entry[:8])
                    val_raw = entry[8:12]
                    if tag == 262 and count >= 1:
                        if dtype == 3:
                            photometric = struct.unpack(endian + "H", val_raw[:2])[0]
                        elif dtype == 4:
                            photometric = struct.unpack(endian + "I", val_raw)[0]
                    elif tag in (34680, 34682) and count > 0:
                        total_bytes = count * type_sizes.get(dtype, 1)
                        if total_bytes <= 4:
                            raw_payload = val_raw[:total_bytes]
                        else:
                            offset = struct.unpack(endian + "I", val_raw)[0]
                            pos = f.tell()
                            f.seek(offset)
                            raw_payload = f.read(total_bytes)
                            f.seek(pos)
                        fei_dict.update(_parse_fei_ini_text(raw_payload))
            elif version == 43:
                if len(header) < 16:
                    return fei_dict, photometric
                ifd_offset = struct.unpack(endian + "Q", header[8:16])[0]
                if ifd_offset <= 0:
                    return fei_dict, photometric
                f.seek(ifd_offset)
                count_bytes = f.read(8)
                if len(count_bytes) < 8:
                    return fei_dict, photometric
                n_entries = struct.unpack(endian + "Q", count_bytes)[0]
                for _ in range(n_entries):
                    entry = f.read(20)
                    if len(entry) < 20:
                        break
                    tag, dtype, count = struct.unpack(endian + "HHQ", entry[:12])
                    val_raw = entry[12:20]
                    if tag == 262 and count >= 1:
                        if dtype == 3:
                            photometric = struct.unpack(endian + "H", val_raw[:2])[0]
                        elif dtype in (4, 16):
                            photometric = struct.unpack(endian + ("I" if dtype == 4 else "Q"), val_raw[:4 if dtype == 4 else 8])[0]
                    elif tag in (34680, 34682) and count > 0:
                        total_bytes = count * type_sizes.get(dtype, 1)
                        if total_bytes <= 8:
                            raw_payload = val_raw[:total_bytes]
                        else:
                            offset = struct.unpack(endian + "Q", val_raw)[0]
                            pos = f.tell()
                            f.seek(offset)
                            raw_payload = f.read(total_bytes)
                            f.seek(pos)
                        fei_dict.update(_parse_fei_ini_text(raw_payload))
    except Exception:
        pass
    return fei_dict, photometric


def detect_lut_inversion_needed(tiff_path, default_if_missing=False):
    """
    Inspects the first TIFF's FEI DigitalContrast metadata (and PhotometricInterpretation)
    to determine whether the acquisition requires LUT inversion for brightfield TEM-like viewing.
    - DigitalContrast < 0 (e.g. -1): already inverted at acquisition -> False
    - DigitalContrast > 0 (e.g. +1): raw uninverted signal -> True
    Returns (invert_needed: bool, reason: str).
    """
    if not tiff_path or not os.path.isfile(tiff_path):
        return default_if_missing, "No TIFF available for LUT inspection; using default."

    fei, photometric = read_fei_tiff_metadata(tiff_path)
    img_sec = fei.get("Image", {}) if isinstance(fei, dict) else {}
    det_sec = fei.get("Detectors", {}) if isinstance(fei, dict) else {}
    det_name = str(det_sec.get("Name", "") or "").strip()
    signal = str(fei.get(det_name, {}).get("Signal", "") or "").strip() if det_name else ""
    det_desc = f"{det_name}/{signal}".strip("/") or "FEI"

    dc_raw = img_sec.get("DigitalContrast")
    if dc_raw not in (None, ""):
        try:
            dc_val = float(str(dc_raw).strip())
            if dc_val < 0:
                return False, f"FEI DigitalContrast={dc_val:g} ({det_desc}): already inverted LUT at acquisition -> keeping LUT."
            if dc_val > 0:
                return True, f"FEI DigitalContrast={dc_val:g} ({det_desc}): native uninverted signal -> applying Invert LUT."
        except Exception:
            pass

    if photometric == 0:
        return False, "TIFF PhotometricInterpretation=MINISWHITE (0): already inverted -> keeping LUT."

    return (
        bool(default_if_missing),
        f"No FEI DigitalContrast tag in header ({os.path.basename(tiff_path)}); using default ({'Invert' if default_if_missing else 'Keep'}).",
    )


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


def _read_indexer_pid_info():
    pid_file = os.path.join(get_app_data_dir(), INDEXER_PID_FILENAME)
    if not os.path.exists(pid_file):
        return None
    try:
        with open(pid_file, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        if not raw:
            return None
        if raw.startswith("{"):
            return json.loads(raw)
        return {"pid": int(raw), "exe": "", "version": 1}
    except Exception:
        return None


def is_indexer_running():
    """Checks if a BioEM indexer process is currently active via PID file."""
    info = _read_indexer_pid_info()
    if not info:
        return False
    pid = int(info.get("pid", 0) or 0)
    if pid <= 0:
        return False
    try:
        import psutil
        if psutil.pid_exists(pid):
            proc = psutil.Process(pid)
            if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                return True
    except Exception:
        pass
    return False


def _stop_stale_indexer():
    """Stops an older-version or differently-located indexer process if active."""
    info = _read_indexer_pid_info()
    if not info:
        return
    pid = int(info.get("pid", 0) or 0)
    if pid <= 0:
        return
    try:
        import psutil
        if psutil.pid_exists(pid):
            proc = psutil.Process(pid)
            if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except Exception:
                    proc.kill()
    except Exception:
        pass


def ensure_indexer_running():
    """
    Launches the background indexer silently if not running, or restarts it
    automatically if an older version / different executable path was left active.
    """
    info = _read_indexer_pid_info()
    if is_indexer_running() and info:
        same_version = int(info.get("version", 1) or 1) == INDEXER_PROTOCOL_VERSION
        running_exe = info.get("exe", "")
        same_exe = (
            bool(running_exe)
            and os.path.normcase(os.path.abspath(running_exe))
            == os.path.normcase(os.path.abspath(sys.executable))
        )
        if same_version and same_exe:
            return True, "Background indexer already running."
        _stop_stale_indexer()

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
