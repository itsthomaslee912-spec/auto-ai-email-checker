param(
    [switch]$SkipLocalStop
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

$pgBin = Join-Path $Root ".local\postgresql\bin"
$pgData = Join-Path $Root ".local\postgresql\data"
$dumpDir = Join-Path $Root ".local\db-migrate"
$localPort = 5433

if (-not (Test-Path (Join-Path $pgBin "pg_dump.exe"))) {
    throw "Local PostgreSQL tools not found under .local\postgresql\bin"
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker is not available."
}
if (-not (Test-Path (Join-Path $Root ".env"))) {
    throw ".env is missing."
}

$dbLine = (Get-Content (Join-Path $Root ".env") | Where-Object { $_ -match "^DATABASE_URL=" } | Select-Object -First 1)
if (-not ($dbLine -match "postgresql\+psycopg://([^:]+):([^@]+)@")) {
    throw "Could not parse DATABASE_URL from .env"
}
$pgUser = $Matches[1]
$env:PGPASSWORD = $Matches[2]
$env:PGGSSENCMODE = "disable"

New-Item -ItemType Directory -Force -Path $dumpDir | Out-Null
$dumpFile = Join-Path $dumpDir ("email_checker_{0:yyyyMMdd_HHmmss}.dump" -f (Get-Date))
$pgLog = Join-Path $Root "logs\postgresql-migrate.log"
New-Item -ItemType Directory -Force -Path (Split-Path $pgLog) | Out-Null

try {
    Write-Host "Starting local Postgres on 127.0.0.1:$localPort ..."
    & "$pgBin\pg_ctl.exe" -D $pgData status *> $null
    if ($LASTEXITCODE -ne 0) {
        & "$pgBin\pg_ctl.exe" -D $pgData -l $pgLog -o "-h 127.0.0.1 -p $localPort" -t 120 -w start
        if ($LASTEXITCODE -ne 0) { throw "Failed to start local Postgres on port $localPort" }
    }

    Write-Host "Dumping local email_checker database..."
    & "$pgBin\pg_dump.exe" --host=127.0.0.1 --port=$localPort --username=$pgUser --dbname=email_checker -w `
        --format=custom --no-owner --no-acl --file=$dumpFile
    if ($LASTEXITCODE -ne 0) { throw "pg_dump failed with exit code $LASTEXITCODE" }
    Write-Host ("Dump ready: {0} ({1:N1} MB)" -f $dumpFile, ((Get-Item $dumpFile).Length / 1MB))

    Write-Host "Stopping Docker app services..."
    docker compose stop api sync-worker classification-worker event-relay notification-service frontend | Out-Null

    Write-Host "Recreating Docker Postgres volume..."
    docker compose rm -sf postgres | Out-Null
    docker volume rm auto-ai-email-checker_postgres_data 2>$null | Out-Null
    docker compose up -d postgres
    if ($LASTEXITCODE -ne 0) { throw "Failed to start Docker Postgres" }

    $deadline = (Get-Date).AddMinutes(2)
    do {
        Start-Sleep -Seconds 2
        $healthy = docker inspect --format="{{.State.Health.Status}}" auto-ai-email-checker-postgres-1 2>$null
    } while ($healthy -ne "healthy" -and (Get-Date) -lt $deadline)
    if ($healthy -ne "healthy") { throw "Docker Postgres did not become healthy" }

    Write-Host "Restoring dump into Docker Postgres..."
    docker compose cp $dumpFile "postgres:/tmp/migrate.dump"
    docker compose exec -T postgres pg_restore -U email_app -d email_platform --clean --if-exists --no-owner --no-acl /tmp/migrate.dump
    if ($LASTEXITCODE -ne 0) { throw "pg_restore failed with exit code $LASTEXITCODE" }
    docker compose exec -T postgres rm -f /tmp/migrate.dump | Out-Null

    Write-Host "Docker row counts:"
    docker compose exec -T postgres psql -U email_app -d email_platform -c @"
SELECT 'users' AS t, count(*)::text AS n FROM users
UNION ALL SELECT 'mailbox_connections', count(*)::text FROM mailbox_connections
UNION ALL SELECT 'email_messages', count(*)::text FROM email_messages
UNION ALL SELECT 'calendar_events', count(*)::text FROM calendar_events
UNION ALL SELECT 'notifications', count(*)::text FROM notifications
UNION ALL SELECT 'activity_events', count(*)::text FROM activity_events
ORDER BY 1;
"@

    Write-Host "Starting full Docker stack..."
    docker compose up -d
    if ($LASTEXITCODE -ne 0) { throw "docker compose up failed" }

    Write-Host ""
    Write-Host "Migration complete."
    Write-Host "UI:  http://localhost:5173"
    Write-Host "API: http://localhost:8000/api/health"
}
finally {
    Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
    if (-not $SkipLocalStop) {
        & "$pgBin\pg_ctl.exe" -D $pgData -m fast -t 60 stop *> $null
    }
}
