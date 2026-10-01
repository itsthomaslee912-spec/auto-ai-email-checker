$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path.TrimEnd("\")
$escapedRoot = [regex]::Escape($root)
$pgCtl = Join-Path $root ".local\postgresql\bin\pg_ctl.exe"
$pgData = Join-Path $root ".local\postgresql\data"
if ((Test-Path $pgCtl) -and (Test-Path $pgData)) {
    & $pgCtl -D $pgData stop -m fast 2>$null
}
$processes = @(Get-CimInstance Win32_Process)

$roots = @($processes | Where-Object {
    $line = $_.CommandLine
    if (-not $line) { return $false }
    ($line -match "$escapedRoot\\scripts\\run-(backend|frontend)\.bat") -or
    ($line -match "$escapedRoot\\scripts\\supervise-services\.ps1") -or
    ($line -match "$escapedRoot\\backend\\\.venv\\Scripts\\python\.exe" -and $line -match "uvicorn app\.main:app") -or
    ($line -match "$escapedRoot\\frontend\\node_modules\\.*vite\.js") -or
    ($_.Name -in @("cmd.exe", "ngrok.exe") -and $line -match "\bngrok(?:\.exe)?`"?\s+http\s+8000\b")
})

if (-not $roots.Count) {
    Write-Host "No email checker servers or ngrok tunnel are running."
    exit 0
}

$depth = @{}
foreach ($process in $roots) { $depth[[int]$process.ProcessId] = 0 }
do {
    $added = $false
    foreach ($process in $processes) {
        $processId = [int]$process.ProcessId
        $parentId = [int]$process.ParentProcessId
        if (-not $depth.ContainsKey($processId) -and $depth.ContainsKey($parentId)) {
            $depth[$processId] = $depth[$parentId] + 1
            $added = $true
        }
    }
} while ($added)

$targets = @($processes | Where-Object { $depth.ContainsKey([int]$_.ProcessId) } |
    Sort-Object { $depth[[int]$_.ProcessId] } -Descending)
foreach ($process in $targets) {
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
}
Write-Host "Stopped email checker backend, frontend, and ngrok processes."
