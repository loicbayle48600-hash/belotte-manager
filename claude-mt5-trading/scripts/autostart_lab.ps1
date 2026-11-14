<#
.SYNOPSIS
    Démarrage automatique du Trading Lab à l'ouverture de session Windows (2026-09-23, demande utilisateur).

.DESCRIPTION
    Appelé par le raccourci du dossier Démarrage (via autostart_lab.vbs, qui masque la console).
    1. attend que le réseau réponde (au démarrage, la carte n'est pas prête tout de suite) ;
    2. lance le lab en AUTO via start_all.ps1, qui reste seul responsable de MT5, des processus
       et de l'anti-doublon (un composant déjà vivant n'est jamais relancé) ;
    3. ouvre une fenêtre Claude Code dans le dossier du projet, avec le message de reprise.

    Le mode AUTO est explicite ici : l'utilisateur a demandé le 2026-09-23 que le bot reparte seul au
    démarrage du PC. start_all.ps1 vérifie de toute façon le compte DEMO attendu avant d'autoriser AUTO.
#>
[CmdletBinding()]
param(
    [int]$NetworkTimeoutSec = 180,
    [switch]$NoClaude
)

$ErrorActionPreference = 'Continue'
$ProjectDir = Split-Path -Parent $PSScriptRoot
$LogFile = Join-Path $ProjectDir 'logs\autostart.log'
New-Item -ItemType Directory -Path (Split-Path $LogFile) -Force | Out-Null

function Write-Log([string]$Message) {
    # -Encoding utf8 : Add-Content écrirait sinon en ANSI, et Tee-Object en UTF-16 (journal illisible)
    Add-Content -LiteralPath $LogFile -Value ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message) -Encoding utf8
}

Write-Log "=== demarrage automatique (session $env:USERNAME) ==="

# 1. réseau : au boot, la carte peut mettre une minute à répondre
$deadline = (Get-Date).AddSeconds($NetworkTimeoutSec)
while ((Get-Date) -lt $deadline) {
    if (Test-Connection -ComputerName '1.1.1.1' -Count 1 -Quiet -ErrorAction SilentlyContinue) { break }
    Start-Sleep -Seconds 5
}
Write-Log "reseau pret (ou delai depasse)"

# 2. le lab (start_all gère MT5, les copieurs et l'anti-doublon)
try {
    # sortie non redirigée ici : start_all.ps1 tient déjà son propre journal (logs\start_all.log), et
    # mélanger les deux flux produisait un fichier moitié UTF-8 moitié UTF-16, illisible
    & (Join-Path $PSScriptRoot 'start_all.ps1') -Mode AUTO | Out-Null
    Write-Log "start_all.ps1 termine (code $LASTEXITCODE) - detail dans logs\start_all.log"
} catch {
    Write-Log "ERREUR start_all : $_"
}

# 3. la conversation Claude Code, dans une vraie fenêtre (interactive : elle ne peut pas être masquée)
if (-not $NoClaude) {
    $claude = (Get-Command claude -ErrorAction SilentlyContinue).Source
    if ($claude) {
        $prompt = 'Reprise apres redemarrage du PC : verifie l etat du Trading Lab (STATUS, RISK, positions, journal), ' +
                  'signale toute anomalie et reprends la surveillance habituelle.'
        # --continue reprend la dernière conversation du projet ; le message est passé en argument.
        Start-Process -FilePath 'cmd.exe' `
            -ArgumentList '/c', 'start', '""', 'cmd', '/k', "cd /d `"$ProjectDir`" && claude --continue `"$prompt`""
        Write-Log "fenetre Claude Code lancee"
    } else {
        Write-Log "executable claude introuvable : fenetre non lancee"
    }
}

Write-Log "=== fin ==="
