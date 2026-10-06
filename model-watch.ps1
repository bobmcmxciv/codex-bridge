# model-watch.ps1 - daily: upgrade the Codex CLI if npm has a newer release, then
# report catalog models that were not seen before and test-call each one.
# The upstream catalog is gated by client_version; codex-bridge re-reads the CLI
# version on its own (every 10 min), and ?refresh=1 below makes it immediate.
# Log: logs\model-watch.log   State: model-watch-state.json
# Dry run (no npm install): powershell -File model-watch.ps1 -NoInstall
param([switch]$NoInstall)

$ErrorActionPreference = 'Continue'
$Root = 'C:\Users\Administrator\codex-bridge'
$Log = Join-Path $Root 'logs\model-watch.log'
$StateFile = Join-Path $Root 'model-watch-state.json'
$Npm = 'C:\Program Files\nodejs\npm.cmd'
$Bridge = 'http://127.0.0.1:8902'
$Cx2cc = 'http://<cx2cc-host>:8901'

function Write-Log([string]$msg) {
    $line = '[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Add-Content -Path $Log -Value $line -Encoding UTF8
    Write-Output $line
}

function Get-EnvValue([string]$name) {
    $m = Select-String -Path (Join-Path $Root '.env') -Pattern "^$name=(.*)$" | Select-Object -First 1
    if ($m) { return $m.Matches[0].Groups[1].Value.Trim() }
    return $null
}

function Get-CliVersion {
    $cli = Get-EnvValue 'CODEX_CLI_PATH'
    if (-not $cli -or -not (Test-Path $cli)) { return $null }
    $out = & $cli --version 2>&1 | Out-String
    if ($out -match '(\d+\.\d+\.\d+)') { return $Matches[1] }
    return $null
}

function Get-Catalog([string]$query) {
    $raw = curl.exe -s -m 60 -H "x-api-key: $Token" "$Bridge/v1/models$query"
    try { return $raw | ConvertFrom-Json } catch { return $null }
}

if ((Test-Path $Log) -and (Get-Item $Log).Length -gt 5MB) { Move-Item $Log "$Log.1" -Force }

$Token = Get-EnvValue 'CODEX_BRIDGE_TOKEN'
if (-not $Token) { Write-Log 'ERROR no CODEX_BRIDGE_TOKEN in .env'; exit 1 }

# 1. Upgrade the CLI when npm has a newer release.
$installed = Get-CliVersion
$latest = (& $Npm view '@openai/codex' version 2>$null | Out-String).Trim()
Write-Log "cli installed=$installed npm latest=$latest"
$newer = $false
try { $newer = $installed -and ([version]$latest -gt [version]$installed) } catch { Write-Log "WARN cannot compare versions '$latest' / '$installed'" }
if ($newer) {
    if ($NoInstall) {
        Write-Log "NoInstall: would upgrade $installed -> $latest"
    } else {
        $npmOut = & $Npm install -g "@openai/codex@$latest" 2>&1 | Out-String
        $after = Get-CliVersion
        if ($after -eq $latest) {
            Write-Log "UPGRADED codex cli $installed -> $after"
        } else {
            Write-Log "ERROR upgrade to $latest failed (now $after): $($npmOut.Trim() -replace '\s+', ' ')"
        }
    }
}

# 2. Force the bridge to re-read the CLI version and refetch the catalog.
$cat = Get-Catalog '?refresh=1'
if (-not $cat) { Write-Log 'ERROR bridge /v1/models unreachable'; exit 1 }
$ids = @($cat.data | Where-Object { $_.source -eq 'catalog' } | ForEach-Object { $_.id } | Sort-Object)
Write-Log "bridge client_version=$($cat.client_version) ($($cat.client_version_source)) catalog=$($ids -join ',')"
if ($cat.catalog_error) { Write-Log "WARN catalog_error=$($cat.catalog_error)" }

# 3. Diff against the last run; first run only records a baseline.
$known = @()
if (Test-Path $StateFile) {
    try { $known = @((Get-Content $StateFile -Raw | ConvertFrom-Json).models) } catch { $known = @() }
}
$new = @($ids | Where-Object { $known -notcontains $_ })
if (-not (Test-Path $StateFile)) {
    Write-Log 'baseline recorded (first run)'
    $new = @()
}

# 4. Real call for each new model through cx2cc.
$body = Join-Path $env:TEMP 'model-watch-body.json'
foreach ($m in $new) {
    $json = '{"model":"' + $m + '","max_tokens":50,"messages":[{"role":"user","content":"Reply with exactly: PONG"}]}'
    [IO.File]::WriteAllText($body, $json, (New-Object Text.UTF8Encoding $false))
    $resp = curl.exe -s -m 120 -w "`nHTTP %{http_code}" -H "x-api-key: $Token" -H 'anthropic-version: 2023-06-01' -H 'content-type: application/json' --data-binary "@$body" "$Cx2cc/v1/messages" | Out-String
    $ok = ($resp -match 'HTTP 200') -and ($resp -match 'PONG')
    Write-Log ("NEW MODEL {0}: {1} | {2}" -f $m, $(if ($ok) { 'CALL OK' } else { 'CALL FAILED' }), ($resp.Trim() -replace '\s+', ' ').Substring(0, [Math]::Min(300, ($resp.Trim() -replace '\s+', ' ').Length)))
}
$gone = @($known | Where-Object { $ids -notcontains $_ })
if ($gone.Count) { Write-Log "REMOVED from catalog: $($gone -join ',')" }

$state = @{ models = $ids; client_version = $cat.client_version; checked = (Get-Date -Format 's') } | ConvertTo-Json
[IO.File]::WriteAllText($StateFile, $state, (New-Object Text.UTF8Encoding $false))
Write-Log 'done'
