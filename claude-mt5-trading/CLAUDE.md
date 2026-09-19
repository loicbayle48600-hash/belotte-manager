# CLAUDE.md — Claude MT5 Trading Lab

Guide pour les sessions Claude Code qui travaillent sur ce dépôt. Tout ce qui suit est tiré du code et des
fichiers `config/*.yaml` : ne rien supposer qui n'y figure pas.

## 1. Mission et périmètre

- Laboratoire de trading algorithmique **multi-agents autonome** relié à **MetaTrader 5**, sur **compte DEMO d'abord**
  (`config/system.yaml` → `account_expected` : login `5056182608`, serveur `MetaQuotes-Demo`, `trade_mode: DEMO`, `EUR`, hedging).
- Package Python `tradinglab` (`src/tradinglab`, Python ≥ 3.11, `pyproject.toml`), processus séparés : orchestrateur,
  watchdog, dashboard (lecture seule), serveur MCP (stdio), CLI.
- Deux brokers : `TRADINGLAB_BROKER=mt5` (package `MetaTrader5`, **Windows x64 uniquement**) et `TRADINGLAB_BROKER=mock`
  (broker simulé déterministe, Linux/tests).
- **État réel du projet** : construit et testé dans un conteneur Linux avec le broker `mock` (`python -m pytest` : 101 tests).
  `src/tradinglab/mt5/mt5_adapter.py` n'a **jamais été exécuté** ici (le package `MetaTrader5` n'existe pas sous Linux).
  Les étapes Windows (audit, installation, connexion MT5, smoke test DEMO réel, lancement SAFE puis AUTO) restent à faire
  sur le PC de l'utilisateur (section 17).
- Aucune promesse de rentabilité. Un `setup_score` est un score interne 0-100, **pas une probabilité de gain**.

## 2. Principes non négociables (10)

1. **Sécurité déterministe** : sizing, SL, limites, gate, watchdog sont du Python pur (`TIER_D`) ; aucun LLM n'y participe.
2. **SL obligatoire** : `risk/stop_loss.py` refuse SL absent/0/mauvais côté/trop proche/trop loin ; après fill, `executor.py`
   remet le SL sinon ferme la position et passe en `SAFE_MODE` ; `position_manager.py` et `watchdog.py` remettent/ferment aussi.
3. **DEMO d'abord** : `PropGuard.authorization()` n'autorise que `TradeMode.DEMO` tant que le mode prop n'est pas
   entièrement validé ; l'orchestrateur verrouille les entrées (`ACCOUNT_MISMATCH`) si login/serveur/trade_mode diffèrent.
4. **Pas de RESEARCH → LIVE direct** : `research/pipeline.py` (`promote()` lève `PromotionError` si une étape manque).
5. **Jamais de donnée inventée** : provenance `FACT | CALCULATED | MODEL_INTERPRETATION | UNKNOWN | UNAVAILABLE`
   (`core/types.py`) ; les providers lèvent `ProviderError` au lieu de renvoyer une valeur plausible ; sans news → `NEWS_DATA_DEGRADED`.
6. **Score ≠ probabilité** : `TradeCandidate.setup_score` est interne ; `similar_situations()` renvoie une statistique descriptive « pas une garantie ».
7. **NO TRADE possible** : verdicts `APPROVE | WAIT | REJECT | NO_TRADE` ; le gate refuse par défaut (`08_news` refuse si état inconnu).
8. **Aucun LLM n'accède à `order_send`** : les agents produisent des `TradeCandidate` ; seul `execution/executor.py` appelle
   `broker.order_send()` après approbation de `execution/gate.py` (20 contrôles).
9. **Journalisation** : chaque décision, ordre, gate, revue, commande → `logs/journal-YYYY-MM-DD.jsonl` (`core/journal.py`, secrets masqués).
10. **Survie aux crashs** : état atomique `state/system_state.json`, clés d'idempotence marquées **avant** `order_send`,
    adoption des positions existantes au redémarrage, démarrage toujours en `SAFE_MODE`, boucle qui ne meurt jamais.

## 3. Architecture

```
Claude Code / agents LLM (TIER_A/B/C)  ──lecture, propositions──┐
                                                                 ▼
                     .mcp.json → tradinglab.mcp.server (stdio, aucune route brute)
                                                                 │ state/commands.jsonl, state/proposals.jsonl
                                                                 ▼
  Registre 105 agents → Market Router → Screeners Python → Revue adversariale → Orchestrator (boucle)
                                                                 │ TradeCandidate (verdict APPROVE)
                                                                 ▼
                                Execution Gate (20 contrôles déterministes)
                                    Risk Manager + Daily Guard + Correlation Guard + Prop Guard
                                                                 │ OrderRequest
                                                                 ▼
                            Executor → BrokerAdapter.order_send() → post-fill (SL vérifié)
                                                                 │
                       MT5Adapter (package MetaTrader5, Windows)  |  MockBroker (Linux/tests)
                                                                 ▼
                                Terminal MT5 (terminal64.exe) → Broker DEMO (MetaQuotes-Demo)

  Watchdog (processus séparé) ──lit MT5 + system_state.json──▶ state/watchdog.json ──▶ Orchestrator / CLI / Dashboard
```

### Carte des modules

| Chemin | Rôle |
|---|---|
| `config/system.yaml` | flags `autonomous_demo/prop`, `safe_mode_on_startup`, magic `51000`, cadences scheduler, seuils d'exécution, `account_expected` |
| `config/risk.yaml` | risque par trade, limites, corrélation, `profit_management` (ADAPTIVE_R_MANAGEMENT), `daily_profit` |
| `config/models.yaml` | tiers TIER_A/B/C/D, budget et quotas LLM |
| `config/markets.yaml`, `prop_firms.yaml`, `strategies.yaml`, `news_sources.yaml` | univers, profil prop (règles relevées, cf. `docs/prop/foxx_funded_regles.md`), champion/challenger + backtest, providers news |
| `src/tradinglab/core/types.py` | enums (`Side`, `TradeMode`, `Regime`, `AgentStatus`, `SystemMode`, `Provenance`, `Verdict`), dataclasses (`TradeCandidate`, `OrderRequest`, `GateResult`…) |
| `core/config.py` | `load_settings()` fusionne les 7 YAML ; secrets **uniquement** via l'environnement (`.env` chargé par `load_dotenv`) |
| `core/state.py` | `SystemState` + `StateStore` (JSON atomique), file `state/commands.jsonl`, heartbeat |
| `core/journal.py` | JSONL par jour + `logs/<composant>.log`, masquage `password/secret/api_key/token` |
| `core/clock.py` | timeframes, clôture de barre, sessions UTC, `forex_market_open()` |
| `mt5/adapter.py` | interface abstraite `BrokerAdapter` |
| `mt5/mt5_adapter.py` | adaptateur réel (import paresseux de `MetaTrader5`, credentials depuis l'environnement) |
| `mt5/mock_adapter.py` | `MockBroker` déterministe + fabrique `make_broker(kind, settings)` |
| `mt5/symbols.py` | suffixes broker (`EURUSD.m` → `EURUSD`), classes d'actifs, devises |
| `risk/risk_manager.py` | `compute_volume()` (arrondi **vers le bas**), `RiskLimits`, `check_limits()` |
| `risk/stop_loss.py` | `validate_stop_loss()`, `normalize_price()`, `is_tighter_or_equal()` |
| `risk/prop_guard.py` | `PropProfile`, `PropGuard` (autorisation DEMO/PROP, limites internes < hard limits) |
| `risk/daily_guard.py` | perte journalière, pertes consécutives, réduction de risque après profit, Giveback Guard |
| `risk/correlation_guard.py`, `portfolio/exposure.py` | facteurs devise, classes d'actifs, clusters corrélés |
| `execution/gate.py` | `ExecutionGate.evaluate()` : 20 contrôles → `GateResult` + `OrderRequest` |
| `execution/executor.py` | **seul** appel `order_send` du runtime, vérification post-fill, `BotPositionPlan` |
| `execution/position_manager.py` | TP partiels, break-even, trailing, `NEVER_WIDEN_STOP`, adoption après restart |
| `market_data/indicators.py`, `regime.py`, `feed.py` | indicateurs purs, régime déterministe, snapshots multi-TF avec cache par barre |
| `agents/registry.py` | 105 `AgentSpec` (familles A..M), statuts persistés dans `data/agent_status.json` |
| `agents/screeners.py` | 18 screeners Python (`@register`) → `TradeCandidate` |
| `agents/review.py` | revue Bull/Bear/Devil + Trade Arbiter (LLM optionnel, repli déterministe) |
| `models/router.py`, `models/client.py` | routage par rôle/tier, budget, quotas, cache, coût mesuré |
| `news/providers.py`, `news/hub.py` | FMP (calendrier + news), `NewsHub.check()` → `NewsCheck` |
| `macro/sentiment.py` | `risk_sentiment()` RISK_ON/OFF/NEUTRAL/UNKNOWN (règle fixe) |
| `orchestration/orchestrator.py` | boucle principale, commandes, reprise, `main()` (`--mode SAFE|AUTO --broker --cycles`) |
| `orchestration/market_router.py`, `scheduler.py` | entonnoir régime/session/marché, dédoublonnage ; cadences + nouvelles barres |
| `learning/store.py`, `post_trade.py`, `reports.py` | SQLite `data/learning.db`, revue post-trade, rapports |
| `research/pipeline.py`, `adapters.py` | pipeline champion/challenger, `DegradationManager`, pont screeners ↔ backtest |
| `shadow/shadow.py` | positions virtuelles des agents SHADOW/CANDIDATE (`state/shadow_positions.json`) |
| `backtest/engine.py` | backtest sans lookahead, walk-forward, Monte Carlo, sensibilité, stress |
| `monitoring/watchdog.py` | processus indépendant, `state/watchdog.json`, action directe sur SL manquant |
| `api/commands.py`, `api/cli.py` | commandes lecture/action, `python -m tradinglab.api.cli <CMD>` |
| `mcp/server.py` | outils MCP (lecture + commandes de sécurité + `propose_trade`) |
| `dashboards/server.py` | HTTP lecture seule `127.0.0.1:8765` |
| `scripts/*.ps1`, `scripts/smoke_test_demo.py` | audit/installation/démarrage/arrêt/autostart Windows, smoke test DEMO |
| `tests/` | 101 tests pytest (mock) : risque/gate, market data, news, backtest, dashboard |

## 4. Règles de sécurité — interdiction absolue de contourner l'Execution Gate

- Les **seuls** appels `order_send` du dépôt : `execution/executor.py` (runtime, après `gate.evaluate()`),
  `mt5/mt5_adapter.py` et `mt5/mock_adapter.py` (implémentations de l'interface), `scripts/smoke_test_demo.py`
  (test manuel, volume minimum, SL obligatoire, fermeture immédiate) et `tests/test_risk_and_gate.py`.
  Ne jamais en ajouter ailleurs ; ne jamais exposer `order_send`/`modify_position` via MCP, CLI ou dashboard.
- Toute ouverture passe par : `TradeCandidate` → `AdversarialReview.review()` (verdict) → `ExecutionGate.evaluate()`
  (20 contrôles, tous `ok`) → `Executor.execute()`. `MAX_ENTRIES_PER_CYCLE = 2`.
- Contrôle `01_health` : broker connecté **et** watchdog vivant (`state/watchdog.json`, heartbeat ≤ `heartbeat_max_age_sec`=45 s).
  Avec le broker `mt5`, **sans watchdog qui tourne, aucune entrée n'est possible** (avec `mock`, le watchdog est optionnel).
- Les commandes de fermeture (`CLOSE`, `CLOSE_ALL_BOT`, `PANIC`) sont **toujours** autorisées, même verrouillé ; jamais d'ouverture par commande.
- `never_widen_stop`, `never_remove_stop`, `allow_martingale/grid/averaging_down/remove_sl/risk_increase_after_entry: false` : ne pas changer.
- Credentials : jamais dans un YAML, un log, un commit. `.gitignore` exclut `.env`, `*.key`, `credentials*`, `state/`, `logs/`, `data/*.db`.

## 5. Model routing (`config/models.yaml`, `models/router.py`)

- Tiers : `TIER_A` (candidat `claude-fable-5-1` ; rôles chief_orchestrator, deep_market_review, macro_synthesis, strategy_research,
  champion_adjudication, post_trade_root_cause, refactor), `TIER_B` (`claude-opus-5` ; senior_strategy, bull_thesis, bear_thesis,
  devil_advocate, setup_validation, complex_news, second_opinion, adversarial_review, trade_arbiter), `TIER_C`
  (`claude-sonnet-5`, `claude-haiku-4-5-20251001` ; worker, screening, technical_analysis, news_summary, classification, bulk),
  `TIER_D` (`python` ; scan, indicators, correlations, sizing, statistics, rules, risk, backtest, ranking, monitoring, scheduling).
- Les identifiants sont des **préférences** vérifiées via `models.list` de l'API Anthropic (`verify_official_availability: true`,
  `refresh_interval_sec: 3600`). Modèle absent → suivant de la liste → tier inférieur → déterministe.
- Budget : `daily_budget_usd: 10.0` ; quotas horaires `max_fable_calls_per_hour: 6`, `max_opus_calls_per_hour: 30`,
  `max_worker_calls_per_hour: 200` ; cache des réponses `cache_ttl_sec: 240`. Compteurs dans `SystemState.model_budget`.
- Budget atteint → `TIER_D` sauf `financial_importance="high"` (arbitre). **Sans `ANTHROPIC_API_KEY` : mode déterministe complet**
  (`llm_skipped` journalisé, revue déterministe). Risk Guard / Watchdog ne dépendent jamais du routeur.
- Toute sortie LLM est marquée `MODEL_INTERPRETATION` ; le prompt commun impose « n'invente aucune donnée, UNKNOWN sinon, JSON uniquement ».

## 6. Agents (`agents/registry.py`, 105 agents)

- Familles : **A** scanners déterministes (11, rôles non générateurs) · **B** trend (10) · **C** breakout (9) · **D** pullback (7) ·
  **E** reversal/mean reversion (7) · **F** structure/price action (6) · **G** volatilité (5, dont un filtre) · **H** macro/cross-asset (7, rôles) ·
  **I** news-aware (5, rôles) · **J** adversarial/review (10, rôles) · **K** stratégies news-sensibles (2, statut `SHADOW`, famille `I`) ·
  **L** variantes par classe d'actif (12) · **M** spécialistes par symbole (14). Challengers générés : `CH101`, `CH102`…
- Statuts (`AgentStatus`) : `RESEARCH → BACKTEST → SHADOW → CANDIDATE → LIVE → DEGRADED → SUSPENDED / RETIRED`, persistés dans
  `data/agent_status.json` (`status` + `challengers`).
- Un agent générateur a une `strategy` = clé d'un screener : `ema_trend`, `mtf_trend_pullback`, `ema_pullback`, `session_breakout`,
  `daily_hl_breakout`, `compression_expansion`, `atr_expansion`, `breakout_retest`, `bollinger_mr`, `rsi_divergence`, `exhaustion`,
  `failed_breakout`, `liquidity_sweep`, `sr_rejection`, `structure_bos`, `choch`, `macd_momentum`, `fib_pullback`.
- Le Market Router n'active que les agents compatibles régime/session/marché (`active_for()`), ignore les symboles `data_quality != OK`
  ou en `NEWS_SHOCK`, dédoublonne par (symbole, sens) (+2 points par agent concordant, max +8 ; −10 si signaux opposés).
- Revue adversariale (`agents/review.py`) : déterministe d'abord (data_quality, news, RR ≥ 1.5, expectancy historique, seuil de score) ;
  si LLM disponible et pas de REJECT déterministe : `bull_thesis`, `bear_thesis`, `devil_advocate`, puis `trade_arbiter` (importance high).
  L'arbitre LLM ne peut pas outrepasser un rejet déterministe ni approuver sous `score − 10`. Le gate a toujours le dernier mot.

## 7. Boucle (`orchestration/orchestrator.py`)

`startup()` : `restarts += 1`, `SAFE_MODE`, 5 tentatives de connexion, vérification `account_expected` (sinon `ACCOUNT_MISMATCH`),
construction de l'univers (`resolve_symbols`), `pm.sync()` (adoption des positions), `_setup_models()`, `_setup_research()`.

`cycle()` : commandes → rapport watchdog → santé/compte (`roll_day_if_needed`) → sync positions + post-trade des fermetures →
news/calendrier (cadence) → snapshots + régimes + macro → gestion des positions (cadence) → `DailyGuard.evaluate()` →
scan/revue/gate/exécution (sur nouvelle barre ou `fast_scanner`) → shadow → recherche/dégradation/modèles/export (hors chemin critique)
→ `_maybe_go_auto()` (AUTO seulement si `--mode AUTO`, `autonomous_demo`, compte DEMO, pas de mismatch, ≥ 2 cycles sains, pas de
`safe_mode_request` du watchdog) → `store.save()`.

Cadences (`config/system.yaml` → `scheduler`) : watchdog 3 s · position_manager 15 s · fast_scanner 60 s · news 120 s ·
calendar 900 s · research 3600 s · models 3600 s · export 300 s · timeframes `M5 M15 H1 H4 D1` (le sommeil s'aligne sur la
prochaine clôture de barre, ≥ 1 s, ≤ 15 s).

## 8. Risk (`config/risk.yaml`)

- `risk_per_trade_percent: 0.25`, `max_risk_per_trade_percent: 0.35`, `max_daily_loss_internal_percent: 1.0`,
  `max_total_open_risk_percent: 1.0`, `max_open_positions: 3`, `max_positions_per_symbol: 1`, `max_consecutive_losses: 3`.
- Corrélation : cluster 0.5 %, facteur devise 0.6 %, classe d'actif 0.75 %, seuil 0.7, lookback 200 barres H1.
- Daily profit : paliers 0.75 % → risque 0.15 % (+5 score requis), 1.00 % → 0.10 % (+10), 1.50 % → 0.075 % (+15) ;
  `max_giveback_percent: 25` (verrou `GIVEBACK_FLOOR` maintenu jusqu'au lendemain).
- Exécution (`system.yaml`) : spread ≤ 40 pts (XAU 60, indices 100) et ≤ 0.15 ATR ; SL entre 0.25 et 4.0 ATR ; RR ≥ 1.5 ;
  `required_setup_score: 65` ; déviation 20 pts ; tick ≤ 30 s ; ≥ 250 barres.
- Sizing : volume = risque monétaire / perte par lot, arrondi **vers le bas** ; volume minimum broker refusé s'il dépasse 0.35 %.

## 9. Prop guard (`risk/prop_guard.py`, `config/prop_firms.yaml`, `core/trading_day.py`)

- Profil `FOXX_FUNDED / 1_STEP / 500000`. Règles **relevées le 2026-09-19** sur
  https://www.foxx-funded.com/fr/faqs et transcrites dans `docs/prop/foxx_funded_regles.md` : objectif 7 %,
  perte jour 4 %, perte totale 8 %, 2 % par idée de trade, cohérence 25 %, 5 jours de trading minimum.
- `CRITICAL_RULES = ea_allowed, news_trading_window_minutes, trading_day_definition, daily_loss_basis` ; toute valeur `UNKNOWN` rend le profil ambigu.
- `AUTONOMOUS_TRADING_PROP` n'est effectif que si `system.autonomous_prop` **et** `prop_rules_verified` **et**
  `user_explicitly_authorized_prop_automation` **et** `ea_approval_obtained` sont vrais **et** aucune règle critique
  `UNKNOWN` (`Settings.autonomous_prop`, `PropGuard.prop_automation_allowed`, `PropGuard.blocking_reasons`).
  Sinon tout compte `REAL/CONTEST` est refusé (`03_authorization`). L'approbation de l'EA par la prop firm est une
  **démarche humaine** : elle ne peut pas être accordée par le code.
- **Bases de calcul imposées par la prop firm** (ne jamais les remplacer par des approximations en equity) :
  - journée de trading = reset **17:00 America/New_York** (`core/trading_day.py`, `TradingDayCalendar`), pas minuit UTC ;
    `SystemState.roll_day_if_needed(equity, balance, now, calendar)` prend un **instant**, plus une date ;
  - plancher du jour = `max(solde, equity) au reset − 4 % du SOLDE INITIAL` (`DailyStats.reference_equity`,
    `SystemState.prop_daily_loss_percent/prop_daily_floor`) ;
  - perte totale = drawdown **statique** sur `SystemState.initial_balance` (figé à la première synchronisation),
    jamais sur un pic d'equity (`prop_overall_loss_percent/prop_overall_floor`) ;
  - **idée de trade** : positions du même sens sur le même symbole agrégées, réouverture sous 10 min comprise
    (`SystemState.register_trade_idea`, `active_trade_idea`) ; risque cumulé plafonné à 2 % du solde initial
    (contrôle `19_prop_trade_idea`) et jamais décrémenté par une perte déjà encaissée ;
  - **week-end** : cryptomonnaies uniquement (contrôle `19_prop_weekend`, borné par le reset 17:00 NY).
- Limites : internes en equity (1 % jour, 60 % du hard global = 4.8 %) puis hard limits prop en % du solde initial
  avec marge 25 % (3 % / 6 % / 1.5 % par idée), risque à ajouter inclus.
- Non bloquants mais suivis dans `compliance_report` : cohérence 25 % (`consistency_status`, contrôlée au paiement
  chez FOXX, jamais disqualifiante) et activité minimale (`activity_status` : 5 jours de trading, ≥ 1 trade/semaine).
- Fenêtre news interne (30 min avant / 15 après) **plus large** que l'exigence prop (5 min) : on ne l'assouplit jamais.
- Le watchdog signale « drawdown proche des hard limits » à 75 % des hard limits.
- Windows : `tzdata` est requis (`requirements.txt`) ; sans base de fuseaux, `TradingDayCalendar.degraded` passe à vrai
  et le repli est l'heure d'hiver (bascule une heure trop tôt en été).

## 10. Profit management (`ADAPTIVE_R_MANAGEMENT`, `execution/position_manager.py`)

1R = distance entrée → SL initial. TP1 à 1.5R (30 %), TP2 à 2.5R (40 %), runner 30 % ; break-even à 1.2R (+0.05R, structure requise) ;
trailing dès 2.0R à 1.5 × ATR avec structure de marché (swing) ; sortie anticipée sur invalidation (`allow_early_exit_on_invalidation`) ;
`regime_overrides` : `RANGING` (TP1 1.0R/50 %, TP2 1.8R/35 %, runner 15 %), `NEWS_SHOCK` (`block_new_entries`). Le SL n'est jamais élargi,
jamais retiré, jamais placé au-delà du prix courant/`stops_level`.

## 11. Learning / champion-challenger / dégradation / shadow

- `data/learning.db` (SQLite) : `trades` (mode `live|shadow|backtest`), `agent_events`, `market_snapshots`. `agent_stats()` : PF, expectancy R,
  drawdown R, Sharpe/Sortino indicatifs, découpages régime/session/symbole/volatilité, fenêtres 20/30/50, `degradation_score`.
- Post-trade (`learning/post_trade.py`) : verdict `VARIANCE_NORMALE | ERREUR_STRATEGIE | ERREUR_EXECUTION | INDETERMINE` ; jamais de
  modification après une seule perte ; `challenger_needed` seulement avec ≥ `min_sample_size` (40) trades.
- Pipeline (`research/pipeline.py`) : `IDEA → BACKTEST → OUT_OF_SAMPLE → WALK_FORWARD → MONTE_CARLO → SHADOW → STATISTICAL_REVIEW →
  RISK_REVIEW → PROMOTION`, persisté dans `data/research/<agent_id>.json` (historique/rollback). Seuils `config/strategies.yaml` :
  PF ≥ 1.2, expectancy ≥ 0.10R, DD ≤ 15R, échantillon ≥ 40 ; `live_self_mutation: false`, `shadow_required: true`.
- Dégradation (`DegradationManager`) : `LIVE → DEGRADED` (score ≥ 0.5, historique ≥ 60), `DEGRADED → SUSPENDED` (≥ 0.75,
  `auto_suspend_degraded`), retour `LIVE` (< 0.25). Challengers : jitter ±20 % des paramètres numériques, max 3 par cycle.
- Shadow (`shadow/shadow.py`) : agents `SHADOW/CANDIDATE` ouvrent des positions virtuelles (jamais d'ordre), SL/TP sur barres M5, timeout 72 h.

## 12. Redémarrage et reprise

- `safe_mode_on_startup: true` : toujours `SAFE_MODE` au démarrage ; passage en `AUTO` seulement si demandé (`--mode AUTO`) et après cycles sains.
- Idempotence : `TradeCandidate.idempotency_key = symbol|side|agent_id|bar_time` ; `state.mark_executed()` **avant** `order_send`
  → aucune ré-entrée après crash (`16_duplicate_idea`).
- Adoption : `PositionManager.sync()` adopte les positions MT5 portant le magic `51000` inconnues de l'état (`agent_id="ADOPTED"`) sans rien rouvrir.
- `state/system_state.json` corrompu → renommé `.corrupt.json`, état neuf. `state/watchdog.json` : rapport du watchdog (heartbeat,
  `safe_mode_request`, `reasons`, `actions`) ; l'orchestrateur bascule en `SAFE_MODE` si `safe_mode_request` est vrai en mode AUTO.
- Exception dans `cycle()` → journal `cycle exception` + `SAFE_MODE`, la boucle continue. `SIGTERM` → arrêt propre.
- Windows : `register_autostart.ps1` relance `start_all.ps1 -Mode SAFE` (codé en dur) à l'ouverture de session.

## 13. Commandes

- CLI : `python -m tradinglab.api.cli <COMMANDE> [arg] [--home DIR] [--json]` (ou `tradinglab <COMMANDE>` après `pip install -e .`).
- Lecture : `STATUS POSITIONS RISK TODAY PERFORMANCE AGENTS TOP_AGENTS DEGRADED_AGENTS NEWS CALENDAR WHY REPORT_DAY REPORT_WEEK RESEARCH_STATUS HELP`.
- Actions (file `state/commands.jsonl`, exécutées par l'orchestrateur) : `PAUSE RESUME SAFE_MODE PANIC CLOSE <ticket|symbole> CLOSE_ALL_BOT BREAK_EVEN <ticket>`.
  Si le heartbeat orchestrateur > 45 s, `PANIC/CLOSE/CLOSE_ALL_BOT` agissent **directement** sur le broker (fermeture/annulation uniquement).
- MCP (`.mcp.json`, `python -m tradinglab.mcp.server`) : `status positions risk account tick rates agents top_agents news calendar why
  report_day research_status command propose_trade`. `propose_trade` exige un SL, journalise dans `state/proposals.jsonl` et l'événement
  `trade_proposal` ; **aucun code ne consomme encore `proposals.jsonl` pour créer un candidat** (à ne pas présenter comme une exécution).

## 14. Fichiers d'état et journaux

| Fichier | Contenu |
|---|---|
| `state/system_state.json` | mode, verrous, compte, equity, `initial_balance`, `daily` (dont `reference_equity`), `bot_positions`, `executed_keys`, `trade_ideas`, `trading_days`, `model_budget`, heartbeats, `top_setups`, `regimes` |
| `state/watchdog.json` | rapport du watchdog (écrit uniquement par lui) |
| `state/commands.jsonl` | file de commandes (vidée par l'orchestrateur) |
| `state/proposals.jsonl` | propositions MCP `propose_trade` |
| `state/shadow_positions.json` | positions virtuelles shadow |
| `state/pids.json` | PID des processus (scripts Windows) |
| `logs/journal-YYYY-MM-DD.jsonl` | audit trail de tous les composants (`kind`: startup, account, universe, candidate, gate, order_send, position_opened, position_managed, post_trade_review, command, watchdog_alert, llm_call…) |
| `logs/orchestrator.log`, `logs/watchdog.log`, `logs/cli.log`, `logs/mcp.log`, `logs/*.out.log/.err.log` | logs texte par composant |
| `data/learning.db` | SQLite apprentissage |
| `data/agent_status.json` | statuts des agents + challengers |
| `data/agent_stats.json`, `reports/leaderboard.json` | export toutes les 300 s |
| `data/research/<agent_id>.json` | étapes de validation champion/challenger |
| `data/cache/news_cache.json` | cache news/calendrier |
| `reports/daily-*.json`, `reports/weekly-*.json`, `reports/audit-*.json` | rapports CLI et audit Windows |

## 15. Tests

`TRADINGLAB_BROKER=mock python -m pytest` (config dans `pyproject.toml` : `testpaths=["tests"]`, `pythonpath=["src"]`, `-q`).
807 tests : `test_risk_and_gate.py` (SL, sizing, verrous, prop, corrélation, 20 contrôles, exécution, ré-entrée, fermeture toujours
autorisée), `test_prop_foxx_rules.py` (règles FOXX relevées : journée 17:00 New York, planchers, drawdown statique, idée de trade,
week-end, cohérence, activité), `test_market_data.py`, `test_news.py`, `test_backtest.py` (déterminisme, anti-lookahead),
`test_strategies_distinct.py`, `test_dashboard.py`.
`conftest.py` fixe `FIXED_NOW = 2026-01-20 10:00 UTC` (session LONDON) et supprime `ANTHROPIC_API_KEY`/`FMP_API_KEY`.

## 16. Dépannage

| Symptôme | Où regarder | Action |
|---|---|---|
| Broker déconnecté (`mt5_connected: false`, `mode_reasons: broker déconnecté`) | `STATUS`, `logs/orchestrator.log`, `state/watchdog.json` | vérifier terminal MT5 ouvert et connecté, `MT5_TERMINAL_PATH`, *Algo Trading* activé ; l'orchestrateur retente à chaque cycle |
| Position sans SL | `watchdog_alert` dans le journal, `positions_without_sl` | le watchdog remet `last_sl/initial_sl` sinon ferme ; vérifier `stops_level` du symbole |
| Orchestrateur mort (`orchestrator_heartbeat_age_sec` > 45) | `STATUS`, `logs/orchestrator.err.log` | `PANIC`/`CLOSE_ALL_BOT` agissent directement ; relancer `start_all.ps1` (SAFE) |
| Budget LLM atteint (`llm_skipped`, `spent_usd ≥ 10`) | `STATUS.model_budget`, `report_day.model_cost_usd` | comportement normal : revue déterministe ; ajuster `daily_budget_usd` si voulu |
| Données périmées (`data_quality: STALE/NO_TICK/INSUFFICIENT`, `04_data_fresh` refusé) | `routing.skipped` dans `last_cycle` | marché fermé ou flux MT5 interrompu ; attendre / reconnecter ; `min_bars_required: 250` |
| Marché fermé (`05_symbol_tradable`, smoke test « MARCHE FERME ») | `core/clock.forex_market_open()` | attendre l'ouverture (dim. 22:00 → ven. 21:00 UTC) |
| `ACCOUNT_MISMATCH` | journal `account` | connecter le compte attendu (`account_expected`) ou corriger la config volontairement |
| `NEWS_DATA_DEGRADED` | `NEWS`, `CALENDAR` | sans `FMP_API_KEY` c'est attendu ; les stratégies `news_sensitive` sont bloquées, les autres passent |

## 17. Conventions de code

- Français dans les docstrings, messages, journaux et docs ; identifiants techniques en anglais.
- Déterministe d'abord : toute règle de sécurité en Python pur, testable sans réseau ni LLM ; LLM optionnel et dégradable.
- Jamais de credentials en dur ni dans les YAML ; lecture via `Settings.secret()` / `os.environ` ; journal scrubbé.
- Décisions sur la **dernière barre clôturée** (`last_closed`), jamais la barre en formation.
- Écritures d'état atomiques (`tempfile` + `os.replace`) ; le watchdog n'écrit jamais `system_state.json`.
- Ajouter un test dans `tests/` pour tout changement de risque/gate ; ne pas relâcher un seuil sans décision humaine documentée.

## 18. Ce qui reste à faire sur Windows (checklist)

- [ ] `scripts/audit_windows.ps1` → code 0 (Windows 64 bits, Python 3.11 x64, MT5, package `MetaTrader5`).
- [ ] `scripts/install_windows.ps1` (venv `.venv`, `pip install -r requirements.txt`, `pip install -e .`, `.env` créé).
- [ ] Remplir `.env` : `MT5_LOGIN=5056182608`, `MT5_SERVER=MetaQuotes-Demo`, `MT5_PASSWORD` (ou connexion manuelle dans le terminal, alors
      aucun mot de passe requis par le package Python), `MT5_TERMINAL_PATH`, `TRADINGLAB_HOME`, `TRADINGLAB_BROKER=mt5`, `ANTHROPIC_API_KEY`, `FMP_API_KEY` (optionnel).
- [ ] Ouvrir MT5, connecter le compte DEMO, activer *Algo Trading*.
- [ ] `python scripts/smoke_test_demo.py` puis `--yes` (marché ouvert) → « SMOKE TEST DEMO REUSSI », aucune position `TLAB:SMOKE` restante.
- [ ] `scripts/start_all.ps1` (SAFE) : vérifier `STATUS`, `state/watchdog.json`, dashboard `http://127.0.0.1:8765`.
- [ ] Observer plusieurs cycles en SAFE (candidats, refus du gate) avant `start_all.ps1 -Mode AUTO`.
- [ ] `scripts/register_autostart.ps1` (tâche `ClaudeMT5TradingLab`, toujours SAFE).
- [ ] Vérifier sur le terminal réel : suffixes de symboles résolus (`universe.missing`), `stops_level`, `filling_mode`, ticket de position
      retrouvé après `order_send` (`_find_position`), deals d'historique pour le post-trade.
