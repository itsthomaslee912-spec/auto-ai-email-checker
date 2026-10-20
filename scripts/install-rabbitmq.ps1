$ErrorActionPreference = "Stop"
$isAdmin = [Security.Principal.WindowsPrincipal]::new(
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    throw "Run this script from PowerShell opened as Administrator."
}

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$InstallerDir = Join-Path $Root ".tmp\installers"
$ErlangInstaller = Join-Path $InstallerDir "otp_win64_29.1.2.exe"
$RabbitInstaller = Join-Path $InstallerDir "rabbitmq-server-4.4.0.exe"
New-Item -ItemType Directory -Force -Path $InstallerDir | Out-Null

function Get-VerifiedInstaller(
    [string]$Path,
    [string]$Url,
    [long]$Size,
    [string]$Sha256
) {
    if ((Test-Path -LiteralPath $Path) -and (Get-Item -LiteralPath $Path).Length -ne $Size) {
        Write-Host "Resuming $(Split-Path -Leaf $Path)..."
        & curl.exe -L --fail --retry 5 --retry-delay 2 --continue-at - --output $Path $Url
    } elseif (-not (Test-Path -LiteralPath $Path)) {
        Write-Host "Downloading $(Split-Path -Leaf $Path)..."
        & curl.exe -L --fail --retry 5 --retry-delay 2 --output $Path $Url
    }
    if ($LASTEXITCODE -ne 0) { throw "Download failed: $Url" }
    $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash
    if ($actualHash -ne $Sha256) { throw "Checksum verification failed for $Path" }
}

Get-VerifiedInstaller `
    $ErlangInstaller `
    "https://github.com/erlang/otp/releases/download/OTP-29.1.2/otp_win64_29.1.2.exe" `
    155770072 `
    "38297EE6FA8CAFBC016CB187EBDA30F7BDAE85618415D9DBBBE413E9B9E27A4E"
Get-VerifiedInstaller `
    $RabbitInstaller `
    "https://github.com/rabbitmq/rabbitmq-server/releases/download/v4.4.0/rabbitmq-server-4.4.0.exe" `
    24457312 `
    "AEDCC039F02F0A46A223AF00E2553955AF8935AA9636608D54B811FB94EDFF06"

if (-not (Get-Command erl.exe -ErrorAction SilentlyContinue)) {
    Write-Host "Installing Erlang/OTP..."
    $process = Start-Process -FilePath $ErlangInstaller -ArgumentList "/S" -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw "Erlang installation failed with exit code $($process.ExitCode)." }
}

if (-not (Get-Service -Name RabbitMQ -ErrorAction SilentlyContinue)) {
    Write-Host "Installing RabbitMQ..."
    $process = Start-Process -FilePath $RabbitInstaller -ArgumentList "/S" -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw "RabbitMQ installation failed with exit code $($process.ExitCode)." }
}

$RabbitSbin = Get-ChildItem "C:\Program Files\RabbitMQ Server" -Directory |
    Sort-Object Name -Descending |
    ForEach-Object { Join-Path $_.FullName "sbin" } |
    Where-Object { Test-Path (Join-Path $_ "rabbitmq-plugins.bat") } |
    Select-Object -First 1
if (-not $RabbitSbin) { throw "RabbitMQ command directory was not found after installation." }

& (Join-Path $RabbitSbin "rabbitmq-plugins.bat") enable rabbitmq_management
Set-Service -Name RabbitMQ -StartupType Automatic
Start-Service -Name RabbitMQ

$deadline = (Get-Date).AddSeconds(60)
do {
    $listening = Get-NetTCPConnection -LocalPort 5672 -State Listen -ErrorAction SilentlyContinue
    if ($listening) { break }
    Start-Sleep -Seconds 1
} while ((Get-Date) -lt $deadline)
if (-not $listening) { throw "RabbitMQ installed but AMQP port 5672 did not become ready." }

Write-Host "RabbitMQ is ready at amqp://127.0.0.1:5672/."
Write-Host "Run .\run.ps1 to start the application."
