---
name: workflows
description: >
  Workflows standard du Claude MT5 Trading Lab (C:\Claude-MT5-Trading\claude-mt5-trading) :
  audit complet du dépôt, correction d'un défaut avec test et redéploiement, revue périodique
  (3 h / loop), redémarrage du bot, mise à jour du suivi (docs/SUIVI.md + mémoire). À invoquer
  avec `/workflows` (audit complet par défaut) ou `/workflows <audit|fix|revue|restart|suivi>`.
  Use when: 'workflows', 'audit complet', 'fait un audit', 'corrige les bugs', 'améliorations
  de partout', 'redémarre le bot', 'mets à jour le suivi'.
user-invocable: true
allowed-tools: ["Read", "Grep", "Glob", "Bash", "Write", "Edit", "Agent"]
argument-hint: "[audit|fix|revue|restart|suivi] [périmètre optionnel]"
---

# /workflows — procédures du Claude MT5 Trading Lab

Ces workflows encodent ce que l'utilisateur attend à chaque fois ; ne pas improviser une
autre séquence. Sans argument → **audit** (workflow 1) puis **fix** (workflow 2) sur tout ce
qui est trouvé, puis rapport + propositions.

## Contraintes permanentes (ne jamais contourner, quel que soit le workflow)

- **Jamais** passer en AUTO sans demande de l'utilisateur (un *redémarrage* en AUTO d'un bot
  déjà en AUTO est autorisé : c'est la continuité, pas un changement de mode).
- **Jamais** modifier `.env` : seulement indiquer ce qui manque. Ne jamais afficher une clé.
- **Demander avant** toute modification d'un seuil de risque (`config/risk.yaml`), du gate
  (`execution/gate.py`) ou des quotas/tiers LLM (`config/models.yaml`) — sauf accord déjà donné
  dans la conversation et consigné dans `docs/SUIVI.md` (« Décisions permanentes »).
- Compte **DEMO** IC Markets 53060800 jusqu'à nouvel ordre. Avant tout passage en réel :
  `risk_per_trade_percent` doit revenir à 0.125 (0.05 = mode test temporaire depuis 2026-09-23).
- Français dans les messages, docstrings, journaux. Identifiants techniques en anglais.
- Toute correction de risque/gate/exécution = **un test** dans `tests/` dont le docstring
  décrit le défaut (fichiers d'audit datés `tests/test_<sujet>_YYYY_MM_DD.py`).

## Outils de vérification (obligatoires)

```bash
# Suite complète (~10 min, ~1 100 tests) — TOUJOURS capturer le vrai code de sortie de pytest
cd C:/Claude-MT5-Trading/claude-mt5-trading
TRADINGLAB_BROKER=mock .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider > "$SCRATCH/pytest.txt" 2>&1; code=$?; echo "PYTEST=$code"; tail -5 "$SCRATCH/pytest.txt"
```
`| tail -2; echo $?` renvoie le code de `tail`, pas de pytest : des échecs ont déjà été
masqués ainsi. Un fichier ciblé se lance de la même façon avec son chemin.

- État du bot : `.venv/Scripts/python.exe -m tradinglab.api.cli STATUS --json` / `RISK --json` /
  `POSITIONS --json` ; `state/watchdog.json` ; `state/system_state.json`.
- Python non-ASCII dans un heredoc bash → `PYTHONIOENCODING=utf-8` (sinon `UnicodeEncodeError`
  cp1252). Scripts temporaires dans le scratchpad, jamais dans le dépôt.
- Compilation rapide : `.venv/Scripts/python.exe -m compileall -q src` ; pas de ruff/mypy installés
  (ne pas les installer sans demande : la venv est celle du bot en production).

## Workflow 1 — `audit` : audit complet

Ordre imposé, chaque étape produit une liste écrite de constats avant de passer à la suivante.

1. **Runtime (4 derniers jours)** : `logs/journal-*.jsonl` → compter les `kind`, isoler `warning`,
   `error`, `cycle exception`, `ERREUR_EXECUTION`, `llm_skipped` (raisons), `watchdog_alert`
   (raisons), refus du gate par contrôle (`gate.checks[].name` non ok) ; `logs/*.err.log` ;
   durée des cycles vs heartbeat 45 s (`cycle_duration` / `last_cycle`).
2. **Trading réel** : `data/learning.db` (`trades`, mode `live`) → par agent/famille/symbole :
   nombre, PF, expectancy R, MFE des perdants, verdicts post-trade ; positions ouvertes vs
   plan (BE armé, TP partiels) ; risque visé vs effectif (`risk_money` vs perte réelle).
3. **Code** : lire module par module `src/tradinglab/` (orchestration, execution, risk, agents,
   models, copy, monitoring, learning, dashboards, api, mcp, scripts). Chercher : `except` qui
   avalent sans journaliser, `TODO/FIXME/XXX`, code mort, dates en dur, secrets, `order_send`
   hors des 5 emplacements autorisés, écritures d'état non atomiques, threads sans verrou,
   `datetime.now()` sans tz, divisions par zéro possibles, `float(...)` sur `None`.
4. **Config** : cohérence `config/*.yaml` ↔ code (clés lues mais absentes, absentes mais lues avec
   défaut silencieux), plafonds de corrélation proportionnels à `risk_per_trade_percent`.
5. **Dashboard / API / MCP** : `dashboards/server.py` (bind 127.0.0.1 uniquement, aucune route
   d'écriture, pas de secret exposé, gestion des erreurs), CLI, MCP (aucun `order_send`).
6. **Copy trading** : `src/tradinglab/copy/` + `state/copy_map_*.json` + journal `copy_trade`
   (ratios, refus broker, symboles non résolus, doublons).
7. **Scripts Windows** : `scripts/*.ps1` (encodage UTF-8 des logs, chemins, `-Mode`).
8. **Tests** : suite complète avec le vrai code de sortie AVANT toute modification (état de
   référence), puis après.

Sortie : tableau des constats classés **BUG** (comportement faux) / **RISQUE** (peut devenir
faux) / **DETTE** (qualité) / **AMÉLIORATION** (nouvelle capacité), chacun avec fichier:ligne.

## Workflow 2 — `fix` : corriger un défaut

1. Reproduire par un test qui **échoue** (docstring : symptôme, chiffres réels, date).
2. Corriger au plus près de la cause ; jamais de contournement dans l'orchestrateur pour un
   défaut d'un module.
3. Fichier ciblé vert, puis **suite complète** verte (vrai code de sortie).
4. Redéployer : `scripts/stop_all.ps1` puis `scripts/start_all.ps1 -Mode AUTO` (si le bot était
   en AUTO) ; vérifier `STATUS` (mode, heartbeat, positions adoptées, aucun `ACCOUNT_MISMATCH`).
5. Mettre à jour `docs/SUIVI.md` (section FAIT, datée) et la mémoire si une règle durable naît.
6. Ne jamais toucher un seuil de risque/gate sans accord : le signaler dans « attend une décision ».

## Workflow 3 — `revue` : revue périodique (3 h / tick du loop)

Dans cet ordre : `STATUS --json`, `RISK --json` ; trades fermés depuis la dernière revue (MT5 +
journal) ; anomalies (même sous-jacent ×2, corrélations, pertes répétées au risque plein, verrous,
écart risque visé/effectif, BE non armé après TP, symboles STALE, cycles > 45 s, agents qui
perdent systématiquement) ; plafonds de concentration proportionnels au risque unitaire ; corriger
les défauts clairs (workflow 2) ; rendre compte en quelques lignes : bougé / corrigé / attend une
décision. Rien à signaler → une ligne. Ne pas re-planifier la revue 3 h (déjà active).

## Workflow 4 — `restart` : redémarrer le bot

`scripts/stop_all.ps1` → attendre la fin des processus (`state/pids.json`) → `scripts/start_all.ps1
-Mode AUTO` (ou SAFE si demandé) → 2 cycles → `STATUS --json` : `mode`, `mt5_connected`,
`orchestrator_heartbeat_age_sec` < 45, positions retrouvées, `copy_start` des suiveurs.

## Workflow 5 — `suivi` : tenir le suivi à jour

`docs/SUIVI.md` = source de vérité pour l'utilisateur (FAIT daté / À FAIRE lundi avec Fable /
Décisions permanentes). Mémoire (`~/.claude/projects/C--Claude-MT5-Trading/memory/`) = règles
durables uniquement (jamais ce que le dépôt enregistre déjà). Mettre à jour **au fur et à mesure**,
y compris depuis les ticks du loop.

## Rapport final (tous workflows)

En français, résultat d'abord : ce qui a été audité, ce qui a été corrigé (avec les tests), ce qui
a été redéployé, puis **propositions d'amélioration classées par priorité** (impact / effort /
risque), en séparant ce qui peut être fait seul de ce qui attend une décision de l'utilisateur.
