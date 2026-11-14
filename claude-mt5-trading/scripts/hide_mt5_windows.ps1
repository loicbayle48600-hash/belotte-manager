<#
.SYNOPSIS
    Masque les fenêtres de tous les terminaux MetaTrader 5 (maître et suiveurs) et les consoles python du labo — demande utilisateur 2026-09-24 :
    « les fenêtres de MT5, je ne veux plus les voir, fais-le en silencieux ».

.DESCRIPTION
    Un terminal masqué continue de fonctionner normalement (connexion, API Python, ordres) : seule sa fenêtre
    disparaît de l'écran et de la barre des tâches. MT5 peut ré-ouvrir sa fenêtre (reconnexion, relance par
    `mt5.initialize`), d'où le mode -Loop qui repasse toutes les 20 s. Une seule boucle à la fois (fichier PID).

    -Loop   : boucle silencieuse (lancée par start_all.ps1)
    -Show   : réaffiche toutes les fenêtres MT5 et arrête la boucle (pour intervenir à la main)
    (aucun) : masque une fois
#>
param([switch]$Loop, [switch]$Show)

$ErrorActionPreference = 'SilentlyContinue'
$root = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $root 'state\hide_mt5.pid'

Add-Type -Namespace TlWin -Name Native -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool ShowWindow(System.IntPtr hWnd, int nCmdShow);
[DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, System.IntPtr lParam);
[DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(System.IntPtr hWnd, out uint pid);
[DllImport("user32.dll")] public static extern bool IsWindowVisible(System.IntPtr hWnd);
[DllImport("user32.dll")] public static extern int GetWindowTextLength(System.IntPtr hWnd);
[DllImport("user32.dll")] public static extern System.IntPtr GetWindow(System.IntPtr hWnd, uint cmd);
public delegate bool EnumProc(System.IntPtr hWnd, System.IntPtr lParam);
'@

function Set-MT5Windows([int]$cmd, [bool]$onlyVisible, [bool]$mt5Only = $false) {
    # terminaux MT5 + consoles python du labo (venv du projet) : rien ne doit rester visible (demande 2026-09-24).
    # En réaffichage ($mt5Only), seules les fenêtres PRINCIPALES des terminaux MT5 (sans propriétaire) reviennent :
    # les consoles python et les fenêtres internes de MT5 restent cachées.
    $venv = Join-Path $root '.venv'
    $ids = @(Get-Process terminal64 | ForEach-Object { $_.Id })
    if (-not $mt5Only) {
        $ids += @(Get-Process python | Where-Object { $_.Path -and $_.Path.StartsWith($venv, [System.StringComparison]::OrdinalIgnoreCase) } | ForEach-Object { $_.Id })
    }
    if (-not $ids.Count) { return 0 }
    $script:n = 0
    $cb = [TlWin.Native+EnumProc]{
        param($h, $l)
        $procId = [uint32]0
        [void][TlWin.Native]::GetWindowThreadProcessId($h, [ref]$procId)
        $principale = -not $mt5Only -or [TlWin.Native]::GetWindow($h, 4) -eq [IntPtr]::Zero   # GW_OWNER
        if ($principale -and $ids -contains [int]$procId -and [TlWin.Native]::GetWindowTextLength($h) -gt 0) {
            if (-not $onlyVisible -or [TlWin.Native]::IsWindowVisible($h)) {
                [void][TlWin.Native]::ShowWindow($h, $cmd); $script:n++
            }
        }
        return $true
    }
    [void][TlWin.Native]::EnumWindows($cb, [IntPtr]::Zero)
    return $script:n
}

if ($Show) {
    if (Test-Path $pidFile) {
        $old = [int](Get-Content $pidFile -Raw)
        if ($old -and $old -ne $PID) { Stop-Process -Id $old -Force -Confirm:$false }
        Remove-Item $pidFile -Force
    }
    [void](Set-MT5Windows 0 $true)    # d'abord tout recacher (consoles python comprises) après un ancien -Show trop large
    $n = Set-MT5Windows 5 $false $true   # SW_SHOW : fenêtres principales MT5 uniquement
    Write-Host "Fenêtres MT5 réaffichées : $n (masquage automatique arrêté jusqu'au prochain start_all)."
    return
}

if ($Loop) {
    if (Test-Path $pidFile) {
        $old = [int](Get-Content $pidFile -Raw)
        if ($old -and $old -ne $PID -and (Get-Process -Id $old)) { return }   # une boucle tourne déjà
    }
    Set-Content -Path $pidFile -Value $PID -Encoding ascii
    while ($true) {
        [void](Set-MT5Windows 0 $true)   # SW_HIDE
        Start-Sleep -Seconds 20
    }
}

$n = Set-MT5Windows 0 $true
Write-Host "Fenêtres MT5 masquées : $n"
