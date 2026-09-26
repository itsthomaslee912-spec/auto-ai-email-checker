$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PgRoot = Join-Path $Root ".local\postgresql"
$PgCtl = Join-Path $PgRoot "bin\pg_ctl.exe"
$DataDir = Join-Path $PgRoot "data"
$LogDir = Join-Path $Root "logs"
$LogFile = Join-Path $LogDir "postgresql.log"

if (-not (Test-Path $PgCtl)) {
    throw "Local PostgreSQL binaries are missing. Complete PostgreSQL setup first."
}
if (-not (Test-Path (Join-Path $DataDir "PG_VERSION"))) {
    throw "Local PostgreSQL data directory is not initialized. Complete PostgreSQL setup first."
}

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
& $PgCtl -D $DataDir status *> $null
if ($LASTEXITCODE -eq 0) {
    Write-Host "PostgreSQL already running at 127.0.0.1:5432"
    return
}

& $PgCtl -D $DataDir -l $LogFile -o "-h 127.0.0.1 -p 5432" start
if ($LASTEXITCODE -ne 0) {
    throw "PostgreSQL failed to start. Check logs/postgresql.log."
}
Write-Host "PostgreSQL running at 127.0.0.1:5432"
