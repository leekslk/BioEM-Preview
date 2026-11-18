# BioEM Preview: Smart Sync & Fast Stack Alignment

An automated acquisition sync and alignment applet for ThermoFisher MAPS (SBF-SEM) datasets, supporting **Single-Tile Z-Stacks**, **2D Mosaics**, and **Multi-Layer Mosaic Z-Stacks**, with results opening directly in **Fiji (Java 8) / TrakEM2**.

## Overview
Moving large SBF-SEM tile streams across the network and configuring alignment parameters in Fiji manually is slow and error-prone. **BioEM Preview** automates the workflow end-to-end:

* Continuously discovers new acquisitions on the NAS in the background.
* Delta-syncs new or updated `.tif` files directly into your local scratch drive (`<DESTINATION_DIR>`).
* Reads FEI/Thermo Fisher `DigitalContrast` TIFF metadata on the first tile so display inversion is applied only when needed.
* Routes each dataset automatically:
  * **Single-Tile Z-Stacks:** Opens in Fiji as a virtual stack (applying `Invert LUT` automatically only for uninverted acquisitions), with an optional right-click **Align and Display** action for Z-alignment.
  * **2D Mosaics & Mosaic Z-Stacks:** Runs fast PyTorch GPU/CPU alignment with live elapsed/remaining time progress, saves a TrakEM2 `.xml` project on your Desktop, and opens it in Fiji.

*Too Long Didn't Read | TLDR:*
* *Install and run the app, complete the quick First-Run Setup once, and click any dataset.*
* *Z-stacks open straight into Fiji as virtual stacks (with optional Z-align); Mosaics and Mosaic Z-stacks are automatically aligned and opened as TrakEM2 projects, ready for portable one-click package export.*

## System Architecture
![BioEM Preview System Architecture](docs/system_architecture.svg)

## Core Features
* **First-Run Setup Wizard & Settings:** On first launch, prompts for your scratch SSD (`DESTINATION_DIR`), NAS drive (`NAS_DIR`), and Fiji executable (`APP_PATH`) with sensible defaults. Automatically places `BioEM_Watcher.ijm` inside Fiji, manages the background NAS crawler (`--indexer`) at login, and restarts stale indexers automatically on version updates.
* **Live Acquisition Browser & Collapsible Logs Sidebar:** Displays recent sessions (last 14 days / minimum 12 entries) under **Available on NAS**, older sessions under **All Available Data**, and local syncs under **Recent Local Syncs**, with tiered acquisition badges (`Z-Stack`, `Mosaic (RxC)`, `Mosaic Z (RxC, z=N)`) and a toggleable **Logs** drawer showing per-pass progress and timings.
* **Direct Delta Sync & Housekeeping:** Syncs datasets directly into `<DESTINATION_DIR>\<DatasetName>`, skips unchanged `.tif` files, and automatically cleans cached acquisition folders older than 30 days.
* **Automatic FEI Contrast / LUT Detection:** Inspects FEI tag `34682` (`Image.DigitalContrast`) on the first `.tif` (`-1` keeps the microscope-inverted LUT as-is; `+1` enables `Invert LUT` in Fiji or TrakEM2's non-destructive `filter_invert` display filter).
* **Contextual Right-Click Menu:**
  * **Show Folder:** Always available; delta-syncs the dataset first and opens it in Explorer.
  * **Align and Display:** Available on **Z-Stacks**; performs delta sync, PyTorch Z-alignment (Pass 2), TrakEM2 `.xml` generation, and launches Fiji.
  * **Export Data Package:** Appears once a dataset has been synchronized, aligned, and saved to a TrakEM2 `.xml`; prompts for a destination folder, copies images (`images/`), mipmaps, and saved alignment logs into `<DatasetName>/`, rewrites `.xml` links as portable relative paths, and opens the exported package.
  * **Refresh Folder (Wipe & Sync)** & **Delete Local Cache:** Re-sync or purge scratch storage safely without touching raw NAS data.
* **Fast GPU/CPU PyTorch Aligner:** Performs coarse-to-fine 2D FFT phase correlation + multi-block IRLS stitching (Pass 1) and rigid Procrustes RANSAC Z-alignment (Pass 2), powered by an isolated on-demand PyTorch runtime (`uv`) in `%LOCALAPPDATA%\BioEM_Preview\runtime\`.

## Requirements
* **Operating System:** Windows 10 / 11 Pro (x64)
* **Storage:** Fast local NVMe/SSD (`D:/` by default) and mapped NAS storage (`I:/` by default)
* **Fiji:** Standard Fiji (Java 8) with TrakEM2
* **Optional (only if building from source):** Python 3.10+ with `customtkinter`, `psutil`, and `pyinstaller` (the alignment engine `torch`, `numpy`, `scipy`, and `tifffile` are bootstrapped on demand by the app)

## Installation & Deployment
### Option A: Download Pre-Built Release (GitHub Releases)

1. Download the latest pre-compiled release [here](https://github.com/leekslk/BioEM-Preview/releases/latest/download/Preview_beta-win64.zip) (~60–75 MB, universal self-bootstrapping archive; all tagged builds are also listed on the [Releases](https://github.com/leekslk/BioEM-Preview/releases) page).
2. Extract the archive and move the extracted folder (keeping `Preview_beta-YYYYMMMDD.exe` and `_internal\` together) into **`C:\Program Files\BioEM Preview\`** (approve the administrator prompt if requested).
3. Right-click `Preview_beta-YYYYMMMDD.exe` → **Send to → Desktop (create shortcut)** for convenient access.
4. Double-click the shortcut to launch:
   * On first launch, confirm your **Scratch Disk**, **NAS Raw Data**, and **Fiji Executable**, then click **Install / Update Engine** (auto-detects NVIDIA CUDA GPU vs CPU and installs an isolated PyTorch runtime into `%LOCALAPPDATA%\BioEM_Preview\runtime\`—or select an existing Python installation).
   * Click **Save & Deploy Sidecars** to deploy `BioEM_Watcher.ijm` and start the background NAS indexer.
   * Runtime settings, caches, alignment registry, and per-dataset logs are stored per-user under `%APPDATA%` / `%LOCALAPPDATA%`, so standard users do not need write access inside `Program Files`.

### Option B: Build from Source via PowerShell (No Git Required)

Run the following PowerShell snippet on a Windows PC with Python (`customtkinter`, `psutil`, `pyinstaller`) installed:

```powershell
Get-Process Preview_beta* -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 1

$zip = "$env:TEMP\BioEM-Preview-main.zip"
$dest = "C:\Users\User\documents\Preview_beta_src"

Invoke-WebRequest -Uri "https://github.com/leekslk/BioEM-Preview/archive/refs/heads/main.zip" -OutFile $zip
if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
Expand-Archive -Path $zip -DestinationPath $dest

cd "$dest\BioEM-Preview-main"
powershell -ExecutionPolicy Bypass -File .\build_windows_release.ps1
```

Once complete, extract `dist\Preview_beta-YYYYMMMDD.zip` and copy its contents (`Preview_beta-YYYYMMMDD.exe` and `_internal\`) into **`C:\Program Files\BioEM Preview\`** as described in Option A.

## Usage
1. Open **BioEM Preview** from your Desktop shortcut (the background crawler runs automatically).
2. Click any acquisition under **Available on NAS** (or use **Pull Latest Acquisition** / **Manually Select Folder**), or right-click any entry for contextual actions (**Show Folder**, **Align and Display**, **Export Data Package**, **Refresh**, **Delete Local Cache**).
3. Watch the progress bar (and optional **Logs** sidebar) for delta sync, FEI contrast LUT check, and alignment passes with elapsed and remaining time.
4. Fiji opens automatically with either your virtual Z-stack or your aligned TrakEM2 `.xml` project.

## Roadmap
* **Relocate TrakEM2 Working Folder into Scratch Data Directory (`D:/`) & Clean Up Temp Folders:** Consolidate temporary TrakEM2 `.xml` / mipmap workspaces out of the Desktop directly inside the data folder (`<DESTINATION_DIR>` / `D:/`) and automatically clean up temporary TrakEM2 artifacts in the next revision.
* **Interactive Preview & Manual Alignment Correction:** Visual preview allowing users to inspect and manually adjust tricky slice/tile alignments that cannot be solved purely mathematically.
* **Slimmer GPU Distribution:** Trimming unused CUDA runtime libraries so GPU-accelerated builds fit comfortably within GitHub's 2 GB release asset limit.
* **OME-Zarr / Out-of-Core Viewing:** Optional chunked pyramidal export and interactive 3D inspection.

## License
Distributed under the MIT License. Developed internally at BioEM-EPFL to streamline EM imaging operations.
