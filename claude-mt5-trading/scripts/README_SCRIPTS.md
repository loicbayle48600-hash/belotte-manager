# Scripts Windows — Claude-MT5-Trading

Mode d'emploi court des scripts PowerShell de `scripts\`. Tous sont compatibles **Windows PowerShell 5.1** et
s'exécutent avec :

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\<script>.ps1 [paramètres]
```

Dossier cible par défaut : `C:\Claude-MT5-Trading`.

## Les 5 scripts

| Script | Rôle | Modifie le poste ? |
|---|---|---|
| `audit_windows.ps1` | Vérifie tous les prérequis (Windows 64 bits, PowerShell, winget, git, Python 3.11 64 bits, pip, venv, Node/npm, Claude Code, MetaTrader 5, package `MetaTrader5`, internet, disque, heure/NTP, droits admin). Tableau à l'écran + `reports\audit-YYYYMMDD-HHmmss.json`. Code retour 0 = complet, 1 = éléments manquants. | Non (écrit seulement le rapport JSON) |
| `install_windows.ps1` | Installation **idempotente** : copie du projet vers `-ProjectDir` (robocopy, sans `.git/.venv/logs/state`), winget (`Python.Python.3.11`, `Git.Git`, `OpenJS.NodeJS.LTS`) uniquement si absents, `npm install -g @anthropic-ai/claude-code` si `claude` absent, MetaTrader 5 depuis l'installateur officiel MetaQuotes si aucun `terminal64.exe` (jamais de réinstallation), venv `.venv`, `pip install -r requirements.txt`, `pip install -e .`, copie `.env.example` → `.env`, vérifications finales. Options : `-ProjectDir`, `-SkipMT5`. | Oui |
| `start_all.ps1` | Attend le réseau (120 s max), charge `.env`, lance MT5 si besoin (attente 15 s), active le venv, démarre en fenêtres réduites le **watchdog**, l'**orchestrateur** (`--mode SAFE` par défaut) et le **dashboard** (sauf `-NoDashboard`), écrit `state\pids.json`, affiche la commande MCP. Ne relance pas un composant déjà vivant. Options : `-ProjectDir`, `-Mode SAFE|AUTO`, `-NoDashboard`. | Oui (processus + logs) |
| `stop_all.ps1` | Lit `state\pids.json`, envoie `python -m tradinglab.api.cli PAUSE`, attend 5 s, puis `Stop-Process` (watchdog → orchestrateur → dashboard). **Ne ferme jamais MetaTrader 5.** Options : `-GraceSec`, `-SkipPause`. | Oui (arrêt des processus) |
| `register_autostart.ps1` | Crée/actualise la tâche planifiée `ClaudeMT5TradingLab` : à l'ouverture de session (délai 2 min), `start_all.ps1 -Mode SAFE`, 3 redémarrages sur échec (1 min), pas d'arrêt sur batterie, durée illimitée, StartWhenAvailable. `-Remove` pour la supprimer. | Oui (tâche planifiée) |

Journaux : `logs\start_all.log`, `logs\stop_all.log`, `logs\<composant>.out.log` / `.err.log` (la version précédente est
conservée en `.prev.log`).

## Ordre d'exécution recommandé

1. `audit_windows.ps1` — état des lieux (facultatif mais conseillé).
2. `install_windows.ps1` — installe ce qui manque, crée le venv et `.env`.
3. **Remplir `.env`** (voir ci-dessous) et **connecter le compte DEMO dans MetaTrader 5**.
4. `audit_windows.ps1` — doit renvoyer le code 0 (package `MetaTrader5` vu dans `.venv`).
5. `start_all.ps1` — démarrage en SAFE ; vérifier les logs et le dashboard.
6. `register_autostart.ps1` — démarrage automatique à l'ouverture de session (toujours en SAFE).
7. `stop_all.ps1` — arrêt propre quand nécessaire.
8. `start_all.ps1 -Mode AUTO` — uniquement sur décision humaine explicite, compte DEMO vérifié.

## Actions qui exigent une intervention humaine

- **UAC** : les installateurs Git, Node.js (winget) et MetaTrader 5 demandent une élévation ; exécuter les scripts
  dans une session interactive et valider les invites. `register_autostart.ps1` peut aussi exiger une session
  administrateur si l'enregistrement de la tâche est refusé.
- **Installateur MetaTrader 5** : interactif (licence, chemin) ; laisser le chemin par défaut
  `C:\Program Files\MetaTrader 5` ou renseigner `MT5_TERMINAL_PATH` dans `.env`.
- **Connexion broker dans MT5** : ouvrir le terminal, `Fichier > Connexion à un compte de trading` (compte **DEMO**,
  serveur identique à `MT5_SERVER`), puis activer le trading algorithmique (bouton *Algo Trading*). Le smoke test
  Python refuse tout compte non DEMO ou différent de `config/system.yaml > account_expected`.
- **Remplissage de `.env`** (jamais versionné, jamais recopié ailleurs) : `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`,
  `MT5_TERMINAL_PATH`, `TRADINGLAB_HOME`, `TRADINGLAB_BROKER` (`mt5` ou `mock`).
- **Clés API** : `ANTHROPIC_API_KEY` (obligatoire pour les agents), `FMP_API_KEY` (facultatif ; sinon mode
  `NEWS_DATA_DEGRADED`). Claude Code lui-même demande une authentification à sa première utilisation (`claude`).
- **Passage en mode AUTO** : décision humaine explicite (`start_all.ps1 -Mode AUTO`) ; le redémarrage automatique
  reste toujours en SAFE_MODE et ne rouvre jamais d'anciens ordres (reprise gérée par l'orchestrateur Python).
- **winget absent** : installer *App Installer* depuis le Microsoft Store (source officielle) avant `install_windows.ps1`.
- **Fermeture de MetaTrader 5** : jamais faite par les scripts ; à faire manuellement si nécessaire.

## Serveur MCP

Lancé à la demande (pas par `start_all.ps1`), depuis le dossier du projet avec le venv :

```powershell
cd C:\Claude-MT5-Trading
.\.venv\Scripts\python.exe -m tradinglab.mcp.server
```

Il est aussi déclaré dans `.mcp.json` : lancer `claude` depuis le dossier du projet (venv activé, `TRADINGLAB_HOME` défini).
