# Installs codex-bridge + cx2cc as Windows services via NSSM.
# Run elevated.  Idempotent: removes any existing services of the same name first.

$ErrorActionPreference = 'Stop'

$Python     = 'C:\Users\Administrator\cx2cc\.venv\Scripts\python.exe'
$BridgeDir  = 'C:\Users\Administrator\codex-bridge'
$Cx2ccDir   = 'C:\Users\Administrator\cx2cc'
$CodexHome  = 'C:\Users\Administrator\.codex'
$LogDir     = 'C:\Users\Administrator\codex-bridge\logs'

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Remove-SvcIfPresent($name) {
    if (Get-Service -Name $name -ErrorAction SilentlyContinue) {
        Write-Host "removing existing service $name"
        & nssm stop $name confirm 2>&1 | Out-Null
        & nssm remove $name confirm 2>&1 | Out-Null
        Start-Sleep -Seconds 2
    }
}

Remove-SvcIfPresent 'cx2cc'
Remove-SvcIfPresent 'codex-bridge'

# --- codex-bridge : loopback only, holds the Codex OAuth session --------------
& nssm install codex-bridge $Python 'codex_bridge.py'
& nssm set codex-bridge AppDirectory $BridgeDir
# Service runs as LocalSystem, whose home is NOT C:\Users\Administrator, so the
# Codex credential location has to be passed explicitly.
& nssm set codex-bridge AppEnvironmentExtra "CODEX_HOME=$CodexHome" 'PYTHONUNBUFFERED=1'
& nssm set codex-bridge AppStdout "$LogDir\codex-bridge.out.log"
& nssm set codex-bridge AppStderr "$LogDir\codex-bridge.err.log"
& nssm set codex-bridge AppRotateFiles 1
& nssm set codex-bridge AppRotateBytes 10485760
& nssm set codex-bridge Start SERVICE_AUTO_START
& nssm set codex-bridge AppExit Default Restart
& nssm set codex-bridge AppRestartDelay 5000
& nssm set codex-bridge Description 'codex-bridge: OpenAI chat/completions -> ChatGPT Codex Responses (127.0.0.1:8902)'

# --- cx2cc : Tailscale-facing front door -------------------------------------
& nssm install cx2cc $Python 'start-cx2cc.py serve'
& nssm set cx2cc AppDirectory $Cx2ccDir
& nssm set cx2cc AppEnvironmentExtra 'PYTHONUNBUFFERED=1'
& nssm set cx2cc AppStdout "$LogDir\cx2cc.out.log"
& nssm set cx2cc AppStderr "$LogDir\cx2cc.err.log"
& nssm set cx2cc AppRotateFiles 1
& nssm set cx2cc AppRotateBytes 10485760
& nssm set cx2cc Start SERVICE_AUTO_START
# cx2cc binds a Tailscale address, so it cannot start before Tailscale has one.
# Restart-on-exit covers the boot race where the interface is not up yet.
& nssm set cx2cc DependOnService Tailscale codex-bridge
& nssm set cx2cc AppExit Default Restart
& nssm set cx2cc AppRestartDelay 5000
& nssm set cx2cc Description 'cx2cc: Anthropic /v1/messages -> OpenAI chat/completions (Tailscale <cx2cc-host>:8901)'

Start-Service codex-bridge
Start-Sleep -Seconds 3
Start-Service cx2cc
Start-Sleep -Seconds 3

Get-Service codex-bridge, cx2cc | Select-Object Name, Status, StartType | Format-Table -AutoSize
