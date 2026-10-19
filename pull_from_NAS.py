import sys
import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox
import shutil
import os
import subprocess
import tempfile
import psutil
import time
import threading
import json

# CONFIGURATION 
DESTINATION_DIR = "D:/" # Points to the local high-speed SSD
NAS_DIR = "I:/"  # Points to the NAS
APP_PATH = r"C:\Users\User\Desktop\Fiji\fiji-windows-x64.exe" # Point to the .exe inside the Fiji folder
PROCESS_NAME = "fiji-windows-x64.exe" # Points to the process name in Task Manager (this might fail)
HISTORY_FILE = os.path.join(os.getenv('APPDATA'), "fiji_sync_history.json")
SCAN_INTERVAL = 2000 # 2 seconds for fast SSD cache polling
# 

# 2026JUN05: Fluent UI update, forces the app into Dark Mode regardless of Windows system settings
ctk.set_appearance_mode("light") 

# set a default accent color here (blue, green, dark-blue)
ctk.set_default_color_theme("dark-blue") 

# FLUENT DESIGN CHEAT SHEET (For buttons and frames later)
# Main Window Background: Default CustomTkinter Light
# Card Container (fg_color): "#2d2d2d" or "#202020"
# Primary Buttons (fg_color): "#0078d4" (Windows 11 Accent Blue)
# Hover States (hover_color): "#2b88d8"
# Corner Radius: 8 for Cards, 4 or 6 for Buttons
# add refresh button for the impatient

class NASPuller(ctk.CTk): #2026JUN05 Fluent UI refresh
    def __init__(self): 
        super().__init__()
        
        # main window setup
        self.title("NAS Smart Sync for Fiji")
        self.geometry("1100x700") 
        
        # TaskBar and MenuBar icon
        try:
            if getattr(sys, 'frozen', False): 
                base_dir = sys._MEIPASS
            else: 
                base_dir = os.path.dirname(os.path.abspath(__file__))
                
            # Using your corrected filename!
            icon_path = os.path.join(base_dir, "bioem_app.ico")
            
            # This single command overwrites the CustomTkinter blue square
            self.iconbitmap(icon_path)
            
        except Exception as e: 
            print(f"Icon Error: {e}")
        
        # MASTER GRID: 1 Row, 2 Columns (Sidebar + Main Content)
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=0) # Sidebar does NOT stretch
        self.grid_columnconfigure(1, weight=1) # Main Content stretches to fill space

        # ZONE 1: THE SIDEBAR (Left)
        self.sidebar = ctk.CTkFrame(self, width=260, corner_radius=0, fg_color="#f3f3f3")
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        
        # We moved the flexible empty space from row 4 down to row 5 to make room!
        self.sidebar.grid_rowconfigure(5, weight=1) 

        # Title / Branding
        self.logo_label = ctk.CTkLabel(self.sidebar, text="NAS Smart Sync", font=("Segoe UI", 20, "bold"), text_color="#111111")
        self.logo_label.grid(row=0, column=0, padx=20, pady=(30, 30))

        # NAVIGATION MENUS
        self.btn_nav_history = ctk.CTkButton(self.sidebar, text="🕒 Recent Local Syncs", 
                                             font=("Segoe UI", 13), anchor="w", height=40,
                                             fg_color="transparent", text_color="black", hover_color="#d4d4d4",
                                             command=self.show_history_view)
        self.btn_nav_history.grid(row=1, column=0, padx=15, pady=5, sticky="ew")

        self.btn_nav_nas = ctk.CTkButton(self.sidebar, text="📡 Available on NAS", 
                                         font=("Segoe UI", 13), anchor="w", height=40,
                                         fg_color="#e5e5e5", text_color="black", hover_color="#e5e5e5",
                                         command=self.show_nas_view)
        self.btn_nav_nas.grid(row=2, column=0, padx=15, pady=5, sticky="ew")

        # 1. Instantiate and grid the 3rd navigation option on row 3
        self.btn_nav_all = ctk.CTkButton(self.sidebar, text="🗄️ All Available Data", 
                                         font=("Segoe UI", 13), anchor="w", height=40,
                                         fg_color="transparent", text_color="black", hover_color="#d4d4d4",
                                         command=self.show_all_view)
        self.btn_nav_all.grid(row=3, column=0, padx=15, pady=5, sticky="ew")

        # 2. Instantiate and grid the Refresh button on row 4
        self.btn_refresh = ctk.CTkButton(self.sidebar, text="🔄 Force Refresh", 
                                         font=("Segoe UI", 12), height=32,
                                         fg_color="transparent", text_color="#0078d4", hover_color="#e5e5e5",
                                         border_width=1, border_color="#0078d4",
                                         command=self.manual_refresh_scan)
        self.btn_refresh.grid(row=4, column=0, padx=20, pady=(15, 0), sticky="ew")

        # GLOBAL ACTIONS (Pinned to bottom: rows 6 and 7)
        self.btn_manual = ctk.CTkButton(self.sidebar, text="Manually Select Folder", 
                                        font=("Segoe UI", 12), height=36, fg_color="#d4d4d4", text_color="black", hover_color="#c8c8c8",
                                        command=self.manual_select)
        self.btn_manual.grid(row=6, column=0, padx=20, pady=(10, 10), sticky="ew")

        self.btn_latest = ctk.CTkButton(self.sidebar, text="Pull Latest Acquisition", 
                                        font=("Segoe UI", 12, "bold"), height=36,
                                        fg_color="#0078d4", hover_color="#2b88d8",
                                        command=self.pull_latest)
        self.btn_latest.grid(row=7, column=0, padx=20, pady=(0, 30), sticky="ew")

        # ZONE 2: MAIN CONTENT AREA (Right)
        self.main_content = ctk.CTkFrame(self, corner_radius=0, fg_color="#ffffff") # Pure white
        self.main_content.grid(row=0, column=1, sticky="nsew")
        self.main_content.grid_rowconfigure(0, weight=1) 
        self.main_content.grid_columnconfigure(0, weight=1)

        # CHANGE: Use CTkScrollableFrame instead of CTkFrame to support vertical scrolling
        self.history_card = ctk.CTkScrollableFrame(self.main_content, fg_color="#ffffff", corner_radius=12)
        self.nas_card = ctk.CTkScrollableFrame(self.main_content, fg_color="#ffffff", corner_radius=12)

        # ADDED: 3rd Scrollable Card & Title
        self.all_card = ctk.CTkScrollableFrame(self.main_content, fg_color="#ffffff", corner_radius=12)

        self.history_title = ctk.CTkLabel(self.history_card, text="Recent Local Syncs", font=("Segoe UI", 24, "bold"), text_color="black")
        self.history_title.pack(anchor="w", padx=30, pady=(30, 20))

        self.nas_title = ctk.CTkLabel(self.nas_card, text="Available on NAS (Last 14 Days)", font=("Segoe UI", 24, "bold"), text_color="black")
        self.nas_title.pack(anchor="w", padx=30, pady=(30, 20))

        # ADDED: Title for 3rd Card
        self.all_title = ctk.CTkLabel(self.all_card, text="All Available Data", font=("Segoe UI", 24, "bold"), text_color="black")
        self.all_title.pack(anchor="w", padx=30, pady=(30, 20))

        # STATUS FOOTER
        self.footer_card = ctk.CTkFrame(self.main_content, corner_radius=0, fg_color="#f9f9f9", height=120)
        self.footer_card.grid(row=1, column=0, sticky="ew")
        
        # Transfer Progress Bar
        self.progress = ctk.CTkProgressBar(self.footer_card, progress_color="#0078d4")
        self.progress.set(0)
        self.progress.pack(fill="x", padx=40, pady=(20, 5))

        # Live Status Text
        self.status = ctk.CTkLabel(self.footer_card, text="System Ready.", font=("Segoe UI", 11), text_color="#555")
        self.status.pack(pady=(0, 5))
        
        # Background Scanner Preview Text
        self.latest_path_label = ctk.CTkLabel(self.footer_card, text="Scanning...", font=("Segoe UI", 11, "italic"), text_color="#888") # Background Scanner Preview Text
        self.latest_path_label.pack(pady=(0, 15))
        
        # DYNAMIC CONTENT INITIALISATION
        # Create empty lists to track the UI buttons so that we can delete and refresh them later
        self.nas_rows = []         # Holds the row frames to wipe on re-render
        self.nas_buttons = []      # Holds the CTkButton objects for disabling/enabling
        self.history_rows = []
        self.history_buttons = []
        self.all_rows = []
        self.all_buttons = []

        # 2026OCT16: Added line to track last seen cache modification time to prevent flickering
        self._last_cache_mtime = 0
        
        self.load_history()
        self.render_nas_buttons()     
        self.render_history_buttons()  
        self.show_nas_view()         # Default view on startup, changed to NAS view

        # Start Background Scans
        self.after(100, self.launch_background_scan)                            # Wait 100ms for the UI to finish drawing, then start the looping NAS scan (prevents startup crash)
        threading.Thread(target=self._run_housekeeping, daemon=True).start()    # Spawn a silent, one-time background thread to delete folders older than 30 days
        
        # Catch the 'X' button click to ensure a clean exit
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

    def on_closing(self): # Forces a hard exit, dropping all file locks so PyInstaller can delete the temp folder.
        self.destroy() # Closes the CustomTkinter UI visually
        os._exit(0)    # Brutally kills the Python engine and background threads instantly

    # add show_history_view and show_nas_view for Sidebar Navigation Logic
    def show_history_view(self): # Hides the NAS view, shows the History view, and updates button colors.
        self.nas_card.grid_forget()
        self.all_card.grid_forget()
        self.history_card.grid(row=0, column=0, sticky="nsew")
        
        # Highlight the active menu button
        self.btn_nav_history.configure(fg_color="#e5e5e5")
        self.btn_nav_nas.configure(fg_color="transparent")
        self.btn_nav_all.configure(fg_color="transparent")

    def show_nas_view(self): # Hides the History view, shows the NAS view, and updates button colors.
        self.history_card.grid_forget()
        self.all_card.grid_forget()
        self.nas_card.grid(row=0, column=0, sticky="nsew")
        
        # Highlight the active menu button
        self.btn_nav_nas.configure(fg_color="#e5e5e5")
        self.btn_nav_history.configure(fg_color="transparent")
        self.btn_nav_all.configure(fg_color="transparent")

    # 2026OCT09: ADDED Method for 3rd view
    def show_all_view(self):
        self.history_card.grid_forget()
        self.nas_card.grid_forget()
        self.all_card.grid(row=0, column=0, sticky="nsew")
        
        self.btn_nav_all.configure(fg_color="#e5e5e5")
        self.btn_nav_history.configure(fg_color="transparent")
        self.btn_nav_nas.configure(fg_color="transparent")

    # MANUAL REFRESH LOGIC
    def manual_refresh_scan(self):
        """Triggered by the Refresh button. Disables the button temporarily to prevent spamming."""
        self._last_cache_mtime = 0 # Force re-render
        self.btn_refresh.configure(text="⏳ Scanning...", state="disabled")
        self.status.configure(text="Manual scan initiated...")
        
        # Run in a quick one-off thread so we don't freeze the UI or duplicate the 30-second looping timer
        threading.Thread(target=self._manual_refresh_worker, daemon=True).start()

    def _manual_refresh_worker(self):
        """The worker that performs the manual scan quietly in the background."""
        try:
            latest = self.get_latest_folder(NAS_DIR)
            
            if latest:
                parts = latest.replace("\\", "/").split("/")
                parts = [p for p in parts if p]
                display_path = ".../" + "/".join(parts[-3:]) if len(parts) > 3 else latest
                self.latest_path_label.configure(text=f"Detected: {display_path}", text_color="gray")
                self.status.configure(text="Manual Scan Complete.")
            else:
                self.latest_path_label.configure(text="No .tif in LayersData found.", text_color="gray")
                self.status.configure(text="Ready (No acquisitions found)")
                
        except Exception as e:
            print(f"Manual scan error: {e}")
            self.status.configure(text="Scan Error Encountered", text_color="red")
        finally:
            # Re-render the buttons safely on the main thread
            self.after(0, self.render_nas_buttons)
            # Turn the refresh button back on
            self.after(0, lambda: self.btn_refresh.configure(text="🔄 Force Refresh", state="normal"))

    # Background Initialisation, Automation and Threading
    def launch_background_scan(self): # starts the background thread, keeps UI responsive
        threading.Thread(target=self._run_background_scan, daemon=True).start()

    #helper methods to do naughty stuff in the background
    def launch_gpu_shadow(self, target_folder, project_name, rows=1, cols=1):
        """
        Detached, invisible background job running bioem_proc.py on dual RTX A4000 GPUs.
        Outputs an OME-Zarr store to D:/<project_name>.zarr.
        """
        try:
            output_zarr = f"D:/{project_name}.zarr"
            script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bioem_proc.py")
            
            if not os.path.exists(script_path):
                print(f"[Shadow Pipeline] Script not found: {script_path}")
                return

            cmd = [
                sys.executable,
                script_path,
                target_folder,
                output_zarr,
                str(rows),
                str(cols)
            ]

            # CREATE_NO_WINDOW (0x08000000) keeps the worker completely invisible to the user on Windows
            creation_flags = 0x08000000 if sys.platform == "win32" else 0
            
            subprocess.Popen(
                cmd,
                creationflags=creation_flags,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            print(f"[Shadow Pipeline] Dispatched headless GPU build -> {output_zarr}")
        except Exception as e:
            # Silent catch so user experience in the traditional workflow is never interrupted
            print(f"[Shadow Pipeline Error] {e}")

    def _run_background_scan(self): # worker function for the background thread
        CACHE_FILE = os.path.join(os.getenv('APPDATA'), "fiji_latest_cache.json")
        try:
            if os.path.exists(CACHE_FILE):
                current_mtime = os.path.getmtime(CACHE_FILE)

                # ONLY refresh UI elements if the indexer has actually updated the cache
                if current_mtime > self._last_cache_mtime:
                    self._last_cache_mtime = current_mtime
                    
                    # Update buttons on the main UI thread
                    self.after(0, self.render_nas_buttons)
                    
                    # Read cache to update the preview label
                    with open(CACHE_FILE, "r") as f:
                        data = json.load(f)

                    # Also update "Detected: ..." label quietly
                    with open(CACHE_FILE, "r") as f:
                        data = json.load(f)
                    
                    latest = data.get("latest_folder") if isinstance(data, dict) else None
                    if latest:
                        parts = latest.replace("\\", "/").split("/")
                        parts = [p for p in parts if p]
                        display_path = ".../" + "/".join(parts[-3:]) if len(parts) > 3 else latest
                        self.latest_path_label.configure(text=f"Detected: {display_path}", text_color="gray")
                        self.status.configure(text="Ready.")
                    else:
                        self.latest_path_label.configure(text="No acquisitions found in cache.", text_color="gray")
                        self.status.configure(text="Ready.")

        except Exception as e:
            print(f"Background check error: {e}")
        finally:
            # Poll quietly every 2 seconds (lightweight, no more UI flashing)
            self.after(SCAN_INTERVAL, self.launch_background_scan)
                    
            
    # Addition on 2026JUN05
    def _run_housekeeping(self): # Background process: Scans SSD and deletes old synced folders based on an exclusion list.
        try:
            # 1. Setup the 30-day cutoff time
            seconds_in_30_days = 30 * 24 * 60 * 60
            cutoff_time = time.time() - seconds_in_30_days
            
            # 2. Define the protected folders (set lowercase for case-insensitive matching)
            protected_folders = [
                "bioem sync tool", 
                "pc_backup", 
                "shareddata", 
                "software",
                # Safety first: Protect hidden Windows system folders on the root drive (if any)
                "system volume information", 
                "$recycle.bin",
                "msocache"
            ]
            
            # Ensure the drive is available before scanning
            if not os.path.exists(DESTINATION_DIR):
                return

            # 3. Scan the D:/ drive
            for item in os.listdir(DESTINATION_DIR):
                item_path = os.path.join(DESTINATION_DIR, item)
                
                # We ONLY want to delete directories, not loose files on the root drive
                if not os.path.isdir(item_path):
                    continue
                    
                # 4. Check the exclusion list
                if item.lower() in protected_folders:
                    continue
                    
                # 5. Check the age of the folder
                try:
                    folder_mtime = os.path.getmtime(item_path)
                    
                    if folder_mtime < cutoff_time:
                        # Obliterate the old folder
                        shutil.rmtree(item_path)
                        # Remove it from the local history JSON if it's in there
                        if path in self.history:
                            self.history.remove(path)
                except Exception:
                    # Fail silently on individual folders (e.g., if a file inside is currently open/locked)
                    continue
                    
            # Save the history list in case we pruned old references
            with open(HISTORY_FILE, "w") as f:
                json.dump(self.history, f)
            # Update the right-hand column so it doesn't show deleted paths
            self.after(0, self.render_history_buttons)
            
        except Exception:
            pass # Fail silently so main app experience is never interrupted

    # File detection Logic
    def get_latest_folder(self, base_path): # Reads the lightning-fast cache created by the Indexer script.
        CACHE_FILE = os.path.join(os.getenv('APPDATA'), "fiji_latest_cache.json")
        
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, "r") as f:
                    data = json.load(f)
                
                cached_path = data.get("latest_folder")
                cache_time = data.get("timestamp", 0)
                
                # Optional safety check: Ensure the cache isn't ancient (e.g., older than 2 hours)
                if time.time() - cache_time < 7200: 
                    # Double-check the folder still actually exists before returning it
                    if cached_path and os.path.exists(cached_path):
                        return cached_path
            except:
                pass
        
        # Replaced old deep walk with mtime-pruned scan
        return self.find_latest_acquisition_fast(base_path)

    def find_latest_acquisition_fast(self, root_dir):
        """
        Dynamically locates the newest valid acquisition folder under LayersData
        containing .tif/.tiff files within the last 14 days.
        """
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
                    tifs = [f for f in files if f.lower().endswith(('.tif', '.tiff'))]
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

    # Managing Previously "read" folders and files / History Management UI
    # HISTORY & NAS BROWSER UI
    def load_history(self): # Loads the last 3 local sync paths from a JSON file.
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, "r") as f: self.history = json.load(f)
            except: self.history = []
        else: self.history = []

    def save_history(self, path): # Updates history, saves to disk, and refreshes the right-hand UI.
        if path in self.history: self.history.remove(path)
        self.history.insert(0, path)
        self.history = self.history[:21]
        with open(HISTORY_FILE, "w") as f:
            json.dump(self.history, f)
        # Refresh the local history side
        self.render_history_buttons()

    def remove_from_history(self, path):
        """Removes selected entry from local history list and refreshes cards."""
        if path in self.history:
            self.history.remove(path)
            try:
                with open(HISTORY_FILE, "w") as f:
                    json.dump(self.history, f)
            except Exception as e:
                print(f"Error updating history file: {e}")
            self.render_history_buttons()

    def format_breadcrumb(self, path, prefix=""):
        try:
            parts = path.replace("\\", "/").split("/")
            parts = [p for p in parts if p]
            
            # 1. Locate 'Maps' to identify the top-level user/owner folder
            maps_index = next((idx for idx, p in enumerate(parts) if p.lower() == "maps"), -1)
            owner = parts[maps_index + 1] if (maps_index != -1 and maps_index + 1 < len(parts)) else ""

            # 2. Locate 'LayersData'
            ld_index = next((idx for idx, p in enumerate(parts) if "layersdata" in p.lower()), -1)
            
            if ld_index > 0:
                # The acquisition/session project folder sits directly before LayersData
                project = parts[ld_index - 1]
                
                # Everything inside LayersData (e.g. ['Layer', 'Tile Set (2)'] or ['STACK-01', 'Tile Set (6)'])
                tail = " / ".join(parts[ld_index + 1:])
                
                # Format: Prefix Project ➔ Owner / Container / TileSet
                if owner and tail:
                    return f"{prefix} {project} ➔ {owner} / {tail}"
                elif tail:
                    return f"{prefix} {project} ➔ {tail}"
                return f"{prefix} {project}"

            return f"{prefix} {parts[-1]}"
        except Exception:
            return f"{prefix} {os.path.basename(path)}"

    def _create_badge_and_button(self, parent_container, item_path, acq_type="z-stack", rows=1, cols=1, prefix="📡", is_history=False):
        """
        Builds a consistent row with a rounded type badge and breadcrumb button.
        Returns (row_frame, btn).
        """
        btn_text = self.format_breadcrumb(item_path, prefix=prefix)

        # 1. Row Container
        row_frame = ctk.CTkFrame(parent_container, fg_color="transparent")
        row_frame.pack(fill="x", padx=30, pady=3)

        # 2. Badge Styling
        if acq_type == "mosaic-z":
            badge_text = "Mosaic Z-Stack"
            badge_bg = "#e74c3c"  # Fluent Red
            badge_width = 190
        elif acq_type == "mosaic":
            badge_text = f"Mosaic ({rows}x{cols})"
            badge_bg = "#e67e22"  # Fluent Orange
            badge_width = 95
        else:
            badge_text = "Z-Stack"
            badge_bg = "#2ecc71"  # Fluent Green
            badge_width = 65

        badge = ctk.CTkLabel(
            row_frame,
            text=badge_text,
            fg_color=badge_bg,
            text_color="#ffffff",
            corner_radius=6,
            font=("Segoe UI", 9, "bold"),
            width=badge_width,
            height=24
        )
        badge.pack(side="left", padx=(0, 8))

        # 3. Action Button
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
            command=lambda: self.run_sync(item_path, acq_type=acq_type, rows=rows, cols=cols)
        )
        btn.pack(side="left", fill="x", expand=True)

        # Bind right-click menu
        if is_history:
            btn.bind("<Button-3>", lambda event, p=item_path: self.show_history_context_menu(event, p))
        else:
            btn.bind("<Button-3>", lambda event, p=item_path: self.show_context_menu(event, p))

        return row_frame, btn
    
    def render_nas_buttons(self): # LEFT COLUMN: Displays latest folders found by the Indexer.
        # 1. Clean up previous frames and buttons
        for row in getattr(self, 'nas_rows', []): row.destroy()
        for row in getattr(self, 'all_rows', []): row.destroy()

        self.nas_rows = []
        self.nas_buttons = []
        self.all_rows = []
        self.all_buttons = []

        cache_path = os.path.join(os.getenv('APPDATA'), "fiji_latest_cache.json")
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r") as f:
                    data = json.load(f)

                if isinstance(data, dict):
                    acquisitions = data.get("acquisitions", [])
                elif isinstance(data, list):
                    acquisitions = data
                else:
                    acquisitions = []

                cutoff_time = time.time() - (14 * 24 * 60 * 60)
                within_14_days_count = sum(
                    1 for item in acquisitions 
                    if (item.get("mtime", 0) if isinstance(item, dict) else 0) >= cutoff_time
                )
                
                split_index = max(within_14_days_count, 12)
                nas_items = acquisitions[:split_index]
                all_items = acquisitions[split_index:]

                # 1. Render 'Available on NAS'
                for item in nas_items:
                    path = item.get('path') if isinstance(item, dict) else item
                    if not path: continue

                    acq_type = item.get('type', 'z-stack') if isinstance(item, dict) else 'z-stack'
                    rows = item.get('rows', 1) if isinstance(item, dict) else 1
                    cols = item.get('cols', 1) if isinstance(item, dict) else 1

                    row_frame, btn = self._create_badge_and_button(
                        parent_container=self.nas_card,
                        item_path=path,
                        acq_type=acq_type,
                        rows=rows,
                        cols=cols,
                        prefix="📡",
                        is_history=False
                    )
                    self.nas_rows.append(row_frame)
                    self.nas_buttons.append(btn)

                # 2. Render 'All Available Data'
                for item in all_items:
                    path = item.get('path') if isinstance(item, dict) else item
                    if not path: continue

                    acq_type = item.get('type', 'z-stack') if isinstance(item, dict) else 'z-stack'
                    rows = item.get('rows', 1) if isinstance(item, dict) else 1
                    cols = item.get('cols', 1) if isinstance(item, dict) else 1

                    row_frame, btn = self._create_badge_and_button(
                        parent_container=self.all_card,
                        item_path=path,
                        acq_type=acq_type,
                        rows=rows,
                        cols=cols,
                        prefix="📁",
                        is_history=False
                    )
                    self.all_rows.append(row_frame)
                    self.all_buttons.append(btn)

            except Exception as e:
                print(f"Error rendering NAS buttons: {e}")

    def render_history_buttons(self):
        for row in getattr(self, 'history_rows', []): row.destroy()
        self.history_rows = []
        self.history_buttons = []

        # Load cache dictionary to look up metadata for history paths
        meta_lookup = {}
        cache_path = os.path.join(os.getenv('APPDATA'), "fiji_latest_cache.json")
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r") as f:
                    cdata = json.load(f)
                acqs = cdata.get("acquisitions", []) if isinstance(cdata, dict) else cdata
                for a in acqs:
                    if isinstance(a, dict) and "path" in a:
                        norm = a["path"].replace("\\", "/").lower()
                        meta_lookup[norm] = a
            except Exception:
                pass

        for path in reversed(self.history):
            if not path: continue
            norm_p = path.replace("\\", "/").lower()
            item_meta = meta_lookup.get(norm_p, {})

            acq_type = item_meta.get("type", "z-stack")
            rows = item_meta.get("rows", 1)
            cols = item_meta.get("cols", 1)

            row_frame, btn = self._create_badge_and_button(
                parent_container=self.history_card,
                item_path=path,
                acq_type=acq_type,
                rows=rows,
                cols=cols,
                prefix="🕒",
                is_history=True
            )
            self.history_rows.append(row_frame)
            self.history_buttons.append(btn)

    # RIGHT-CLICK WIPE & RE-SYNC MODULE
    def show_context_menu(self, event, path): # Spawns a right-click menu at the mouse coordinates.
        menu = tk.Menu(self, tearoff=0)
        # (1) Existing Refresh Option
        menu.add_command(
            label="Refresh Folder (Wipe & Sync)", command=lambda: self.wipe_and_sync(folder_name)
            )
        # (2) Permanent Deletion Option
        menu.add_command(
            label="Delete Existing Data", command=lambda: self.show_delete_modal(folder_name)
            )
        menu.add_separator()
        # (3) Experimental Napari Shadow Option
        menu.add_command(
            label="Show Me the Future", command=lambda: self.show_future_modal(folder_name)
        )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def show_history_context_menu(self, event, path): # Extended right-click menu exclusively for Recent Local Syncs."""
        menu = tk.Menu(self, tearoff=0, font=("Arial", 10))
        menu.add_command(label="🔄 Refresh Folder (Wipe & Sync)", command=lambda p=path: self.confirm_refresh(p))
        menu.add_separator()
        menu.add_command(label="❌ Remove from History", command=lambda p=path: self.remove_from_history(p))
        menu.tk_popup(event.x_root, event.y_root)    

    def show_future_modal(self, folder_name):
        """Displays the elusive 'Show Me the Future' confirmation dialog."""
        zarr_path = f"D:/{folder_name}.zarr"
        if not os.path.exists(zarr_path):
            messagebox.showinfo(
                "Temporal Resonance Disruption",
                f"The future for '{folder_name}' has not coalesced yet.\n\n"
                "(The GPU shadow pipeline is still computing the volume in the background, or no sync has occurred yet.)"
            )
            return

        modal = ctk.CTkToplevel(self)
        modal.title("Temporal Gateway: Experimental Engine")
        modal.geometry("520x260")
        modal.resizable(False, False)
        modal.grab_set()  # Make window modal

        # Elusive advisory notice
        title_lbl = ctk.CTkLabel(
            modal, 
            text="⚠️ SYSTEM NOTICE: UNCOMMITTED REALITY ⚠️", 
            font=ctk.CTkFont(size=14, weight="bold"), 
            text_color="#f39c12"
        )
        title_lbl.pack(pady=(18, 8))

        elusive_text = (
            "You are about to access an uncalibrated, GPU-accelerated volumetric continuum.\n"
            "This interface bypasses the classical Java-bound fabric in favor of raw out-of-core tensors.\n"
            "What you observe may be faster, smoother, and fundamentally irreversible.\n\n"
            "Are you sure you want to continue?"
        )
        msg_lbl = ctk.CTkLabel(
            modal, 
            text=elusive_text, 
            font=ctk.CTkFont(size=12), 
            justify="center", 
            wraplength=480
        )
        msg_lbl.pack(pady=5)

        btn_frame = ctk.CTkFrame(modal, fg_color="transparent")
        btn_frame.pack(side="bottom", pady=20)

        def on_confirm():
            modal.destroy()
            viewer_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "view_in_napari.py")
            subprocess.Popen([sys.executable, viewer_script, zarr_path])

        def on_cancel():
            modal.destroy()

        btn_confirm = ctk.CTkButton(
            btn_frame, 
            text="Yes, show me the future", 
            fg_color="#27ae60", 
            hover_color="#2ecc71", 
            text_color="#ffffff",
            font=ctk.CTkFont(weight="bold"),
            command=on_confirm
        )
        btn_confirm.pack(side="left", padx=12)

        btn_cancel = ctk.CTkButton(
            btn_frame, 
            text="No, I don't know what I am doing here", 
            fg_color="#7f8c8d", 
            hover_color="#95a5a6", 
            command=on_cancel
        )
        btn_cancel.pack(side="left", padx=12)

    def show_delete_modal(self, folder_name):
        """Displays the 'Destructive Action' dataset wipe dialog."""
        modal = ctk.CTkToplevel(self)
        modal.title("Warning: Destructive Action")
        modal.geometry("520x250")
        modal.resizable(False, False)
        modal.grab_set()

        warning_title = ctk.CTkLabel(
            modal, 
            text="🚨 WARNING: DESTRUCTIVE ACTION 🚨", 
            font=ctk.CTkFont(size=15, weight="bold"), 
            text_color="#e74c3c"
        )
        warning_title.pack(pady=(18, 6))

        warning_body = (
            "This operation will permanently purge all synchronized image slices, cached mosaics,\n"
            "and corresponding volumetric Zarr stores for this dataset from the local storage."
        )
        msg_lbl = ctk.CTkLabel(
            modal, 
            text=warning_body, 
            font=ctk.CTkFont(size=12), 
            justify="center", 
            wraplength=480
        )
        msg_lbl.pack(pady=4)

        # Dataset name displayed prominently above confirmation button
        name_lbl = ctk.CTkLabel(
            modal, 
            text=f"Dataset: {folder_name}", 
            font=ctk.CTkFont(size=13, weight="bold"), 
            text_color="#ecf0f1"
        )
        name_lbl.pack(pady=(8, 0))

        btn_frame = ctk.CTkFrame(modal, fg_color="transparent")
        btn_frame.pack(side="bottom", pady=20)

        def on_delete():
            modal.destroy()
            self.execute_delete_dataset(folder_name)

        def on_cancel():
            modal.destroy()

        btn_delete = ctk.CTkButton(
            btn_frame, 
            text="Yes, remove this dataset", 
            fg_color="#c0392b", 
            hover_color="#e74c3c", 
            text_color="#ffffff",
            font=ctk.CTkFont(weight="bold"),
            command=on_delete
        )
        btn_delete.pack(side="left", padx=12)

        btn_cancel = ctk.CTkButton(
            btn_frame, 
            text="No, I don't know what I am doing here", 
            fg_color="#7f8c8d", 
            hover_color="#95a5a6", 
            command=on_cancel
        )
        btn_cancel.pack(side="left", padx=12)

    def execute_delete_dataset(self, folder_name):
        """Purges local folder and .zarr store."""
        import shutil
        target_dir = os.path.join(TARGET_DIR, folder_name)
        zarr_path = f"D:/{folder_name}.zarr"
        
        purged = []
        if os.path.exists(target_dir):
            shutil.rmtree(target_dir, ignore_errors=True)
            purged.append(target_dir)
            
        if os.path.exists(zarr_path):
            shutil.rmtree(zarr_path, ignore_errors=True)
            purged.append(zarr_path)

        if purged:
            self.status.configure(text=f"🗑️ Purged local data for: {folder_name}", text_color="#e74c3c")
            # Refresh buttons/history UI state
            self.refresh_ui_lists()
        else:
            self.status.configure(text=f"No local data found on SSD to delete.", text_color="#f39c12")

    def confirm_refresh(self, path): # Creates the custom warning pop-up.
        # Create a custom TopLevel window
        dialog = ctk.CTkToplevel(self)
        dialog.title("Warning: Destructive Action")
        dialog.geometry("450x220")
        dialog.attributes("-topmost", True) # Keep on top of main app
        dialog.transient(self) # Dim parent window
        dialog.grab_set() # Force user to click one of the buttons
        
        warn_text = ("This action will wipe previous files on this computer and\n"
                     "re-sync with the latest content with the current folder on NAS.\n\n"
                     "Are you sure you want to continue?")
        
        lbl = ctk.CTkLabel(dialog, text=warn_text, font=("Arial", 12))
        lbl.pack(pady=(20, 15), padx=20)
        
        # The YES button
        btn_yes = ctk.CTkButton(dialog, text="Yes, wipe and re-sync", 
                                fg_color="#c0392b", hover_color="#e74c3c", font=("Arial", 12, "bold"),
                                command=lambda: self.execute_wipe_and_resync(path, dialog))
        btn_yes.pack(pady=5)
        
        # The NO button
        btn_no = ctk.CTkButton(dialog, text="No, I don't know what I am doing here", 
                               fg_color="#34495e", hover_color="#2c3e50", font=("Arial", 11),
                               command=dialog.destroy)
        btn_no.pack(pady=5)

    def execute_wipe_and_resync(self, source_folder, dialog): # Deletes the local folder and starts a fresh sync.
        dialog.destroy() # Close the pop-up
        
        # 1. Determine exactly what the local folder is named
        # (This uses the exact same safe naming logic from run_sync)
        try:
            path_parts = source_folder.replace("\\", "/").split("/")
            path_parts = [p for p in path_parts if p]
            ld_index = -1
            for i, part in enumerate(path_parts):
                if "layersdata" in part.lower():
                    ld_index = i
                    break
            project_name = path_parts[ld_index - 1] if ld_index > 0 else "Unknown Project"
            tile_name = path_parts[-1] 
            raw_folder_name = f"{project_name}_{tile_name}".replace(" ", "_")
            safe_folder_name = "".join([c for c in raw_folder_name if c.isalnum() or c in ['_', '-']])
        except:
            safe_folder_name = os.path.basename(source_folder).replace(" ", "_")

        final_dest = os.path.join(DESTINATION_DIR, safe_folder_name)

        # 2. THE WIPE
        if os.path.exists(final_dest):
            try:
                self.status.configure(text=f"Wiping local folder: {safe_folder_name}...", text_color="red")
                self.update()
                # shutil.rmtree obliterates the folder and everything inside it
                shutil.rmtree(final_dest) 
                time.sleep(0.5) # Brief pause so the OS releases the file handles
            except Exception as e:
                messagebox.showerror("Wipe Error", f"Could not delete local folder. Ensure no files are open in Fiji.\nError: {e}")
                return
                
        # 3. THE FRESH SYNC
        # Call the normal sync function now that the destination is gone
        self.run_sync(source_folder)

# Search & Destroy, just kidding, Search and Detect (Synchronises then Execute)
    # added lastest modified file awareness in 3 parts (part 1 moved up with the scan for timing)
    # part 2, the app does a deep scan for the "latest file"
    def pull_latest(self): # Grabs the absolute newest entry from the indexer cache.
        cache_path = os.path.join(os.getenv('APPDATA'), "fiji_latest_cache.json")
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r") as f:
                    data = json.load(f)

                latest_path = None
                if isinstance(data, dict):
                    latest_path = data.get("latest_folder")
                elif isinstance(data, list) and data:
                    latest_path = data[0].get("path")

                if latest_path and os.path.exists(latest_path):
                    self.run_sync(latest_path)
                    return
            except Exception as e:
                print(f"Error reading latest acquisition: {e}")

        # Fallback if cache is missing or path invalid
        latest = self.get_latest_folder(NAS_DIR)
        if latest:
            self.run_sync(latest)
        else:
            messagebox.showwarning("Not Found", "Could not detect a valid acquisition.")

    # FallBack for manual selected: reworked for manual selection even when latest folder is detected
    def manual_select(self):
        source = filedialog.askdirectory(
            title="Select NAS Folder", initialdir=os.path.join(NAS_DIR, "Maps")
        )
        if source:
            self.run_sync(source)

# Sync and Execute
    # fiji nuclear option
    def launch_fiji(self, folder_path, project_name="EM_Project", acq_type="z-stack", rows=1, cols=1):
        try:
            # 1. PREPARE THE PATH & PAYLOAD
            formatted_path = folder_path.replace("\\", "/")
            trigger_path = "D:/fiji_trigger.txt"

            payload = f"{formatted_path}|{project_name}|{acq_type}|{rows}|{cols}"
            with open(trigger_path, "w") as f:
                f.write(payload)
            
            # 2. CHECK IF FIJI IS ACTIVELY RUNNING
            fiji_active = False
            for proc in psutil.process_iter(['name']):
                try:
                    pname = proc.info['name'].lower()
                    # Check for main fiji executable
                    if "fiji-windows-x64" in pname or pname == "fiji.exe":
                        fiji_active = True
                        break
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            # 3. EXECUTION LOGIC
            if fiji_active:
                self.status.configure(text="✅ Sync complete. Alerted active Fiji instance.", text_color="#2ecc71")
            else:
                self.status.configure(text="Launching fresh Fiji instance...", text_color="#0078d4")
                self.update()
                
                # CRITICAL: Set cwd to Fiji's folder so it can locate its bundled JRE and jars
                fiji_dir = os.path.dirname(APP_PATH)
                subprocess.Popen([APP_PATH], cwd=fiji_dir, shell=False)

        except Exception as e:
            messagebox.showwarning("Trigger Error", f"Sync worked, but couldn't alert Fiji: {e}")

    #def run_sync(self, source_folder): # Core Logic: Compare NAS and SSD, copies missing/different
    def run_sync(self, source_folder, acq_type="z-stack", rows=1, cols=1):
        if acq_type == "mosaic-z":
            messagebox.showwarning(
                "Workflow Not Supported",
                "Mosaic Z-Stacks are not currently supported for automated TrakEM2 assembly.\n"
                "Files will still be copied to SSD, but automated stitching must be configured manually."
            )    
        # 1) PATH DECONSTRUCTION & SAFE NAMING
        try: # psuedo-smart path deconstruction
            # Standardize slashes and split the path into individual folder names
            path_parts = source_folder.replace("\\", "/").split("/")
            path_parts = [p for p in path_parts if p]
            
            # Find the exact position of 'LayersData' to orient our breadcrumbs
            ld_index = -1
            for i, part in enumerate(path_parts):
                if "layersdata" in part.lower():
                    ld_index = i
                    break
            
            # Extract names relative to LayersData
            # Project Name is usually 1 level before LayersData
            project_name = path_parts[ld_index - 1] if ld_index > 0 else "Unknown Project"
            # Name of the TileSet is usually the last folder in the string
            tile_name = path_parts[-1] 
            
            # Create the UI display string (e.g., "Project_A ➔ Tile_01")
            display_title = f"{project_name} ➔ {tile_name}"
            
            # Create a safe folder name combining project and tile to prevent mixed stacks in Fiji
            # E.g., it turns "HL_0203 humanCell" into "HL_0203_humanCell_Tile_01"
            raw_folder_name = f"{project_name}_{tile_name}".replace(" ", "_")
            # Keep only alphanumeric characters, underscores, and hyphens to avoid Windows path errors
            safe_folder_name = "".join([c for c in raw_folder_name if c.isalnum() or c in ['_', '-']])

        except:
            # Fallback if the path doesn't match the expected LayersData/Layer structure
            display_title = os.path.basename(source_folder)
            safe_folder_name = display_title.replace(" ", "_")

        # 2) UPDATE UI LABELS & DESTINATION
        # Update the UI to show the user exactly what is currently syncing
        self.latest_path_label.configure(text=f"Syncing: {display_title}", text_color="#3498db")
        
        # Define the final destination using our new safe, unique folder name
        final_dest = os.path.join(DESTINATION_DIR, safe_folder_name)

        try:
            # Disable inputs during sync to prevent user from clicking twice and causing overlapping syncs
            all_action_buttons = (
                self.history_buttons + 
                self.nas_buttons + 
                self.all_buttons + 
                [self.btn_manual, self.btn_latest]
            )
            for b in all_action_buttons:
                b.configure(state="disabled")

            # Let user know we are starting the analysis phase
            self.status.configure(text="Analyzing files for changes...")
            self.update()

            # Create destination directory on the SSD if it doesn't exist yet
            os.makedirs(final_dest, exist_ok=True)
            
            # Gather all files from the source folder recursively to calculate total progress
            # Updated to only collect files ending with .tif
            all_files = []
            for root, dirs, files in os.walk(source_folder):
                for file in files:
                    if file.lower().endswith(".tif"):
                        all_files.append(os.path.join(root, file))
            
            total_files = len(all_files)
            
            if total_files == 0:
                messagebox.showinfo("No Images Found", "This folder contains no .tif files.")
                return

            # Initialise tracking counters for the UI
            files_processed = 0
            files_skipped = 0
            files_copied = 0

            # 3) SMART SYNC LOGIC
            for i, s_file in enumerate(all_files):
                # Calculate the relative path of the file to maintain exact folder structure inside the destination
                rel = os.path.relpath(s_file, source_folder)
                d_file = os.path.join(final_dest, rel)
                
                # Ensure the sub-directory exists in the destination before attempting to copy
                os.makedirs(os.path.dirname(d_file), exist_ok=True)

                should_copy = True # Default assumption is that we need to copy the file
                
                # If the file already exists on the SSD, check its metadata to see if we can skip it
                if os.path.exists(d_file):
                    s_stat = os.stat(s_file) # Source file stats (NAS)
                    d_stat = os.stat(d_file) # Destination file stats (SSD)
                    
                    # If both size AND modification time are identical, the file hasn't changed. Skip it.
                    if s_stat.st_size == d_stat.st_size and s_stat.st_mtime == d_stat.st_mtime:
                        should_copy = False
                
                # Execute the copy or skip
                if should_copy:
                    # shutil.copy2 is critical here: it copies the file AND preserves its original timestamp
                    shutil.copy2(s_file, d_file)
                    files_copied += 1
                else:
                    files_skipped += 1

                # 4) PROGRESS UPDATES
                files_processed += 1
                self.progress.set(files_processed / total_files) # Update progress bar (0.0 to 1.0)
                
                # Update status text with live counters so user knows it hasn't frozen
                self.status.configure(
                    text=f"Progress: {files_processed}/{total_files} | Copied: {files_copied} | Skipped: {files_skipped}"
                )
                
                # Update UI periodically (every 5 files) to keep it responsive but maintain high transfer speeds
                if files_processed % 5 == 0:
                    self.update()

            # 5) COMPLETION & HANDOFF
            # Save this successful run to the history JSON
            self.save_history(source_folder)
            
            # Stamp the completion time on the UI so user knows how fresh the data is
            timestamp = time.strftime("%H:%M")
            self.latest_path_label.configure(text=f"Last Sync: {display_title} [{timestamp}]", text_color="green")
            self.status.configure(
                text=f"Success! {files_copied} new, {files_skipped} already on SSD.", 
                text_color="green"
            )
            
            # Hand the final combined folder over to Fiji
            #self.launch_fiji(final_dest)
            # Pass the extracted project name and parameters to Fiji
            self.launch_fiji(final_dest, project_name=project_name, acq_type=acq_type, rows=rows, cols=cols)

            # 6) SHADOW PIPELINE: Parallel headless GPU background processing
            self.launch_gpu_shadow(final_dest, project_name=project_name, rows=rows, cols=cols)

        except Exception as e:
            # Catch any unexpected network/file errors and show them to the user
            messagebox.showerror("Sync Error", str(e))
        finally:
            # No matter what happens (success or error), always re-enable the buttons!
            all_action_buttons = (
                self.history_buttons + 
                self.nas_buttons + 
                self.all_buttons + 
                [self.btn_manual, self.btn_latest]
            )
            for b in all_action_buttons:
                b.configure(state="normal")

# THE ON SWITCH 
if __name__ == "__main__":
    # Ensure the local D:/ drive or folder exists
    if not os.path.exists(DESTINATION_DIR):
        try:
            os.makedirs(DESTINATION_DIR)
        except Exception:
            pass # Drive might not be writable yet
            
    app = NASPuller()
    app.mainloop()