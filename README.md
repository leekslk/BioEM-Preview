# BioEM Preview: Smart Sync & Fast Stack Alignment

An automated acquisition sync and alignment applet for ThermoFisher MAPS (SBF-SEM) datasets, supporting **Single-Tile Z-Stacks**, **2D Mosaics**, and **Multi-Layer Mosaic Z-Stacks**, with results opening directly in **Fiji (Java 8) / TrakEM2**.

## Overview
---
Moving large SBF-SEM tile streams across the network and configuring alignment parameters in Fiji manually is slow and error-prone. **BioEM Preview** automates the workflow end-to-end:

* Continuously discovers new acquisitions on the NAS in the background.
* Delta-syncs only new or updated `.tif` files to a fast local scratch SSD.
* Routes each dataset automatically:
  * **Single-Tile Z-Stacks:** Opens in Fiji as a virtual stack with inverted LUT.
  * **2D Mosaics & Mosaic Z-Stacks:** Runs fast PyTorch GPU/CPU alignment with live elapsed/remaining time progress, saves a TrakEM2 `.xml` project on your Desktop, and opens it in Fiji.

*Too Long Didn't Read | TLDR:*
* *Install and run the app, complete the quick First-Run Setup once, and click any dataset.*
* *Z-stacks open straight into Fiji as virtual stacks; Mosaics and Mosaic Z-stacks are automatically aligned and opened as TrakEM2 projects.*

## System Architecture
---
![BioEM Preview System Architecture](docs/system_architecture.svg)

## Core Features
---
* **First-Run Setup Wizard & Settings:** On first launch, prompts for your scratch SSD (`DESTINATION_DIR`), NAS drive (`NAS_DIR`), and Fiji executable (`APP_PATH`) with sensible defaults. Automatically places `BioEM_Watcher.ijm` inside Fiji and starts the built-in background NAS crawler (`--indexer`) at Windows login.
* **Live Acquisition Browser:** Shows recent sessions (last 14 days / minimum 12 entries) under **Available on NAS**, older sessions under **All Available Data**, and local syncs under **Recent Local Syncs**.
* **Smart Delta Sync & Housekeeping:** Skips unchanged `.tif` files and automatically cleans cached folders older than 30 days inside `<DESTINATION_DIR>\BioEM_Cache\`.
* **Context Menu Actions:** Right-click any dataset to **Refresh Folder (Wipe & Sync)** or **Delete Local Cache** without touching raw NAS data.
* **Fast GPU/CPU Mosaic & Z-Stack Alignment:** Aligns 2D tiles and serial Z-sections with live per-step progress, elapsed time, and ETA, then exports a ready-to-inspect TrakEM2 `.xml` project.

## Requirements
---
* **Operating System:** Windows 10 / 11 Pro (x64)
* **Storage:** Fast local NVMe/SSD (`D:/` by default) and mapped NAS storage (`I:/` by default)
* **Fiji:** Standard Fiji (Java 8) with TrakEM2
* **Optional (only if building from source):** Python 3.10+ with `torch` (e.g. `cu118`), `customtkinter`, `psutil`, `numpy`, `scipy`, `tifffile`, and `pyinstaller`

## Installation & Deployment
---
### Option A: Download Pre-Built Release (GitHub Releases)

1. Download the release archive from the **Releases** page on GitHub (note: GitHub Releases hosts the CPU-compatible pre-built archive due to GitHub's 2 GB per-file limit; for CUDA GPU acceleration, build locally using Option B).
2. Extract the archive and move the extracted folder (keeping `Preview_beta-YYYYMMMDD.exe` and `_internal\` together) into **`C:\Program Files\BioEM Preview\`** (approve the administrator prompt if requested).
3. Right-click `Preview_beta-YYYYMMMDD.exe` → **Send to → Desktop (create shortcut)** for convenient access.
4. Double-click the shortcut to launch:
   * On first launch, confirm your **Scratch Disk**, **NAS Raw Data**, **Fiji Executable**, and **Start background NAS crawler automatically** option, then click **Save & Deploy Sidecars**.
   * Runtime settings and caches are stored per-user under `%APPDATA%\BioEM_Preview\`, so standard users do not need write access inside `Program Files`.

### Option B: Build Self-Contained CUDA/GPU Bundle via PowerShell (No Git Required)

Run the following PowerShell snippet on a Windows PC that has Python and PyTorch (`cu118`) + build dependencies installed (`customtkinter`, `psutil`, `numpy`, `scipy`, `tifffile`, `pyinstaller`):

```powershell
Get-Process Preview_beta* -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 1

$zip = "$env:TEMP\BioEM-Preview-beta.zip"
$dest = "C:\Users\User\documents\Preview_beta_src"

Invoke-WebRequest -Uri "https://github.com/leekslk/BioEM-Preview/archive/refs/heads/beta/v0.2.zip" -OutFile $zip
if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
Expand-Archive -Path $zip -DestinationPath $dest

cd "$dest\BioEM-Preview-beta-v0.2"
powershell -ExecutionPolicy Bypass -File .\build_windows_release.ps1
```

Once complete, extract `dist\Preview_beta-YYYYMMMDD.zip` and copy its contents (`Preview_beta-YYYYMMMDD.exe` and `_internal\`) into **`C:\Program Files\BioEM Preview\`** as described in Option A.

## Usage
---
1. Open **BioEM Preview** from your Desktop shortcut (the background crawler runs automatically).
2. Click any acquisition under **Available on NAS** (or use **Pull Latest Acquisition** / **Manually Select Folder**).
3. Watch the progress bar for delta sync and (for mosaics) multi-pass alignment with elapsed and remaining time.
4. Fiji opens automatically with either your virtual Z-stack or your aligned TrakEM2 `.xml` project saved on the Desktop.

## Roadmap
---
* **Interactive Preview & Manual Alignment Correction:** Visual preview allowing users to inspect and manually adjust tricky slice/tile alignments that cannot be solved purely mathematically.
* **Slimmer GPU Distribution:** Trimming unused CUDA runtime libraries so GPU-accelerated builds fit within GitHub's 2 GB release asset limit.
* **OME-Zarr / Out-of-Core Viewing:** Optional chunked pyramidal export and interactive 3D inspection.

## License
---
Developed internally at BioEM-EPFL to streamline EM imaging operations. Distributed under the MIT License.
