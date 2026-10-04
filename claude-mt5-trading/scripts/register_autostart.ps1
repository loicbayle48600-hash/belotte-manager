#Requires -Version 5.1
<#
.SYNOPSIS
    Crée / met à jour (ou supprime avec -Remove) la tâche planifiée Windows "ClaudeMT5TradingLab".

.DESCRIPTION
    La tâche démarre le laboratoire à l'ouverture de session de l'utilisateur courant, avec un délai de 2 minutes :
        powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<ProjectDir>\scripts\start_all.ps1" -ProjectDir "<ProjectDir>" -Mode SAFE
    Paramètres de la tâche :
      - redémarrage automatique en cas d'échec : 3 tentatives, intervalle 1 minute ;
      - pas d'arrêt / pas de blocage sur batterie ;
      - aucune limite de durée d'exécution (ExecutionTimeLimit = PT0S) ;
      - StartWhenAvailable (exécution rattrapée si le déclencheur a été manqué) ;
      - une seule instance à la fois (IgnoreNew) ;
      - session interactive de l'utilisateur (les fenêtres réduites et MT5 sont visibles).

    IMPORTANT - REPRISE APRÈS REDÉMARRAGE :
      Le système redémarre TOUJOURS en SAFE_MODE (-Mode SAFE est codé en dur ici, et
      config/system.yaml : safe_mode_on_startup = true). Aucun ancien ordre n'est jamais "rouvert" :
      la logique de reprise (relecture de state/system_state.json, réconciliation des positions
      réellement présentes dans MT5, journal des décisions) est entièrement dans l'orchestrateur Python.
      Le passage en AUTO reste une décision humaine explicite (start_all.ps1 -Mode AUTO).

.PARAMETER ProjectDir
    Racine du projet (défaut : dossier parent de ce script).

.PARAMETER TaskName
    Nom de la tâche (défaut : ClaudeMT5TradingLab).

.PARAMETER Remove
    Désinscrit la tâche planifiée.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\register_autostart.ps1
    powershell -ExecutionPolicy Bypass -File .\scripts\register_autostart.ps1 -Remove

.NOTES
    Enregistrer une tâche pour son propre compte (LogonType Interactive) ne nécessite normalement pas
    de droits administrateur ; si Register-ScheduledTask refuse (accès refusé), relancer en administrateur.
    Compatible Windows PowerShell 5.1 (module ScheduledTasks intégré à Windows 8+/Server 2012+).
#>
[CmdletBinding()]
param(
    [string]$ProjectDir = '',
    [string]$TaskName = 'ClaudeMT5TradingLab',
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

if (-not $ProjectDir) { $ProjectDir = Split-Path -Parent $PSScriptRoot }
$ProjectDir = [System.IO.Path]::GetFullPath($ProjectDir).TrimEnd('\')
$StartScript = Join-Path $ProjectDir 'scripts\start_all.ps1'
$UserName = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

Write-Host ''
Write-Host '=================================================================' -ForegroundColor Cyan
Write-Host "  TÂCHE PLANIFIÉE - $TaskName" -ForegroundColor Cyan
Write-Host '=================================================================' -ForegroundColor Cyan
Write-Host "  Projet      : $ProjectDir"
Write-Host "  Utilisateur : $UserName"

try { Import-Module ScheduledTasks -ErrorAction Stop } catch {
    Write-Host "Module ScheduledTasks indisponible : $($_.Exception.Message)" -ForegroundColor Red
    exit 2
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

# --- Suppression -----------------------------------------------------------------
if ($Remove) {
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Tâche '$TaskName' supprimée." -ForegroundColor Green
    } else {
        Write-Host "Tâche '$TaskName' inexistante : rien à faire." -ForegroundColor Yellow
    }
    exit 0
}

# --- Création / mise à jour --------------------------------------------------------
if (-not (Test-Path -LiteralPath $StartScript)) {
    Write-Host "Script de démarrage introuvable : $StartScript" -ForegroundColor Red
    exit 2
}

# Le mode est volontairement figé à SAFE : voir le bloc IMPORTANT dans l'en-tête.
$arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Minimized -File `"$StartScript`" -ProjectDir `"$ProjectDir`" -Mode SAFE"
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments -WorkingDirectory $ProjectDir

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $UserName
$trigger.Delay = 'PT2M'   # délai de 2 minutes après l'ouverture de session (réseau, MT5, services)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -MultipleInstances IgnoreNew `
    -Compatibility Win8
$settings.ExecutionTimeLimit = 'PT0S'   # PT0S = aucune limite de durée (la valeur 0 est parfois mal propagée)

$principal = New-ScheduledTaskPrincipal -UserId $UserName -LogonType Interactive -RunLevel Limited

$description = "Claude-MT5-Trading : démarrage automatique du laboratoire en SAFE_MODE à l'ouverture de session (délai 2 min). " +
               "Aucun ancien ordre n'est rouvert ; la reprise est gérée par l'orchestrateur Python. Projet : $ProjectDir"

if ($existing) { Write-Host "Tâche existante détectée : mise à jour." -ForegroundColor Yellow } else { Write-Host 'Création de la tâche.' }
$task = Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description $description -Force

Write-Host ''
Write-Host "Tâche '$($task.TaskName)' enregistrée (état : $($task.State))." -ForegroundColor Green
Write-Host "  Déclencheur : ouverture de session de $UserName, délai 2 min"
Write-Host "  Action      : powershell.exe $arguments"
Write-Host '  Échecs      : 3 redémarrages, intervalle 1 min | Batterie : autorisé | Limite durée : aucune | StartWhenAvailable : oui'
Write-Host ''
Write-Host 'Commandes utiles :' -ForegroundColor Cyan
Write-Host "  Tester maintenant : Start-ScheduledTask -TaskName $TaskName"
Write-Host "  État / dernier run : Get-ScheduledTaskInfo -TaskName $TaskName"
Write-Host "  Supprimer          : powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Remove"
Write-Host ''
Write-Host 'Rappel : le laboratoire redémarre toujours en SAFE_MODE ; le passage en AUTO est une action humaine explicite.' -ForegroundColor Yellow
exit 0
