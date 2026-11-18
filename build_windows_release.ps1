# Build self-contained Windows --onedir package and ZIP archive:
#   dist\Preview_beta-YYYYMMMDD.zip
# Usage (PowerShell on Windows with your target Python env active):
#   .\build_windows_release.ps1

$ErrorActionPreference = "Stop"

Push-Location $PSScriptRoot
try {
    $DateStamp = (Get-Date).ToString("yyyyMMMdd", [System.Globalization.CultureInfo]::InvariantCulture).ToUpper()
    $BuildName = "Preview_beta-$DateStamp"
    $env:BIOEM_BUILD_NAME = $BuildName

    Write-Host "=== Building $BuildName ===" -ForegroundColor Cyan

    if (-not (Test-Path "Preview_beta.spec")) {
        throw "Preview_beta.spec not found in $PSScriptRoot. Make sure all files from branch beta/v0.2 are pulled/copied into this folder."
    }

    # Verify required build & runtime dependencies
    python -c "import customtkinter, psutil, numpy, scipy, tifffile, torch, PyInstaller; print('Torch:', torch.__version__, '| CUDA compiled:', torch.version.cuda)"
    if ($LASTEXITCODE -ne 0) {
        throw "Python dependency check failed."
    }

    # Clean prior build artifacts for this version
    if (Test-Path "build\$BuildName") { Remove-Item -Recurse -Force "build\$BuildName" }
    if (Test-Path "dist\$BuildName") { Remove-Item -Recurse -Force "dist\$BuildName" }
    if (Test-Path "dist\$BuildName.zip") { Remove-Item -Force "dist\$BuildName.zip" }

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
1. Extract this entire folder (keep $BuildName.exe and _internal\ together).
2. Double-click $BuildName.exe.
3. On first launch, confirm:
   - Scratch Disk (DESTINATION_DIR, default D:\)
   - NAS Raw Data (NAS_DIR, default I:\)
   - Fiji (Java 8) Executable (APP_PATH)
   - Auto-start background NAS indexer at Windows login
4. Click "Save & Deploy Sidecars".
   BioEM_Watcher.ijm will be placed into <Fiji>\macros\AutoRun\ automatically,
   and the NAS indexer will run in the background with no separate Python install needed.
"@
    Set-Content -Path "$OutDir\README_QUICKSTART.txt" -Value $QuickStart -Encoding UTF8

    $Suffix = $env:BIOEM_ZIP_SUFFIX
    $ZipName = if ($Suffix) { "$BuildName-$Suffix.zip" } else { "$BuildName.zip" }
    $ZipPath = "dist\$ZipName"
    Write-Host "Creating self-contained archive: $ZipPath ..." -ForegroundColor Cyan
    Compress-Archive -Path $OutDir -DestinationPath $ZipPath -CompressionLevel Optimal

    Write-Host "Done! Release bundle ready at: $ZipPath" -ForegroundColor Green
}
finally {
    Pop-Location
}
