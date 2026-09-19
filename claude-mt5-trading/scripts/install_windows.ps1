#Requires -Version 5.1
<#
.SYNOPSIS
    Installation idempotente du laboratoire Claude-MT5-Trading sur Windows (sources officielles uniquement).

.DESCRIPTION
    - Copie l'arborescence du projet vers -ProjectDir si le script n'est pas déjà exécuté depuis ce dossier
      (robocopy, en excluant .git, .venv, logs, state, __pycache__).
    - Installe UNIQUEMENT ce qui manque, via winget (ids exacts) :
        Python.Python.3.11, Git.Git, OpenJS.NodeJS.LTS
    - Installe Claude Code (npm install -g @anthropic-ai/claude-code) si la commande "claude" est absente.
    - MetaTrader 5 : si aucun terminal64.exe n'est trouvé, télécharge l'installateur officiel MetaQuotes
      (https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe) et le lance
      (installateur interactif : une action humaine peut être requise). Ne réinstalle JAMAIS MT5 s'il existe.
    - Crée le venv <ProjectDir>\.venv (py -3.11 -m venv, repli sur python), met pip à jour,
      installe requirements.txt puis le projet en mode éditable (pip install -e .).
    - Copie .env.example vers .env s'il n'existe pas (à remplir par vous ; aucun secret n'est écrit ailleurs).
    - Vérifie réellement en fin d'exécution : python, pip, git, claude, import MetaTrader5.

.PARAMETER ProjectDir
    Dossier d'installation cible (défaut : C:\Claude-MT5-Trading).

.PARAMETER SkipMT5
    Ne pas vérifier / installer MetaTrader 5.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\install_windows.ps1
    powershell -ExecutionPolicy Bypass -File .\scripts\install_windows.ps1 -ProjectDir D:\TradingLab -SkipMT5

.NOTES
    - Certaines installations winget (Git, Node.js) déclenchent une demande d'élévation UAC : exécuter
      dans une session interactive. Le script lui-même n'exige pas d'être lancé "en administrateur".
    - Aucun mot de passe, login ou clé API n'est demandé ni stocké par ce script : tout va dans .env.
    - Codes retour : 0 = OK, 1 = au moins une vérification finale a échoué, 2 = erreur bloquante.
    Compatible Windows PowerShell 5.1.
#>
[CmdletBinding()]
param(
    [string]$ProjectDir = 'C:\Claude-MT5-Trading',
    [switch]$SkipMT5
)

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$MT5SetupUrl = 'https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe'
$WingetPackages = @(
    @{ Id = 'Python.Python.3.11'; Nom = 'Python 3.11'; Test = 'python311' },
    @{ Id = 'Git.Git';            Nom = 'Git';         Test = 'git' },
    @{ Id = 'OpenJS.NodeJS.LTS';  Nom = 'Node.js LTS'; Test = 'node' }
)

# ---------------------------------------------------------------------------
# Outils internes
# ---------------------------------------------------------------------------
$script:Summary = New-Object System.Collections.ArrayList

function Write-Step([string]$Message) { Write-Host ''; Write-Host "=== $Message ===" -ForegroundColor Cyan }
function Write-Ok([string]$Message)   { Write-Host "  [OK]        $Message" -ForegroundColor Green }
function Write-Warn2([string]$Message) { Write-Host "  [ATTENTION] $Message" -ForegroundColor Yellow }
function Write-Err2([string]$Message)  { Write-Host "  [ERREUR]    $Message" -ForegroundColor Red }

function Add-Summary([string]$Etape, [string]$Statut, [string]$Detail = '') {
    [void]$script:Summary.Add((New-Object PSObject -Property ([ordered]@{ Etape = $Etape; Statut = $Statut; Detail = $Detail })))
}

function Invoke-Native {
    <# Exécute une commande native, capture la sortie, ne lève jamais d'exception. #>
    param([string]$Exe, [string[]]$Arguments = @())
    $ErrorActionPreference = 'Continue'
    $cmd = Get-Command $Exe -ErrorAction SilentlyContinue
    if (-not $cmd) { return @{ Ok = $false; ExitCode = -1; Output = "commande introuvable : $Exe" } }
    try {
        $lines = & $Exe @Arguments 2>&1 | ForEach-Object { "$_" }
        $code = $LASTEXITCODE
        return @{ Ok = ($code -eq 0); ExitCode = $code; Output = ((($lines | Where-Object { $_ -ne $null }) -join "`n").Trim()) }
    } catch {
        return @{ Ok = $false; ExitCode = -1; Output = $_.Exception.Message }
    }
}

function Invoke-NativeStream {
    <# Exécute une commande native en affichant sa sortie en direct ; renvoie le code retour. #>
    param([string]$Exe, [string[]]$Arguments = @())
    $ErrorActionPreference = 'Continue'
    try {
        & $Exe @Arguments 2>&1 | ForEach-Object { Write-Host "    $_" }
        return $LASTEXITCODE
    } catch {
        Write-Err2 $_.Exception.Message
        return -1
    }
}

function Get-FirstLine([string]$Text) {
    if (-not $Text) { return '' }
    return ($Text -split "`r?`n")[0].Trim()
}

function Update-PathFromRegistry {
    <# Recharge PATH (machine + utilisateur) après une installation, sans rouvrir le terminal. #>
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = (@($machine, $user, $env:Path) | Where-Object { $_ }) -join ';'
    # Emplacements usuels non toujours propagés immédiatement
    $extra = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Launcher'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\Scripts'),
        'C:\Program Files\Python311',
        'C:\Program Files\Python311\Scripts',
        'C:\Program Files\Git\cmd',
        'C:\Program Files\nodejs',
        (Join-Path $env:APPDATA 'npm')
    )
    foreach ($dir in $extra) {
        if ($dir -and (Test-Path -LiteralPath $dir) -and (($env:Path -split ';') -notcontains $dir)) { $env:Path = "$env:Path;$dir" }
    }
}

function Get-Python311 {
    <# Renvoie l'interpréteur Python 3.11+ 64 bits à utiliser (py -3.11 en priorité), ou $null. #>
    $cands = @(
        @{ Exe = 'py';      Pre = @('-3.11'); Label = 'py -3.11' },
        @{ Exe = 'python';  Pre = @();        Label = 'python' },
        @{ Exe = 'python3'; Pre = @();        Label = 'python3' }
    )
    foreach ($c in $cands) {
        $rv = Invoke-Native $c.Exe (@($c.Pre) + @('-c', 'import sys;print(sys.version.split()[0])'))
        if (-not $rv.Ok) { continue }
        $v = Get-FirstLine $rv.Output
        $rb = Invoke-Native $c.Exe (@($c.Pre) + @('-c', "import struct;print(struct.calcsize('P')*8)"))
        $bits = if ($rb.Ok) { Get-FirstLine $rb.Output } else { '?' }
        if (($v -match '^3\.(1[1-9]|[2-9]\d)') -and ($bits -eq '64')) {
            return @{ Exe = $c.Exe; Pre = $c.Pre; Label = $c.Label; Version = $v }
        }
    }
    return $null
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

function Install-WingetPackage {
    <# Installe un package winget (id exact) avec acceptation silencieuse des accords. Renvoie $true si succès. #>
    param([string]$Id, [string]$Nom)
    Write-Host "  Installation de $Nom via winget ($Id)... (une demande UAC peut apparaître)" -ForegroundColor Yellow
    $code = Invoke-NativeStream 'winget' @('install', '--id', $Id, '--exact', '--source', 'winget',
        '--accept-package-agreements', '--accept-source-agreements', '--silent')
    Update-PathFromRegistry
    # 0 = OK ; -1978335189 (0x8A15002B) = aucune mise à jour disponible / déjà installé
    if ($code -eq 0 -or $code -eq -1978335189) { return $true }
    Write-Err2 "winget a renvoyé le code $code pour $Id"
    return $false
}

# ---------------------------------------------------------------------------
Write-Host ''
Write-Host '=================================================================' -ForegroundColor Cyan
Write-Host '  INSTALLATION - Claude-MT5-Trading (Windows)' -ForegroundColor Cyan
Write-Host '=================================================================' -ForegroundColor Cyan
$SourceDir = Split-Path -Parent $PSScriptRoot
Write-Host "  Source  : $SourceDir"
Write-Host "  Cible   : $ProjectDir"
Write-Host "  SkipMT5 : $($SkipMT5.IsPresent)"

$isAdmin = $false
try {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    $isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
} catch { }
if (-not $isAdmin) {
    Write-Warn2 "Session non élevée : les installateurs Git / Node.js peuvent afficher une invite UAC à valider manuellement."
}

# --- 1. Copie du projet vers ProjectDir -------------------------------------
Write-Step '1/8 Arborescence du projet'
$srcFull = [System.IO.Path]::GetFullPath($SourceDir).TrimEnd('\')
$dstFull = [System.IO.Path]::GetFullPath($ProjectDir).TrimEnd('\')
if ($srcFull.ToLowerInvariant() -eq $dstFull.ToLowerInvariant()) {
    Write-Ok "Le script s'exécute déjà depuis $ProjectDir : aucune copie nécessaire."
    Add-Summary 'Copie du projet' 'OK' 'déjà en place'
} else {
    if (-not (Test-Path -LiteralPath $dstFull)) { New-Item -ItemType Directory -Path $dstFull -Force | Out-Null }
    # Idempotence : ne jamais écraser sur la cible le .env (credentials), les données/rapports/sauvegardes
    # réels ni les bases *.db par ceux d'une copie source (clone, session mock). Ces dossiers sont recréés
    # vides plus bas s'ils manquent.
    Write-Host "  robocopy $srcFull -> $dstFull (exclusions : .git .venv logs state data reports backups __pycache__ ; .env *.db *.pyc)"
    $rc = Invoke-NativeStream 'robocopy' @($srcFull, $dstFull, '/E', '/XD', '.git', '.venv', 'logs', 'state', 'data', 'reports', 'backups', '__pycache__', '.pytest_cache', '/XF', '*.pyc', '.env', '*.db', '/R:2', '/W:2', '/NFL', '/NDL', '/NJH', '/NP')
    # robocopy : codes < 8 = succès (0 rien à copier, 1 fichiers copiés, 2/4 extras/mismatch)
    if ($rc -ge 0 -and $rc -lt 8) {
        Write-Ok "Projet copié vers $dstFull (code robocopy $rc)"
        Add-Summary 'Copie du projet' 'OK' "robocopy code $rc"
    } else {
        Write-Err2 "robocopy a échoué (code $rc). Arrêt."
        Add-Summary 'Copie du projet' 'ECHEC' "robocopy code $rc"
        exit 2
    }
}
$ProjectDir = $dstFull
foreach ($sub in @('logs', 'state', 'state\private', 'reports', 'data', 'backups', 'models')) {
    $p = Join-Path $ProjectDir $sub
    if (-not (Test-Path -LiteralPath $p)) { New-Item -ItemType Directory -Path $p -Force | Out-Null }
}
if (-not (Test-Path -LiteralPath (Join-Path $ProjectDir 'requirements.txt'))) {
    Write-Err2 "requirements.txt introuvable dans $ProjectDir : le dossier ne contient pas le projet."
    exit 2
}

# --- 2. winget --------------------------------------------------------------
Write-Step '2/8 Gestionnaire de paquets winget'
Update-PathFromRegistry
$wingetOk = (Invoke-Native 'winget' @('--version')).Ok
if ($wingetOk) {
    Write-Ok ("winget " + (Get-FirstLine (Invoke-Native 'winget' @('--version')).Output))
    Add-Summary 'winget' 'OK'
} else {
    Write-Warn2 "winget est absent : installez 'App Installer' depuis le Microsoft Store (source officielle Microsoft)."
    Write-Warn2 "Les outils manquants ne pourront pas être installés automatiquement."
    Add-Summary 'winget' 'ABSENT' 'App Installer (Microsoft Store) requis'
}

# --- 3. Python / Git / Node via winget (uniquement si absents) ----------------
Write-Step '3/8 Python 3.11, Git, Node.js (winget, uniquement si absents)'
foreach ($pkg in $WingetPackages) {
    $present = $false
    $detail = ''
    switch ($pkg.Test) {
        'python311' {
            $py = Get-Python311
            if ($py) { $present = $true; $detail = "$($py.Label) -> $($py.Version)" }
        }
        default {
            $r = Invoke-Native $pkg.Test @('--version')
            if ($r.Ok) { $present = $true; $detail = Get-FirstLine $r.Output }
        }
    }
    if ($present) {
        Write-Ok "$($pkg.Nom) déjà présent : $detail"
        Add-Summary $pkg.Nom 'OK' "déjà présent : $detail"
        continue
    }
    if (-not $wingetOk) {
        Write-Err2 "$($pkg.Nom) absent et winget indisponible."
        Add-Summary $pkg.Nom 'ECHEC' 'absent, winget indisponible'
        continue
    }
    $ok = Install-WingetPackage -Id $pkg.Id -Nom $pkg.Nom
    # Re-vérification réelle après installation
    $present = $false
    switch ($pkg.Test) {
        'python311' { $py = Get-Python311; if ($py) { $present = $true; $detail = "$($py.Label) -> $($py.Version)" } }
        default     { $r = Invoke-Native $pkg.Test @('--version'); if ($r.Ok) { $present = $true; $detail = Get-FirstLine $r.Output } }
    }
    if ($present) {
        Write-Ok "$($pkg.Nom) installé : $detail"
        Add-Summary $pkg.Nom 'INSTALLE' $detail
    } else {
        Write-Err2 "$($pkg.Nom) toujours introuvable après winget (rouvrir le terminal peut être nécessaire)."
        Add-Summary $pkg.Nom 'ECHEC' "winget ok=$ok, commande introuvable"
    }
}

# --- 4. Claude Code ---------------------------------------------------------
Write-Step '4/8 Claude Code (npm install -g @anthropic-ai/claude-code)'
Update-PathFromRegistry
$rc = Invoke-Native 'claude' @('--version')
if ($rc.Ok) {
    Write-Ok "Claude Code déjà présent : $(Get-FirstLine $rc.Output)"
    Add-Summary 'Claude Code' 'OK' "déjà présent : $(Get-FirstLine $rc.Output)"
} else {
    if ((Invoke-Native 'npm' @('--version')).Ok) {
        $code = Invoke-NativeStream 'npm' @('install', '-g', '@anthropic-ai/claude-code')
        Update-PathFromRegistry
        $rc = Invoke-Native 'claude' @('--version')
        if ($code -eq 0 -and $rc.Ok) {
            Write-Ok "Claude Code installé : $(Get-FirstLine $rc.Output)"
            Add-Summary 'Claude Code' 'INSTALLE' (Get-FirstLine $rc.Output)
        } else {
            Write-Err2 "Installation de Claude Code non confirmée (npm code $code)."
            Add-Summary 'Claude Code' 'ECHEC' "npm code $code"
        }
    } else {
        Write-Err2 "npm indisponible : impossible d'installer Claude Code."
        Add-Summary 'Claude Code' 'ECHEC' 'npm indisponible'
    }
}

# --- 5. MetaTrader 5 ----------------------------------------------------------
Write-Step '5/8 MetaTrader 5'
if ($SkipMT5) {
    Write-Warn2 'Vérification MetaTrader 5 ignorée (-SkipMT5).'
    Add-Summary 'MetaTrader 5' 'IGNORE' '-SkipMT5'
} else {
    $terminals = @(Find-MT5Terminals)
    if ($terminals.Count -gt 0) {
        Write-Ok "MetaTrader 5 déjà installé : $($terminals -join ' | ') (aucune réinstallation)"
        Add-Summary 'MetaTrader 5' 'OK' ($terminals -join ' | ')
    } else {
        Write-Host "  Aucun terminal64.exe trouvé. Téléchargement de l'installateur officiel MetaQuotes :" -ForegroundColor Yellow
        Write-Host "    $MT5SetupUrl"
        $setup = Join-Path $env:TEMP 'mt5setup.exe'
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -Uri $MT5SetupUrl -OutFile $setup -UseBasicParsing -ErrorAction Stop
            Write-Ok "Téléchargé : $setup ($([math]::Round((Get-Item $setup).Length / 1MB, 1)) Mo)"
            Write-Host ''
            Write-Host '  >>> ACTION HUMAINE REQUISE : l''installateur MetaTrader 5 est interactif.' -ForegroundColor Magenta
            Write-Host '  >>> Validez l''UAC, acceptez la licence et laissez le chemin par défaut (C:\Program Files\MetaTrader 5).' -ForegroundColor Magenta
            Write-Host '  >>> Après installation, connectez le compte DEMO dans MT5 (Fichier > Connexion à un compte de trading).' -ForegroundColor Magenta
            Write-Host ''
            Start-Process -FilePath $setup -Wait
            $terminals = @(Find-MT5Terminals)
            if ($terminals.Count -gt 0) {
                Write-Ok "MetaTrader 5 installé : $($terminals -join ' | ')"
                Add-Summary 'MetaTrader 5' 'INSTALLE' ($terminals -join ' | ')
            } else {
                Write-Warn2 "terminal64.exe toujours introuvable : l'installation a pu être annulée ou est encore en cours."
                Add-Summary 'MetaTrader 5' 'A VERIFIER' 'terminal64.exe introuvable après installation'
            }
        } catch {
            Write-Err2 "Téléchargement / lancement de mt5setup.exe impossible : $($_.Exception.Message)"
            Add-Summary 'MetaTrader 5' 'ECHEC' $_.Exception.Message
        }
    }
}

# --- 6. Environnement virtuel Python ----------------------------------------
Write-Step '6/8 Environnement virtuel .venv et dépendances'
Update-PathFromRegistry
$VenvDir = Join-Path $ProjectDir '.venv'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
if (Test-Path -LiteralPath $VenvPython) {
    Write-Ok "venv déjà présent : $VenvDir"
} else {
    $py = Get-Python311
    if (-not $py) {
        Write-Err2 'Aucun Python 3.11+ 64 bits disponible : impossible de créer le venv. Arrêt.'
        Add-Summary 'venv' 'ECHEC' 'Python 3.11 introuvable'
        exit 2
    }
    Write-Host "  Création du venv avec $($py.Label) ($($py.Version))..."
    $code = Invoke-NativeStream $py.Exe (@($py.Pre) + @('-m', 'venv', $VenvDir))
    if ($code -ne 0 -or -not (Test-Path -LiteralPath $VenvPython)) {
        Write-Err2 "Création du venv échouée (code $code). Arrêt."
        Add-Summary 'venv' 'ECHEC' "code $code"
        exit 2
    }
    Write-Ok "venv créé : $VenvDir"
}
Add-Summary 'venv' 'OK' $VenvDir

$env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
Write-Host '  Mise à jour de pip...'
$code = Invoke-NativeStream $VenvPython @('-m', 'pip', 'install', '--upgrade', 'pip')
Add-Summary 'pip --upgrade' $(if ($code -eq 0) { 'OK' } else { 'ECHEC' }) "code $code"

Write-Host '  pip install -r requirements.txt ...'
$code = Invoke-NativeStream $VenvPython @('-m', 'pip', 'install', '-r', (Join-Path $ProjectDir 'requirements.txt'))
if ($code -eq 0) { Write-Ok 'requirements.txt installé' } else { Write-Err2 "pip install -r requirements.txt : code $code" }
Add-Summary 'requirements.txt' $(if ($code -eq 0) { 'OK' } else { 'ECHEC' }) "code $code"

Write-Host '  pip install -e . (package tradinglab en mode éditable) ...'
$code = Invoke-NativeStream $VenvPython @('-m', 'pip', 'install', '-e', $ProjectDir)
if ($code -eq 0) { Write-Ok 'tradinglab installé en mode éditable' } else { Write-Err2 "pip install -e . : code $code" }
Add-Summary 'pip install -e .' $(if ($code -eq 0) { 'OK' } else { 'ECHEC' }) "code $code"

# --- 7. Fichier .env ----------------------------------------------------------
Write-Step '7/8 Fichier .env'
$envFile = Join-Path $ProjectDir '.env'
$envExample = Join-Path $ProjectDir '.env.example'
if (Test-Path -LiteralPath $envFile) {
    Write-Ok '.env déjà présent (contenu non lu, non modifié).'
    Add-Summary '.env' 'OK' 'déjà présent'
} elseif (Test-Path -LiteralPath $envExample) {
    Copy-Item -LiteralPath $envExample -Destination $envFile
    Write-Ok ".env créé à partir de .env.example : $envFile"
    Add-Summary '.env' 'CREE' 'à compléter'
} else {
    Write-Warn2 '.env.example introuvable : créez .env manuellement.'
    Add-Summary '.env' 'A CREER' '.env.example absent'
}
Write-Host ''
Write-Host '  >>> ACTION HUMAINE REQUISE : remplissez .env (MT5_LOGIN, MT5_PASSWORD, MT5_SERVER, ANTHROPIC_API_KEY, FMP_API_KEY).' -ForegroundColor Magenta
Write-Host '  >>> Ne mettez JAMAIS de mot de passe ou de clé API ailleurs que dans .env (fichier ignoré par git).' -ForegroundColor Magenta

# --- 8. Vérifications finales réelles ----------------------------------------
Write-Step '8/8 Vérifications finales'
Update-PathFromRegistry
$checks = @(
    @{ Nom = 'python --version (venv)';   Exe = $VenvPython; Args = @('--version') },
    @{ Nom = 'pip --version (venv)';      Exe = $VenvPython; Args = @('-m', 'pip', '--version') },
    @{ Nom = 'git --version';             Exe = 'git';       Args = @('--version') },
    @{ Nom = 'claude --version';          Exe = 'claude';    Args = @('--version') },
    @{ Nom = 'python -c "import MetaTrader5"'; Exe = $VenvPython; Args = @('-c', 'import MetaTrader5;print(MetaTrader5.__version__)') },
    @{ Nom = 'python -c "import tradinglab"';  Exe = $VenvPython; Args = @('-c', 'import tradinglab;print(tradinglab.__name__)') }
)
$failed = @()
foreach ($chk in $checks) {
    $r = Invoke-Native $chk.Exe $chk.Args
    if ($r.Ok) {
        Write-Ok "$($chk.Nom) -> $(Get-FirstLine $r.Output)"
        Add-Summary "Vérif : $($chk.Nom)" 'OK' (Get-FirstLine $r.Output)
    } else {
        Write-Err2 "$($chk.Nom) -> ECHEC : $(Get-FirstLine $r.Output)"
        Add-Summary "Vérif : $($chk.Nom)" 'ECHEC' (Get-FirstLine $r.Output)
        $failed += $chk.Nom
    }
}

# --- Récapitulatif --------------------------------------------------------------
Write-Host ''
Write-Host '=================================================================' -ForegroundColor Cyan
Write-Host '  RÉCAPITULATIF' -ForegroundColor Cyan
Write-Host '=================================================================' -ForegroundColor Cyan
$script:Summary | Format-Table -Property Etape, Statut, Detail -AutoSize -Wrap | Out-String -Width 200 | Write-Host

Write-Host 'Prochaines étapes :' -ForegroundColor Cyan
Write-Host "  1. Remplir $envFile (compte DEMO uniquement au départ)."
Write-Host '  2. Ouvrir MetaTrader 5 et connecter le compte DEMO (login/serveur identiques au .env), activer le trading algorithmique.'
Write-Host "  3. Auditer : powershell -ExecutionPolicy Bypass -File `"$ProjectDir\scripts\audit_windows.ps1`""
Write-Host "  4. Démarrer : powershell -ExecutionPolicy Bypass -File `"$ProjectDir\scripts\start_all.ps1`" -Mode SAFE"
Write-Host "  5. (optionnel) Démarrage automatique : powershell -ExecutionPolicy Bypass -File `"$ProjectDir\scripts\register_autostart.ps1`""

if ($failed.Count -gt 0) {
    Write-Host ''
    Write-Host ("Vérifications en échec ({0}) : {1}" -f $failed.Count, ($failed -join ', ')) -ForegroundColor Red
    Write-Host 'Rouvrez un terminal (PATH) puis relancez ce script : il est idempotent.' -ForegroundColor Yellow
    exit 1
}
Write-Host ''
Write-Host 'Installation terminée avec succès.' -ForegroundColor Green
exit 0
