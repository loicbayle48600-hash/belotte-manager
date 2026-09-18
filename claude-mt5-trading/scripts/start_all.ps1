#Requires -Version 5.1
<#
.SYNOPSIS
    Démarre l'ensemble du laboratoire Claude-MT5-Trading (MT5, watchdog, orchestrateur, dashboard).

.DESCRIPTION
    Étapes :
      1. Attend la disponibilité du réseau (Test-NetConnection, 120 s maximum).
      2. Charge <ProjectDir>\.env dans l'environnement du processus (KEY=VALUE, commentaires ignorés).
      3. Repère / démarre terminal64.exe (MT5_TERMINAL_PATH du .env, sinon recherche standard), attend 15 s.
      4. Active le venv <ProjectDir>\.venv.
      5. Lance dans des processus séparés (fenêtres réduites, sorties dans logs\*.out.log / *.err.log) :
           - watchdog       : python -m tradinglab.monitoring.watchdog
           - orchestrateur  : python -m tradinglab.orchestration.orchestrator --mode <Mode>
           - dashboard      : python -m tradinglab.dashboards.server (sauf -NoDashboard)
      6. Écrit les PID dans state\pids.json et affiche comment lancer le serveur MCP.
    Un composant dont un PID vivant existe déjà dans state\pids.json n'est PAS relancé (pas de doublon).

.PARAMETER ProjectDir
    Racine du projet (défaut : dossier parent de ce script).

.PARAMETER Mode
    SAFE (défaut, aucune ouverture de position) ou AUTO (trading autonome sur compte DEMO).

.PARAMETER NoDashboard
    Ne pas démarrer le dashboard web.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\start_all.ps1
    powershell -ExecutionPolicy Bypass -File .\scripts\start_all.ps1 -Mode AUTO

.NOTES
    Codes retour : 0 = OK, 2 = erreur bloquante (venv absent, package tradinglab non importable).
    Compatible Windows PowerShell 5.1.
#>
[CmdletBinding()]
param(
    [string]$ProjectDir = '',
    [ValidateSet('SAFE', 'AUTO')]
    [string]$Mode = 'SAFE',
    [switch]$NoDashboard
)

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

if (-not $ProjectDir) { $ProjectDir = Split-Path -Parent $PSScriptRoot }
$ProjectDir = [System.IO.Path]::GetFullPath($ProjectDir).TrimEnd('\')
$LogsDir    = Join-Path $ProjectDir 'logs'
$StateDir   = Join-Path $ProjectDir 'state'
$PidsFile   = Join-Path $StateDir 'pids.json'
$VenvDir    = Join-Path $ProjectDir '.venv'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
$NetworkTimeoutSec = 120
$MT5StartWaitSec   = 15

foreach ($d in @($LogsDir, $StateDir)) { if (-not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null } }
$StartLog = Join-Path $LogsDir 'start_all.log'

# ---------------------------------------------------------------------------
# Outils internes
# ---------------------------------------------------------------------------
function Write-TlLog {
    param([string]$Message, [string]$Level = 'INFO')
    $line = "{0} [{1}] {2}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    $color = switch ($Level) { 'OK' { 'Green' } 'WARN' { 'Yellow' } 'ERREUR' { 'Red' } default { 'Gray' } }
    Write-Host $line -ForegroundColor $color
    try { Add-Content -LiteralPath $StartLog -Value $line -Encoding UTF8 } catch { }
}

function Import-DotEnv {
    <# Charge un fichier .env (KEY=VALUE) dans l'environnement du processus. Renvoie la liste des clés chargées.
       Même convention que tradinglab.core.config.load_dotenv : lignes vides / '#' ignorées,
       commentaire de fin de ligne après deux espaces + '#', guillemets simples/doubles retirés. #>
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

function Find-MT5Terminals {
    <# Recherche toutes les installations de terminal64.exe. #>
    $candidates = @()
    $candidates += 'C:\Program Files\MetaTrader 5\terminal64.exe'
    if ($env:ProgramFiles) { $candidates += (Join-Path $env:ProgramFiles 'MetaTrader 5\terminal64.exe') }
    Get-ChildItem -Path 'C:\Program Files\*\terminal64.exe' -ErrorAction SilentlyContinue | ForEach-Object { $candidates += $_.FullName }
    $dataRoot = Join-Path $env:APPDATA 'MetaQuotes\Terminal'
    if (Test-Path -LiteralPath $dataRoot) {
        Get-ChildItem -Path $dataRoot -Directory -ErrorAction SilentlyContinue | ForEach-Object {
            $origin = Join-Path $_.FullName 'origin.txt'
            if (Test-Path -LiteralPath $origin) {
                try {
                    $dir = (Get-Content -LiteralPath $origin -Raw -ErrorAction Stop).Trim().Trim([char]0)
                    if ($dir) { $candidates += (Join-Path $dir 'terminal64.exe') }
                } catch { }
            }
        }
    }
    $found = @()
    $seen = @{}
    foreach ($c in $candidates) {
        if (-not $c) { continue }
        $key = $c.ToLowerInvariant()
        if ($seen.ContainsKey($key)) { continue }
        $seen[$key] = $true
        try { if (Test-Path -LiteralPath $c -PathType Leaf) { $found += $c } } catch { }
    }
    return $found
}

function Get-TradinglabProcess {
    <# Renvoie le processus (CIM) si le PID est vivant ET correspond bien à un python tradinglab, sinon $null. #>
    param([int]$ProcId, [string]$Pattern)
    if ($ProcId -le 0) { return $null }
    try {
        $p = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId=$ProcId" -ErrorAction Stop
        if (-not $p) { return $null }
        if ($p.Name -notlike 'python*') { return $null }
        if ($Pattern -and -not $p.CommandLine) {
            # CommandLine illisible (autre élévation/session) : processus python vivant de nature inconnue.
            # On le considère PRÉSENT pour ne jamais lancer un second orchestrateur sur le même état.
            Write-TlLog ("PID {0} : ligne de commande illisible (élévation/session différente) ; considéré comme {1} déjà en cours, non relancé." -f $ProcId, $Pattern) 'WARN'
            return $p
        }
        if ($Pattern -and ($p.CommandLine -notlike "*$Pattern*")) { return $null }
        return $p
    } catch { return $null }
}

function Read-PidsFile {
    if (-not (Test-Path -LiteralPath $PidsFile)) { return @() }
    try {
        $json = [System.IO.File]::ReadAllText($PidsFile, [System.Text.Encoding]::UTF8)
        $obj = $json | ConvertFrom-Json
        if ($obj -and $obj.processus) { return @($obj.processus) }
    } catch { Write-TlLog "state\pids.json illisible : $($_.Exception.Message)" 'WARN' }
    return @()
}

function Write-PidsFile {
    param([object[]]$Entries)
    $doc = [ordered]@{
        horodatage = (Get-Date -Format 'yyyy-MM-ddTHH:mm:sszzz')
        mode       = $Mode
        projet     = $ProjectDir
        processus  = @($Entries)
    }
    $json = $doc | ConvertTo-Json -Depth 5
    [System.IO.File]::WriteAllText($PidsFile, $json, (New-Object System.Text.UTF8Encoding($false)))
}

function Start-Component {
    <# Démarre un module Python dans un processus séparé (fenêtre réduite, sorties journalisées). Renvoie une entrée PID. #>
    param([string]$Name, [string[]]$ModuleArgs, [string]$Pattern)
    $outLog = Join-Path $LogsDir "$Name.out.log"
    $errLog = Join-Path $LogsDir "$Name.err.log"
    foreach ($f in @($outLog, $errLog)) {
        if (Test-Path -LiteralPath $f) {
            try { Move-Item -LiteralPath $f -Destination ($f -replace '\.log$', '.prev.log') -Force -ErrorAction Stop } catch { }
        }
    }
    $proc = Start-Process -FilePath $VenvPython -ArgumentList $ModuleArgs -WorkingDirectory $ProjectDir `
        -WindowStyle Minimized -RedirectStandardOutput $outLog -RedirectStandardError $errLog -PassThru
    Start-Sleep -Seconds 3
    $alive = $null -ne (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue)
    if ($alive) {
        Write-TlLog ("{0} démarré (PID {1}) : python {2}" -f $Name, $proc.Id, ($ModuleArgs -join ' ')) 'OK'
    } else {
        Write-TlLog ("{0} s'est arrêté immédiatement (code {1}). Voir {2}" -f $Name, $proc.ExitCode, $errLog) 'ERREUR'
        try { Get-Content -LiteralPath $errLog -Tail 15 -ErrorAction Stop | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkRed } } catch { }
    }
    return [ordered]@{
        nom        = $Name
        pid        = $proc.Id
        motif      = $Pattern
        commande   = ("python " + ($ModuleArgs -join ' '))
        demarre_le = (Get-Date -Format 'yyyy-MM-ddTHH:mm:sszzz')
        log_out    = $outLog
        log_err    = $errLog
        vivant     = $alive
    }
}

# ---------------------------------------------------------------------------
Write-Host ''
Write-Host '=================================================================' -ForegroundColor Cyan
Write-Host "  DÉMARRAGE - Claude-MT5-Trading (mode $Mode)" -ForegroundColor Cyan
Write-Host '=================================================================' -ForegroundColor Cyan
Write-TlLog "Projet : $ProjectDir | Mode : $Mode | Dashboard : $(-not $NoDashboard.IsPresent)"
if ($Mode -eq 'AUTO') {
    Write-TlLog 'Mode AUTO demandé explicitement : trading autonome (compte DEMO attendu, vérifié par le smoke test Python).' 'WARN'
}

# --- 1. Réseau ----------------------------------------------------------------
Write-TlLog "Attente du réseau (max $NetworkTimeoutSec s)..."
$deadline = (Get-Date).AddSeconds($NetworkTimeoutSec)
$netOk = $false
while (-not $netOk -and (Get-Date) -lt $deadline) {
    try { $netOk = [bool](Test-NetConnection -ComputerName 'pypi.org' -Port 443 -InformationLevel Quiet -WarningAction SilentlyContinue -ErrorAction Stop) } catch { $netOk = $false }
    if (-not $netOk) { Start-Sleep -Seconds 5 }
}
if ($netOk) { Write-TlLog 'Réseau disponible.' 'OK' } else { Write-TlLog "Réseau indisponible après $NetworkTimeoutSec s : démarrage quand même (les modules Python gèrent le mode dégradé)." 'WARN' }

# --- 2. .env ----------------------------------------------------------------
$envFile = Join-Path $ProjectDir '.env'
$loadedKeys = @(Import-DotEnv -Path $envFile)
if ($loadedKeys.Count -gt 0) {
    Write-TlLog (".env chargé ({0} clés : {1})" -f $loadedKeys.Count, ($loadedKeys -join ', ')) 'OK'
} else {
    Write-TlLog ".env absent ou vide ($envFile) : copiez .env.example vers .env et remplissez-le." 'WARN'
}
if (-not $env:TRADINGLAB_HOME) { $env:TRADINGLAB_HOME = $ProjectDir }
$env:PYTHONPATH = Join-Path $ProjectDir 'src'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
if (-not $env:TRADINGLAB_BROKER) { $env:TRADINGLAB_BROKER = 'mt5' }

# --- 3. MetaTrader 5 ----------------------------------------------------------
if ($env:TRADINGLAB_BROKER -eq 'mock') {
    Write-TlLog 'TRADINGLAB_BROKER=mock : MetaTrader 5 non requis, étape ignorée.'
} else {
    $mt5Path = $null
    if ($env:MT5_TERMINAL_PATH -and (Test-Path -LiteralPath $env:MT5_TERMINAL_PATH -PathType Leaf)) {
        $mt5Path = $env:MT5_TERMINAL_PATH
    } else {
        if ($env:MT5_TERMINAL_PATH) { Write-TlLog "MT5_TERMINAL_PATH du .env introuvable ($($env:MT5_TERMINAL_PATH)) : recherche standard." 'WARN' }
        $terms = @(Find-MT5Terminals)
        if ($terms.Count -gt 0) { $mt5Path = $terms[0] }
    }
    $running = @()
    try { $running = @(Get-CimInstance -ClassName Win32_Process -Filter "Name='terminal64.exe'" -ErrorAction Stop) } catch { }
    if ($running.Count -gt 0) {
        Write-TlLog ("MetaTrader 5 déjà en cours (PID {0})." -f (($running | ForEach-Object { $_.ProcessId }) -join ', ')) 'OK'
    } elseif ($mt5Path) {
        Write-TlLog "Démarrage de MetaTrader 5 : $mt5Path"
        try {
            Start-Process -FilePath $mt5Path -WorkingDirectory (Split-Path -Parent $mt5Path) | Out-Null
            Write-TlLog "Attente de $MT5StartWaitSec s pour l'initialisation du terminal..."
            Start-Sleep -Seconds $MT5StartWaitSec
            Write-TlLog 'Rappel : le compte DEMO doit être connecté dans MT5 et le trading algorithmique activé (action humaine la première fois).' 'WARN'
        } catch {
            Write-TlLog "Impossible de lancer terminal64.exe : $($_.Exception.Message)" 'ERREUR'
        }
    } else {
        Write-TlLog 'Aucun terminal64.exe trouvé : lancez install_windows.ps1 ou renseignez MT5_TERMINAL_PATH dans .env. Poursuite (l''orchestrateur restera dégradé).' 'WARN'
    }
}

# --- 4. venv ----------------------------------------------------------------
if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-TlLog "venv introuvable ($VenvPython) : exécutez scripts\install_windows.ps1. Arrêt." 'ERREUR'
    exit 2
}
# Activation manuelle (équivalent de Activate.ps1, sans dépendre de la politique d'exécution)
$env:VIRTUAL_ENV = $VenvDir
$env:Path = (Join-Path $VenvDir 'Scripts') + ';' + $env:Path
if ($env:PYTHONHOME) { Remove-Item Env:\PYTHONHOME -ErrorAction SilentlyContinue }
$importTest = & $VenvPython -c 'import tradinglab;print(tradinglab.__name__)' 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-TlLog "Le package tradinglab n'est pas importable dans le venv ($importTest). Relancez install_windows.ps1. Arrêt." 'ERREUR'
    exit 2
}
Write-TlLog "venv activé : $VenvDir" 'OK'

# --- 5. Composants --------------------------------------------------------------
$existing = @(Read-PidsFile)
$entries = @()

$components = @(
    @{ Name = 'watchdog';     Pattern = 'tradinglab.monitoring.watchdog';          Args = @('-m', 'tradinglab.monitoring.watchdog') },
    @{ Name = 'orchestrator'; Pattern = 'tradinglab.orchestration.orchestrator';   Args = @('-m', 'tradinglab.orchestration.orchestrator', '--mode', $Mode) }
)
if (-not $NoDashboard) {
    $components += @{ Name = 'dashboard'; Pattern = 'tradinglab.dashboards.server'; Args = @('-m', 'tradinglab.dashboards.server') }
}

foreach ($comp in $components) {
    $alreadyRunning = $null
    foreach ($e in $existing) {
        if ($e.nom -eq $comp.Name) {
            $p = Get-TradinglabProcess -ProcId ([int]$e.pid) -Pattern $comp.Pattern
            if ($p) { $alreadyRunning = $e; break }
        }
    }
    if (-not $alreadyRunning) {
        # Sécurité supplémentaire : un orchestrateur lancé hors pids.json (ex. à la main) ne doit pas être doublé
        try {
            $orphans = @(Get-CimInstance -ClassName Win32_Process -Filter "Name='python.exe'" -ErrorAction Stop |
                Where-Object { $_.CommandLine -like "*$($comp.Pattern)*" -and $_.CommandLine -like "*$VenvDir*" })
            if ($orphans.Count -gt 0) {
                $alreadyRunning = [ordered]@{ nom = $comp.Name; pid = [int]$orphans[0].ProcessId; motif = $comp.Pattern; commande = $orphans[0].CommandLine; demarre_le = 'inconnu'; log_out = ''; log_err = ''; vivant = $true }
            }
        } catch { }
    }
    if ($alreadyRunning) {
        Write-TlLog ("{0} déjà en cours (PID {1}) : non relancé." -f $comp.Name, $alreadyRunning.pid) 'WARN'
        $entries += $alreadyRunning
        continue
    }
    $entries += (Start-Component -Name $comp.Name -ModuleArgs $comp.Args -Pattern $comp.Pattern)
}

# --- 6. pids.json + MCP --------------------------------------------------------
try {
    Write-PidsFile -Entries $entries
    Write-TlLog "PID enregistrés dans $PidsFile" 'OK'
} catch {
    Write-TlLog "Écriture de $PidsFile impossible : $($_.Exception.Message)" 'ERREUR'
}

Write-Host ''
Write-Host 'Processus :' -ForegroundColor Cyan
foreach ($e in $entries) {
    $etat = if ($e.vivant) { 'vivant' } else { 'ARRÊTÉ' }
    Write-Host ("  {0,-13} PID {1,-7} {2,-8} {3}" -f $e.nom, $e.pid, $etat, $e.commande)
}
Write-Host ''
Write-Host 'Serveur MCP (à lancer à la demande, par exemple pour Claude Code) :' -ForegroundColor Cyan
Write-Host "  cd `"$ProjectDir`""
Write-Host "  `"$VenvPython`" -m tradinglab.mcp.server"
Write-Host '  (déclaré dans .mcp.json : lancez "claude" depuis le dossier du projet, venv activé, TRADINGLAB_HOME défini)'
Write-Host ''
Write-Host "Journaux : $LogsDir\*.out.log / *.err.log | Arrêt : scripts\stop_all.ps1" -ForegroundColor Gray
if ($Mode -eq 'SAFE') { Write-Host 'Mode SAFE : aucune position ne sera ouverte. Passez en AUTO uniquement via -Mode AUTO (compte DEMO).' -ForegroundColor Yellow }
# Code de sortie non nul si un composant lancé ici s'est arrêté immédiatement : c'est ce qui permet à la
# tâche planifiée (register_autostart.ps1, -RestartCount 3) de relancer automatiquement. Les entrées
# « déjà en cours » ont vivant = $true et ne comptent pas.
$dead = @($entries | Where-Object { -not $_.vivant })
if ($dead.Count -gt 0) {
    Write-TlLog ("Composant(s) arrêté(s) au démarrage : {0}" -f (($dead | ForEach-Object { $_.nom }) -join ', ')) 'ERREUR'
    exit 1
}
exit 0
