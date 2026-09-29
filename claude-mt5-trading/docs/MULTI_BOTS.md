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

1. **Copier le dossier** (bot arrêté ou non, peu importe) :
   ```powershell
   robocopy C:\Claude-MT5-Trading\claude-mt5-trading C:\Claude-MT5-Trading\bot-ftmo /E /XD .venv logs state archives reports data\cache __pycache__
   ```
   Puis recréer l'environnement Python dans le nouveau dossier :
   ```powershell
   cd C:\Claude-MT5-Trading\bot-ftmo
   .\scripts\install_windows.ps1
   ```
2. **Installer un terminal MT5 portable dédié** dans `C:\Claude-MT5-Trading\mt5-bot-ftmo\`, s'y connecter une fois à la main
   avec le compte de la prop firm, activer « Trading algorithmique ».
3. **Remplir `.env`** du nouveau dossier (toi seul — jamais Claude) : `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`,
   `MT5_TERMINAL_PATH` (le terminal du point 2), `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` (un groupe par bot, conseillé).
4. **Paramétrer** (§3), puis **reprendre les agents** (§4).
5. **Premier lancement en PAPER**, puis AUTO sur ta demande (§5).

---

## 3. Ce qui DOIT être différent d'un bot à l'autre

| Réglage | Fichier | Pourquoi |
|---|---|---|
| `magic_number` (51000, 51100, 51200…) | `config/system.yaml` | Chaque bot ne gère que SES positions |
| `account_expected` (login, serveur, DEMO/REAL) | `config/system.yaml` | Le bot refuse de trader sur un autre compte |
| Profil de la prop firm | `config/prop_firms.yaml` | Perte jour / totale, cohérence, week-end, annonces, lots max, durée minimale |
| `risk_per_trade_percent`, plafonds | `config/risk.yaml` | À caler sur la taille du compte et la perte max de la firm |
| Port du tableau de bord (8765, 8766…) | `scripts/start_all.ps1` (voir §8) | Deux tableaux de bord ne peuvent pas écouter le même port |
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
.\scripts\start_all.ps1 -Mode PAPER     # premier lancement : aucun ordre réel
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
  mettre `learning.research_external: true` et **ne pas** lancer de worker (option `-NoResearch` à ajouter, §8).
- Un seul optimiseur à la fois sur le PC, toujours en priorité basse.
- Les suites de tests : jamais pendant qu'un bot a des positions ouvertes sensibles, toujours en priorité basse.

---

## 8. Adaptations du code à faire AVANT le 2ᵉ bot (à me demander)

Aujourd'hui le code suppose un seul bot par PC. Trois points à corriger avant de lancer le deuxième :

1. **Port du tableau de bord** : `start_all.ps1` lance toujours le port 8765 → ajouter `-DashboardPort`.
2. **Détection des processus** : `start_all.ps1` reconnaît ses composants à leur ligne de commande
   (`tradinglab.dashboards.server`…), identique pour tous les bots → un bot pourrait croire l'autre déjà lancé.
   À restreindre au dossier du bot (`TRADINGLAB_HOME`).
3. **Option `-NoResearch`** pour ne pas lancer de worker de recherche dans les bots d'exécution.

Environ une heure de travail avec les tests. Dis « prépare le multi-bots » et je le fais.
