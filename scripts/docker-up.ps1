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

function Get-DotEnvValue([string]$Key) {
    $line = Get-Content (Join-Path $Root ".env") | Where-Object { $_ -match ("^\s*" + [regex]::Escape($Key) + "\s*=") } | Select-Object -First 1
    if (-not $line) { return $null }
    return ($line -replace ("^\s*" + [regex]::Escape($Key) + "\s*="), "").Trim().Trim([char]34).Trim([char]39)
}

# ngrok runs as a Compose service by default. Prefer NGROK_AUTHTOKEN from the
# environment / .env; otherwise reuse the local Windows ngrok config token.
if (-not $env:NGROK_AUTHTOKEN) {
    $fromEnvFile = Get-DotEnvValue "NGROK_AUTHTOKEN"
    if ($fromEnvFile) {
        $env:NGROK_AUTHTOKEN = $fromEnvFile
    } else {
        $ngrokConfig = Join-Path $env:LOCALAPPDATA "ngrok\ngrok.yml"
        if (Test-Path $ngrokConfig) {
            $tokenLine = Get-Content $ngrokConfig | Where-Object { $_ -match '^\s*authtoken:\s*(\S+)' } | Select-Object -First 1
            if ($tokenLine -match '^\s*authtoken:\s*(\S+)') {
                $env:NGROK_AUTHTOKEN = $Matches[1]
                Write-Host "Using ngrok authtoken from $ngrokConfig"
            }
        }
    }
}
if (-not $env:NGROK_AUTHTOKEN) {
    throw "NGROK_AUTHTOKEN is required for the Docker ngrok tunnel. Set it in .env or run: ngrok config add-authtoken <token>"
}

$webhookBaseUrl = Get-DotEnvValue "WEBHOOK_BASE_URL"
if (-not $webhookBaseUrl -or $webhookBaseUrl -notmatch 'ngrok(-free)?\.(app|dev)') {
    Write-Warning "WEBHOOK_BASE_URL should be your reserved ngrok HTTPS URL so the Docker tunnel can bind it."
}

# Native RabbitMQ / app bind the same host ports. Prefer a clean Docker stack.
if (Test-Path (Join-Path $Root "stop.bat")) {
    Write-Host "Stopping native Windows services that may hold Docker ports..."
    & cmd.exe /c (Join-Path $Root "stop.bat")
}

$composeArgs = @("compose", "up", "--remove-orphans")
if ($Build) { $composeArgs += "--build" }
if (-not $Attach) { $composeArgs += "-d" }

Write-Host "Starting Auto AI Email Checker in Docker (ngrok enabled)..."
& docker @composeArgs
if ($LASTEXITCODE -ne 0) {
    throw "docker compose up failed with exit code $LASTEXITCODE"
}

if (-not $Attach) {
    Write-Host ""
    Write-Host "UI:          http://localhost:5173"
    Write-Host "API:         http://localhost:8000/api/health"
    Write-Host "ngrok:       http://localhost:4040  (inspect)"
    if ($webhookBaseUrl) {
        Write-Host "Webhook:     $webhookBaseUrl"
    }
    Write-Host "RabbitMQ UI: http://localhost:15672  (guest / guest)"
    Write-Host "Logs:        docker compose logs -f"
    Write-Host "Stop:        .\docker-down.bat"
}
