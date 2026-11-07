param(
    [switch]$Volumes
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

$args = @("compose", "down", "--remove-orphans")
if ($Volumes) { $args += "--volumes" }

& docker @args
if ($LASTEXITCODE -ne 0) {
    throw "docker compose down failed with exit code $LASTEXITCODE"
}

Write-Host "Docker stack stopped."
