import io
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile

from bioem_config import load_config, resource_path, save_config

UV_WIN_ZIP_URL = (
    "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip"
)
REQUIRED_PACKAGES = ["numpy", "scipy", "tifffile"]

TORCH_INDEX_URLS = {
    "cu118": "https://download.pytorch.org/whl/cu118",
    "cu124": "https://download.pytorch.org/whl/cu124",
    "cpu": "https://download.pytorch.org/whl/cpu",
}


def _popen_kwargs():
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    return kwargs


def get_runtime_root():
    """Returns the per-user writable directory where the alignment Python runtime lives."""
    override = os.environ.get("BIOEM_RUNTIME_DIR")
    if override:
        os.makedirs(override, exist_ok=True)
        return override

    if sys.platform == "win32":
        base = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA") or os.path.expanduser("~")
    else:
        base = os.path.join(os.path.expanduser("~"), ".local", "share")

    root = os.path.join(base, "BioEM_Preview", "runtime")
    os.makedirs(root, exist_ok=True)
    return root


def get_managed_venv_python():
    venv_dir = os.path.join(get_runtime_root(), "venv")
    if sys.platform == "win32":
        return os.path.join(venv_dir, "Scripts", "python.exe")
    return os.path.join(venv_dir, "bin", "python")


def detect_nvidia_gpus():
    """Returns a list of detected NVIDIA GPU names, or [] if none / nvidia-smi unavailable."""
    try:
        res = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=5,
            **_popen_kwargs(),
        )
        if res.returncode == 0 and res.stdout.strip():
            return [line.strip() for line in res.stdout.splitlines() if line.strip()]
    except Exception:
        pass
    return []


def recommended_torch_variant():
    if sys.platform == "win32":
        return "cu118" if detect_nvidia_gpus() else "cpu"
    return "default"


def validate_python_engine(python_exe):
    """
    Verifies that `python_exe` can import torch, numpy, scipy, and tifffile.
    Returns (ok: bool, description: str).
    """
    if not python_exe or not os.path.exists(python_exe):
        return False, "Python executable not found."
    probe = (
        "import torch, numpy, scipy, tifffile; "
        "cuda = torch.cuda.is_available(); "
        "mps = hasattr(torch.backends, 'mps') and torch.backends.mps.is_available(); "
        "dev = ('CUDA x' + str(torch.cuda.device_count())) if cuda else ('MPS' if mps else 'CPU'); "
        "print(f'Torch {torch.__version__} ({dev}) | SciPy {scipy.__version__}')"
    )
    try:
        res = subprocess.run(
            [python_exe, "-c", probe],
            capture_output=True,
            text=True,
            timeout=30,
            **_popen_kwargs(),
        )
        if res.returncode == 0 and res.stdout.strip():
            return True, res.stdout.strip().splitlines()[-1]
        err = (res.stderr or res.stdout or "Unknown import error").strip()
        return False, err.splitlines()[-1]
    except Exception as exc:
        return False, str(exc)


def resolve_engine_python(cfg=None):
    """
    Finds a ready Python executable with torch/numpy/scipy/tifffile.
    Returns (python_exe_or_None, status_description).
    """
    cfg = cfg if cfg is not None else load_config()
    candidates = []

    configured = cfg.get("engine_python")
    if configured:
        candidates.append(configured)

    managed = get_managed_venv_python()
    if managed not in candidates:
        candidates.append(managed)

    if not getattr(sys, "frozen", False) and sys.executable not in candidates:
        candidates.append(sys.executable)

    for py_exe in candidates:
        if py_exe and os.path.exists(py_exe):
            ok, desc = validate_python_engine(py_exe)
            if ok:
                return py_exe, desc

    return None, "Alignment engine not installed yet."


def _find_or_download_uv(progress_cb=None):
    """Finds bundled/system `uv` or downloads it into runtime/bin on Windows."""
    uv_name = "uv.exe" if sys.platform == "win32" else "uv"
    bundled = resource_path(uv_name)
    if os.path.exists(bundled):
        return bundled

    system_uv = shutil.which("uv")
    if system_uv:
        return system_uv

    bin_dir = os.path.join(get_runtime_root(), "bin")
    os.makedirs(bin_dir, exist_ok=True)
    local_uv = os.path.join(bin_dir, uv_name)
    if os.path.exists(local_uv):
        return local_uv

    if sys.platform != "win32":
        raise RuntimeError(
            "Could not find `uv` on PATH. Please install `uv` or select an existing Python with PyTorch."
        )

    if progress_cb:
        progress_cb(0.06, "Downloading uv package manager (~30 MB)...")

    req = urllib.request.Request(UV_WIN_ZIP_URL, headers={"User-Agent": "BioEM-Preview"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        zip_bytes = resp.read()

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for member in zf.namelist():
            if member.lower().endswith("uv.exe"):
                with zf.open(member) as src, open(local_uv, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                return local_uv

    raise RuntimeError("Downloaded uv archive did not contain uv.exe.")


def _run_uv_step(cmd, env, progress_cb=None, start_frac=0.0, end_frac=1.0, label=""):
    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        **_popen_kwargs(),
    )
    tail = []
    if progress_cb:
        progress_cb(start_frac, label)

    for raw_line in proc.stdout:
        line = raw_line.strip()
        if line:
            tail.append(line)
            if len(tail) > 25:
                tail.pop(0)
            if progress_cb:
                progress_cb(
                    min(end_frac, start_frac + (end_frac - start_frac) * 0.5),
                    f"{label} — {line[:90]}",
                )

    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"{label} failed (exit {rc}):\n" + "\n".join(tail[-8:]))


def install_engine_runtime(variant="auto", progress_cb=None):
    """
    Bootstraps an isolated Python 3.12 + PyTorch + SciPy + NumPy + tifffile
    environment inside %LOCALAPPDATA%/BioEM_Preview/runtime/ using `uv`.
    Returns (venv_python, engine_description).
    """
    if variant == "auto":
        variant = recommended_torch_variant()

    runtime_root = get_runtime_root()
    py_install_dir = os.path.join(runtime_root, "python")
    cache_dir = os.path.join(runtime_root, "uv_cache")
    venv_dir = os.path.join(runtime_root, "venv")
    venv_python = get_managed_venv_python()

    env = os.environ.copy()
    env["UV_PYTHON_INSTALL_DIR"] = py_install_dir
    env["UV_CACHE_DIR"] = cache_dir
    env["UV_LINK_MODE"] = "copy"

    if progress_cb:
        progress_cb(0.03, f"Preparing alignment engine ({variant})...")

    uv_exe = _find_or_download_uv(progress_cb=progress_cb)

    _run_uv_step(
        [uv_exe, "python", "install", "3.12"],
        env=env,
        progress_cb=progress_cb,
        start_frac=0.10,
        end_frac=0.28,
        label="Installing portable Python 3.12",
    )

    _run_uv_step(
        [uv_exe, "venv", "--python", "3.12", "--clear", venv_dir],
        env=env,
        progress_cb=progress_cb,
        start_frac=0.28,
        end_frac=0.35,
        label="Creating isolated runtime environment",
    )

    torch_cmd = [uv_exe, "pip", "install", "--python", venv_python, "torch"]
    index_url = TORCH_INDEX_URLS.get(variant)
    if index_url:
        torch_cmd.extend(["--index-url", index_url])

    _run_uv_step(
        torch_cmd,
        env=env,
        progress_cb=progress_cb,
        start_frac=0.35,
        end_frac=0.78,
        label=f"Installing PyTorch ({variant})",
    )

    _run_uv_step(
        [uv_exe, "pip", "install", "--python", venv_python] + REQUIRED_PACKAGES,
        env=env,
        progress_cb=progress_cb,
        start_frac=0.78,
        end_frac=0.92,
        label="Installing NumPy, SciPy & tifffile",
    )

    if progress_cb:
        progress_cb(0.95, "Cleaning temporary download cache...")
    shutil.rmtree(cache_dir, ignore_errors=True)

    ok, desc = validate_python_engine(venv_python)
    if not ok:
        raise RuntimeError(f"Installed runtime failed validation: {desc}")

    cfg = load_config()
    cfg["engine_python"] = venv_python
    cfg["engine_variant"] = variant
    save_config(cfg)

    if progress_cb:
        progress_cb(1.0, f"Engine ready: {desc}")
    return venv_python, desc


def run_align_subprocess(
    python_exe,
    target_dir,
    project_name,
    rows,
    cols,
    mode,
    overlap_pct,
    fiji_exe,
    open_fiji=True,
    progress_callback=None,
    log_callback=None,
):
    """
    Runs bioem_torch_align.py in the managed/selected Python runtime and
    streams __BIOEM_PROGRESS__ JSON lines back to `progress_callback`.
    Returns the generated TrakEM2 XML path.
    """
    align_script = resource_path("bioem_torch_align.py")
    if not os.path.exists(align_script):
        raise FileNotFoundError(f"Alignment script not found: {align_script}")

    arg_string = f"{target_dir}|{project_name}|{rows}|{cols}|{mode}"
    cmd = [
        python_exe,
        "-u",
        align_script,
        arg_string,
        "--overlap",
        str(overlap_pct),
        "--progress-json",
    ]
    if fiji_exe:
        cmd.extend(["--fiji-exe", fiji_exe])
    if not open_fiji:
        cmd.append("--no-open")

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **_popen_kwargs(),
    )

    xml_path = None
    log_tail = []
    for raw_line in proc.stdout:
        line = raw_line.rstrip("\r\n")
        if line.startswith("__BIOEM_PROGRESS__:"):
            payload = line[len("__BIOEM_PROGRESS__:"):]
            try:
                info = json.loads(payload)
                if progress_callback:
                    progress_callback(info)
            except Exception:
                pass
        elif line.startswith("__BIOEM_RESULT__:"):
            payload = line[len("__BIOEM_RESULT__:"):]
            try:
                res = json.loads(payload)
                xml_path = res.get("xml_path") or xml_path
            except Exception:
                pass
        elif line:
            print(line, flush=True)
            if log_callback:
                try:
                    log_callback(line)
                except Exception:
                    pass
            log_tail.append(line)
            if len(log_tail) > 40:
                log_tail.pop(0)

    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(
            f"Alignment subprocess failed (exit {rc}):\n" + "\n".join(log_tail[-12:])
        )

    if not xml_path:
        xml_path = os.path.join(
            os.path.expanduser("~"), "Desktop", project_name, f"{project_name}.xml"
        ).replace("\\", "/")
    return xml_path
