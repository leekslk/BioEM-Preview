# BioEM Preview: Smart Sync & Headless Multi-Layer Alignment

An applet that performs automated acquisition ingestion, synchronization, and volumetric registration framework designed for datasets acquired from MAPS (ThermoFisher), currently supports Single-Tile Z-Stack, 2D Mosaic, and Multi-Layer Mosaic Z-Stack.

## Overview
---
SBF-SEM produces large image streams across 2D spatial tiles and serial sections. There are multiple possible workflows, depending on the user's intent and the project's requirements. Moving these datasets manually across the local network and configuring the alignment parameters in FIJI is time-consuming and prone to errors.

The **BioEM Preview** automates the entire image import and stack alignment or file-assembly lifecycle:
* Detects and categorizes incoming acquisitions in near real-time from the network storage (NAS).
* Performs "fast", time/delta-synchronised transfer to the local high-speed NVMe storage.
* Signals Fiji to execute a multi-pass hierarchical registration (intra-slice 2D stitching followed by inter-slice Z-alignment, as necessary).
* Generates fully configured, aligned TrakEM2 XML project workspaces ready for immediate visual review or downstream segmentation.

*Too Long Didn't Read | TLDR:*
 * *The app runs on the SupportPC and constantly scans for new acquisitions from MAPS.*  
 * *Automatically detects acquisitions that falls broadly into 3 categories: Z-stacks, Mosaic (mutiple tiles) and Mosaic Z-stack.*  
 * *Clicking on the Dataset triggers a synchronisation of .tiff files that are pulled from the NAS and copied into a local folder for speedy visualisation in Fiji/imageJ.*  
 * *Mosaic and Mosaic Z-stacks are automatically aligned and presented in TrakEM2 (takes time, corresponding to the size of the acquisition). Some features may require a right-click at the right place.*

## System Architecture
---
![BioEM Preview System Architecture](docs/system_architecture.svg)

## Core Features & Active Modules
---
### 1. Image Ingestion & Desktop Sync Manager (`Preview_beta-YYYYMMMDD.exe` / `pull_from_NAS.py`)

**First-Run Setup & Settings:** On first launch, a Setup Wizard prompts for the local scratch drive (`DESTINATION_DIR`), NAS root (`NAS_DIR`), and Fiji (Java 8) executable (`APP_PATH`) pre-filled with default paths. Saving automatically deploys `BioEM_Watcher.ijm` to Fiji's `macros/AutoRun/` folder and starts the integrated background crawler (`--indexer`).  
**Live Discovery:** Displays acquisitions performed within the last 14 days (minimum 12 entries) under **Available on NAS**, and catalogs older sessions under **All Available Data**.  
**Smart Delta Synchronization:** Compares file sizes and modification timestamps (`st_mtime`) between NAS and `<DESTINATION_DIR>/BioEM_Cache/`. Skips untouched frames and only copies new or updated `.tif` files.  
**Coordinate Pattern Extraction:** Parses tile grid configurations via regular expressions (`Tile_<dim1>-<dim2>-<z>`) supporting real-time indexing even when an acquisition is in progress.  
**Automated Housekeeping:** Background worker prunes cached folders inside `<DESTINATION_DIR>/BioEM_Cache/` older than 30 days.  
**Workflow Routing:**  
* **Simple Z-Stack:** Syncs to local NVMe cache and triggers Fiji (`BioEM_Watcher.ijm`) to open as a virtual stack (`Image Sequence... sort use`) with inverted LUT.  
* **2D Mosaic & Mosaic Z-Stack:** Runs `bioem_torch_align.py` directly inside the app with live per-pass progress, elapsed time, and ETA, writes a native TrakEM2 `.xml` project on the Desktop, and opens it in Java 8 Fiji.

### 2. Context Menu & Dataset Operations

Right-clicking any dataset entry in the UI provides:  
**Refresh Folder (Wipe & Sync):** Displays a safety confirmation dialog, deletes the local cached dataset directory, and executes a fresh synchronization from the NAS source.  
**Delete Local Cache:** Displays a confirmation prompt showing the cached folder name and permanently purges it from the local scratch disk (without touching NAS raw data).  

### 3. PyTorch GPU/CPU Alignment Engine (`bioem_torch_align.py`)
* **Pass 1 (2D Intra-Section Montaging):** Cross-correlates overlapping tile strips via bandpass-filtered FFT phase correlation (CUDA multi-GPU / Apple MPS / CPU) and solves global tile coordinates with weighted sparse least squares (`scipy.sparse.linalg.lsqr`).
* **Pass 2 (Layer-as-Rigid-Unit Z-Alignment):** Aligns consecutive Z-sections using per-tile phase correlation and centroid-centered 2D Procrustes rigid fitting with median-displacement outlier rejection.
* **Pass 3 (Native TrakEM2 XML Export):** Writes a Java 8 TrakEM2 `.xml` project directly to `Desktop/<ProjectName>/<ProjectName>.xml` and launches Fiji.

## Hardware & Software Requirements

The application and associated helper scripts were designed to accomodated MAPS project acquisitions. However, similar Apreo 2S or acquisition systems could probably recycle most of the processing steps with minimal modifications.
* **Operating System:** Windows 10/11 Pro (x64)
* **Storage:** Local NVMe SSD dedicated to caching (`D:/`) and mapped high-throughput NAS storage (`I:/`)
* **Fiji:** Fiji (ImageJ 1.54+) bundled with TrakEM2 and Python/Jython 2.7 scripting support
* **Host Python Environment:** Python 3.10+ (for building the `.exe` or running source scripts)

## Installation & Deployment

You can deploy the self-contained Windows release archive (`Preview_beta-YYYYMMMDD.zip`) or build it from source on a Windows workstation.

### Method A: Quick Start via Self-Contained Release Bundle (Recommended)

1. Download and extract `Preview_beta-YYYYMMMDD.zip` anywhere on the workstation (keep `Preview_beta-YYYYMMMDD.exe` and the `_internal\` folder together).
2. Double-click `Preview_beta-YYYYMMMDD.exe`.
3. On first launch, confirm or browse for the three paths in the **First-Run Setup Wizard** (pre-filled with the default `#CONFIGURATION` paths):
   * **Scratch Disk (`DESTINATION_DIR`):** Local fast SSD (default `D:/`)
   * **NAS Raw Data (`NAS_DIR`):** Network acquisition share (default `I:/`)
   * **Fiji Executable (`APP_PATH`):** Java 8 Fiji executable (e.g., `C:\Users\User\Desktop\Fiji\fiji-windows-x64.exe`)
   * **Background Indexer Auto-Start:** Automatically registers `Preview_beta-YYYYMMMDD.exe --indexer` in Windows Startup (`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`).
4. Click **Save & Deploy Sidecars** — the app stores settings in `%APPDATA%\BioEM_Preview\config.json`, deploys `BioEM_Watcher.ijm` into `<Fiji>\macros\AutoRun\`, and starts the background NAS indexer immediately with **no separate Python installation required**.

### Method B: Building `Preview_beta-YYYYMMMDD.zip` from Source (Windows)

#### 1. Set Up Python Environment & Dependencies

```powershell
python -m venv venv
.\venv\Scripts\activate

# Example with PyTorch CUDA 11.8 (or CPU wheel)
pip install torch --index-url https://download.pytorch.org/whl/cu118
pip install customtkinter psutil numpy scipy tifffile pyinstaller
```

#### 2. Build the Self-Contained Package

```powershell
.\build_windows_release.ps1
```

This invokes `Preview_beta.spec` (`--onedir` PyInstaller build) and outputs `dist\Preview_beta-YYYYMMMDD.zip` ready for distribution.

## Usage Guide
---
### 1. Background Crawler (`--indexer`)

The background crawler is built directly into `Preview_beta-YYYYMMMDD.exe --indexer` (and starts automatically on app launch / Windows login when enabled in Setup or **⚙️ Settings & Paths**). It periodically crawls `<NAS_DIR>/Maps` and caches acquisition metadata under `%APPDATA%\BioEM_Preview\fiji_latest_cache.json`.

### 2. Launch the Application

Double-click `Preview_beta-YYYYMMMDD.exe` (or run `python pull_from_NAS.py` from source):

1. Find your acquisition under **Available on NAS** (or **All Available Data**).
2. Click the dataset button to begin the delta sync to `<DESTINATION_DIR>\BioEM_Cache\`.
3. When the sync completes, the pipeline routes automatically:
   * **Single-Tile Z-Stacks:** Triggers Fiji (`BioEM_Watcher.ijm`) to open the folder as a virtual stack (`Image Sequence... sort use`) with inverted LUT.
   * **2D Mosaics & Mosaic Z-Stacks:** Runs `bioem_torch_align.py` with live per-pass progress, elapsed time, and remaining time estimates, exports `Desktop\<ProjectName>\<ProjectName>.xml`, and opens the TrakEM2 project in Fiji.

## Roadmap
---
This deployment architecture is tailored for the current Fiji/TrakEM2 processing model. Future releases will introduce the following extensions:  
**GPU-Accelerated Processing Engine (`bioem_proc.py`):** Standalone headless alignment pipeline built on PyTorch and CUDA for sub-pixel Fourier phase correlation, offloading computation directly to the dual NVIDIA RTX GPUs (A4000 on the SupportPC).  
**OME-Zarr Streaming Integration:** Direct compilation of synchronized acquisitions into chunked, pyramidal OME-Zarr array structures on NVMe storage, bypassing RAM limits during multi-slice processing (considering implementation using the webknossos CLI for compatibility).  
**Napari Interactive Volume Viewer (`view_in_napari.py`):** Immediate out-of-core inspection interface launched via a dedicated right-click action (**"Show Me the Future"**) for interactive review alongside TrakEM2 projects.  
**Unified Standalone Installer:** Packaging of the Python host, GPU runtime, and viewer components into a streamlined installation wizard.  

## License
---
This project is developed for use internally at BioEM-EPFL to facilitate imaging operations and automated pipelines. Distributed under the MIT License.