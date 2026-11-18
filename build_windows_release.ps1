# Build lightweight self-bootstrapping Windows package and ZIP archive:
#   dist\Preview_beta-YYYYMMMDD.zip
# Usage (PowerShell on Windows):
#   .\build_windows_release.ps1

$ErrorActionPreference = "Stop"

Push-Location $PSScriptRoot
try {
    $DateStamp = (Get-Date).ToString("yyyyMMMdd", [System.Globalization.CultureInfo]::InvariantCulture).ToUpper()
    $BuildName = "Preview_beta-$DateStamp"
    $env:BIOEM_BUILD_NAME = $BuildName

    Write-Host "=== Building Lightweight Core App: $BuildName ===" -ForegroundColor Cyan

    if (-not (Test-Path "Preview_beta.spec")) {
        throw "Preview_beta.spec not found in $PSScriptRoot."
    }

    # Verify only lightweight core build dependencies (no torch/scipy needed in build env)
    python -c "import customtkinter, psutil, PyInstaller; print('Core build deps OK')"
    if ($LASTEXITCODE -ne 0) {
        throw "Missing core build dependencies. Install with: pip install customtkinter psutil pyinstaller"
    }

    # Bundle static uv.exe (~35 MB) so the app can bootstrap Python + PyTorch offline from system Python
    if (-not (Test-Path "tools\uv.exe")) {
        Write-Host "Downloading uv.exe for self-bootstrapping..." -ForegroundColor Cyan
        New-Item -ItemType Directory -Force -Path "tools" | Out-Null
        $uvZip = "$env:TEMP\uv-win64.zip"
        Invoke-WebRequest -Uri "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip" -OutFile $uvZip
        $uvExtract = "$env:TEMP\uv-win64-extract"
        if (Test-Path $uvExtract) { Remove-Item -Recurse -Force $uvExtract }
        Expand-Archive -Path $uvZip -DestinationPath $uvExtract
        $uvFound = Get-ChildItem -Path $uvExtract -Filter "uv.exe" -Recurse | Select-Object -First 1
        if (-not $uvFound) { throw "uv.exe not found inside downloaded archive." }
        Copy-Item -Path $uvFound.FullName -Destination "tools\uv.exe" -Force
        Remove-Item -Force $uvZip -ErrorAction SilentlyContinue
        Remove-Item -Recurse -Force $uvExtract -ErrorAction SilentlyContinue
    }

    # Clean prior build artifacts for this version
    if (Test-Path "build\$BuildName") { Remove-Item -Recurse -Force "build\$BuildName" }
    if (Test-Path "dist\$BuildName") { Remove-Item -Recurse -Force "dist\$BuildName" }
    if (Test-Path "dist\$BuildName*.zip") { Remove-Item -Force "dist\$BuildName*.zip" }

    pyinstaller --clean --noconfirm Preview_beta.spec
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller build failed with exit code $LASTEXITCODE."
    }

    $OutDir = "dist\$BuildName"
    if (-not (Test-Path "$OutDir\$BuildName.exe")) {
        throw "Expected executable not found: $OutDir\$BuildName.exe"
    }

    $QuickStart = @"
BioEM Preview ($BuildName) - Quick Start
========================================
1. Extract this folder (keep $BuildName.exe and _internal\ together) into:
   C:\Program Files\BioEM Preview\
2. Create a Desktop shortcut to $BuildName.exe and launch it.
3. On first launch, confirm your Scratch Disk, NAS Drive, and Fiji path, then
   click "Install / Update Engine" (auto-detects NVIDIA CUDA GPU vs CPU and
   installs an isolated runtime in %LOCALAPPDATA%\BioEM_Preview\runtime).
4. Click "Save & Deploy Sidecars" to install BioEM_Watcher.ijm into Fiji and
   start the background NAS indexer automatically.
"@
    Set-Content -Path "$OutDir\README_QUICKSTART.txt" -Value $QuickStart -Encoding UTF8

    $ZipPath = "dist\$BuildName.zip"
    Write-Host "Creating archive: $ZipPath ..." -ForegroundColor Cyan
    Compress-Archive -Path $OutDir -DestinationPath $ZipPath -CompressionLevel Optimal

    $SizeMB = [math]::Round((Get-Item $ZipPath).Length / 1MB, 1)
    Write-Host "Done! Release bundle ready at: $ZipPath ($SizeMB MB)" -ForegroundColor Green
}
finally {
    Pop-Location
}
