$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

$requiredPostgresPaths = @(
    (Join-Path $Root ".local\postgresql\bin\pg_ctl.exe"),
    (Join-Path $Root ".local\postgresql\bin\initdb.exe"),
    (Join-Path $Root ".local\postgresql\lib"),
    (Join-Path $Root ".local\postgresql\share")
)
foreach ($path in $requiredPostgresPaths) {
    if (-not (Test-Path $path)) {
        throw "Local PostgreSQL runtime is incomplete: $path"
    }
}

& (Join-Path $Root "backend\scripts\build-desktop-sidecar.ps1")
if ($LASTEXITCODE -ne 0) { throw "Backend sidecar build failed." }

Push-Location $Root
try {
    & (Join-Path $Root "frontend\node_modules\.bin\tauri.cmd") build
    if ($LASTEXITCODE -ne 0) { throw "Tauri desktop build failed." }
} finally {
    Pop-Location
}
