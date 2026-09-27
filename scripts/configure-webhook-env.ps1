param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^https://')]
    [string]$WebhookBaseUrl,
    [string]$GmailPushServiceAccount = ""
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$baseUrl = $WebhookBaseUrl.TrimEnd("/")
$values = [ordered]@{
    WEBHOOK_BASE_URL = $baseUrl
    GMAIL_PUBSUB_AUDIENCE = "$baseUrl/api/webhooks/gmail"
    AUTO_SYNC_INTERVAL_SECONDS = "120"
    AUTO_SYNC_MAX_MESSAGES = "200"
}
if ($GmailPushServiceAccount.Trim()) {
    $values.GMAIL_PUBSUB_SERVICE_ACCOUNT = $GmailPushServiceAccount.Trim()
}

function Set-EnvValues([string]$Path, [System.Collections.IDictionary]$Updates) {
    $lines = [System.Collections.Generic.List[string]]::new()
    if (Test-Path $Path) {
        foreach ($existingLine in Get-Content $Path) {
            $lines.Add([string]$existingLine)
        }
    }
    foreach ($entry in $Updates.GetEnumerator()) {
        $prefix = "$($entry.Key)="
        $index = -1
        for ($i = 0; $i -lt $lines.Count; $i++) {
            if ($lines[$i].StartsWith($prefix, [System.StringComparison]::Ordinal)) {
                $index = $i
                break
            }
        }
        $line = "$prefix$($entry.Value)"
        if ($index -ge 0) {
            $lines[$index] = $line
        } else {
            $lines.Add($line)
        }
    }
    Set-Content -LiteralPath $Path -Value $lines -Encoding UTF8
}

Set-EnvValues (Join-Path $Root ".env") $values
Set-EnvValues (Join-Path $Root "backend\.env") $values
Write-Host "Webhook and automatic-sync environment settings updated."
