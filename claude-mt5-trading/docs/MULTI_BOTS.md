# Plusieurs bots, une prop firm chacun — lancer et paramétrer

État au 29/09/2026. Objectif : 3 ou 4 bots indépendants sur le même PC, chacun sur son compte de prop firm, avec ses règles,
et tous nourris par les agents qui ont fait leurs preuves ici.

---

## 1. Le principe

Chaque bot est un **dossier complet**, copie de `claude-mt5-trading` :

```
C:\Claude-MT5-Trading\
├── claude-mt5-trading\      ← bot 1 (IC Markets démo, le laboratoire actuel)
├── bot-ftmo\                 ← bot 2 (exemple)
├── bot-fundednext\           ← bot 3
└── mt5-bot-ftmo\             ← un terminal MetaTrader 5 portable PAR bot
```

Chaque bot a **son** compte MT5, **son** terminal, **ses** règles, **son** état, **ses** journaux. Ce qu'ils partagent :
les agents validés (copiés au départ) et, si tu veux, les résultats (voir §6).

Un bot = environ 10 processus Python et 1 terminal MT5. Sur ce PC (24 cœurs, 32 Go), 4 bots passent, mais un seul
worker de recherche et un seul optimiseur à la fois (voir §7).

---

## 2. Créer un nouveau bot (une fois)

1. **Créer le dossier** avec le script (copie le code, les agents et leur historique, donne un numéro magique propre,
   désactive le copy trading, prépare `.env.a_remplir`, crée l'environnement Python) :
   ```powershell
   cd C:\Claude-MT5-Trading\claude-mt5-trading
   .\scripts\new_bot.ps1 -Nom bot-ftmo -Magic 51100
   ```
   Numéros magiques : 51100, 51200, 51300… (51000 = le laboratoire).
2. **Installer un terminal MT5 portable dédié** dans `C:\Claude-MT5-Trading\mt5-bot-ftmo\`, s'y connecter une fois à la main
   avec le compte de la prop firm, activer « Trading algorithmique ».
3. **Compléter `.env.a_remplir` puis le renommer en `.env`** (toi seul — jamais Claude) : `MT5_LOGIN`, `MT5_PASSWORD`,
   `MT5_SERVER`, `MT5_TERMINAL_PATH` (le terminal du point 2), `DASHBOARD_PORT` (8766, 8767…), `TRADINGLAB_NO_RESEARCH=1`,
   `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` (un groupe par bot, conseillé).
4. **Paramétrer** (§3) — surtout `account_expected` et les règles de la prop firm.
5. **Premier lancement en SAFE** (aucune entrée), puis AUTO sur ta décision (§5).

---

## 3. Ce qui DOIT être différent d'un bot à l'autre

| Réglage | Fichier | Pourquoi |
|---|---|---|
| `magic_number` (51000, 51100, 51200…) | `config/system.yaml` | Chaque bot ne gère que SES positions |
| `account_expected` (login, serveur, DEMO/REAL) | `config/system.yaml` | Le bot refuse de trader sur un autre compte |
| Profil de la prop firm | `config/prop_firms.yaml` | Perte jour / totale, cohérence, week-end, annonces, lots max, durée minimale |
| `risk_per_trade_percent`, plafonds | `config/risk.yaml` | À caler sur la taille du compte et la perte max de la firm |
| Port du tableau de bord (8765, 8766…) | `DASHBOARD_PORT` dans `.env` | Deux tableaux de bord ne peuvent pas écouter le même port |
| Nom de la tâche de démarrage auto | `register_autostart.ps1 -TaskName ...` | Une tâche Windows par bot |
| `copy_trading.enabled` | `config/copy_trading.yaml` | `false` sur les nouveaux bots (sauf si tu veux des suiveurs) |

**Règles de la prop firm** : donne-les-moi (capture ou texte de leur page « règles »). Je remplis `prop_firms.yaml` et
j'ajoute les tests, comme pour FOXX le 23/09.

---

## 4. Reprendre les agents qui marchent

Deux fichiers portent tout :

- `data/agent_status.json` — le statut de chaque agent (LIVE / SHADOW / SUSPENDED).
- `data/learning.db` — l'historique des trades : les agents gardent leurs statistiques, pas besoin de refaire 40 trades.

Copie au départ :
```powershell
copy C:\Claude-MT5-Trading\claude-mt5-trading\data\agent_status.json C:\Claude-MT5-Trading\bot-ftmo\data\
copy C:\Claude-MT5-Trading\claude-mt5-trading\data\learning.db     C:\Claude-MT5-Trading\bot-ftmo\data\
```

Conseil : sur un compte de prop firm payant, ne garder en **LIVE** que les agents prouvés (ex. X01, X02, CH157, E05) et
laisser les autres en SHADOW. Demande-le-moi : je prépare la liste avec les chiffres du moment.

---

## 5. Lancer, arrêter, surveiller (par bot)

Toutes les commandes se lancent **depuis le dossier du bot** :

```powershell
cd C:\Claude-MT5-Trading\bot-ftmo
.\scripts\start_all.ps1 -Mode SAFE      # premier lancement : aucune entrée
.\scripts\start_all.ps1 -Mode AUTO      # trading, seulement quand tu l'as décidé
.\scripts\stop_all.ps1                  # arrêt propre (PAUSE puis arrêt des processus de CE bot)
.venv\Scripts\python.exe -m tradinglab.api.cli STATUS     # état
.venv\Scripts\python.exe -m tradinglab.api.cli RISK       # risque ouvert
.venv\Scripts\python.exe -m tradinglab.api.cli PAUSE      # plus d'entrées, gestion des positions continue
```

Tableau de bord : `https://localhost:8766` pour le bot 2, `8767` pour le 3, etc.

**Démarrage automatique au démarrage de Windows** (une tâche par bot) :
```powershell
cd C:\Claude-MT5-Trading\bot-ftmo
.\scripts\register_autostart.ps1 -TaskName ClaudeMT5-FTMO
```

---

## 6. Faire évoluer les agents ensuite

Le laboratoire (bot 1, démo) reste **l'endroit où l'on cherche** : optimiseur, ombre, trades papier, challengers. Les
bots de prop firm **exécutent**. Quand un agent passe LIVE au laboratoire, on le recopie dans les autres bots :
```powershell
.venv\Scripts\python.exe -m tradinglab.api.cli RESEARCH_STATUS   # au laboratoire : qui est LIVE
```
puis je dépose le changement de statut dans `state\agent_status_requests.jsonl` du bot concerné (appliqué au cycle
suivant, sans redémarrage).

---

## 7. Charge du PC

- Le worker de recherche (backtests) et l'optimiseur tournent **uniquement au laboratoire** : dans les autres bots,
  mettre `TRADINGLAB_NO_RESEARCH=1` dans leur `.env` (ou lancer `start_all.ps1 -NoResearch`).
- Un seul optimiseur à la fois sur le PC, toujours en priorité basse.
- Les suites de tests : jamais pendant qu'un bot a des positions ouvertes sensibles, toujours en priorité basse.

---

## 8. Ce qui a été adapté pour le multi-bots (29/09)

- `start_all.ps1 -DashboardPort 8766` (ou `DASHBOARD_PORT` dans `.env`) : un port de tableau de bord par bot.
- `start_all.ps1 -NoResearch` (ou `TRADINGLAB_NO_RESEARCH=1`) : pas de worker de recherche dans les bots d'exécution.
- Terminal MT5 : avec `MT5_TERMINAL_PATH`, `start_all` ne regarde que CE terminal (un autre bot ou un suiveur ouvert ne le
  trompe plus) et le lance en mode portable s'il n'est pas dans Program Files.
- Détection des processus : `start_all` / `stop_all` s'appuient sur `state\pids.json` et sur le `.venv` du dossier — chaque
  bot a les siens, un bot n'arrête jamais les processus d'un autre.
- `scripts\new_bot.ps1` : création d'un bot en une commande (§2).
