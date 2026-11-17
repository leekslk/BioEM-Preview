import os
import time
import json
import re

from bioem_config import (
    get_app_data_dir,
    get_cache_file_path,
    load_config,
    INDEXER_PID_FILENAME,
)

# CONFIGURATION
SCAN_INTERVAL = 5  # seconds between NAS scans

# Matches 'Tile' followed by 3 coordinate numbers regardless of '-' or '_' delimiters
TILE_PATTERN = re.compile(r"Tile[_-](\d+)[_-](\d+)[_-](\d+)", re.IGNORECASE)

def detect_acquisition_type(filenames):
    """
    Evaluates in-memory filenames to categorize:
      - 'z-stack' (1 spatial tile, multiple Z)
      - 'mosaic' (multiple spatial tiles, single Z) -> returns grid rows & cols
      - 'mosaic-z' (multiple spatial tiles AND multiple Z)
    """
    dim1_set = set()
    dim2_set = set()
    z_set = set()
    spatial_coords = set()

    for f in filenames:
        m = TILE_PATTERN.search(f)
        if m:
            d1 = int(m.group(1))
            d2 = int(m.group(2))
            z  = int(m.group(3))
            
            dim1_set.add(d1)
            dim2_set.add(d2)
            z_set.add(z)
            spatial_coords.add((d1, d2))

    if not dim1_set or not dim2_set:
        return {"type": "z-stack", "rows": 1, "cols": 1}

    # Use max() to represent true grid dimensions for TrakEM2 even during active acquisitions
    rows = max(dim1_set)
    cols = max(dim2_set)

    num_spatial_tiles = len(spatial_coords)
    num_z_slices = len(z_set)

    # 1. Multi-tile grid AND multi-slice Z -> Mosaic Z-Stack (Unsupported)
    if num_spatial_tiles > 1 and num_z_slices > 1:
        return {"type": "mosaic-z", "rows": rows, "cols": cols}

    # 2. Multi-tile grid AND single Z -> Pure 2D Mosaic (TrakEM2)
    if num_spatial_tiles > 1:
        return {"type": "mosaic", "rows": rows, "cols": cols}

    # 3. Single spatial tile position -> Standard Z-Stack
    return {"type": "z-stack", "rows": 1, "cols": 1}

def get_all_acquisitions(base_path):
    search_root = os.path.join(base_path, "Maps") if os.path.exists(os.path.join(base_path, "Maps")) else base_path
    if not os.path.exists(search_root):
        return []

    found_folders = {}

    try:
        for root, dirs, files in os.walk(search_root, topdown=True):
            # Prune non-data branches immediately to avoid unnecessary network hops
            dirs[:] = [
                d for d in dirs 
                if "electron snapshot" not in d.lower() 
                and not d.startswith(".") 
                and "$recycle" not in d.lower()
            ]

            norm_root = root.replace("\\", "/").lower()
            if "/layersdata" in norm_root:
                tifs = [f for f in files if f.lower().endswith(('.tif', '.tiff'))]
                if tifs:
                    try:
                        folder_mtime = os.path.getmtime(root)
                        acq_meta = detect_acquisition_type(tifs)
                        found_folders[root] = (folder_mtime, acq_meta)
                        # Stop descending further into subfolders of an active acquisition folder
                        dirs[:] = []
                    except Exception:
                        pass

        results = [
            {
                "path": p,
                "mtime": data[0],
                "type": data[1]["type"],
                "rows": data[1]["rows"],
                "cols": data[1]["cols"]
            }
            for p, data in found_folders.items()
        ]
        results.sort(key=lambda x: x["mtime"], reverse=True)
        return results

    except Exception as e:
        print(f"❌ Scan error: {e}")
        return []

def run_indexer_loop(stop_event=None):
    """Runs the background NAS acquisition crawler loop, reloading config on each scan."""
    print("===========================================")
    print("   BIO-EM NAS INDEXER SERVICE (ALL DATA)   ")
    print("===========================================")

    pid_file = os.path.join(get_app_data_dir(), INDEXER_PID_FILENAME)
    try:
        with open(pid_file, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass

    try:
        while stop_event is None or not stop_event.is_set():
            cfg = load_config()
            nas_dir = cfg.get("nas_dir", "I:/")
            cache_file = get_cache_file_path()

            start_time = time.time()
            results = get_all_acquisitions(nas_dir)

            if results:
                payload = {
                    "latest_folder": results[0]["path"],
                    "timestamp": time.time(),
                    "acquisitions": results,
                }
                try:
                    temp_cache = cache_file + ".tmp"
                    with open(temp_cache, "w", encoding="utf-8") as f:
                        json.dump(payload, f, indent=4)
                    os.replace(temp_cache, cache_file)

                    elapsed = round(time.time() - start_time, 2)
                    print(
                        f"[{time.strftime('%H:%M:%S')}] Cache updated: {len(results)} items from {nas_dir} (Took {elapsed}s)."
                    )
                except Exception as e:
                    print(f"❌ Cache Write Error: {e}")
            else:
                print(
                    f"[{time.strftime('%H:%M:%S')}] Scan completed ({nas_dir}): No valid acquisitions found."
                )

            if stop_event is not None:
                stop_event.wait(SCAN_INTERVAL)
            else:
                time.sleep(SCAN_INTERVAL)
    finally:
        try:
            if os.path.exists(pid_file):
                with open(pid_file, "r", encoding="utf-8") as f:
                    if f.read().strip() == str(os.getpid()):
                        os.remove(pid_file)
        except Exception:
            pass


if __name__ == "__main__":
    run_indexer_loop()