# Point Claude Code at the cx2cc endpoint on vircs (Windows).
#
#   . .\client-setup.ps1            # this shell only
#   .\client-setup.ps1 -Persist     # also store as a user-level env var
#
# Requires the machine to be on the same Tailscale tailnet as vircs.

param([switch]$Persist)

$Cx2ccUrl = 'http://<cx2cc-host>:8901'
$Cx2ccKey = '<shared-secret>'

$env:ANTHROPIC_BASE_URL = $Cx2ccUrl
$env:ANTHROPIC_API_KEY  = $Cx2ccKey
# Claude Code sends ANTHROPIC_AUTH_TOKEN as `Authorization: Bearer`, but cx2cc
# only reads `x-api-key`. Clear it so it cannot shadow the key above.
Remove-Item Env:ANTHROPIC_AUTH_TOKEN -ErrorAction SilentlyContinue

if ($Persist) {
    [Environment]::SetEnvironmentVariable('ANTHROPIC_BASE_URL', $Cx2ccUrl, 'User')
    [Environment]::SetEnvironmentVariable('ANTHROPIC_API_KEY',  $Cx2ccKey, 'User')
    [Environment]::SetEnvironmentVariable('ANTHROPIC_AUTH_TOKEN', $null,   'User')
    Write-Host 'Persisted to user environment (new shells pick it up).'
}

Write-Host "ANTHROPIC_BASE_URL=$env:ANTHROPIC_BASE_URL"
try {
    $h = Invoke-RestMethod "$Cx2ccUrl/health" -TimeoutSec 10
    Write-Host ("health: " + ($h | ConvertTo-Json -Compress))
} catch {
    Write-Warning "UNREACHABLE - check 'tailscale status'. $_"
}
try {
    $u = Invoke-RestMethod "$Cx2ccUrl/usage" -TimeoutSec 10 -Headers @{ 'x-api-key' = $Cx2ccKey }
    Write-Host ("usage: plan=" + $u.plan_type + " (subscription quota, served from vircs)")
} catch {
    Write-Warning "usage endpoint failed: $_"
}
Write-Host 'CC Switch quota display: paste cc-switch-usage-script.js (next to this script)'
Write-Host 'into 供应商卡片 → 用量查询(📊) → 自定义. {{baseUrl}}/{{apiKey}} come from the provider.'
