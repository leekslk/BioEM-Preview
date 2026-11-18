import multiprocessing
import os
import sys

# Must run BEFORE importing GUI/torch libraries so `--indexer` stays lightweight
# and PyInstaller worker processes behave safely on Windows.
if __name__ == "__main__":
    multiprocessing.freeze_support()
    if "--indexer" in sys.argv:
        from nas_indexer import run_indexer_loop
        run_indexer_loop()
        sys.exit(0)

import json
import shutil
import subprocess
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk
import psutil

from bioem_config import (
    DEFAULT_CONFIG,
    config_exists,
    configure_indexer_autostart,
    deploy_fiji_watcher,
    detect_fiji_executable,
    ensure_indexer_running,
    get_cache_file_path,
    get_history_file_path,
    get_sync_cache_root,
    get_trigger_file_path,
    load_config,
    resource_path,
    save_config,
)
from bioem_engine import (
    detect_nvidia_gpus,
    install_engine_runtime,
    recommended_torch_variant,
    resolve_engine_python,
    run_align_subprocess,
    validate_python_engine,
)
from nas_indexer import detect_acquisition_type

# CONFIGURATION DEFAULTS (used to pre-populate the First-Run Setup Wizard)
DESTINATION_DIR = DEFAULT_CONFIG["destination_dir"]
NAS_DIR = DEFAULT_CONFIG["nas_dir"]
APP_PATH = DEFAULT_CONFIG["app_path"]
SCAN_INTERVAL = 2000  # 2 seconds for fast SSD cache polling

# Fluent UI Light theme
ctk.set_appearance_mode("light")
ctk.set_default_color_theme("dark-blue")


def format_duration(seconds):
    """Formats seconds into a readable elapsed/ETA string."""
    seconds = max(0.0, float(seconds))
    if seconds < 60.0:
        return f"{seconds:.1f}s"
    mins, secs = divmod(int(round(seconds)), 60)
    if mins < 60:
        return f"{mins}m {secs:02d}s"
    hrs, mins = divmod(mins, 60)
    return f"{hrs}h {mins:02d}m {secs:02d}s"


class SetupDialog(ctk.CTkToplevel):
    """First-run installation & Settings dialog prompting for paths and the on-demand PyTorch engine."""

    def __init__(self, parent, initial_cfg, is_first_run=False, on_saved=None):
        super().__init__(parent)
        self.on_saved = on_saved
        self.saved = False
        self._installing_engine = False
        self.title("BioEM Preview — First-Run Setup" if is_first_run else "BioEM Preview — Settings")
        self.geometry("720x590")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        header_text = (
            "Welcome to BioEM Preview — Configure Paths & Engine"
            if is_first_run
            else "Application & Pipeline Settings"
        )
        ctk.CTkLabel(
            self, text=header_text, font=("Segoe UI", 18, "bold"), text_color="#111111"
        ).pack(anchor="w", padx=25, pady=(18, 4))

        sub_text = (
            "Select your local scratch disk, raw NAS drive, and Fiji (Java 8) executable.\n"
            "Saving deploys BioEM_Watcher.ijm to Fiji and starts the built-in NAS indexer."
        )
        ctk.CTkLabel(
            self, text=sub_text, font=("Segoe UI", 11), text_color="#555555", justify="left"
        ).pack(anchor="w", padx=25, pady=(0, 12))

        form = ctk.CTkFrame(self, fg_color="transparent")
        form.pack(fill="x", padx=25)
        form.grid_columnconfigure(1, weight=1)

        self.var_dest = tk.StringVar(value=initial_cfg.get("destination_dir", DESTINATION_DIR))
        self.var_nas = tk.StringVar(value=initial_cfg.get("nas_dir", NAS_DIR))
        detected_fiji = detect_fiji_executable(initial_cfg.get("app_path", APP_PATH))
        self.var_fiji = tk.StringVar(value=detected_fiji)
        self.var_engine_py = tk.StringVar(value=initial_cfg.get("engine_python", ""))
        self.var_variant = tk.StringVar(value=initial_cfg.get("engine_variant", "auto"))
        self.var_autostart = tk.BooleanVar(value=bool(initial_cfg.get("auto_start_indexer", True)))

        self._add_path_row(
            form, row=0, label="Scratch Disk (DESTINATION_DIR):",
            var=self.var_dest, browse_cmd=self._browse_dest,
        )
        self._add_path_row(
            form, row=1, label="NAS Raw Data (NAS_DIR):",
            var=self.var_nas, browse_cmd=self._browse_nas,
        )
        self._add_path_row(
            form, row=2, label="Fiji Executable (APP_PATH):",
            var=self.var_fiji, browse_cmd=self._browse_fiji,
        )

        # On-demand Alignment Engine Section
        engine_box = ctk.CTkFrame(self, fg_color="#f5f7fa", corner_radius=8)
        engine_box.pack(fill="x", padx=25, pady=(14, 6))

        ctk.CTkLabel(
            engine_box,
            text="Alignment Engine (PyTorch + SciPy — stored per-user in %LOCALAPPDATA%)",
            font=("Segoe UI", 12, "bold"),
            text_color="#111111",
        ).pack(anchor="w", padx=14, pady=(10, 4))

        self.engine_status_lbl = ctk.CTkLabel(
            engine_box,
            text="Checking alignment engine...",
            font=("Segoe UI", 11),
            text_color="#555555",
            wraplength=640,
            justify="left",
        )
        self.engine_status_lbl.pack(anchor="w", padx=14, pady=(0, 8))

        engine_ctrl = ctk.CTkFrame(engine_box, fg_color="transparent")
        engine_ctrl.pack(fill="x", padx=14, pady=(0, 8))

        ctk.CTkLabel(engine_ctrl, text="Compute Variant:", font=("Segoe UI", 11)).pack(side="left", padx=(0, 6))
        self.opt_variant = ctk.CTkOptionMenu(
            engine_ctrl,
            values=["auto", "cu118", "cu124", "cpu"],
            variable=self.var_variant,
            width=105,
        )
        self.opt_variant.pack(side="left", padx=(0, 12))

        self.btn_install_engine = ctk.CTkButton(
            engine_ctrl,
            text="⬇️ Install / Update Engine",
            width=185,
            fg_color="#0078d4",
            hover_color="#2b88d8",
            command=self._start_engine_install,
        )
        self.btn_install_engine.pack(side="left", padx=(0, 8))

        self.btn_browse_python = ctk.CTkButton(
            engine_ctrl,
            text="Use Existing Python...",
            width=155,
            fg_color="#e5e5e5",
            text_color="black",
            hover_color="#d4d4d4",
            command=self._browse_python,
        )
        self.btn_browse_python.pack(side="left")

        self.engine_progress = ctk.CTkProgressBar(engine_box, progress_color="#0078d4")
        self.engine_progress.set(0)
        self.engine_progress.pack(fill="x", padx=14, pady=(2, 10))

        self.chk_autostart = ctk.CTkCheckBox(
            self,
            text="Start background NAS crawler (--indexer) automatically when logging into Windows",
            variable=self.var_autostart,
            font=("Segoe UI", 12),
        )
        self.chk_autostart.pack(anchor="w", padx=25, pady=(12, 8))

        btn_bar = ctk.CTkFrame(self, fg_color="transparent")
        btn_bar.pack(side="bottom", fill="x", padx=25, pady=16)

        if not is_first_run:
            self.btn_cancel = ctk.CTkButton(
                btn_bar, text="Cancel", width=110, fg_color="#d4d4d4",
                text_color="black", hover_color="#c0c0c0", command=self.destroy,
            )
            self.btn_cancel.pack(side="right", padx=(10, 0))

        self.btn_save = ctk.CTkButton(
            btn_bar, text="Save & Deploy Sidecars", width=190,
            fg_color="#0078d4", hover_color="#2b88d8", font=("Segoe UI", 12, "bold"),
            command=self._save_and_apply,
        )
        self.btn_save.pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self.destroy if not is_first_run else self._cancel_first_run)
        threading.Thread(target=self._probe_engine_async, daemon=True).start()

    def _probe_engine_async(self):
        cfg_probe = {"engine_python": self.var_engine_py.get().strip()}
        py_exe, desc = resolve_engine_python(cfg_probe)
        gpus = detect_nvidia_gpus()
        rec = recommended_torch_variant()
        if py_exe:
            self.var_engine_py.set(py_exe)
            status_txt = f"✅ Ready: {desc}  ({py_exe})"
            color = "#27ae60"
            prog = 1.0
        else:
            gpu_note = f"NVIDIA GPU detected ({', '.join(gpus)}) → recommended: {rec}" if gpus else f"No NVIDIA GPU detected → recommended: {rec}"
            status_txt = f"⚠️ Not installed yet ({gpu_note}). Click 'Install / Update Engine' or select an existing Python."
            color = "#b7791f"
            prog = 0.0
        self.after(0, lambda: (
            self.engine_status_lbl.configure(text=status_txt, text_color=color),
            self.engine_progress.set(prog),
        ))

    def _start_engine_install(self):
        if self._installing_engine:
            return
        self._installing_engine = True
        self.btn_install_engine.configure(state="disabled", text="Installing...")
        self.btn_browse_python.configure(state="disabled")
        self.btn_save.configure(state="disabled")
        self.engine_progress.set(0.02)
        variant = self.var_variant.get()

        def _worker():
            try:
                def _on_prog(frac, msg):
                    self.after(0, lambda f=frac, m=msg: (
                        self.engine_progress.set(f),
                        self.engine_status_lbl.configure(text=m, text_color="#333333"),
                    ))

                py_exe, desc = install_engine_runtime(variant=variant, progress_cb=_on_prog)
                self.var_engine_py.set(py_exe)
                self.after(0, lambda: (
                    self.engine_progress.set(1.0),
                    self.engine_status_lbl.configure(text=f"✅ Installed & verified: {desc}", text_color="#27ae60"),
                ))
            except Exception as exc:
                err = str(exc)
                self.after(0, lambda e=err: (
                    self.engine_progress.set(0),
                    self.engine_status_lbl.configure(text=f"❌ Install failed: {e}", text_color="#e74c3c"),
                    messagebox.showerror("Engine Install Error", e, parent=self),
                ))
            finally:
                self._installing_engine = False
                self.after(0, lambda: (
                    self.btn_install_engine.configure(state="normal", text="⬇️ Install / Update Engine"),
                    self.btn_browse_python.configure(state="normal"),
                    self.btn_save.configure(state="normal"),
                ))

        threading.Thread(target=_worker, daemon=True).start()

    def _browse_python(self):
        filetypes = [("Python Executable", "python*.exe"), ("All Files", "*.*")] if sys.platform == "win32" else [("All Files", "*")]
        chosen = filedialog.askopenfilename(title="Select Python Executable with PyTorch", filetypes=filetypes)
        if not chosen:
            return
        ok, desc = validate_python_engine(chosen)
        if ok:
            self.var_engine_py.set(chosen)
            self.engine_progress.set(1.0)
            self.engine_status_lbl.configure(text=f"✅ Validated: {desc} ({chosen})", text_color="#27ae60")
        else:
            messagebox.showwarning(
                "Missing Packages",
                f"Selected Python cannot import torch/numpy/scipy/tifffile:\n{desc}",
                parent=self,
            )

    def _add_path_row(self, parent, row, label, var, browse_cmd):
        ctk.CTkLabel(parent, text=label, font=("Segoe UI", 12, "bold"), anchor="w").grid(
            row=row, column=0, sticky="w", pady=6, padx=(0, 10)
        )
        ctk.CTkEntry(parent, textvariable=var, font=("Segoe UI", 11)).grid(
            row=row, column=1, sticky="ew", pady=6, padx=(0, 8)
        )
        ctk.CTkButton(
            parent, text="Browse...", width=85, fg_color="#e5e5e5",
            text_color="black", hover_color="#d4d4d4", command=browse_cmd,
        ).grid(row=row, column=2, pady=6)

    def _browse_dest(self):
        chosen = filedialog.askdirectory(title="Select Fast Scratch Disk / Cache Drive", initialdir=self.var_dest.get())
        if chosen:
            self.var_dest.set(chosen)

    def _browse_nas(self):
        chosen = filedialog.askdirectory(title="Select NAS Raw Data Directory", initialdir=self.var_nas.get())
        if chosen:
            self.var_nas.set(chosen)

    def _browse_fiji(self):
        filetypes = [("Executable", "*.exe"), ("All Files", "*.*")] if sys.platform == "win32" else [("All Files", "*")]
        chosen = filedialog.askopenfilename(title="Select Fiji Executable (e.g., fiji-windows-x64.exe)", filetypes=filetypes)
        if chosen:
            self.var_fiji.set(chosen)

    def _cancel_first_run(self):
        if self._installing_engine:
            return
        self._save_and_apply()

    def _save_and_apply(self):
        if self._installing_engine:
            return
        dest_dir = self.var_dest.get().strip() or DESTINATION_DIR
        nas_dir = self.var_nas.get().strip() or NAS_DIR
        fiji_exe = self.var_fiji.get().strip() or APP_PATH
        autostart = bool(self.var_autostart.get())

        cfg = save_config({
            "destination_dir": dest_dir,
            "nas_dir": nas_dir,
            "app_path": fiji_exe,
            "auto_start_indexer": autostart,
            "engine_python": self.var_engine_py.get().strip(),
            "engine_variant": self.var_variant.get().strip() or "auto",
        })

        notices = []
        cache_root = get_sync_cache_root(dest_dir)
        try:
            os.makedirs(cache_root, exist_ok=True)
        except Exception as exc:
            notices.append(f"Cache dir notice ({cache_root}): {exc}")

        ok_watcher, msg_watcher = deploy_fiji_watcher(fiji_exe)
        if not ok_watcher:
            notices.append(msg_watcher)

        ok_auto, msg_auto = configure_indexer_autostart(autostart)
        if not ok_auto:
            notices.append(msg_auto)

        ok_idx, msg_idx = ensure_indexer_running()
        if not ok_idx:
            notices.append(msg_idx)

        if notices:
            messagebox.showwarning("Setup Completed with Notices", "\n\n".join(notices), parent=self)

        self.saved = True
        if self.on_saved:
            self.on_saved(cfg)
        self.destroy()


class NASPuller(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.config = load_config()
        self._is_busy = False
        self._last_cache_mtime = 0
        self.history = []

        self.title("BioEM Preview — NAS Smart Sync & PyTorch Aligner")
        self.geometry("1120x720")

        # TaskBar and MenuBar icon
        try:
            icon_path = resource_path("bioem_app.ico")
            if os.path.exists(icon_path):
                self.iconbitmap(icon_path)
        except Exception as e:
            print(f"Icon Error: {e}")

        # MASTER GRID: 1 Row, 2 Columns (Sidebar + Main Content)
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=1)

        # ZONE 1: THE SIDEBAR (Left)
        self.sidebar = ctk.CTkFrame(self, width=260, corner_radius=0, fg_color="#f3f3f3")
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_rowconfigure(5, weight=1)

        self.logo_label = ctk.CTkLabel(
            self.sidebar, text="BioEM Preview", font=("Segoe UI", 20, "bold"), text_color="#111111"
        )
        self.logo_label.grid(row=0, column=0, padx=20, pady=(30, 30))

        self.btn_nav_history = ctk.CTkButton(
            self.sidebar, text="🕒 Recent Local Syncs",
            font=("Segoe UI", 13), anchor="w", height=40,
            fg_color="transparent", text_color="black", hover_color="#d4d4d4",
            command=self.show_history_view,
        )
        self.btn_nav_history.grid(row=1, column=0, padx=15, pady=5, sticky="ew")

        self.btn_nav_nas = ctk.CTkButton(
            self.sidebar, text="📡 Available on NAS",
            font=("Segoe UI", 13), anchor="w", height=40,
            fg_color="#e5e5e5", text_color="black", hover_color="#e5e5e5",
            command=self.show_nas_view,
        )
        self.btn_nav_nas.grid(row=2, column=0, padx=15, pady=5, sticky="ew")

        self.btn_nav_all = ctk.CTkButton(
            self.sidebar, text="🗄️ All Available Data",
            font=("Segoe UI", 13), anchor="w", height=40,
            fg_color="transparent", text_color="black", hover_color="#d4d4d4",
            command=self.show_all_view,
        )
        self.btn_nav_all.grid(row=3, column=0, padx=15, pady=5, sticky="ew")

        self.btn_refresh = ctk.CTkButton(
            self.sidebar, text="🔄 Force Refresh",
            font=("Segoe UI", 12), height=32,
            fg_color="transparent", text_color="#0078d4", hover_color="#e5e5e5",
            border_width=1, border_color="#0078d4",
            command=self.manual_refresh_scan,
        )
        self.btn_refresh.grid(row=4, column=0, padx=20, pady=(15, 0), sticky="ew")

        # GLOBAL ACTIONS (Pinned to bottom: rows 6, 7, 8)
        self.btn_settings = ctk.CTkButton(
            self.sidebar, text="⚙️ Settings & Paths",
            font=("Segoe UI", 12), height=34, fg_color="#e5e5e5", text_color="black", hover_color="#d4d4d4",
            command=self.open_settings_dialog,
        )
        self.btn_settings.grid(row=6, column=0, padx=20, pady=(10, 8), sticky="ew")

        self.btn_manual = ctk.CTkButton(
            self.sidebar, text="Manually Select Folder",
            font=("Segoe UI", 12), height=36, fg_color="#d4d4d4", text_color="black", hover_color="#c8c8c8",
            command=self.manual_select,
        )
        self.btn_manual.grid(row=7, column=0, padx=20, pady=(0, 10), sticky="ew")

        self.btn_latest = ctk.CTkButton(
            self.sidebar, text="Pull Latest Acquisition",
            font=("Segoe UI", 12, "bold"), height=36,
            fg_color="#0078d4", hover_color="#2b88d8",
            command=self.pull_latest,
        )
        self.btn_latest.grid(row=8, column=0, padx=20, pady=(0, 30), sticky="ew")

        # ZONE 2: MAIN CONTENT AREA (Right)
        self.main_content = ctk.CTkFrame(self, corner_radius=0, fg_color="#ffffff")
        self.main_content.grid(row=0, column=1, sticky="nsew")
        self.main_content.grid_rowconfigure(0, weight=1)
        self.main_content.grid_columnconfigure(0, weight=1)

        self.history_card = ctk.CTkScrollableFrame(self.main_content, fg_color="#ffffff", corner_radius=12)
        self.nas_card = ctk.CTkScrollableFrame(self.main_content, fg_color="#ffffff", corner_radius=12)
        self.all_card = ctk.CTkScrollableFrame(self.main_content, fg_color="#ffffff", corner_radius=12)

        ctk.CTkLabel(
            self.history_card, text="Recent Local Syncs", font=("Segoe UI", 24, "bold"), text_color="black"
        ).pack(anchor="w", padx=30, pady=(30, 20))
        ctk.CTkLabel(
            self.nas_card, text="Available on NAS (Last 14 Days)", font=("Segoe UI", 24, "bold"), text_color="black"
        ).pack(anchor="w", padx=30, pady=(30, 20))
        ctk.CTkLabel(
            self.all_card, text="All Available Data", font=("Segoe UI", 24, "bold"), text_color="black"
        ).pack(anchor="w", padx=30, pady=(30, 20))

        # STATUS FOOTER
        self.footer_card = ctk.CTkFrame(self.main_content, corner_radius=0, fg_color="#f9f9f9", height=125)
        self.footer_card.grid(row=1, column=0, sticky="ew")

        self.progress = ctk.CTkProgressBar(self.footer_card, progress_color="#0078d4")
        self.progress.set(0)
        self.progress.pack(fill="x", padx=40, pady=(18, 6))

        self.status = ctk.CTkLabel(self.footer_card, text="System Ready.", font=("Segoe UI", 11, "bold"), text_color="#333333")
        self.status.pack(pady=(0, 3))

        self.latest_path_label = ctk.CTkLabel(
            self.footer_card, text="Scanning...", font=("Segoe UI", 11, "italic"), text_color="#777777"
        )
        self.latest_path_label.pack(pady=(0, 14))

        self.nas_rows = []
        self.nas_buttons = []
        self.history_rows = []
        self.history_buttons = []
        self.all_rows = []
        self.all_buttons = []

        self.load_history()
        self.render_nas_buttons()
        self.render_history_buttons()
        self.show_nas_view()

        # First-run setup wizard or silent startup initialization
        self.after(150, self._startup_initialize)
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

    def _startup_initialize(self):
        if not config_exists():
            SetupDialog(self, self.config, is_first_run=True, on_saved=self._on_config_updated)
        else:
            try:
                os.makedirs(get_sync_cache_root(self.config.get("destination_dir")), exist_ok=True)
            except Exception:
                pass
            deploy_fiji_watcher(self.config.get("app_path"))
            ensure_indexer_running()

        self.launch_background_scan()
        threading.Thread(target=self._run_housekeeping, daemon=True).start()

    def _on_config_updated(self, new_cfg):
        self.config = new_cfg
        self._last_cache_mtime = 0
        self.status.configure(
            text=f"Configured | Scratch: {new_cfg['destination_dir']} | NAS: {new_cfg['nas_dir']}",
            text_color="#0078d4",
        )
        self.manual_refresh_scan()

    def open_settings_dialog(self):
        SetupDialog(self, self.config, is_first_run=False, on_saved=self._on_config_updated)

    def on_closing(self):
        self.destroy()
        os._exit(0)

    # Sidebar Navigation Logic
    def show_history_view(self):
        self.nas_card.grid_forget()
        self.all_card.grid_forget()
        self.history_card.grid(row=0, column=0, sticky="nsew")
        self.btn_nav_history.configure(fg_color="#e5e5e5")
        self.btn_nav_nas.configure(fg_color="transparent")
        self.btn_nav_all.configure(fg_color="transparent")

    def show_nas_view(self):
        self.history_card.grid_forget()
        self.all_card.grid_forget()
        self.nas_card.grid(row=0, column=0, sticky="nsew")
        self.btn_nav_nas.configure(fg_color="#e5e5e5")
        self.btn_nav_history.configure(fg_color="transparent")
        self.btn_nav_all.configure(fg_color="transparent")

    def show_all_view(self):
        self.history_card.grid_forget()
        self.nas_card.grid_forget()
        self.all_card.grid(row=0, column=0, sticky="nsew")
        self.btn_nav_all.configure(fg_color="#e5e5e5")
        self.btn_nav_history.configure(fg_color="transparent")
        self.btn_nav_nas.configure(fg_color="transparent")

    # Manual & Background Scan
    def manual_refresh_scan(self):
        self._last_cache_mtime = 0
        self.btn_refresh.configure(text="⏳ Scanning...", state="disabled")
        if not self._is_busy:
            self.status.configure(text="Manual scan initiated...", text_color="#333333")
        threading.Thread(target=self._manual_refresh_worker, daemon=True).start()

    def _manual_refresh_worker(self):
        try:
            nas_dir = self.config.get("nas_dir", NAS_DIR)
            latest = self.get_latest_folder(nas_dir)
            if not self._is_busy:
                if latest:
                    parts = [p for p in latest.replace("\\", "/").split("/") if p]
                    display_path = ".../" + "/".join(parts[-3:]) if len(parts) > 3 else latest
                    self.after(0, lambda: self.latest_path_label.configure(text=f"Detected: {display_path}", text_color="gray"))
                    self.after(0, lambda: self.status.configure(text="Manual Scan Complete.", text_color="#333333"))
                else:
                    self.after(0, lambda: self.latest_path_label.configure(text="No .tif in LayersData found.", text_color="gray"))
                    self.after(0, lambda: self.status.configure(text="Ready (No acquisitions found)", text_color="#333333"))
        except Exception as e:
            print(f"Manual scan error: {e}")
            self.after(0, lambda: self.status.configure(text="Scan Error Encountered", text_color="red"))
        finally:
            self.after(0, self.render_nas_buttons)
            self.after(0, lambda: self.btn_refresh.configure(text="🔄 Force Refresh", state="normal"))

    def launch_background_scan(self):
        threading.Thread(target=self._run_background_scan, daemon=True).start()

    def _run_background_scan(self):
        cache_file = get_cache_file_path()
        try:
            if os.path.exists(cache_file):
                current_mtime = os.path.getmtime(cache_file)
                if current_mtime > self._last_cache_mtime:
                    self._last_cache_mtime = current_mtime
                    self.after(0, self.render_nas_buttons)

                    with open(cache_file, "r", encoding="utf-8") as f:
                        data = json.load(f)

                    if not self._is_busy:
                        latest = data.get("latest_folder") if isinstance(data, dict) else None
                        if latest:
                            parts = [p for p in latest.replace("\\", "/").split("/") if p]
                            display_path = ".../" + "/".join(parts[-3:]) if len(parts) > 3 else latest
                            self.after(0, lambda dp=display_path: self.latest_path_label.configure(
                                text=f"Detected: {dp}", text_color="gray"
                            ))
                            self.after(0, lambda: self.status.configure(text="Ready.", text_color="#333333"))
                        else:
                            self.after(0, lambda: self.latest_path_label.configure(
                                text="No acquisitions found in cache.", text_color="gray"
                            ))
                            self.after(0, lambda: self.status.configure(text="Ready.", text_color="#333333"))
        except Exception as e:
            print(f"Background check error: {e}")
        finally:
            self.after(SCAN_INTERVAL, self.launch_background_scan)

    def _run_housekeeping(self):
        """Scans BioEM_Cache inside DESTINATION_DIR and removes cached acquisitions older than 30 days."""
        try:
            seconds_in_30_days = 30 * 24 * 60 * 60
            cutoff_time = time.time() - seconds_in_30_days
            cache_root = get_sync_cache_root(self.config.get("destination_dir"))
            if not os.path.exists(cache_root):
                return

            for item in os.listdir(cache_root):
                item_path = os.path.join(cache_root, item)
                if not os.path.isdir(item_path):
                    continue
                try:
                    if os.path.getmtime(item_path) < cutoff_time:
                        shutil.rmtree(item_path)
                except Exception:
                    continue
        except Exception:
            pass

    # File Detection Logic
    def get_latest_folder(self, base_path):
        cache_file = get_cache_file_path()
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                cached_path = data.get("latest_folder")
                cache_time = data.get("timestamp", 0)
                if time.time() - cache_time < 7200 and cached_path and os.path.exists(cached_path):
                    return cached_path
            except Exception:
                pass
        return self.find_latest_acquisition_fast(base_path)

    def find_latest_acquisition_fast(self, root_dir):
        search_root = os.path.join(root_dir, "Maps") if os.path.exists(os.path.join(root_dir, "Maps")) else root_dir
        if not os.path.exists(search_root):
            return None

        cutoff_time = time.time() - (14 * 24 * 60 * 60)
        found = []
        try:
            for root, dirs, files in os.walk(search_root, topdown=True):
                if "electron snapshot" in root.lower():
                    dirs[:] = []
                    continue
                norm_root = root.replace("\\", "/").lower()
                if "/layersdata/" in norm_root or norm_root.endswith("/layersdata"):
                    tifs = [f for f in files if f.lower().endswith((".tif", ".tiff"))]
                    if tifs:
                        try:
                            folder_mtime = max(os.path.getmtime(os.path.join(root, f)) for f in tifs)
                            if folder_mtime > cutoff_time:
                                found.append((folder_mtime, root))
                                dirs[:] = []
                                continue
                        except Exception:
                            pass
            if found:
                found.sort(key=lambda x: x[0], reverse=True)
                return found[0][1]
        except Exception as e:
            print(f"Scan error: {e}")
        return None

    # History & Breadcrumbs
    def load_history(self):
        history_file = get_history_file_path()
        if os.path.exists(history_file):
            try:
                with open(history_file, "r", encoding="utf-8") as f:
                    self.history = json.load(f)
            except Exception:
                self.history = []
        else:
            self.history = []

    def save_history(self, path):
        if path in self.history:
            self.history.remove(path)
        self.history.insert(0, path)
        self.history = self.history[:21]
        with open(get_history_file_path(), "w", encoding="utf-8") as f:
            json.dump(self.history, f)
        self.render_history_buttons()

    def remove_from_history(self, path):
        if path in self.history:
            self.history.remove(path)
            try:
                with open(get_history_file_path(), "w", encoding="utf-8") as f:
                    json.dump(self.history, f)
            except Exception as e:
                print(f"Error updating history file: {e}")
            self.render_history_buttons()

    def _extract_project_and_folder(self, source_folder):
        """Extracts (project_name, safe_folder_name, display_title) consistently from a NAS/source path."""
        try:
            path_parts = [p for p in source_folder.replace("\\", "/").split("/") if p]
            ld_index = next((i for i, p in enumerate(path_parts) if "layersdata" in p.lower()), -1)
            project_name = path_parts[ld_index - 1] if ld_index > 0 else "Unknown_Project"
            tile_name = path_parts[-1]
            display_title = f"{project_name} ➔ {tile_name}"
            raw_folder_name = f"{project_name}_{tile_name}".replace(" ", "_")
            safe_folder_name = "".join(c for c in raw_folder_name if c.isalnum() or c in ["_", "-"])
            safe_project_name = "".join(c for c in project_name.replace(" ", "_") if c.isalnum() or c in ["_", "-"]) or "EM_Project"
            return safe_project_name, safe_folder_name, display_title
        except Exception:
            base = os.path.basename(source_folder.rstrip("\\/")) or "EM_Project"
            safe = "".join(c for c in base.replace(" ", "_") if c.isalnum() or c in ["_", "-"]) or "EM_Project"
            return safe, safe, base

    def format_breadcrumb(self, path, prefix=""):
        try:
            parts = [p for p in path.replace("\\", "/").split("/") if p]
            maps_index = next((idx for idx, p in enumerate(parts) if p.lower() == "maps"), -1)
            owner = parts[maps_index + 1] if (maps_index != -1 and maps_index + 1 < len(parts)) else ""
            ld_index = next((idx for idx, p in enumerate(parts) if "layersdata" in p.lower()), -1)
            if ld_index > 0:
                project = parts[ld_index - 1]
                tail = " / ".join(parts[ld_index + 1:])
                if owner and tail:
                    return f"{prefix} {project} ➔ {owner} / {tail}"
                if tail:
                    return f"{prefix} {project} ➔ {tail}"
                return f"{prefix} {project}"
            return f"{prefix} {parts[-1]}"
        except Exception:
            return f"{prefix} {os.path.basename(path)}"

    def _create_badge_and_button(self, parent_container, item_path, acq_type="z-stack", rows=1, cols=1, prefix="📡", is_history=False):
        btn_text = self.format_breadcrumb(item_path, prefix=prefix)
        row_frame = ctk.CTkFrame(parent_container, fg_color="transparent")
        row_frame.pack(fill="x", padx=30, pady=3)

        if acq_type == "mosaic-z":
            badge_text = f"Mosaic Z ({rows}x{cols})"
            badge_bg = "#8e44ad"  # Fluent Purple
            badge_width = 115
        elif acq_type == "mosaic":
            badge_text = f"Mosaic ({rows}x{cols})"
            badge_bg = "#e67e22"  # Fluent Orange
            badge_width = 115
        else:
            badge_text = "Z-Stack"
            badge_bg = "#2ecc71"  # Fluent Green
            badge_width = 115

        ctk.CTkLabel(
            row_frame,
            text=badge_text,
            fg_color=badge_bg,
            text_color="#ffffff",
            corner_radius=6,
            font=("Segoe UI", 9, "bold"),
            width=badge_width,
            height=24,
        ).pack(side="left", padx=(0, 8))

        btn_fg = "transparent" if is_history else "#f0f0f0"
        btn_border = 1 if is_history else 0
        btn_border_col = "#d0d0d0" if is_history else None
        text_col = "#444444" if is_history else "black"

        btn = ctk.CTkButton(
            row_frame,
            text=btn_text,
            height=32,
            font=("Segoe UI", 12),
            fg_color=btn_fg,
            hover_color="#e0e0e0" if not is_history else "#f0f0f0",
            text_color=text_col,
            border_width=btn_border,
            border_color=btn_border_col,
            anchor="w",
            corner_radius=6,
            command=lambda p=item_path, t=acq_type, r=rows, c=cols: self.run_sync(p, acq_type=t, rows=r, cols=c),
        )
        btn.pack(side="left", fill="x", expand=True)

        if is_history:
            btn.bind("<Button-3>", lambda event, p=item_path: self.show_history_context_menu(event, p))
        else:
            btn.bind("<Button-3>", lambda event, p=item_path: self.show_context_menu(event, p))

        return row_frame, btn

    def render_nas_buttons(self):
        for row in getattr(self, "nas_rows", []):
            row.destroy()
        for row in getattr(self, "all_rows", []):
            row.destroy()

        self.nas_rows = []
        self.nas_buttons = []
        self.all_rows = []
        self.all_buttons = []

        cache_path = get_cache_file_path()
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                acquisitions = data.get("acquisitions", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])

                cutoff_time = time.time() - (14 * 24 * 60 * 60)
                within_14_days_count = sum(
                    1 for item in acquisitions
                    if (item.get("mtime", 0) if isinstance(item, dict) else 0) >= cutoff_time
                )
                split_index = max(within_14_days_count, 12)
                nas_items = acquisitions[:split_index]
                all_items = acquisitions[split_index:]

                for item in nas_items:
                    path = item.get("path") if isinstance(item, dict) else item
                    if not path:
                        continue
                    acq_type = item.get("type", "z-stack") if isinstance(item, dict) else "z-stack"
                    rows = item.get("rows", 1) if isinstance(item, dict) else 1
                    cols = item.get("cols", 1) if isinstance(item, dict) else 1
                    row_frame, btn = self._create_badge_and_button(
                        self.nas_card, path, acq_type=acq_type, rows=rows, cols=cols, prefix="📡", is_history=False
                    )
                    self.nas_rows.append(row_frame)
                    self.nas_buttons.append(btn)

                for item in all_items:
                    path = item.get("path") if isinstance(item, dict) else item
                    if not path:
                        continue
                    acq_type = item.get("type", "z-stack") if isinstance(item, dict) else "z-stack"
                    rows = item.get("rows", 1) if isinstance(item, dict) else 1
                    cols = item.get("cols", 1) if isinstance(item, dict) else 1
                    row_frame, btn = self._create_badge_and_button(
                        self.all_card, path, acq_type=acq_type, rows=rows, cols=cols, prefix="📁", is_history=False
                    )
                    self.all_rows.append(row_frame)
                    self.all_buttons.append(btn)
            except Exception as e:
                print(f"Error rendering NAS buttons: {e}")

        if self._is_busy:
            self._set_action_buttons_state("disabled")

    def render_history_buttons(self):
        for row in getattr(self, "history_rows", []):
            row.destroy()
        self.history_rows = []
        self.history_buttons = []

        meta_lookup = {}
        cache_path = get_cache_file_path()
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    cdata = json.load(f)
                acqs = cdata.get("acquisitions", []) if isinstance(cdata, dict) else cdata
                for a in acqs:
                    if isinstance(a, dict) and "path" in a:
                        meta_lookup[a["path"].replace("\\", "/").lower()] = a
            except Exception:
                pass

        for path in reversed(self.history):
            if not path:
                continue
            item_meta = meta_lookup.get(path.replace("\\", "/").lower(), {})
            row_frame, btn = self._create_badge_and_button(
                self.history_card,
                path,
                acq_type=item_meta.get("type", "z-stack"),
                rows=item_meta.get("rows", 1),
                cols=item_meta.get("cols", 1),
                prefix="🕒",
                is_history=True,
            )
            self.history_rows.append(row_frame)
            self.history_buttons.append(btn)

        if self._is_busy:
            self._set_action_buttons_state("disabled")

    # Context Menus & Local Data Management
    def show_context_menu(self, event, path):
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="🔄 Refresh Folder (Wipe & Sync)", command=lambda p=path: self.confirm_refresh(p))
        menu.add_command(label="🗑️ Delete Local Cache", command=lambda p=path: self.show_delete_modal(p))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def show_history_context_menu(self, event, path):
        menu = tk.Menu(self, tearoff=0, font=("Arial", 10))
        menu.add_command(label="🔄 Refresh Folder (Wipe & Sync)", command=lambda p=path: self.confirm_refresh(p))
        menu.add_command(label="🗑️ Delete Local Cache", command=lambda p=path: self.show_delete_modal(p))
        menu.add_separator()
        menu.add_command(label="❌ Remove from History", command=lambda p=path: self.remove_from_history(p))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def show_delete_modal(self, source_path):
        project_name, safe_folder_name, _ = self._extract_project_and_folder(source_path)
        modal = ctk.CTkToplevel(self)
        modal.title("Warning: Destructive Action")
        modal.geometry("520x240")
        modal.resizable(False, False)
        modal.transient(self)
        modal.grab_set()

        ctk.CTkLabel(
            modal, text="🚨 WARNING: DESTRUCTIVE ACTION 🚨",
            font=ctk.CTkFont(size=15, weight="bold"), text_color="#e74c3c",
        ).pack(pady=(18, 6))

        ctk.CTkLabel(
            modal,
            text="This will purge the synchronized local cache for this dataset on your scratch disk.\n"
                 "(Original raw data on the NAS is NOT affected.)",
            font=ctk.CTkFont(size=12), justify="center", wraplength=480,
        ).pack(pady=4)

        ctk.CTkLabel(
            modal, text=f"Cached Folder: {safe_folder_name}",
            font=ctk.CTkFont(size=13, weight="bold"), text_color="#333333",
        ).pack(pady=(8, 0))

        btn_frame = ctk.CTkFrame(modal, fg_color="transparent")
        btn_frame.pack(side="bottom", pady=20)

        def on_delete():
            modal.destroy()
            self.execute_delete_dataset(safe_folder_name)

        ctk.CTkButton(
            btn_frame, text="Yes, remove local cache", fg_color="#c0392b", hover_color="#e74c3c",
            text_color="#ffffff", font=ctk.CTkFont(weight="bold"), command=on_delete,
        ).pack(side="left", padx=12)

        ctk.CTkButton(
            btn_frame, text="Cancel", fg_color="#7f8c8d", hover_color="#95a5a6", command=modal.destroy,
        ).pack(side="left", padx=12)

    def execute_delete_dataset(self, safe_folder_name):
        dest_dir = self.config.get("destination_dir", DESTINATION_DIR)
        candidates = [
            os.path.join(get_sync_cache_root(dest_dir), safe_folder_name),
            os.path.join(dest_dir, safe_folder_name),  # Legacy Alpha root path
        ]
        purged = False
        for target_dir in candidates:
            if os.path.exists(target_dir):
                shutil.rmtree(target_dir, ignore_errors=True)
                purged = True

        if purged:
            self.status.configure(text=f"🗑️ Purged local cache: {safe_folder_name}", text_color="#e74c3c")
            self.render_nas_buttons()
            self.render_history_buttons()
        else:
            self.status.configure(text="No local cache found on scratch disk to delete.", text_color="#f39c12")

    def confirm_refresh(self, path):
        dialog = ctk.CTkToplevel(self)
        dialog.title("Warning: Destructive Action")
        dialog.geometry("460x220")
        dialog.attributes("-topmost", True)
        dialog.transient(self)
        dialog.grab_set()

        warn_text = (
            "This action will wipe previous cached files on this computer and\n"
            "re-sync the latest content from the NAS.\n\n"
            "Are you sure you want to continue?"
        )
        ctk.CTkLabel(dialog, text=warn_text, font=("Segoe UI", 12)).pack(pady=(20, 15), padx=20)

        ctk.CTkButton(
            dialog, text="Yes, wipe and re-sync",
            fg_color="#c0392b", hover_color="#e74c3c", font=("Segoe UI", 12, "bold"),
            command=lambda: self.execute_wipe_and_resync(path, dialog),
        ).pack(pady=5)

        ctk.CTkButton(
            dialog, text="Cancel", fg_color="#34495e", hover_color="#2c3e50",
            font=("Segoe UI", 11), command=dialog.destroy,
        ).pack(pady=5)

    def execute_wipe_and_resync(self, source_folder, dialog):
        dialog.destroy()
        _, safe_folder_name, _ = self._extract_project_and_folder(source_folder)
        dest_dir = self.config.get("destination_dir", DESTINATION_DIR)
        for final_dest in [
            os.path.join(get_sync_cache_root(dest_dir), safe_folder_name),
            os.path.join(dest_dir, safe_folder_name),
        ]:
            if os.path.exists(final_dest):
                try:
                    self.status.configure(text=f"Wiping local folder: {safe_folder_name}...", text_color="red")
                    self.update()
                    shutil.rmtree(final_dest)
                    time.sleep(0.3)
                except Exception as e:
                    messagebox.showerror(
                        "Wipe Error",
                        f"Could not delete local folder. Ensure no files are open in Fiji.\nError: {e}",
                    )
                    return
        self.run_sync(source_folder)

    # Pull Latest & Manual Select
    def pull_latest(self):
        cache_path = get_cache_file_path()
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                acqs = data.get("acquisitions", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
                if acqs and isinstance(acqs[0], dict):
                    latest_path = acqs[0].get("path")
                    if latest_path and os.path.exists(latest_path):
                        self.run_sync(
                            latest_path,
                            acq_type=acqs[0].get("type", "auto"),
                            rows=acqs[0].get("rows", 1),
                            cols=acqs[0].get("cols", 1),
                        )
                        return
                latest_path = data.get("latest_folder") if isinstance(data, dict) else None
                if latest_path and os.path.exists(latest_path):
                    self.run_sync(latest_path, acq_type="auto")
                    return
            except Exception as e:
                print(f"Error reading latest acquisition: {e}")

        latest = self.get_latest_folder(self.config.get("nas_dir", NAS_DIR))
        if latest:
            self.run_sync(latest, acq_type="auto")
        else:
            messagebox.showwarning("Not Found", "Could not detect a valid acquisition.")

    def manual_select(self):
        nas_dir = self.config.get("nas_dir", NAS_DIR)
        maps_dir = os.path.join(nas_dir, "Maps")
        initial = maps_dir if os.path.exists(maps_dir) else nas_dir
        source = filedialog.askdirectory(title="Select NAS Folder", initialdir=initial)
        if source:
            self.run_sync(source, acq_type="auto")

    # Fiji Virtual Stack Launch (Workflow 1: Z-Stack)
    def launch_fiji_virtual_stack(self, folder_path, project_name="EM_Project"):
        app_path = self.config.get("app_path", APP_PATH)
        if not app_path or not os.path.exists(app_path):
            raise FileNotFoundError(f"Fiji executable not found at: {app_path}\nUpdate it in Settings.")

        deploy_fiji_watcher(app_path)
        formatted_path = folder_path.replace("\\", "/")
        trigger_path = get_trigger_file_path()
        payload = f"{formatted_path}|{project_name}|z-stack|1|1"
        with open(trigger_path, "w", encoding="utf-8") as f:
            f.write(payload)

        exe_basename = os.path.basename(app_path).lower()
        fiji_active = False
        for proc in psutil.process_iter(["name"]):
            try:
                pname = (proc.info.get("name") or "").lower()
                if (
                    pname == exe_basename
                    or "fiji-windows-x64" in pname
                    or "imagej-win64" in pname
                    or pname == "fiji.exe"
                ):
                    fiji_active = True
                    break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        if fiji_active:
            self.after(0, lambda: self.status.configure(
                text="✅ Z-Stack synced. Alerted active Fiji instance (Virtual Stack).", text_color="#2ecc71"
            ))
        else:
            self.after(0, lambda: self.status.configure(
                text="✅ Z-Stack synced. Launching Fiji (Virtual Stack)...", text_color="#0078d4"
            ))
            fiji_dir = os.path.dirname(os.path.abspath(app_path))
            kwargs = {"cwd": fiji_dir, "shell": False, "close_fds": True}
            if sys.platform == "win32":
                kwargs["creationflags"] = 0x00000008 | 0x00000200
            subprocess.Popen([app_path], **kwargs)

    # Core Sync + Alignment Orchestrator
    def _set_action_buttons_state(self, state):
        all_action_buttons = (
            self.history_buttons
            + self.nas_buttons
            + self.all_buttons
            + [self.btn_manual, self.btn_latest, self.btn_settings]
        )
        for b in all_action_buttons:
            try:
                b.configure(state=state)
            except Exception:
                pass

    def run_sync(self, source_folder, acq_type="auto", rows=1, cols=1):
        if self._is_busy:
            return
        if not os.path.exists(source_folder):
            messagebox.showerror("Source Not Found", f"Source folder does not exist:\n{source_folder}")
            return

        self._is_busy = True
        self._set_action_buttons_state("disabled")
        self.progress.set(0)

        worker = threading.Thread(
            target=self._sync_and_process_worker,
            args=(source_folder, acq_type, rows, cols),
            daemon=True,
        )
        worker.start()

    def _sync_and_process_worker(self, source_folder, acq_type, rows, cols):
        project_name, safe_folder_name, display_title = self._extract_project_and_folder(source_folder)
        dest_root = get_sync_cache_root(self.config.get("destination_dir", DESTINATION_DIR))
        final_dest = os.path.join(dest_root, safe_folder_name)

        try:
            self.after(0, lambda: self.latest_path_label.configure(
                text=f"Syncing: {display_title}", text_color="#3498db"
            ))
            self.after(0, lambda: self.status.configure(text="Scanning source .tif files...", text_color="#333333"))

            os.makedirs(final_dest, exist_ok=True)

            all_files = []
            for root, _, files in os.walk(source_folder):
                for fname in files:
                    if fname.lower().endswith((".tif", ".tiff")):
                        all_files.append(os.path.join(root, fname))

            total_files = len(all_files)
            if total_files == 0:
                self.after(0, lambda: messagebox.showinfo("No Images Found", "This folder contains no .tif/.tiff files."))
                return

            # Auto-detect acquisition type when invoked manually or via fallback
            if acq_type in (None, "", "auto"):
                detected = detect_acquisition_type([os.path.basename(f) for f in all_files])
                acq_type = detected["type"]
                rows = detected["rows"]
                cols = detected["cols"]

            files_processed = 0
            files_skipped = 0
            files_copied = 0
            t_sync_start = time.time()

            for s_file in all_files:
                rel = os.path.relpath(s_file, source_folder)
                d_file = os.path.join(final_dest, rel)
                os.makedirs(os.path.dirname(d_file), exist_ok=True)

                should_copy = True
                if os.path.exists(d_file):
                    s_stat = os.stat(s_file)
                    d_stat = os.stat(d_file)
                    if s_stat.st_size == d_stat.st_size and abs(s_stat.st_mtime - d_stat.st_mtime) < 1e-3:
                        should_copy = False

                if should_copy:
                    shutil.copy2(s_file, d_file)
                    files_copied += 1
                else:
                    files_skipped += 1

                files_processed += 1
                if files_processed % 5 == 0 or files_processed == total_files:
                    frac = files_processed / float(total_files)
                    elapsed = time.time() - t_sync_start
                    eta = (elapsed / files_processed) * (total_files - files_processed)
                    msg = (
                        f"[Sync] {files_processed}/{total_files} ({frac * 100:.1f}%) | "
                        f"Copied: {files_copied}, Skipped: {files_skipped} | "
                        f"Elapsed: {format_duration(elapsed)} | Remaining: {format_duration(eta)}"
                    )
                    self.after(0, lambda f=frac, m=msg: (
                        self.progress.set(f),
                        self.status.configure(text=m, text_color="#333333"),
                    ))

            self.after(0, lambda: self.save_history(source_folder))

            # WORKFLOW 1: Simple Z-Stack -> Fiji Virtual Stack
            if acq_type == "z-stack":
                self.launch_fiji_virtual_stack(final_dest, project_name=project_name)
                stamp = time.strftime("%H:%M")
                self.after(0, lambda: self.latest_path_label.configure(
                    text=f"Last Sync (Z-Stack): {display_title} [{stamp}]", text_color="#2ecc71"
                ))
                return

            # WORKFLOW 2: Mosaic / Mosaic Z-Stack -> PyTorch Aligner -> TrakEM2 XML -> Fiji
            self.after(0, lambda: (
                self.progress.set(0.02),
                self.latest_path_label.configure(
                    text=f"Aligning ({acq_type}, {rows}x{cols}): {display_title}", text_color="#8e44ad"
                ),
                self.status.configure(text="Checking PyTorch alignment engine...", text_color="#333333"),
            ))

            engine_python, engine_desc = resolve_engine_python(self.config)
            if not engine_python:
                prompt_done = threading.Event()
                should_install = [False]
                variant = self.config.get("engine_variant", "auto")
                rec_variant = recommended_torch_variant() if variant == "auto" else variant

                def _ask_install():
                    should_install[0] = messagebox.askyesno(
                        "Alignment Engine Required",
                        "The PyTorch alignment engine is not installed yet.\n\n"
                        f"Download and install an isolated Python + PyTorch ({rec_variant}) runtime "
                        "into %LOCALAPPDATA%\\BioEM_Preview\\runtime now?\n\n"
                        "(Requires internet once; takes a few minutes.)",
                        parent=self,
                    )
                    prompt_done.set()

                self.after(0, _ask_install)
                prompt_done.wait()

                if not should_install[0]:
                    self.after(0, lambda: (
                        self.status.configure(
                            text="Sync complete. Install Alignment Engine in Settings to run Mosaic alignment.",
                            text_color="#b7791f",
                        ),
                        self.open_settings_dialog(),
                    ))
                    return

                def _on_install_prog(frac, msg):
                    self.after(0, lambda f=frac, m=msg: (
                        self.progress.set(f),
                        self.status.configure(text=m, text_color="#333333"),
                    ))

                engine_python, engine_desc = install_engine_runtime(
                    variant=variant, progress_cb=_on_install_prog
                )
                self.config = load_config()

            self.after(0, lambda d=engine_desc: self.status.configure(
                text=f"Starting BioEM PyTorch Aligner ({d})...", text_color="#333333"
            ))

            def on_align_progress(info):
                frac = info.get("progress", 0.0)
                msg = info.get("message", "Aligning...")
                self.after(0, lambda f=frac, m=msg: (
                    self.progress.set(f),
                    self.status.configure(text=m, text_color="#333333"),
                ))

            xml_path = run_align_subprocess(
                python_exe=engine_python,
                target_dir=final_dest,
                project_name=project_name,
                rows=rows,
                cols=cols,
                mode=acq_type,
                overlap_pct=float(self.config.get("overlap_pct", 10.0)),
                fiji_exe=self.config.get("app_path", APP_PATH),
                open_fiji=True,
                progress_callback=on_align_progress,
            )

            stamp = time.strftime("%H:%M")
            self.after(0, lambda: (
                self.progress.set(1.0),
                self.status.configure(text=f"✅ Aligned & opened in Fiji: {os.path.basename(xml_path)}", text_color="#2ecc71"),
                self.latest_path_label.configure(text=f"Completed: {display_title} [{stamp}]", text_color="#2ecc71"),
            ))

        except Exception as exc:
            err_msg = str(exc)
            self.after(0, lambda m=err_msg: (
                self.status.configure(text=f"Error: {m}", text_color="#e74c3c"),
                messagebox.showerror("Sync / Alignment Error", m),
            ))
        finally:
            self._is_busy = False
            self.after(0, lambda: self._set_action_buttons_state("normal"))


if __name__ == "__main__":
    app = NASPuller()
    app.mainloop()
