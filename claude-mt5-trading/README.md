# Claude MT5 Trading Lab

Laboratoire de trading algorithmique **multi-agents autonome** relié à **MetaTrader 5**, conçu pour tourner d'abord sur un
**compte DEMO**. Le système combine des agents Claude (analyse, revue adversariale, optionnels) et un noyau **déterministe en
Python** (risque, stop loss, Execution Gate, watchdog) qui garde le dernier mot sur chaque ordre.

> **État du projet.** Le code a été construit et testé dans un conteneur Linux avec un broker simulé
> (`TRADINGLAB_BROKER=mock`, 101 tests `pytest`, orchestrateur/watchdog/CLI/smoke test exécutés sur mock). L'adaptateur MT5 réel
> (`src/tradinglab/mt5/mt5_adapter.py`) **n'a pas pu être exécuté** ici : le package `MetaTrader5` n'existe que sous Windows x64.
> Les étapes Windows (audit, installation, connexion MT5, smoke test DEMO réel, lancement SAFE puis AUTO) restent à réaliser sur
> votre PC avec `scripts/*.ps1` puis `scripts/smoke_test_demo.py`.

Sommaire : [Installation](#installation) · [Architecture](#architecture) · [MT5](#mt5) · [MCP](#mcp) · [Models](#models) ·
[Agents](#agents) · [News](#news) · [Loop](#loop) · [Risk](#risk) · [Profit management](#profit-management) · [Learning](#learning) ·
[Backtest](#backtest) · [Shadow](#shadow) · [Prop mode](#prop-mode) · [Logs](#logs) · [Dashboard](#dashboard) · [Commands](#commands) ·
[Troubleshooting](#troubleshooting) · [Emergency procedures](#emergency-procedures) · [Final acceptance test](#final-acceptance-test) ·
[Avertissement](#avertissement)

## Installation

### Windows (cible réelle, compte DEMO)

Tous les scripts sont compatibles Windows PowerShell 5.1 et se lancent avec
`powershell -ExecutionPolicy Bypass -File .\scripts\<script>.ps1`. Dossier cible par défaut : `C:\Claude-MT5-Trading`.
Détails dans `scripts/README_SCRIPTS.md`.

1. **Audit (lecture seule)** — `scripts\audit_windows.ps1` : Windows 64 bits, PowerShell, winget, git, Python 3.11 x64, pip, venv,
   Node/npm, Claude Code, MetaTrader 5 (`terminal64.exe`), package `MetaTrader5`, internet, disque, heure/NTP. Rapport
   `reports\audit-YYYYMMDD-HHmmss.json`. Code retour 0 = complet, 1 = éléments manquants.
2. **Installation idempotente** — `scripts\install_windows.ps1 [-ProjectDir C:\Claude-MT5-Trading] [-SkipMT5]` : copie du projet,
   winget (`Python.Python.3.11`, `Git.Git`, `OpenJS.NodeJS.LTS`) seulement si absents, `npm install -g @anthropic-ai/claude-code`
   si `claude` manque, MetaTrader 5 depuis l'installateur officiel MetaQuotes si aucun terminal n'existe, venv `.venv`,
   `pip install -r requirements.txt`, `pip install -e .`, copie `.env.example` → `.env`, vérifications finales (`import MetaTrader5`).
3. **Remplir `.env`** (jamais versionné) :
   ```
   MT5_LOGIN=5056132326
   MT5_PASSWORD=<mot de passe du compte DEMO>      # OU laissez vide et connectez-vous dans le terminal MT5
   MT5_SERVER=MetaQuotes-Demo
   MT5_TERMINAL_PATH=C:\Program Files\MetaTrader 5\terminal64.exe
   ANTHROPIC_API_KEY=<clé>                          # absent → mode déterministe (TIER_D), aucun appel LLM
   FMP_API_KEY=<clé>                                # absent → NEWS_DATA_DEGRADED=true
   TRADINGLAB_HOME=C:\Claude-MT5-Trading
   TRADINGLAB_BROKER=mt5
   ```
   Le mot de passe ne va **que** dans `.env` (variable `MT5_PASSWORD`). Si le terminal MT5 est déjà connecté au compte, `mt5.initialize()`
   est appelé sans credentials et **aucun mot de passe n'est nécessaire** au package Python.
4. **Connexion MT5** : ouvrir le terminal, `Fichier > Connexion à un compte de trading` (compte DEMO `5056132326`, serveur
   `MetaQuotes-Demo`), activer le bouton *Algo Trading*.
5. **Smoke test DEMO** (marché ouvert) :
   ```powershell
   .\.venv\Scripts\python.exe scripts\smoke_test_demo.py           # affichage compte + données, sans ordre
   .\.venv\Scripts\python.exe scripts\smoke_test_demo.py --yes     # ordre de test au volume minimum, SL obligatoire, fermeture immédiate
   ```
   Le script refuse tout compte non DEMO ou différent de `config/system.yaml > account_expected` (code 3), reporte si le marché est fermé
   (code 6) et doit se terminer par `SMOKE TEST DEMO REUSSI` sans position `TLAB:SMOKE` restante.
6. **Démarrage** — `scripts\start_all.ps1 -Mode SAFE` (attente réseau, chargement `.env`, lancement de MT5 si besoin, puis watchdog,
   orchestrateur `--mode SAFE`, dashboard ; PID dans `state\pids.json`). Après observation de plusieurs cycles :
   `scripts\start_all.ps1 -Mode AUTO` (décision humaine explicite ; AUTO n'est appliqué qu'après ≥ 2 cycles sains sur compte DEMO).
7. **Démarrage automatique** — `scripts\register_autostart.ps1` : tâche planifiée `ClaudeMT5TradingLab` à l'ouverture de session
   (délai 2 min), **toujours** `-Mode SAFE`, 3 redémarrages sur échec. `-Remove` pour la supprimer.
8. **Arrêt** — `scripts\stop_all.ps1` : envoie `PAUSE`, attend 5 s, arrête watchdog → orchestrateur → dashboard. Ne ferme jamais MT5.

### Linux / test (broker simulé)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt            # MetaTrader5 est ignoré hors Windows (marqueur sys_platform)
export TRADINGLAB_BROKER=mock PYTHONPATH=src
python -m pytest                           # 127 tests
python -m tradinglab.orchestration.orchestrator --broker mock --cycles 4 --mode AUTO
python -m tradinglab.monitoring.watchdog --broker mock
python -m tradinglab.api.cli STATUS
python scripts/smoke_test_demo.py --broker mock --yes
```

Le `MockBroker` (`src/tradinglab/mt5/mock_adapter.py`) fournit 16 symboles synthétiques déterministes (graine fixe), les règles broker
(volume min/step, `stops_level`), un fill immédiat, des SL/TP déclenchés sur les barres suivantes et des hooks de test
(`fail_next_order`, `drop_sl_on_fill`, `reject_modify`, `set_connected(False)`). Il se présente par défaut comme le compte DEMO attendu.

## Architecture

```
Claude Code / agents LLM (TIER_A/B/C)   ── lecture, propositions ──┐
                                                                    ▼
                         .mcp.json → python -m tradinglab.mcp.server (stdio)
                                   aucune route brute vers order_send
                                                                    │ state/commands.jsonl · state/proposals.jsonl
                                                                    ▼
   Registre (105 agents) → Market Router → Screeners Python → Revue adversariale → Orchestrateur
                                                                    │ TradeCandidate (verdict APPROVE)
                                                                    ▼
                          Execution Gate — 20 contrôles déterministes
                     Risk Manager · Daily Guard · Correlation Guard · Prop Guard
                                                                    │ OrderRequest (SL obligatoire)
                                                                    ▼
                    Executor → BrokerAdapter.order_send() → vérification post-fill du SL
                                                                    │
                    MT5Adapter (package MetaTrader5, Windows)   |   MockBroker (Linux/tests)
                                                                    ▼
                          Terminal MetaTrader 5 (terminal64.exe) → Broker DEMO

   Watchdog (processus indépendant) ── MT5 + system_state.json ──▶ state/watchdog.json ──▶ orchestrateur, CLI, dashboard
```

Processus : **orchestrateur** (`tradinglab.orchestration.orchestrator`), **watchdog** (`tradinglab.monitoring.watchdog`),
**dashboard** lecture seule (`tradinglab.dashboards.server`), **serveur MCP** à la demande (`tradinglab.mcp.server`), **CLI**
(`tradinglab.api.cli`). Ils communiquent uniquement par fichiers atomiques dans `state/` et par le journal `logs/`.

Configuration : `config/system.yaml`, `risk.yaml`, `models.yaml`, `markets.yaml`, `prop_firms.yaml`, `strategies.yaml`,
`news_sources.yaml` (fusionnés par `core/config.py`). Les secrets viennent **exclusivement** de l'environnement / `.env`.

## MT5

- Interface `BrokerAdapter` (`mt5/adapter.py`) : `connect`, `account_info`, `symbols`, `symbol_info`, `tick`, `rates`, `positions`,
  `pending_orders`, `history_deals`, `order_check`, `order_send`, `modify_position`, `close_position`, `cancel_order`.
- `MT5Adapter` : import paresseux de `MetaTrader5` ; `initialize(path=MT5_TERMINAL_PATH[, login, password, server])` ; `trade_mode`
  0/1/2 → `DEMO/CONTEST/REAL` ; hedging = `margin_mode == 2` ; mode de remplissage FOK/IOC/RETURN choisi selon `filling_mode` du symbole ;
  retcodes `10009` (done) / `10008` (placed) ; après un ordre au marché, le ticket de position est retrouvé par magic + symbole.
- Identification des positions du bot : `magic_number: 51000`, commentaire `TLAB:<agent_id>` (`order_comment_prefix`).
- Univers (`config/markets.yaml`) : majors, minors, métaux, indices, énergies, crypto. Les racines sont résolues vers le symbole réel du
  broker (`EURUSD.m`, `XAUUSDmicro`, `GER40.cash`…) par `mt5/symbols.py`. Les symboles absents sont journalisés (`universe.missing`).
- Garde-fous : compte attendu (`account_expected`) vérifié au démarrage, sinon verrou `ACCOUNT_MISMATCH` ; seul un compte `DEMO` est
  autorisé à l'automatisation tant que le mode prop n'est pas validé.
- **Non vérifié sur terminal réel** : suffixes du broker, `stops_level`, `filling_mode`, récupération du ticket, deals d'historique.

## MCP

- `.mcp.json` déclare le serveur `mt5-trading-lab` : `python -m tradinglab.mcp.server` avec `TRADINGLAB_HOME` et `PYTHONPATH=src`.
  Lancer `claude` depuis le dossier du projet (venv activé) ; compatible `mcp` 1.x (`FastMCP`) et 2.x (`MCPServer`).
- Outils exposés : `status`, `positions`, `risk`, `account` (jamais de mot de passe), `tick`, `rates` (≤ 500 barres), `agents`,
  `top_agents`, `news`, `calendar`, `why`, `report_day`, `research_status`, `command` (`PAUSE | RESUME | SAFE_MODE | PANIC |
  CLOSE <ticket|symbole> | CLOSE_ALL_BOT | BREAK_EVEN <ticket>`), `propose_trade`.
- **Absence de route brute** : aucun outil n'appelle `order_send`, `modify_position` ni `close_position` directement ; `command` passe par la
  file `state/commands.jsonl` de l'orchestrateur et n'ouvre jamais de position. `propose_trade` exige un SL, journalise la proposition
  (`state/proposals.jsonl`, événement `trade_proposal`) — dans l'état actuel du code, aucun composant ne relit ce fichier pour
  fabriquer un candidat : une proposition n'est jamais exécutée automatiquement.

## Models

`config/models.yaml` définit quatre tiers, routés par rôle (`models/router.py`) :

| Tier | Candidats | Rôles |
|---|---|---|
| `TIER_A` | `claude-fable-5-1` | chief_orchestrator, deep_market_review, macro_synthesis, strategy_research, champion_adjudication, post_trade_root_cause, refactor |
| `TIER_B` | `claude-opus-5` | senior_strategy, bull_thesis, bear_thesis, devil_advocate, setup_validation, complex_news, second_opinion, adversarial_review, trade_arbiter |
| `TIER_C` | `claude-sonnet-5`, `claude-haiku-4-5-20251001` | worker, screening, technical_analysis, news_summary, classification, bulk |
| `TIER_D` | `python` | scan, indicators, correlations, sizing, statistics, rules, risk, backtest, ranking, monitoring, scheduling |

- Les identifiants sont des préférences **vérifiées au démarrage** via `models.list` de l'API Anthropic (rafraîchies toutes les 3600 s).
  Modèle absent → suivant de la liste → tier inférieur → déterministe.
- Budget `daily_budget_usd: 10.0`, quotas horaires `max_fable_calls_per_hour: 6`, `max_opus_calls_per_hour: 30`,
  `max_worker_calls_per_hour: 200`, cache `cache_ttl_sec: 240`, coût mesuré sur les tokens (`llm_call` journalisé).
- **Sans `ANTHROPIC_API_KEY`** : mode déterministe (`TIER_D`), revue déterministe, `llm_skipped` journalisé. Budget atteint → seules les
  tâches `financial_importance="high"` (arbitre) peuvent encore appeler un modèle. Le risque et le watchdog ne dépendent jamais des modèles.
- Toute sortie LLM est marquée `MODEL_INTERPRETATION` et doit répondre `UNKNOWN` quand une donnée manque.

## Agents

- Registre de **105 agents** (`agents/registry.py`), familles : **A** scanners déterministes (11) · **B** trend (10) · **C** breakout (9) ·
  **D** pullback (7) · **E** reversal / mean reversion (7) · **F** structure / price action (6) · **G** volatilité (5) · **H** macro /
  cross-asset (7) · **I** news-aware (5) · **J** adversarial / review (10) · **K** stratégies news-sensibles (2, en `SHADOW`) ·
  **L** variantes par classe d'actif (12) · **M** spécialistes par symbole (14). C'est un **pool** : 105 agents ≠ 105 appels LLM.
- Chaque agent générateur porte une `strategy` = un des 18 **screeners Python** (`agents/screeners.py`) : `ema_trend`, `mtf_trend_pullback`,
  `ema_pullback`, `session_breakout`, `daily_hl_breakout`, `compression_expansion`, `atr_expansion`, `breakout_retest`, `bollinger_mr`,
  `rsi_divergence`, `exhaustion`, `failed_breakout`, `liquidity_sweep`, `sr_rejection`, `structure_bos`, `choch`, `macd_momentum`,
  `fib_pullback`. Ils travaillent sur la **dernière barre clôturée** et produisent un `TradeCandidate` (entrée, SL structure/ATR, plan de TP,
  `setup_score` 0-100, arguments pour/contre, règle d'invalidation).
- Statuts : `RESEARCH → BACKTEST → SHADOW → CANDIDATE → LIVE → DEGRADED → SUSPENDED / RETIRED` (persistés dans `data/agent_status.json`).
- Market Router (`orchestration/market_router.py`) : agents actifs = compatibles régime + session + classe d'actif/symbole ; symboles
  `data_quality != OK` ou en `NEWS_SHOCK` ignorés ; une seule idée par (symbole, sens), bonus de concordance (+2/agent, max +8),
  pénalité −10 si des agents s'opposent sur le même symbole.
- Revue adversariale (`agents/review.py`) : filtre déterministe (qualité des données, état news, RR ≥ 1.5, expectancy historique négative
  sur ≥ 40 trades, score ≥ 65 + bonus journalier) puis, si un modèle est disponible, `bull_thesis` / `bear_thesis` / `devil_advocate` et
  `trade_arbiter`. L'arbitre ne peut ni annuler un rejet déterministe ni approuver sous « seuil − 10 ». Verdicts `APPROVE | WAIT | REJECT |
  NO_TRADE`. **Le Risk Gate a toujours le dernier mot.**

## News

- Providers (`config/news_sources.yaml`, `news/providers.py`) : `fmp_economic_calendar` et `fmp_news` (Financial Modeling Prep,
  `https://financialmodelingprep.com/stable`), clé lue dans `FMP_API_KEY`, timeout 10 s, jamais de valeur inventée (`ProviderError`).
- Hub (`news/hub.py`) : cache `data/cache/news_cache.json`, dédoublonnage, `max_age_minutes: 90`, `degraded_after_failures: 3`.
  `NewsHub.check()` renvoie `OK | BLOCKED_PRE_NEWS (30 min) | BLOCKED_POST_NEWS (15 min) | SHOCK | DEGRADED`.
- Fait vs interprétation : événements de calendrier et titres = `FACT` ; importance/actifs = classification par mots-clés ; direction
  (`BULLISH/BEARISH/NEUTRAL`) = `MODEL_INTERPRETATION` à faible confiance.
- **Sans `FMP_API_KEY`** : `NEWS_DATA_DEGRADED=true` et `calendar_data_degraded=true` ; les agents `news_sensitive` (familles H, I, K) sont
  bloqués, les autres passent avec l'état marqué `DEGRADED`.
- Macro (`macro/sentiment.py`) : `RISK_ON / RISK_OFF / NEUTRAL / UNKNOWN` calculé sur les rendements H1 récents des indices, de l'or et
  d'AUDJPY (règle fixe, `CALCULATED`).

## Loop

Cycle de l'orchestrateur (`orchestration/orchestrator.py`) :

1. commandes en attente (`state/commands.jsonl`) et rapport du watchdog ;
2. santé + compte (reconnexion, roulement de journée, equity) ;
3. synchronisation des positions, revue post-trade des positions fermées ;
4. news / calendrier (cadence), snapshots multi-timeframes, régimes, sentiment macro ;
5. gestion des positions ouvertes (toutes les 15 s) ;
6. garde journalière ;
7. sur nouvelle barre ou `fast_scanner` (60 s) : routage → screeners → enrichissement (stats, news, cas similaires) → revue → classement →
   pour chaque `APPROVE` : Execution Gate → exécution (max **2 entrées par cycle**) ;
8. shadow trading ;
9. recherche / dégradation (thread hors chemin critique, toutes les 3600 s), rafraîchissement des modèles, export (300 s) ;
10. passage en `AUTO` si demandé et sain ; sauvegarde de l'état.

Cadences (`config/system.yaml`) : watchdog 3 s, position_manager 15 s, fast_scanner 60 s, news 120 s, calendar 900 s, research 3600 s ;
timeframes `M5 M15 H1 H4 D1`. Pas de HFT : le sommeil s'aligne sur la prochaine clôture de barre (1 à 15 s).

## Risk

Execution Gate (`execution/gate.py`) — 20 contrôles, **tous** obligatoires : `01_health` (broker + watchdog vivant), `02_account`,
`03_authorization` (DEMO / prop), `04_data_fresh`, `05_symbol_tradable`, `06_fresh_price` (≤ 0.5 ATR), `07_spread`, `08_news`,
`09_daily_guard` + `09b_sizing`, `10_stop_loss`, `11_volume`, `12_daily_drawdown`, `13_overall_drawdown`, `14_*` (limites portefeuille),
`15_*` (corrélation), `16_duplicate_idea`, `17_existing_position`, `18_order_check`, `19_prop_hard_*`, `20_final_approval`
(mode AUTO, pas de verrou, RR, score, verdict).

Valeurs de `config/risk.yaml` :

| Paramètre | Valeur |
|---|---|
| `risk_per_trade_percent` / `max_risk_per_trade_percent` | 0.25 % / 0.35 % |
| `max_daily_loss_internal_percent` | 1.0 % |
| `max_total_open_risk_percent` | 1.0 % |
| `max_open_positions` / `max_positions_per_symbol` | 3 / 1 |
| `max_consecutive_losses` | 3 |
| martingale, grid, averaging down, retrait de SL, hausse de risque après entrée | interdits |
| corrélation : cluster / facteur devise / classe d'actif | 0.5 % / 0.6 % / 0.75 % (seuil 0.7, 200 barres) |
| daily profit : paliers 0.75 / 1.00 / 1.50 % | risque 0.15 / 0.10 / 0.075 %, score requis +5 / +10 / +15 |
| `max_giveback_percent` | 25 % du pic de P&L du jour |

Exécution (`config/system.yaml`) : spread ≤ 40 pts (XAU 60, indices 100) et ≤ 0.15 ATR, SL entre 0.25 et 4 ATR, RR ≥ 1.5,
`required_setup_score` 65, déviation 20 pts, tick ≤ 30 s, ≥ 250 barres. Le volume est calculé par `risk/risk_manager.py`
(risque monétaire / perte par lot, arrondi vers le bas, volume minimum refusé s'il dépasse 0.35 %).

## Profit management

Mode `ADAPTIVE_R_MANAGEMENT` (`execution/position_manager.py`, 1R = distance entrée → SL initial) :

- TP1 à **1.5R** (30 % du volume), TP2 à **2.5R** (40 %), runner 30 % ;
- break-even à **1.2R** (+0.05R), seulement si la structure de marché le confirme (`break_even_requires_structure`) ;
- trailing dès **2.0R** : 1.5 × ATR, ajusté sur le dernier swing ;
- sortie anticipée sur invalidation (clôture au-delà de l'EMA50 avant 0.5R, si l'agent le prévoit) ; jamais de sortie forcée en `NEWS_SHOCK` ;
- `regime_overrides` : `RANGING` → TP1 1.0R/50 %, TP2 1.8R/35 %, runner 15 % ; `NEWS_SHOCK` → pas de nouvelle entrée ;
- règles absolues : **NEVER_WIDEN_STOP**, **NEVER_REMOVE_STOP**, SL jamais au-delà du prix courant / `stops_level` ; la fermeture est toujours autorisée.

## Learning

- Base SQLite `data/learning.db` (`learning/store.py`) : trades (`live | shadow | backtest`), événements d'agents, mémoire de marché
  (`market_snapshots` avec résultat). Statistiques par agent : win rate, profit factor, expectancy R, drawdown R, Sharpe/Sortino
  indicatifs, découpages par régime/session/symbole/volatilité, fenêtres récentes 20/30/50, `degradation_score`, calibration score/résultat.
- Revue post-trade (`learning/post_trade.py`) : qualité du SL, de la sortie, de l'exécution, verdict `VARIANCE_NORMALE | ERREUR_STRATEGIE |
  ERREUR_EXECUTION | INDETERMINE`. Jamais de modification de stratégie après une seule perte ; un challenger n'est demandé qu'avec ≥ 40 trades.
- Champion / challenger (`research/pipeline.py`) : `IDEA → BACKTEST → OUT_OF_SAMPLE → WALK_FORWARD → MONTE_CARLO → SHADOW →
  STATISTICAL_REVIEW → RISK_REVIEW → PROMOTION`, persisté dans `data/research/<agent_id>.json` (historique, rollback). **RESEARCH → LIVE
  direct est impossible** (`PromotionError`). Seuils (`config/strategies.yaml`) : PF ≥ 1.2, expectancy ≥ 0.10R, DD ≤ 15R, ≥ 40 trades.
- Dégradation : `LIVE → DEGRADED` (score ≥ 0.5 sur ≥ 60 trades), `DEGRADED → SUSPENDED` (≥ 0.75), retour `LIVE` (< 0.25). Un champion
  dégradé génère des challengers (paramètres ± 20 %, max 3 par cycle). `live_self_mutation: false`.
- Rapports (`learning/reports.py`) : jour, semaine, mois, stratégie, risque, coût des modèles, apprentissage — les pertes ne sont jamais masquées.

## Backtest

`backtest/engine.py` : moteur barre par barre **sans lookahead** (le signal ne voit que `df.iloc[:i+1]`, exécution à l'open de i+1,
spread/2 + slippage défavorables, SL prioritaire si SL et TP touchés dans la même barre, une position à la fois), `assert_no_lookahead()`,
`split_in_out_of_sample`, `walk_forward` (4 plis), `monte_carlo` (500 tirages), `parameter_sensitivity`, `stress_test`. Coûts par défaut
(`config/strategies.yaml`) : spread 12 pts, commission 0, slippage 3 pts. `research/adapters.py` transforme un agent en `signal_fn`.

## Shadow

`shadow/shadow.py` : les agents `SHADOW` et `CANDIDATE` ouvrent des positions **virtuelles** (entrée, SL, TP, horodatage) sans aucun ordre
réel, clôturées sur les barres M5 (SL prioritaire, timeout 72 h) et enregistrées en mode `shadow` dans `data/learning.db`. État dans
`state/shadow_positions.json`. `shadow_required: true` : aucune promotion sans phase shadow.

## Prop mode

- Profil `config/prop_firms.yaml` (`FOXX_FUNDED`, `1_STEP`, 500 000, cible 7 %, hard limits 4 % jour / 8 % global) : **hypothèses à
  confirmer officiellement** ; `rules_source_url`, `rules_version`, `rules_verified_at` sont vides.
- Règles critiques `UNKNOWN` et bloquantes : `ea_allowed`, `news_trading_window_minutes`, `trading_day_definition`, `daily_loss_basis`.
- `AUTONOMOUS_TRADING_PROP` ne peut devenir effectif que si **`autonomous_prop: true`** (system.yaml) **et `prop_rules_verified: true`
  et `user_explicitly_authorized_prop_automation: true` et aucune règle critique `UNKNOWN`** (`risk/prop_guard.py`). Sinon tout compte
  `REAL`/`CONTEST` est refusé par `03_authorization`, le mode DEMO restant autorisé.
- Limites internes toujours plus strictes : 1 % jour, 60 % du hard global, puis hard limits avec marge de 25 %. `RISK` / outil MCP `risk`
  renvoient le rapport de conformité (`compliance_report`).

## Logs

| Fichier | Contenu |
|---|---|
| `logs/journal-YYYY-MM-DD.jsonl` | audit trail de tous les composants : `startup`, `account`, `universe`, `recovery`, `models`, `candidate`, `gate` (20 contrôles), `order_send`, `position_opened`, `position_managed`, `partial_tp`, `sl_modified`, `post_trade_review`, `command`, `emergency_action`, `watchdog_alert`, `llm_call` / `llm_skipped`, `research_stage`, `agent_status_change`… |
| `logs/orchestrator.log`, `logs/watchdog.log`, `logs/cli.log`, `logs/mcp.log`, `logs/smoke_test.log` | logs texte par composant |
| `logs/<composant>.out.log` / `.err.log`, `logs/start_all.log`, `logs/stop_all.log` | sorties des processus lancés par les scripts Windows |
| `state/system_state.json` | état persistant (mode, verrous, positions du bot, clés d'idempotence, budget modèles, heartbeats) |
| `state/watchdog.json` | rapport du watchdog |
| `data/learning.db`, `data/agent_status.json`, `data/agent_stats.json`, `data/research/*.json`, `data/cache/news_cache.json` | apprentissage, statuts, exports |
| `reports/leaderboard.json`, `reports/daily-*.json`, `reports/weekly-*.json`, `reports/audit-*.json` | rapports |

Les clés ressemblant à `password`, `secret`, `api_key`, `token` sont remplacées par `***` avant écriture.

## Dashboard

```bash
python -m tradinglab.dashboards.server            # http://127.0.0.1:8765 (options --port, --host, --home)
```

Lecture seule, bibliothèque standard uniquement : page HTML auto-rafraîchie (5 s), `/api/state` (état public, prop, risque, queue du journal,
filtre `?kinds=gate,order_send`), `/api/journal?day=YYYY-MM-DD`, `/health`. Il ne lit jamais `.env`, ne pousse aucune commande et n'envoie
aucun ordre ; une valeur absente s'affiche `UNKNOWN / UNAVAILABLE`.

## Commands

```bash
python -m tradinglab.api.cli STATUS               # ou : tradinglab STATUS (après pip install -e .)
python -m tradinglab.api.cli WHY 123456 --json
python -m tradinglab.api.cli CLOSE EURUSD
```

| Commande | Type | Effet |
|---|---|---|
| `STATUS` | lecture | mode, verrous, compte, equity, P&L/drawdown du jour, positions, heartbeats, watchdog, news, budget modèles |
| `POSITIONS` | lecture | positions du bot avec leur plan (SL initial, TP partiels, R max/min) |
| `RISK` | lecture | rapport de risque + conformité prop |
| `TODAY` / `REPORT_DAY` | lecture | rapport journalier (refus du gate par contrôle, événements, pertes incluses) ; `REPORT_DAY` écrit `reports/daily-*.json` |
| `PERFORMANCE` / `REPORT_WEEK` | lecture | rapports hebdo + mensuel ; `REPORT_WEEK` écrit `reports/weekly-*.json` |
| `AGENTS`, `TOP_AGENTS`, `DEGRADED_AGENTS` | lecture | registre, classement par expectancy, agents dégradés/suspendus |
| `NEWS`, `CALENDAR` | lecture | état news (dégradé ou non), cache, événements à venir |
| `WHY <ticket\|symbole>` | lecture | reconstitue la décision depuis le journal du jour (contrôles échoués inclus) |
| `RESEARCH_STATUS` | lecture | pipeline champion/challenger, challengers, trades shadow |
| `HELP` | lecture | liste des commandes |
| `PAUSE` / `RESUME` | action | `PAUSED` ↔ `AUTO` (RESUME refusé si compte non DEMO ou `ACCOUNT_MISMATCH`) |
| `SAFE_MODE` | action | passe en `SAFE_MODE` (aucune nouvelle entrée) |
| `PANIC` | action | `PANIC` + verrou, annule les ordres et ferme toutes les positions du bot (`panic_close_all_bot_positions: true`) |
| `CLOSE <ticket\|symbole>` | action | ferme une position (ou toutes celles du symbole) |
| `CLOSE_ALL_BOT` | action | ferme toutes les positions portant le magic `51000` |
| `BREAK_EVEN <ticket>` | action | déplace le SL au break-even (jamais élargi) |

Les actions sont déposées dans `state/commands.jsonl` et exécutées au cycle suivant. Si le heartbeat de l'orchestrateur dépasse 45 s,
`PANIC`, `CLOSE` et `CLOSE_ALL_BOT` sont **exécutés directement** via le broker (fermeture/annulation uniquement, jamais d'ouverture).
Aucune commande ne permet d'ouvrir une position.

## Troubleshooting

| Problème | Diagnostic | Résolution |
|---|---|---|
| `STATUS` → `mt5_connected: false`, mode `SAFE_MODE` « broker déconnecté » | terminal MT5 fermé/déconnecté, `MT5_TERMINAL_PATH` faux, *Algo Trading* désactivé, package `MetaTrader5` absent | ouvrir/connecter MT5, corriger `.env`, relancer `audit_windows.ps1` ; l'orchestrateur et le watchdog retentent en boucle |
| `lock_reasons: ACCOUNT_MISMATCH` | compte connecté ≠ `account_expected` ou non DEMO | connecter le compte `5056132326` / `MetaQuotes-Demo` |
| `01_health` refusé alors que MT5 est connecté | watchdog absent (pas de `state/watchdog.json` récent) | lancer `python -m tradinglab.monitoring.watchdog` (fait par `start_all.ps1`) |
| `04_data_fresh` / `routing.skipped: STALE, NO_TICK, INSUFFICIENT` | tick > 30 s, marché fermé, < 250 barres | attendre l'ouverture, vérifier le flux MT5, charger l'historique dans le terminal |
| `05_symbol_tradable` / smoke test « MARCHE FERME » | hors dim. 22:00 → ven. 21:00 UTC, ou `trade_allowed` faux | attendre ; vérifier le symbole dans MT5 |
| `universe.missing` long | suffixes du broker non reconnus | ajuster `config/markets.yaml` aux noms réels du broker |
| `08_news: DEGRADED` bloque des agents | `FMP_API_KEY` absent ou 3 échecs consécutifs | renseigner la clé ou accepter que seuls les agents non news-sensibles tradent |
| `llm_skipped`, `available_models: []` | `ANTHROPIC_API_KEY` absent, `models.list` en échec, budget/quotas atteints | attendu en mode déterministe ; vérifier la clé ou `daily_budget_usd` |
| `watchdog_alert: position sans SL` | SL absent après fill ou retiré manuellement | le watchdog remet le SL attendu, sinon ferme ; contrôler `stops_level` |
| `orchestrator_heartbeat_age_sec` > 45 | processus mort ou bloqué | voir `logs/orchestrator.err.log` ; `PANIC`/`CLOSE_ALL_BOT` agissent directement ; relancer en SAFE |
| `state/system_state.corrupt.json` présent | JSON d'état illisible | état réinitialisé automatiquement ; les positions MT5 sont adoptées au démarrage |
| `RESUME` refusé | compte non DEMO ou `autonomous_demo: false` | vérifier le compte ; le mode prop exige la procédure de la section Prop mode |

## Emergency procedures

1. **Tout fermer immédiatement** : `python -m tradinglab.api.cli PANIC` (ou outil MCP `command PANIC`). Mode `PANIC`, verrou `PANIC`,
   annulation des ordres en attente, fermeture de toutes les positions du bot. Si l'orchestrateur ne répond plus, la CLI agit directement sur le broker.
2. **Stopper les entrées sans fermer** : `python -m tradinglab.api.cli SAFE_MODE` (ou `PAUSE`). Les positions restent gérées (SL, TP partiels).
3. **Fermer sans changer de mode** : `CLOSE_ALL_BOT` ou `CLOSE <ticket|symbole>` — toujours autorisés, même verrouillé.
4. **Arrêter les processus** : `scripts\stop_all.ps1` (PAUSE, délai 5 s, `Stop-Process` watchdog → orchestrateur → dashboard ; MT5 reste ouvert).
   Sous Linux : `SIGTERM` sur l'orchestrateur (arrêt propre), `Ctrl-C` sur le watchdog.
5. **Positions restantes** : après arrêt, les positions ouvertes portant le magic `51000` restent dans MT5 avec leur SL ; fermez-les dans le
   terminal ou relancez en SAFE (elles seront adoptées, jamais rouvertes).
6. **Ce que fait le watchdog** (processus indépendant, toutes les 3 s, sans LLM) : vérifie MT5, le compte, les positions du bot, la présence
   des SL, la fraîcheur des ticks, les drawdowns (limite interne 1 %, 75 % des hard limits) et le heartbeat de l'orchestrateur ; publie
   `state/watchdog.json` (`safe_mode_request`, `reasons`, `actions`) ; **agit directement** sur une position sans SL (remise du SL attendu,
   sinon fermeture `WATCHDOG no-SL`). Il n'écrit jamais `system_state.json` ; l'orchestrateur en AUTO repasse en `SAFE_MODE` sur `safe_mode_request`.
7. **Retour à la normale** : corriger la cause, `python -m tradinglab.api.cli RESUME` (compte DEMO uniquement) ou redémarrage `start_all.ps1`
   (SAFE par défaut, puis `-Mode AUTO` sur décision humaine).

## Final acceptance test

Le cahier des charges (section 43) n'étant pas versionné dans ce dépôt, les 36 points ci-dessous sont **reconstitués à partir du code** et
de la procédure ; l'état indique ce qui a été validé sur le broker simulé (`pytest` + exécutions manuelles) et ce qui reste à valider sur Windows.

| # | Point | État |
|---|---|---|
| 1 | Audit du poste Windows (`audit_windows.ps1` code 0) | à valider sur Windows |
| 2 | Installation idempotente (`install_windows.ps1`, venv, `import MetaTrader5`) et tâche planifiée (`register_autostart.ps1`, SAFE) | à valider sur Windows |
| 3 | `.env` seul dépositaire des secrets ; aucune clé/mot de passe dans YAML, journaux, dashboard | validé sur mock (scrub journal, tests providers) — à revérifier sur Windows |
| 4 | Connexion `mt5.initialize()` + `account_info()` réels | à valider sur Windows |
| 5 | `DEMO ACCOUNT CONFIRMED` (login `5056132326`, `MetaQuotes-Demo`, `DEMO`, EUR) | validé sur mock — à valider sur Windows |
| 6 | Refus de tout compte non DEMO / différent d'`account_expected` (smoke test + `ACCOUNT_MISMATCH`) | validé sur mock |
| 7 | Résolution des suffixes broker et classes d'actifs | validé sur mock — à valider sur le terminal réel |
| 8 | Ticks, OHLC multi-TF, fraîcheur (`OK/STALE/NO_TICK/INSUFFICIENT`), régime déterministe | validé sur mock — à valider sur Windows |
| 9 | Smoke test : ordre volume minimum avec SL, SL vérifié, SL resserré, fermeture, aucune position restante | validé sur mock (`--broker mock --yes`) — à valider sur Windows |
| 10 | Démarrage toujours en `SAFE_MODE` (`safe_mode_on_startup`) | validé sur mock |
| 11 | Passage `AUTO` seulement sur `--mode AUTO` + ≥ 2 cycles sains + compte DEMO | validé sur mock |
| 12 | Aucun LLM / outil MCP n'accède à `order_send` ; seuls executor, adaptateurs, smoke test, tests l'appellent | validé (revue du code) |
| 13 | Execution Gate : 20 contrôles, refus si un seul échoue | validé sur mock (`test_risk_and_gate.py`) |
| 14 | SL obligatoire : refus SL absent / 0 / mauvais côté / trop proche / trop loin | validé sur mock |
| 15 | SL vérifié après fill ; correction, sinon fermeture + `SAFE_MODE` | validé sur mock |
| 16 | Sizing déterministe, arrondi vers le bas, risque max 0.35 % jamais dépassé, volume minimum refusé si excessif | validé sur mock |
| 17 | Perte journalière 1 %, 3 pertes consécutives, réduction de risque après profit, Giveback 25 % | validé sur mock |
| 18 | Exposition totale 1 %, 3 positions max, 1 par symbole, anti-averaging/grid | validé sur mock |
| 19 | Correlation Guard (facteur devise, classe d'actif, cluster corrélé) | validé sur mock |
| 20 | Prop Guard : règles `UNKNOWN` bloquantes, triple condition pour l'automatisation prop, limites internes < hard limits | validé sur mock |
| 21 | News : dégradé sans provider / après 3 échecs, blocage 30 min avant / 15 min après un événement HIGH, `SHOCK` | validé sur mock (`test_news.py`) — FMP réel à valider avec clé |
| 22 | Registre de 105 agents, familles A..M, statuts persistés | validé sur mock |
| 23 | Market Router : activation régime/session/marché, dédoublonnage, concordance/opposition | validé sur mock (cycles orchestrateur) |
| 24 | Revue adversariale : déterministe sans clé ; Bull/Bear/Devil/Arbitre avec modèle, arbitre borné | déterministe validé sur mock — LLM à valider avec `ANTHROPIC_API_KEY` |
| 25 | Model Router : vérification `models.list`, budget 10 USD/jour, quotas horaires, repli déterministe | repli validé sur mock — vérification API à valider avec clé |
| 26 | `ADAPTIVE_R_MANAGEMENT` : TP partiels, break-even, trailing, jamais d'élargissement ni de retrait du SL | validé sur mock (code, cycles) — à valider sur positions réelles |
| 27 | Idempotence : clé marquée avant `order_send`, aucune ré-entrée après redémarrage | validé sur mock (`test_no_reentry_after_restart`) |
| 28 | Adoption des positions existantes (magic 51000) au redémarrage, jamais de réouverture | validé sur mock — à valider sur Windows |
| 29 | Watchdog indépendant : heartbeat, SL manquant traité directement, `safe_mode_request` pris en compte | validé sur mock — à valider sur Windows |
| 30 | Commandes CLI/MCP : lecture + actions via file ; fermeture toujours autorisée même verrouillé | validé sur mock (`test_close_always_allowed_even_when_locked`) |
| 31 | Urgence : `PANIC`/`CLOSE`/`CLOSE_ALL_BOT` directs si orchestrateur silencieux, jamais d'ouverture | validé (code) sur mock — à valider sur Windows |
| 32 | Journal JSONL complet (candidats, gate, ordres, revues, commandes) et commande `WHY` | validé sur mock |
| 33 | Learning SQLite, revue post-trade, rapports jour/semaine/mois sans masquer les pertes | validé sur mock |
| 34 | Pipeline champion/challenger sans `RESEARCH → LIVE` direct, backtest sans lookahead, walk-forward, Monte Carlo | validé sur mock (`test_backtest.py`, pipeline) |
| 35 | Shadow trading virtuel des agents `SHADOW/CANDIDATE`, jamais d'ordre | validé sur mock |
| 36 | Dashboard lecture seule `127.0.0.1:8765`, sans secret, sans action | validé sur mock (`test_dashboard.py`) |

Commande de vérification locale : `TRADINGLAB_BROKER=mock python -m pytest` (127 tests).

## Avertissement

Ce logiciel est un **laboratoire expérimental**. Il ne constitue ni un conseil en investissement ni une promesse de performance :
**aucune rentabilité n'est garantie**, et le trading sur marge peut entraîner la perte totale du capital. Un `setup_score`, une expectancy
historique ou un résultat de backtest ne sont **pas des probabilités de gain**. Utilisez exclusivement un compte DEMO tant que chaque point
de la section précédente n'a pas été validé sur votre installation ; tout passage à un compte réel ou prop relève d'une décision humaine
explicite, après vérification officielle des règles, et reste sous votre entière responsabilité.
