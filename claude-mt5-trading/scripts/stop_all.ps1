#Requires -Version 5.1
<#
.SYNOPSIS
    Arrête proprement les processus du laboratoire Claude-MT5-Trading (jamais le terminal MT5).

.DESCRIPTION
    1. Lit state\pids.json (et repère aussi d'éventuels processus python "tradinglab" orphelins du venv).
    2. Envoie la commande PAUSE à l'orchestrateur : python -m tradinglab.api.cli PAUSE.
    3. Attend le délai de grâce (5 s par défaut).
    4. Stop-Process sur le watchdog (en premier, pour qu'il ne relance rien), l'orchestrateur, puis le dashboard.
    5. Met à jour state\pids.json.
    Le terminal MetaTrader 5 (terminal64.exe) n'est JAMAIS fermé par ce script.

.PARAMETER ProjectDir
    Racine du projet (défaut : dossier parent de ce script).

.PARAMETER GraceSec
    Délai d'attente après la commande PAUSE avant l'arrêt forcé (défaut : 5 s).

.PARAMETER SkipPause
    Ne pas envoyer la commande PAUSE (arrêt direct).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\stop_all.ps1

.NOTES
    Compatible Windows PowerShell 5.1.
#>
[CmdletBinding()]
param(
    [string]$ProjectDir = '',
    [int]$GraceSec = 5,
    [switch]$SkipPause
)

$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

if (-not $ProjectDir) { $ProjectDir = Split-Path -Parent $PSScriptRoot }
$ProjectDir = [System.IO.Path]::GetFullPath($ProjectDir).TrimEnd('\')
$LogsDir    = Join-Path $ProjectDir 'logs'
$StateDir   = Join-Path $ProjectDir 'state'
$PidsFile   = Join-Path $StateDir 'pids.json'
$VenvDir    = Join-Path $ProjectDir '.venv'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $LogsDir)) { New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null }
$StopLog = Join-Path $LogsDir 'stop_all.log'

function Write-TlLog {
    param([string]$Message, [string]$Level = 'INFO')
    $line = "{0} [{1}] {2}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    $color = switch ($Level) { 'OK' { 'Green' } 'WARN' { 'Yellow' } 'ERREUR' { 'Red' } default { 'Gray' } }
    Write-Host $line -ForegroundColor $color
    try { Add-Content -LiteralPath $StopLog -Value $line -Encoding UTF8 } catch { }
}

function Import-DotEnv {
    <# Charge .env (KEY=VALUE) dans l'environnement du processus ; même convention que tradinglab.core.config. #>
    param([string]$Path)
    $keys = @()
    if (-not (Test-Path -LiteralPath $Path)) { return $keys }
    foreach ($raw in [System.IO.File]::ReadAllLines($Path, [System.Text.Encoding]::UTF8)) {
        $line = $raw.Trim()
        if ($line.Length -eq 0 -or $line.StartsWith('#')) { continue }
        $idx = $line.IndexOf('=')
        if ($idx -lt 1) { continue }
        $key = $line.Substring(0, $idx).Trim()
        $val = $line.Substring($idx + 1)
        $c = $val.IndexOf('  #')
        if ($c -ge 0) { $val = $val.Substring(0, $c) }
        $val = $val.Trim()
        if ($val.Length -ge 2 -and (($val.StartsWith('"') -and $val.EndsWith('"')) -or ($val.StartsWith("'") -and $val.EndsWith("'")))) {
            $val = $val.Substring(1, $val.Length - 2)
        }
        if ($key -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { continue }
        [Environment]::SetEnvironmentVariable($key, $val, 'Process')
        $keys += $key
    }
    return $keys
}

function Get-TradinglabProcess {
    <# Renvoie le processus CIM si le PID est vivant, est un python et (si motif) exécute bien tradinglab. Jamais terminal64. #>
    param([int]$ProcId, [string]$Pattern)
    if ($ProcId -le 0) { return $null }
    try {
        $p = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId=$ProcId" -ErrorAction Stop
        if (-not $p) { return $null }
        if ($p.Name -like 'terminal64*') { return $null }
        if ($p.Name -notlike 'python*') { return $null }
        if ($Pattern -and ($p.CommandLine -notlike "*$Pattern*")) { return $null }
        return $p
    } catch { return $null }
}

# ---------------------------------------------------------------------------
Write-Host ''
Write-Host '=================================================================' -ForegroundColor Cyan
Write-Host '  ARRÊT - Claude-MT5-Trading' -ForegroundColor Cyan
Write-Host '=================================================================' -ForegroundColor Cyan
Write-TlLog "Projet : $ProjectDir"

[void](Import-DotEnv -Path (Join-Path $ProjectDir '.env'))
if (-not $env:TRADINGLAB_HOME) { $env:TRADINGLAB_HOME = $ProjectDir }
$env:PYTHONPATH = Join-Path $ProjectDir 'src'
$env:PYTHONUTF8 = '1'

# --- 1. Cibles : pids.json + orphelins du venv ------------------------------
$targets = @()   # objets @{ nom; pid; cmd }
$seenPids = @{}
$entries = @()
if (Test-Path -LiteralPath $PidsFile) {
    try {
        $obj = [System.IO.File]::ReadAllText($PidsFile, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
        if ($obj -and $obj.processus) { $entries = @($obj.processus) }
    } catch { Write-TlLog "state\pids.json illisible : $($_.Exception.Message)" 'WARN' }
} else {
    Write-TlLog "state\pids.json absent : recherche des processus tradinglab du venv uniquement." 'WARN'
}
foreach ($e in $entries) {
    $p = Get-TradinglabProcess -ProcId ([int]$e.pid) -Pattern ([string]$e.motif)
    if ($p) {
        $targets += @{ nom = [string]$e.nom; pid = [int]$e.pid; cmd = $p.CommandLine }
        $seenPids[[int]$e.pid] = $true
    } else {
        Write-TlLog ("{0} (PID {1}) : déjà arrêté ou PID réutilisé." -f $e.nom, $e.pid)
    }
}
try {
    $orphans = @(Get-CimInstance -ClassName Win32_Process -Filter "Name='python.exe'" -ErrorAction Stop |
        Where-Object { $_.CommandLine -like '*-m tradinglab.*' -and $_.CommandLine -like "*$VenvDir*" })
    foreach ($o in $orphans) {
        if ($seenPids.ContainsKey([int]$o.ProcessId)) { continue }
        # Ne pas cibler le serveur MCP ni la CLI : uniquement les composants de service
        if ($o.CommandLine -notmatch 'tradinglab\.(monitoring\.watchdog|orchestration\.orchestrator|dashboards\.server)') { continue }
        $nom = 'python'
        if ($o.CommandLine -like '*monitoring.watchdog*') { $nom = 'watchdog' }
        elseif ($o.CommandLine -like '*orchestration.orchestrator*') { $nom = 'orchestrator' }
        elseif ($o.CommandLine -like '*dashboards.server*') { $nom = 'dashboard' }
        $targets += @{ nom = $nom; pid = [int]$o.ProcessId; cmd = $o.CommandLine }
        $seenPids[[int]$o.ProcessId] = $true
        Write-TlLog ("Processus {0} hors pids.json détecté (PID {1})." -f $nom, $o.ProcessId) 'WARN'
    }
} catch { }

if ($targets.Count -eq 0) {
    Write-TlLog 'Aucun processus tradinglab en cours : rien à arrêter.' 'OK'
} else {
    Write-TlLog ("{0} processus à arrêter : {1}" -f $targets.Count, (($targets | ForEach-Object { "$($_.nom)#$($_.pid)" }) -join ', '))

    # --- 2. Commande PAUSE (arrêt propre des prises de position) ------------------
    $hasOrch = @($targets | Where-Object { $_.nom -eq 'orchestrator' }).Count -gt 0
    if ($SkipPause) {
        Write-TlLog 'Commande PAUSE ignorée (-SkipPause).' 'WARN'
    } elseif (-not (Test-Path -LiteralPath $VenvPython)) {
        Write-TlLog "venv introuvable ($VenvPython) : commande PAUSE impossible, arrêt direct." 'WARN'
    } elseif (-not $hasOrch) {
        Write-TlLog 'Aucun orchestrateur vivant : commande PAUSE inutile.'
    } else {
        Write-TlLog 'Envoi de la commande PAUSE (python -m tradinglab.api.cli PAUSE)...'
        Push-Location $ProjectDir
        try {
            $out = & $VenvPython -m tradinglab.api.cli PAUSE 2>&1 | ForEach-Object { "$_" }
            if ($LASTEXITCODE -eq 0) { Write-TlLog ("PAUSE acceptée. {0}" -f (($out -join ' ').Trim())) 'OK' }
            else { Write-TlLog ("PAUSE a renvoyé le code {0} : {1}" -f $LASTEXITCODE, (($out -join ' ').Trim())) 'WARN' }
        } catch {
            Write-TlLog "Commande PAUSE impossible : $($_.Exception.Message)" 'WARN'
        } finally { Pop-Location }
        Write-TlLog "Délai de grâce : $GraceSec s..."
        Start-Sleep -Seconds $GraceSec
    }

    # --- 3. Stop-Process, dans l'ordre watchdog -> orchestrateur -> dashboard -> autres ------
    $order = @{ 'watchdog' = 0; 'orchestrator' = 1; 'dashboard' = 2 }
    $sorted = $targets | Sort-Object -Property @{ Expression = { if ($order.ContainsKey($_.nom)) { $order[$_.nom] } else { 9 } } }
    foreach ($t in $sorted) {
        $p = Get-TradinglabProcess -ProcId $t.pid -Pattern ''
        if (-not $p) { Write-TlLog ("{0} (PID {1}) : déjà terminé." -f $t.nom, $t.pid) 'OK'; continue }
        if ($p.Name -like 'terminal64*') { Write-TlLog ("PID {0} est terminal64.exe : IGNORÉ (jamais fermé par ce script)." -f $t.pid) 'WARN'; continue }
        try {
            Stop-Process -Id $t.pid -Force -ErrorAction Stop
            Start-Sleep -Milliseconds 500
            if (Get-Process -Id $t.pid -ErrorAction SilentlyContinue) {
                Write-TlLog ("{0} (PID {1}) : toujours vivant après Stop-Process." -f $t.nom, $t.pid) 'ERREUR'
            } else {
                Write-TlLog ("{0} (PID {1}) arrêté." -f $t.nom, $t.pid) 'OK'
            }
        } catch {
            Write-TlLog ("{0} (PID {1}) : échec Stop-Process : {2}" -f $t.nom, $t.pid, $_.Exception.Message) 'ERREUR'
        }
    }
}

# --- 4. Mise à jour de pids.json ------------------------------------------------
try {
    if (-not (Test-Path -LiteralPath $StateDir)) { New-Item -ItemType Directory -Path $StateDir -Force | Out-Null }
    $doc = [ordered]@{
        horodatage = (Get-Date -Format 'yyyy-MM-ddTHH:mm:sszzz')
        mode       = 'STOPPED'
        projet     = $ProjectDir
        arrete_le  = (Get-Date -Format 'yyyy-MM-ddTHH:mm:sszzz')
        processus  = @()
    }
    [System.IO.File]::WriteAllText($PidsFile, ($doc | ConvertTo-Json -Depth 4), (New-Object System.Text.UTF8Encoding($false)))
    Write-TlLog "state\pids.json mis à jour." 'OK'
} catch {
    Write-TlLog "Mise à jour de pids.json impossible : $($_.Exception.Message)" 'WARN'
}

$mt5 = @()
try { $mt5 = @(Get-CimInstance -ClassName Win32_Process -Filter "Name='terminal64.exe'" -ErrorAction Stop) } catch { }
if ($mt5.Count -gt 0) { Write-TlLog ("MetaTrader 5 reste ouvert (PID {0}) : fermeture manuelle uniquement." -f (($mt5 | ForEach-Object { $_.ProcessId }) -join ', ')) }
Write-Host ''
Write-Host 'Arrêt terminé.' -ForegroundColor Green
exit 0
