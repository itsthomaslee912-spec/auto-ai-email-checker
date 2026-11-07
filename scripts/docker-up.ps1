param(
    [switch]$Build,
    [switch]$Attach
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker is not available. Install Docker Desktop and start it first."
}

try {
    docker info *> $null
    if ($LASTEXITCODE -ne 0) { throw "Docker daemon unavailable" }
} catch {
    throw "Docker Desktop is not running. Start it, then retry."
}

if (-not (Test-Path (Join-Path $Root ".env"))) {
    if (Test-Path (Join-Path $Root ".env.example")) {
        Copy-Item (Join-Path $Root ".env.example") (Join-Path $Root ".env")
        Write-Host "Created .env from .env.example. Fill in OAuth and AI keys before connecting mailboxes."
    } else {
        throw ".env is missing."
    }
}

# Native RabbitMQ / app bind the same host ports. Prefer a clean Docker stack.
if (Test-Path (Join-Path $Root "stop.bat")) {
    Write-Host "Stopping native Windows services that may hold Docker ports..."
    & cmd.exe /c (Join-Path $Root "stop.bat")
}

$composeArgs = @("compose", "up", "--remove-orphans")
if ($Build) { $composeArgs += "--build" }
if (-not $Attach) { $composeArgs += "-d" }

Write-Host "Starting Auto AI Email Checker in Docker..."
& docker @composeArgs
if ($LASTEXITCODE -ne 0) {
    throw "docker compose up failed with exit code $LASTEXITCODE"
}

if (-not $Attach) {
    Write-Host ""
    Write-Host "UI:          http://localhost:5173"
    Write-Host "API:         http://localhost:8000/api/health"
    Write-Host "RabbitMQ UI: http://localhost:15672  (guest / guest)"
    Write-Host "Logs:        docker compose logs -f"
    Write-Host "Stop:        .\docker-down.bat"
}
