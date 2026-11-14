#Requires -Version 5.1
<#
.SYNOPSIS
    Crée un nouveau bot (une prop firm) à partir du laboratoire : copie du code et des agents validés (docs/MULTI_BOTS.md).

.DESCRIPTION
    1. Copie le dossier du laboratoire vers <Racine>\<Nom> sans l'état d'exécution (venv, logs, state, archives, rapports, cache).
    2. Copie les agents (data\agent_status.json) et leur historique (data\learning.db).
    3. Donne au bot son propre numéro magique (config\system.yaml) et désactive le copy trading.
    4. Prépare .env.a_remplir (copie de .env.example) : À REMPLIR À LA MAIN puis renommer en .env —
       compte MT5, serveur, MT5_TERMINAL_PATH, DASHBOARD_PORT, TRADINGLAB_NO_RESEARCH=1, Telegram.
    5. Crée le venv (install_windows.ps1).
    Ne lance rien et ne touche à aucun compte : le premier démarrage se fait à la main, en SAFE.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\new_bot.ps1 -Nom bot-ftmo -Magic 51100
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Nom,
    [Parameter(Mandatory = $true)][int]$Magic,
    [string]$Racine = '',
    [switch]$SansVenv        # essai : ne crée pas l'environnement Python
)
$ErrorActionPreference = 'Stop'
$Source = Split-Path -Parent $PSScriptRoot
if (-not $Racine) { $Racine = Split-Path -Parent $Source }
$Cible = Join-Path $Racine $Nom
if (Test-Path -LiteralPath $Cible) { throw "Le dossier $Cible existe déjà : choisir un autre nom." }
if ($Magic -eq 51000) { throw "51000 est le numéro magique du laboratoire : en choisir un autre (51100, 51200…)." }

Write-Host "1. Copie du code vers $Cible"
& robocopy $Source $Cible /E /NFL /NDL /NJH /NJS /NP /XD .venv logs state archives reports backups __pycache__ .pytest_cache (Join-Path $Source 'data\cache') (Join-Path $Source 'data\research') /XF .env *.log | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy a échoué (code $LASTEXITCODE)" }

Write-Host "2. Copie des agents et de leur historique"
New-Item -ItemType Directory -Path (Join-Path $Cible 'data') -Force | Out-Null
foreach ($f in @('agent_status.json', 'learning.db')) {
    $src = Join-Path $Source "data\$f"
    if (Test-Path -LiteralPath $src) { Copy-Item -LiteralPath $src -Destination (Join-Path $Cible "data\$f") -Force }
}

Write-Host "3. Numéro magique $Magic, copy trading désactivé"
$sys = Join-Path $Cible 'config\system.yaml'
$txt = [System.IO.File]::ReadAllText($sys, [System.Text.Encoding]::UTF8)
$txt = [regex]::Replace($txt, '(?m)^(\s*magic_number:\s*)\d+', "`${1}$Magic")
[System.IO.File]::WriteAllText($sys, $txt, (New-Object System.Text.UTF8Encoding($false)))
$ct = Join-Path $Cible 'config\copy_trading.yaml'
if (Test-Path -LiteralPath $ct) {
    $t = [System.IO.File]::ReadAllText($ct, [System.Text.Encoding]::UTF8)
    $t = [regex]::Replace($t, '(?m)^(\s*enabled:\s*)true(\s*)$', '${1}false${2}', 1)
    [System.IO.File]::WriteAllText($ct, $t, (New-Object System.Text.UTF8Encoding($false)))
}

Write-Host "4. .env.a_remplir (à compléter à la main puis renommer en .env)"
$ex = Join-Path $Cible '.env.example'
if (Test-Path -LiteralPath $ex) {
    $e = [System.IO.File]::ReadAllText($ex, [System.Text.Encoding]::UTF8)
    $e += "`r`n# --- multi-bots (docs/MULTI_BOTS.md) ---`r`nDASHBOARD_PORT=8766`r`nTRADINGLAB_NO_RESEARCH=1`r`n"
    [System.IO.File]::WriteAllText((Join-Path $Cible '.env.a_remplir'), $e, (New-Object System.Text.UTF8Encoding($false)))
}

if (-not $SansVenv) {
    Write-Host "5. Création de l'environnement Python"
    & powershell -ExecutionPolicy Bypass -File (Join-Path $Cible 'scripts\install_windows.ps1') -ProjectDir $Cible
}

Write-Host ""
Write-Host "Bot $Nom prêt. Reste à faire (à la main) :" -ForegroundColor Green
Write-Host "  - config\system.yaml : account_expected (login, serveur, DEMO/REAL)"
Write-Host "  - config\prop_firms.yaml et config\risk.yaml : règles de la prop firm (donner les règles à Claude)"
Write-Host "  - .env.a_remplir -> .env : compte, MT5_TERMINAL_PATH (terminal portable dédié), DASHBOARD_PORT, Telegram"
Write-Host "  - premier lancement : .\scripts\start_all.ps1 -Mode SAFE depuis $Cible"
