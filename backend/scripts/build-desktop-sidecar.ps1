$ErrorActionPreference = "Stop"
$Backend = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $Backend ".venv\Scripts\python.exe"
$Output = Join-Path (Split-Path -Parent $Backend) "src-tauri\binaries"

if (-not (Test-Path $VenvPython)) {
  throw "Backend virtual environment not found. Run scripts\setup.bat first."
}

& $VenvPython -m pip install pyinstaller
& $VenvPython -m PyInstaller --noconfirm --clean --log-level WARN --onefile --name email-checker-backend `
  --paths $Backend `
  --distpath $Output `
  --workpath (Join-Path $Backend "build-desktop") `
  --specpath (Join-Path $Backend "build-desktop") `
  (Join-Path $Backend "desktop_server.py")

# Tauri selects the target-specific sidecar name during Windows builds.
$TargetBinary = Join-Path $Output "email-checker-backend-x86_64-pc-windows-msvc.exe"
Move-Item -Force (Join-Path $Output "email-checker-backend.exe") $TargetBinary
