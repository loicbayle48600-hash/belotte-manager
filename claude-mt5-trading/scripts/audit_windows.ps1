#Requires -Version 5.1
<#
.SYNOPSIS
    Audit complet du poste Windows pour le laboratoire Claude-MT5-Trading (LECTURE SEULE).

.DESCRIPTION
    Vérifie la présence et la version de tous les prérequis (Windows 64 bits, PowerShell,
    winget, git, Python 3.11 64 bits, pip, venv, Node.js, npm, Claude Code, MetaTrader 5,
    package Python MetaTrader5), l'accès internet, l'espace disque, l'heure système
    (fuseau + décalage NTP) et les droits administrateur.

    Le script NE MODIFIE RIEN sur le poste : il affiche un tableau récapitulatif et écrit
    un rapport JSON dans <ProjectDir>\reports\audit-YYYYMMDD-HHmmss.json.

.PARAMETER ProjectDir
    Racine du projet (défaut : dossier parent de ce script).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\audit_windows.ps1

.NOTES
    Codes retour : 0 = tous les éléments requis sont présents,
                   1 = au moins un élément requis manque (liste affichée en fin).
    Compatible Windows PowerShell 5.1 (aucune fonctionnalité PowerShell 7 utilisée).
#>
[CmdletBinding()]
param(
    [string]$ProjectDir = ''
)

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

if (-not $ProjectDir) { $ProjectDir = Split-Path -Parent $PSScriptRoot }

# ---------------------------------------------------------------------------
# Outils internes
# ---------------------------------------------------------------------------
$script:Results = New-Object System.Collections.ArrayList

function Add-Result {
    <# Ajoute une ligne au tableau d'audit. Statut : OK | MANQUANT | AVERTISSEMENT | INFO #>
    param(
        [string]$Composant,
        [string]$Statut,
        [string]$Detail = '',
        [bool]$Requis = $true
    )
    $row = New-Object PSObject -Property ([ordered]@{
        Composant = $Composant
        Statut    = $Statut
        Requis    = $(if ($Requis) { 'oui' } else { 'non' })
        Detail    = $Detail
    })
    [void]$script:Results.Add($row)
    $color = switch ($Statut) {
        'OK'            { 'Green' }
        'MANQUANT'      { 'Red' }
        'AVERTISSEMENT' { 'Yellow' }
        default         { 'Gray' }
    }
    Write-Host ("  [{0,-13}] {1,-28} {2}" -f $Statut, $Composant, $Detail) -ForegroundColor $color
}

function Invoke-Native {
    <# Exécute une commande native et renvoie @{ Ok; ExitCode; Output } sans jamais lever d'exception. #>
    param([string]$Exe, [string[]]$Arguments = @())
    $ErrorActionPreference = 'Continue'
    $cmd = Get-Command $Exe -ErrorAction SilentlyContinue
    if (-not $cmd) { return @{ Ok = $false; ExitCode = -1; Output = "commande introuvable : $Exe" } }
    try {
        $lines = & $Exe @Arguments 2>&1 | ForEach-Object { "$_" }
        $code = $LASTEXITCODE
        $text = (($lines | Where-Object { $_ -ne $null }) -join "`n").Trim()
        return @{ Ok = ($code -eq 0); ExitCode = $code; Output = $text }
    } catch {
        return @{ Ok = $false; ExitCode = -1; Output = $_.Exception.Message }
    }
}

function Get-FirstLine([string]$Text) {
    if (-not $Text) { return '' }
    return ($Text -split "`r?`n")[0].Trim()
}

function Find-MT5Terminals {
    <# Recherche toutes les installations de terminal64.exe (MetaTrader 5). Renvoie un tableau de chemins. #>
    $candidates = @()
    $candidates += 'C:\Program Files\MetaTrader 5\terminal64.exe'
    if ($env:ProgramFiles) { $candidates += (Join-Path $env:ProgramFiles 'MetaTrader 5\terminal64.exe') }
    # Installations de brokers (ex. "C:\Program Files\XM MT5\terminal64.exe")
    Get-ChildItem -Path 'C:\Program Files\*\terminal64.exe' -ErrorAction SilentlyContinue | ForEach-Object { $candidates += $_.FullName }
    # Instances déclarées dans %APPDATA%\MetaQuotes\Terminal\<hash>\origin.txt (chemin d'installation)
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

function Test-Port443([string]$HostName) {
    try {
        $ok = Test-NetConnection -ComputerName $HostName -Port 443 -InformationLevel Quiet -WarningAction SilentlyContinue -ErrorAction Stop
        return [bool]$ok
    } catch {
        # Repli si Test-NetConnection est indisponible : socket TCP direct
        try {
            $client = New-Object System.Net.Sockets.TcpClient
            $async = $client.BeginConnect($HostName, 443, $null, $null)
            $done = $async.AsyncWaitHandle.WaitOne(5000, $false)
            if ($done -and $client.Connected) { $client.Close(); return $true }
            $client.Close()
        } catch { }
        return $false
    }
}

# ---------------------------------------------------------------------------
Write-Host ''
Write-Host '=================================================================' -ForegroundColor Cyan
Write-Host '  AUDIT WINDOWS - Claude-MT5-Trading (lecture seule)' -ForegroundColor Cyan
Write-Host '=================================================================' -ForegroundColor Cyan
Write-Host "  Projet : $ProjectDir"
Write-Host "  Date   : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Host ''

# --- Système d'exploitation --------------------------------------------------
Write-Host '--- Système ---' -ForegroundColor Cyan
$osInfo = $null
try { $osInfo = Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop } catch { }
if ($osInfo) {
    $is64 = ($osInfo.OSArchitecture -match '64')
    Add-Result 'Windows' $(if ($is64) { 'OK' } else { 'MANQUANT' }) ("{0} (version {1}, build {2}, {3})" -f $osInfo.Caption, $osInfo.Version, $osInfo.BuildNumber, $osInfo.OSArchitecture)
    if (-not $is64) { Add-Result 'Architecture 64 bits' 'MANQUANT' 'MetaTrader 5 et le package MetaTrader5 exigent Windows 64 bits' }
} else {
    Add-Result 'Windows' 'AVERTISSEMENT' 'Get-CimInstance Win32_OperatingSystem indisponible' $false
}

$psv = $PSVersionTable.PSVersion
Add-Result 'PowerShell' $(if ($psv.Major -ge 5) { 'OK' } else { 'MANQUANT' }) ("version {0} ({1})" -f $psv.ToString(), $PSVersionTable.PSEdition)

$isAdmin = $false
try {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    $isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
} catch { }
Add-Result 'Droits administrateur' 'INFO' $(if ($isAdmin) { 'session élevée (admin)' } else { 'session standard (UAC possible pour winget/installateurs)' }) $false

# --- Outils en ligne de commande --------------------------------------------
Write-Host '--- Outils ---' -ForegroundColor Cyan
$r = Invoke-Native 'winget' @('--version')
Add-Result 'winget' $(if ($r.Ok) { 'OK' } else { 'MANQUANT' }) $(if ($r.Ok) { Get-FirstLine $r.Output } else { 'installer "App Installer" depuis le Microsoft Store' })

$r = Invoke-Native 'git' @('--version')
Add-Result 'git' $(if ($r.Ok) { 'OK' } else { 'MANQUANT' }) (Get-FirstLine $r.Output)

# --- Python : py -3.11, puis python, puis python3 -----------------------------
$pyCandidates = @(
    @{ Exe = 'py';      Pre = @('-3.11'); Label = 'py -3.11' },
    @{ Exe = 'python';  Pre = @();        Label = 'python' },
    @{ Exe = 'python3'; Pre = @();        Label = 'python3' }
)
$pythonFound = $null
$pythonTried = @()
foreach ($cand in $pyCandidates) {
    $args1 = @($cand.Pre) + @('-c', 'import sys;print(sys.version.split()[0])')
    $rv = Invoke-Native $cand.Exe $args1
    if (-not $rv.Ok) { $pythonTried += ("{0}: absent" -f $cand.Label); continue }
    $version = Get-FirstLine $rv.Output
    $args2 = @($cand.Pre) + @('-c', "import struct;print(struct.calcsize('P')*8)")
    $rb = Invoke-Native $cand.Exe $args2
    $bits = if ($rb.Ok) { Get-FirstLine $rb.Output } else { '?' }
    $pythonTried += ("{0}: {1} ({2} bits)" -f $cand.Label, $version, $bits)
    $isOk = ($version -match '^3\.(1[1-9]|[2-9]\d)') -and ($bits -eq '64')
    if ($isOk -and -not $pythonFound) {
        $pythonFound = @{ Exe = $cand.Exe; Pre = $cand.Pre; Label = $cand.Label; Version = $version; Bits = $bits }
    }
}
if ($pythonFound) {
    Add-Result 'Python 3.11+ (64 bits)' 'OK' ("{0} -> {1}, {2} bits [{3}]" -f $pythonFound.Label, $pythonFound.Version, $pythonFound.Bits, ($pythonTried -join ' ; '))
} else {
    Add-Result 'Python 3.11+ (64 bits)' 'MANQUANT' ("aucun interpréteur 3.11+ 64 bits [{0}]" -f ($pythonTried -join ' ; '))
}

if ($pythonFound) {
    $r = Invoke-Native $pythonFound.Exe (@($pythonFound.Pre) + @('-m', 'pip', '--version'))
    Add-Result 'pip' $(if ($r.Ok) { 'OK' } else { 'MANQUANT' }) (Get-FirstLine $r.Output)
    $r = Invoke-Native $pythonFound.Exe (@($pythonFound.Pre) + @('-c', 'import venv;print(venv.__name__)'))
    Add-Result 'module venv' $(if ($r.Ok) { 'OK' } else { 'MANQUANT' }) $(if ($r.Ok) { 'disponible' } else { Get-FirstLine $r.Output })
} else {
    Add-Result 'pip' 'MANQUANT' 'Python introuvable'
    Add-Result 'module venv' 'MANQUANT' 'Python introuvable'
}

$r = Invoke-Native 'node' @('--version')
Add-Result 'Node.js' $(if ($r.Ok) { 'OK' } else { 'MANQUANT' }) (Get-FirstLine $r.Output)
$r = Invoke-Native 'npm' @('--version')
Add-Result 'npm' $(if ($r.Ok) { 'OK' } else { 'MANQUANT' }) (Get-FirstLine $r.Output)
$r = Invoke-Native 'claude' @('--version')
Add-Result 'Claude Code (claude)' $(if ($r.Ok) { 'OK' } else { 'MANQUANT' }) $(if ($r.Ok) { Get-FirstLine $r.Output } else { 'npm install -g @anthropic-ai/claude-code' })

# --- MetaTrader 5 -------------------------------------------------------------
Write-Host '--- MetaTrader 5 ---' -ForegroundColor Cyan
$terminals = @(Find-MT5Terminals)
if ($terminals.Count -gt 0) {
    Add-Result 'MetaTrader 5 (terminal64.exe)' 'OK' ("{0} installation(s) : {1}" -f $terminals.Count, ($terminals -join ' | '))
} else {
    Add-Result 'MetaTrader 5 (terminal64.exe)' 'MANQUANT' 'aucun terminal64.exe trouvé (Program Files, brokers, %APPDATA%\MetaQuotes\Terminal\*\origin.txt)'
}
$mt5Procs = @()
try { $mt5Procs = @(Get-CimInstance -ClassName Win32_Process -Filter "Name='terminal64.exe'" -ErrorAction Stop) } catch { }
if ($mt5Procs.Count -gt 0) {
    $desc = ($mt5Procs | ForEach-Object { "PID {0} ({1})" -f $_.ProcessId, $_.ExecutablePath }) -join ' | '
    Add-Result 'Processus terminal64' 'INFO' ("en cours : {0}" -f $desc) $false
} else {
    Add-Result 'Processus terminal64' 'INFO' 'non lancé (start_all.ps1 le démarrera)' $false
}

# Package Python MetaTrader5 : venv du projet en priorité, sinon Python système
$venvPython = Join-Path $ProjectDir '.venv\Scripts\python.exe'
$pipTarget = $null
if (Test-Path -LiteralPath $venvPython) {
    $pipTarget = @{ Exe = $venvPython; Pre = @(); Label = '.venv du projet' }
} elseif ($pythonFound) {
    $pipTarget = @{ Exe = $pythonFound.Exe; Pre = $pythonFound.Pre; Label = $pythonFound.Label }
}
if ($pipTarget) {
    $r = Invoke-Native $pipTarget.Exe (@($pipTarget.Pre) + @('-m', 'pip', 'show', 'MetaTrader5'))
    if ($r.Ok -and $r.Output -match '(?m)^Version:\s*(.+)$') {
        Add-Result 'Package Python MetaTrader5' 'OK' ("version {0} ({1})" -f $Matches[1].Trim(), $pipTarget.Label)
    } else {
        Add-Result 'Package Python MetaTrader5' 'MANQUANT' ("non installé dans {0} (install_windows.ps1 l'installe via requirements.txt)" -f $pipTarget.Label)
    }
} else {
    Add-Result 'Package Python MetaTrader5' 'MANQUANT' 'Python introuvable'
}
Add-Result 'venv du projet (.venv)' 'INFO' $(if (Test-Path -LiteralPath $venvPython) { $venvPython } else { 'absent (créé par install_windows.ps1)' }) $false
Add-Result 'Fichier .env' 'INFO' $(if (Test-Path -LiteralPath (Join-Path $ProjectDir '.env')) { 'présent (contenu non lu)' } else { 'absent : copier .env.example vers .env et le remplir' }) $false

# --- Réseau -------------------------------------------------------------------
Write-Host '--- Réseau ---' -ForegroundColor Cyan
$okPypi = Test-Port443 'pypi.org'
Add-Result 'Internet : pypi.org:443' $(if ($okPypi) { 'OK' } else { 'MANQUANT' }) $(if ($okPypi) { 'joignable' } else { 'injoignable (pip install impossible)' })
$okFmp = Test-Port443 'financialmodelingprep.com'
Add-Result 'Internet : financialmodelingprep.com:443' $(if ($okFmp) { 'OK' } else { 'AVERTISSEMENT' }) $(if ($okFmp) { 'joignable' } else { 'injoignable (mode NEWS_DATA_DEGRADED)' }) $false

# --- Disque -------------------------------------------------------------------
Write-Host '--- Disque et heure ---' -ForegroundColor Cyan
$freeGB = $null
try {
    $disk = Get-CimInstance -ClassName Win32_LogicalDisk -Filter "DeviceID='C:'" -ErrorAction Stop
    $freeGB = [math]::Round($disk.FreeSpace / 1GB, 1)
    $sizeGB = [math]::Round($disk.Size / 1GB, 1)
    $statut = if ($freeGB -ge 10) { 'OK' } elseif ($freeGB -ge 3) { 'AVERTISSEMENT' } else { 'MANQUANT' }
    Add-Result 'Espace disque C:' $statut ("{0} Go libres sur {1} Go (minimum conseillé : 10 Go)" -f $freeGB, $sizeGB)
} catch {
    Add-Result 'Espace disque C:' 'AVERTISSEMENT' 'lecture impossible' $false
}

# --- Heure système / fuseau / NTP ----------------------------------------------
$tz = [System.TimeZoneInfo]::Local
$now = Get-Date
Add-Result 'Heure système' 'INFO' ("{0} | fuseau {1} ({2}) | UTC{3}" -f $now.ToString('yyyy-MM-dd HH:mm:ss'), $tz.Id, $tz.DisplayName, $tz.GetUtcOffset($now).ToString()) $false

$ntpOffset = $null
$r = Invoke-Native 'w32tm' @('/stripchart', '/computer:time.windows.com', '/samples:1', '/dataonly')
if ($r.Output) {
    $m = [regex]::Matches($r.Output, '([+-]\d+[\.,]\d+)s')
    if ($m.Count -gt 0) {
        $raw = $m[$m.Count - 1].Groups[1].Value -replace ',', '.'
        try { $ntpOffset = [double]::Parse($raw, [System.Globalization.CultureInfo]::InvariantCulture) } catch { }
    }
}
if ($null -ne $ntpOffset) {
    $statut = if ([math]::Abs($ntpOffset) -le 2) { 'OK' } else { 'AVERTISSEMENT' }
    Add-Result 'Décalage NTP (time.windows.com)' $statut ("{0:+0.000;-0.000} s (tolérance conseillée : 2 s)" -f $ntpOffset) $false
} else {
    Add-Result 'Décalage NTP (time.windows.com)' 'AVERTISSEMENT' 'mesure impossible (w32tm indisponible ou UDP/123 bloqué)' $false
}

# ---------------------------------------------------------------------------
# Tableau récapitulatif + rapport JSON
# ---------------------------------------------------------------------------
Write-Host ''
Write-Host '=================================================================' -ForegroundColor Cyan
Write-Host '  RÉCAPITULATIF' -ForegroundColor Cyan
Write-Host '=================================================================' -ForegroundColor Cyan
$script:Results | Format-Table -Property Composant, Statut, Requis, Detail -AutoSize -Wrap | Out-String -Width 220 | Write-Host

$manquants = @($script:Results | Where-Object { $_.Statut -eq 'MANQUANT' -and $_.Requis -eq 'oui' } | ForEach-Object { $_.Composant })
$avertissements = @($script:Results | Where-Object { $_.Statut -eq 'AVERTISSEMENT' } | ForEach-Object { $_.Composant })
$exitCode = if ($manquants.Count -gt 0) { 1 } else { 0 }

$reportsDir = Join-Path $ProjectDir 'reports'
$reportPath = $null
try {
    if (-not (Test-Path -LiteralPath $reportsDir)) { New-Item -ItemType Directory -Path $reportsDir -Force | Out-Null }
    $reportPath = Join-Path $reportsDir ("audit-{0}.json" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
    $report = [ordered]@{
        horodatage      = (Get-Date -Format 'yyyy-MM-ddTHH:mm:sszzz')
        machine         = $env:COMPUTERNAME
        utilisateur     = $env:USERNAME
        projet          = $ProjectDir
        admin           = $isAdmin
        python_retenu   = $(if ($pythonFound) { $pythonFound.Label } else { $null })
        terminaux_mt5   = $terminals
        decalage_ntp_s  = $ntpOffset
        espace_libre_go = $freeGB
        resultats       = @($script:Results | ForEach-Object { [ordered]@{ composant = $_.Composant; statut = $_.Statut; requis = ($_.Requis -eq 'oui'); detail = $_.Detail } })
        manquants       = $manquants
        avertissements  = $avertissements
        code_retour     = $exitCode
    }
    $json = $report | ConvertTo-Json -Depth 6
    [System.IO.File]::WriteAllText($reportPath, $json, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "Rapport JSON : $reportPath" -ForegroundColor Gray
} catch {
    Write-Host "Impossible d'écrire le rapport JSON : $($_.Exception.Message)" -ForegroundColor Yellow
}

Write-Host ''
if ($avertissements.Count -gt 0) {
    Write-Host ("Avertissements ({0}) : {1}" -f $avertissements.Count, ($avertissements -join ', ')) -ForegroundColor Yellow
}
if ($manquants.Count -gt 0) {
    Write-Host ("ÉLÉMENTS MANQUANTS ({0}) :" -f $manquants.Count) -ForegroundColor Red
    foreach ($m in $manquants) { Write-Host "  - $m" -ForegroundColor Red }
    Write-Host 'Lancez scripts\install_windows.ps1 pour installer les prérequis manquants.' -ForegroundColor Yellow
} else {
    Write-Host 'Tous les éléments requis sont présents.' -ForegroundColor Green
}
exit $exitCode
