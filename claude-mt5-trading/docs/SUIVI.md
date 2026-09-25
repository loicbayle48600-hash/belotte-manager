# Suivi du Trading Lab — état au 2026-09-23

Note de reprise. **Si l'utilisateur dit « fait les corrections avec Fable »**, il parle de la liste
« À FAIRE (lundi, avec Fable) » plus bas. Jusqu'à dimanche, Fable reste utilisable pour finir les
corrections en cours.

Contexte du budget : l'abonnement Claude (Max 5×) alimente à la fois le bot et les conversations. Au
2026-09-23 à 01h00 : session 25 %, semaine 56 %, semaine Fable 67 % (reset le 27/09 à 12h59). Le bot
n'utilise PAS Fable (retiré de TIER_A le 2026-09-23) : le quota Fable est réservé aux échanges.

---

## FAIT (21 → 23 septembre)

### Couche LLM
- Backend **abonnement** (`TRADINGLAB_LLM_BACKEND=claude_code`) : plus aucun crédit API nécessaire.
  Bascule **automatique** vers le terminal si une clé API tombe en panne de crédit (jamais sur un
  simple timeout), journalisée.
- **Opus 5.5** en tête des tiers senior ; la contrainte « réflexion adaptive obligatoire » est
  **apprise automatiquement** par modèle (plus de liste à maintenir).
- Réflexion désactivée là où c'est permis, budgets de tokens calibrés (thèses 700, arbitre 600),
  quota horaire **réservé au routage** (plus de dépassement en parallèle), cache par barre M15.
- Prompt de revue nettoyé : l'IA juge le setup, plus les champs calculés après elle (risque,
  corrélation), et un historique court n'est plus lu comme un mauvais signe.

### Gate et gestion du risque (chaque changement approuvé explicitement)
- **`07b_spread_vs_sl`** (21ᵉ contrôle) : le spread ne peut plus dépasser 35 % de la distance
  entrée→stop. Motivé par la mesure : GBPTRY, spread médian 4 126 pts pour un stop de 38 pts.
- **Break-even à 1,0 R réellement effectif** (`break_even_requires_structure: false`) : 4 trades sur
  54 montaient au-delà de 1 R puis revenaient au stop plein (−1 895 $).
- `data_max_age_sec` 30 → 60 s (fin du battement nocturne du watchdog).
- Plafond par classe d'actifs 0,375 → 0,5 % (forex et indices saturaient à 3 positions).
- Giveback Guard en mode `off` (phase DEMO de collecte) — modes `reduce_risk` et `lock` prêts.

### Agents et données
- **Famille N** (saisonnalité) et **famille O** (cycle long H4/D1) créées, en SHADOW — 113 agents.
- **COT (CFTC)** : avertissement quand un trade suit un positionnement spéculatif déjà extrême.
- Fenêtres news **doublées autour des banques centrales** (60/30 min au lieu de 30/15).

### Interface et exploitation
- Dashboard distant `https://tradingdu48.ddns.net:8765` : HTTPS, page de connexion (admin /
  Loic48600), cookie 30 jours, DDNS mis à jour toutes les 10 min par tâche planifiée.
- Pages **📈 Statistiques** (jour / semaine / mois / année, par paire, par agent, onglet par compte)
  et **🎛️ Contrôle** (pause, reprise, safe mode, break-even, tout fermer, arrêt d'urgence,
  redémarrage, ajout de compte suiveur).
- **Copy trading** opérationnel : export du maître à chaque cycle, copieur par compte, taille
  proportionnelle à l'equity, partiels et stops répliqués, table d'appariement persistante
  (l'incident des 16 doublons sur Admirals ne peut plus se reproduire), terminaux dédiés créés
  automatiquement en mode portable et masqué, AutoTrading pré-activé.
- **Démarrage automatique Windows** : `scripts/autostart_lab.ps1` + raccourci dans le dossier
  Démarrage (`claude-mt5-trading.vbs`) — attend le réseau, lance le lab en AUTO, ouvre une fenêtre
  Claude Code qui reprend la conversation.
- Notifications Telegram vers le groupe, avec la paire dans chaque message.
- Export copy trading **résistant aux collisions de fichier Windows** (2026-09-23) : `os.replace`
  réessaie brièvement au lieu de sauter un cycle (PermissionError constaté à 03:09).
- **Quota d'abonnement atteint → appels LLM suspendus 30 min** (2026-09-23) au lieu de marteler le
  terminal : une seule alerte au journal, trading poursuivi sur les règles déterministes, reprise
  automatique. Distinct d'une panne ordinaire, qui garde le repli par appel.
- **Lots divisés par 2,5** (2026-09-23, TEMPORAIRE, mode test) : `risk_per_trade_percent` 0,125 → 0,05 %
  → jusqu'à 20 positions simultanées au lieu de 8, à budget de risque total inchangé (1 %). But :
  accumuler plus vite les 40 trades/agent nécessaires au jugement. **À remettre à 0,125 avant le réel.**
- **Break-even armé sur le PIC** (`plan.max_r`) et non sur le prix instantané : un pic survenu entre deux
  cycles ou avant un changement de réglage arme désormais le stop. Corrige CADCHF (−586 $) et NETH25
  (−521 $), tous deux montés au-delà de 1 R avant de revenir au stop plein.
- **Correspondance des symboles entre brokers** (`copy/symbol_map.py`) : US500↔SPX500↔[SP500],
  XNGUSD↔NGAS, XAUUSD↔GOLD, XTIUSD↔USOIL… plus le retrait des décorations (`EURSGD.m`, `#EURUSD`).
  Les positions du maître se répliquent sur l'instrument équivalent du suiveur ; un instrument vraiment
  absent n'est pas copié (signalé une fois), jamais remplacé par un approchant.
- Famille O : exception documentée dans `test_strategies_distinct` (elle rejoue délibérément les
  screeners génériques en H4/D1) + test qui vérifie que son repli reste résolvable et en SHADOW.

---

### Audit complet du 23 septembre (après-midi) — corrigé, testé (`tests/test_correctifs_2026_09_23.py`, `tests/test_copy_trading.py`)
- **Copy trading : plus de martèlement des brokers.** 1 858 ouvertures refusées en 4 jours (« Trade
  disabled », « No money », « AutoTrading disabled ») retentées toutes les 60 s, et 170 modifications
  SL/TP refusées toutes les 5 s sur un même ticket, sans jamais journaliser la réponse du broker.
  Désormais : temporisation 60 s → 120 → 240 … plafonnée à 15 min par ticket, `retcode` + commentaire
  du broker au journal, instruments « non négociables » ignorés (signalés une fois). Les fermetures
  ne sont jamais temporisées.
- **Watchdog : fini le AUTO ↔ SAFE sur coupure IPC.** Une coupure MT5 (« Authorization failed »,
  « IPC timeout ») de 1 à 3 s déclenchait SAFE_MODE (6 fois en 4 jours, 111 re-qualifications
  « cycles sains »). Une coupure n'est signalée qu'après 2 contrôles consécutifs (≈ 6 s) ; une coupure
  réelle l'est toujours.
- **LLM : 246 revues (≈ 1 000 appels) économisées** — un candidat sur un symbole déjà porté par le bot
  n'est plus soumis au LLM (le gate le refusait de toute façon : 1 position par symbole) ; `llm_skipped`
  l'explique. Le cache est désormais **mesuré** (`model_budget.cache_hits_day` dans STATUS).
- **Post-trade complet après redémarrage** : le candidat d'entrée (revue, session, snapshot) est persisté
  avec le plan de position ; 32 trades sur 69 n'avaient ni verdict ni contexte en base.
- **Heartbeat pendant le chargement initial** : re-persisté toutes les 15 s pendant la construction des
  snapshots (premier cycle de 159 s mesuré le 22/09, le watchdog déclarait l'orchestrateur mort).
- **Mesures faites (pas de changement de règle)** : fenêtre de rollover 20:50–21:15 UTC = 3 sorties SL,
  une seule anormale (EURSGD +0,61 R de surcoût) ; hors fenêtre le surcoût moyen est nul. Pas de règle
  d'interdiction justifiée à ce stade. 17 perdants sur 36 n'ont jamais dépassé +0,25 R (C : 5, D : 3,
  E : 3, G : 3). Shadow N/O : N12 +14 R (6/7), N06 +10 R (6/11), O03 −8 R (0/8), échantillons trop courts.
- **Règles FOXX Funded relevées le 23/09 (collées par l'utilisateur) et appliquées** — `docs/prop/foxx_funded_regles.md`,
  `tests/test_prop_foxx_lots_coherence_2026_09_23.py` : plafond de lots par classe (500 k : forex 10 / mat. premières 3 /
  indices 6 / crypto 3, par idée, volume raboté), **cohérence 25 % en direct** dès 1 % de profit net (risque réduit pour
  qu'aucune idée ne vise plus de 25 % du total ; idée ouverte fermée au plafond), hedging interdit, pas d'inversion dans
  les 30 min après une perte, aucune sortie anticipée avant 1 min. Levier / commissions / paiements consignés.
- **Commission prop dans le coût d'entrée** (accord utilisateur 23/09) : 7 $/lot forex et matières premières,
  3 $ crypto, 0 indice, convertie en distance de prix et ajoutée au spread dans `07b_spread_vs_sl`.
- **24/09 nuit — relevé réel par compte + 2 défauts graves du copieur** (`tests/test_releve_reel_2026_09_24.py`,
  `tests/test_copy_trading.py`) : (a) statistiques de CHAQUE compte tirées de l'historique MT5 (bénéfice, dépôt,
  swap, commission, solde — `state/statement_master.json` et `statement` des copieurs), agents associés par ticket ;
  (b) **taille de contrat** : 1 lot d'argent = 1 000 oz chez IC, 5 000 chez Admirals → copies à 5× le risque
  (−2 781 $ sur demo 1 pour −6 $ au maître) ; le volume copié suit désormais la valeur d'un mouvement de prix
  (NGAS : copies 10× trop petites avant) ; (c) stops du maître trop proches pour Admirals la nuit (46 pts) →
  stop porté à la distance minimale (≤ ×2), TP trop proche retiré, jamais d'élargissement en modification.
- **24/09 soir — 7 améliorations (« fais tous les points »)**, `tests/test_ameliorations_2026_09_24.py` :
  (1) filtre d'extension MESURÉ (distance entrée/EMA20 en ATR sur chaque candidat, `entry_extension_filter`,
  aucun refus réel) ; (2) rapport hebdomadaire Telegram le lundi 08:00 ; (3) comptes suiveurs vérifiés (demo 4 à
  facteur 4 manque de marge : 2 conseillé) ; (4) commission d'entrée comptée dans le P&L ; (5) suspension rapide
  d'un agent LIVE à ≥ 10 trades et PF < 0,5 ; (6) cohérence 25 % appliquée dès 0,3 % de profit ; (7) règle des paires
  exotiques au rollover **annulée le soir même sur demande de l'utilisateur** (code gardé, inactif).
- **24/09 soir — nouveau compte maître : IC Markets démo 53068680** (Hedge, 500 000 USD, levier 30), connecté à la
  main dans MT5 et utilisé par le bot via la session enregistrée (`MT5_PASSWORD` vide dans .env). Ancien 53060800
  archivé « compte test 1 » (`archives/`), état du compte / cycle FOXX / stats du maître remis à zéro
  (`master_account_since`), agents conservés. Le compte 53060775 (Netting, refusé à la connexion) n'est pas utilisé.
  **Algo Trading** : `Enabled=1`, `Account=0`, `Profile=0` forcés avant chaque lancement de terminal (start_all +
  suiveurs) — MT5 coupait Algo Trading à chaque changement de compte (67 copies refusées par compte le 24/09).
  **5 nouveaux agents d'annonces** K03-K07 en SHADOW (`agents/news_strategies.py`).
- **24/09 après-midi** : comptes « demo 2 500k » et « Demo 3 » supprimés (config, fichiers d'état, lignes .env sur
  demande explicite) — **leurs positions copiées restent ouvertes chez Admirals** (connexion API refusée « Authorization
  failed », à fermer à la main dans MT5 : `scripts\hide_mt5_windows.ps1 -Show` pour revoir les fenêtres). Bouton
  « 🗑️ Supprimer » avec confirmation dans le panneau (`/api/copy/remove`, `remove_follower`). Fenêtres MT5 masquées
  en continu (`scripts/hide_mt5_windows.ps1 -Loop`, lancé par start_all). Accueil : un résumé par compte suiveur.
- **Copy trading corrigé (24/09, captures utilisateur)** : (1) US30 était traduit en « #DOW » chez Admirals = l'action
  Dow Inc. ; les actions « #… » ne servent plus jamais de cible par alias, US30 → [DJI30], USTEC → [NQ100],
  JP225 → [NIKKEI225] ; (2) une copie stoppée chez le suiveur était rouverte (9 fois le 24/09, dont AUDJPY 12 min
  après son stop) : un ticket maître déjà copié n'est plus jamais rouvert (`state/copy_done_<prefix>.json`) ;
  (3) « No changes » (10025) = succès, prix du maître arrondis à la précision du suiveur.
- **Dashboard refait (24/09, deux agents d'audit + deux agents d'implémentation, `tests/test_dashboard.py`)** :
  accueil avec bandeau d'état (bot, jour, positions, cycle FOXX), jauges des planchers prop, cohérence unique
  (profit net du cycle), journal lisible en heure de Paris, positions en cartes ; Statistiques avec courbe
  d'equity et planchers, cycle de paiement raconté (conditions ✅/❌), cohérence et stats par compte, échecs de
  copie ; Contrôle avec contexte et bouton « Paiement effectué ». Serveur : journal plafonné (9 s → 0,17 s),
  payload 187 → 62 Ko compressé, cache des stats, en-têtes de sécurité, 5 essais de connexion par IP, /logout,
  état lu en lecture seule. **Vérifié à 390 px (émulation mobile Chrome) : aucune page ne déborde.**
- **Cycle de paiement automatique FOXX** (décision utilisateur 23/09 : « je retire tous mes bénéfices tous
  les 7 jours, les comptes démo font comme un compte financé », `tests/test_cycle_paiement_2026_09_23.py`) :
  14 jours de trading pour le 1er paiement puis 7 ; éligible = profit > 0, cohérence du cycle ≤ 25 %, pas de
  violation → entrées verrouillées, positions fermées, retrait de tout le profit (max 15 % du solde initial),
  simulé sur DEMO, `payout_ready` + `PAYOUT_DONE <montant>` sur compte financé. La cohérence 25 % en direct
  et le graphique du maître ne regardent que le cycle courant. Cartes « Cycle de paiement » dans Statistiques.
- **Statistiques et cohérence 25 % par compte dans le dashboard** (demande utilisateur 23/09,
  `tests/test_coherence_par_compte_2026_09_23.py`) : chaque copieur exporte les trades fermés de son compte
  (90 jours) ; l'onglet de chaque compte a ses propres cartes (P&L fermé, trades, win rate), son graphique
  jour/semaine/mois/année, son tableau par paire et un **graphique de cohérence** (part de chaque idée de
  trade dans le profit, limite 25 % tracée). Le maître a le même graphique, calculé depuis `learning.db`.
- **Leaderboard lisible** (signalé par l'utilisateur) : colonnes Statut / Trades (gagnants / perdants) /
  Total R renseignées, « aucune perte » à la place d'un profit factor 99, échantillons ≥ 5 trades
  classés d'abord (un coup gagnant unique ne domine plus 14 trades).

- **Copie des stops quand le suiveur cote un autre contrat** (25/09, signalé par l'utilisateur : BRENT
  « stop du maître trop proche pour ce broker : copie reportée » ×8, `tests/test_copy_trading.py`) : le Brent
  valait 104,90 chez IC et 98,21 chez Admirals, le stop 104,34 recopié tel quel tombait au-dessus du prix d'un
  achat. Au-delà de 0,2 % d'écart entre le prix du maître et celui du suiveur, SL et TP sont recopiés à la même
  distance du prix (décalés de l'écart), pour l'ouverture comme pour les modifications, sur tous les comptes.
  Le décalage est mémorisé par copie (table maître → suiveur) : sans cela, une modification SL/TP repartait
  toutes les 5 s en dérivant avec le prix (constaté juste après le redéploiement).
- **Verdict ERREUR_EXECUTION mal attribué** (25/09, US500 L03 −614,9 $, −0,98 R) : l'ordre était correctement
  exécuté (0,3 point de glissement), stop touché 4 min plus tard. Le seuil fixe de 40 points de spread classait
  l'US500 (50 points = 0,5 point d'indice, 8 % du risque) en erreur d'exécution. Le spread est maintenant jugé
  en part du stop, comme le contrôle 07b du gate (max 35 %) ; `tests/test_verdict_erreur_execution.py`.

- **Retour aux stratégies du 19/09** (25/09, décision utilisateur « reviens au 19 septembre, ça marchait mieux »,
  option « stratégies du 19 ») : sauvegarde complète dans `archives/avant-retour-19-sept_2026-09-25_1713`. Les
  filtres d'entrée ajoutés après le 19 (06b RR au prix courant, 07b spread + commission / stop, 11b risque utile)
  ne refusent plus rien (`execution.gate_checks_disabled`, toujours journalisés « aurait refusé ») ; seules les
  invalidations EMA50 d'origine ferment une position avant son stop (`execution.invalidation_levels: false`).
  Conservés : risque 628 $, règles FOXX du 25/09, compte maître IC, copie, dashboard, abonnement, gestion des
  positions (break-even, TP replacé). Les stratégies qui tradent n'avaient pas changé depuis le 19 (seuls des
  agents SHADOW ont été ajoutés). Résultats réels live : 20-21/09 -4,3 R ; 22-23/09 +21,1 R ; 24-25/09 -19,1 R.
  Le même jour, l'utilisateur a réactivé le filtre 07b spread + commission / stop (actif pendant les bons jours du 22-23).

- **Stop suiveur dès +1,05 R, toujours en bénéfice** (25/09, demande utilisateur, `tests/test_stop_suiveur_2026_09_25.py`) :
  le suivi du stop (1,5 ATR derrière le prix) démarre à +1,05 R au lieu de +2 R et ne descend jamais sous le
  break-even. Le break-even couvre maintenant la commission FOXX (entrée + 0,05 R + commission ramenée en prix) :
  avec un stop serré, 7 $/lot dépassaient les 0,05 R. Les suiveurs recopient chaque déplacement du stop du maître.

- **Agents mis en avant** (25/09, demande utilisateur) : E05, F06, B02, C06, B04, E01, D06, D07, C08 passent avant
  les autres candidats (revue IA et entrée ; `learning.agents_prioritaires`). Gate et risque inchangés.
- **100 agents SHADOW** (25/09, demande utilisateur « pour avoir de nouveaux agents au top ») : CH101 à CH200,
  variantes des 9 agents mis en avant (11 chacun, 12 pour E05), paramètres ±20 %. Ils mesurent en positions
  virtuelles et passent par le pipeline (backtest, hors échantillon, walk-forward, Monte Carlo, 20 trades shadow)
  avant toute promotion LIVE. Statuts sauvegardés avant création dans `archives/agent_status_avant-100-shadow_*`.
- **Pipeline de recherche débloqué** (25/09) : il ne faisait avancer que les 2 premiers agents du registre (K01/K02,
  en échec permanent au backtest). Désormais 20 par passage horaire, les plus anciennement essayés d'abord, un
  échec retenté après 24 h (`research_agents_per_cycle`, `research_retry_hours`).
- **Plafond shadow** (25/09) : 50 positions virtuelles, dont 48 tenues par les agents de cycle long ; relevé à 400,
  avec 4 par agent au plus (`shadow_max_open`, `shadow_max_open_per_agent`). Virtuel : aucun risque réel.

- **Plan « bot professionnel » (25/09, 8 points validés par l'utilisateur)** :
  1. Versions : code enregistré dans Git et étiqueté `lab-vN` ; la version (`core/version.py`), le coût d'entrée et le
     glissement sont notés sur chaque trade. Données d'exécution (`data/agent_status.json`, `data/research/`…) hors Git,
     sauvegardées par `start_all` (archivesuto).
  2. Gel des réglages : `system.gel_reglages` (version lab-v1, 100 trades). Suivi dans la page Qualité et le rapport de 17 h.
  3. Validation des agents LIVE : `python -m tradinglab.research.validate_live` (processus séparé, lecture seule,
     `data/validation_live/`, rapport `reports/validation_live.json`). Le backtest teste désormais 3 symboles par agent
     (1 seul donnait 2 à 6 trades). La décision de garder un noyau de 20-30 agents revient à l'utilisateur.
  4. Filtre 07b spread + commission / stop : 35 % → 20 % du risque. Rejeu de la semaine : +1,14 R et drawdown plus faible
     qu'à 35 %, coût d'entrée déduit.
  5. Page Qualité du dashboard (`/qualite`) : espérance par agent avec marge à 95 %, verdict (avantage prouvé / perdant
     prouvé / pas encore concluant, jamais avant 10 trades), coût d'entrée, glissement, MFE / MAE, gel.
  6. Rejeu : `python -m tradinglab.research.replay --jours 7 --reglages proposition.yaml` avant tout changement.
  7. Journal : alertes du watchdog dédupliquées (1 491 → 1 par nature, rappel toutes les 15 min), candidats et
     décisions du gate écrits une fois par nature.
  8. Rapport Telegram à la clôture de la journée FOXX (17 h New York) : P&L maître et suiveurs, coûts, meilleurs /
     pires agents, alertes, avancement du gel.
- **Cycles trop longs (25/09, 20 h 30 et 20 h 41)** : un appel `claude -p` bloqué 30 s puis l'arbitre portaient le cycle
  à 48-52 s (tolérance du watchdog 45 s). La revue IA est bornée à 25 s ; hors délai → revue déterministe, journalisée.
  Recherche : 5 agents × 3 symboles toutes les 30 min (charge bornée dans le processus de trading).
- **Appels IA sur l'abonnement** (vérifié le 25/09) : `TRADINGLAB_LLM_BACKEND=claude_code` dans `.env`, clé API retirée
  de l'environnement du terminal Claude à chaque appel. La clé API encore présente dans `.env` n'est pas utilisée.

## À FAIRE (lundi, avec Fable)

- **Plus tard, à la demande de l'utilisateur (25/09 : « laisse pour le moment, pourquoi pas plus tard ») :** retirer
  le TP broker pour les agents qui visent 2 R ou moins, afin que TP2 (2,5 R) et le stop suiveur prennent le relais.
  Constat : le TP broker ferme les 70 % restants à 2 R avant TP2 (sur 165 trades live : TP1 pris 55 fois, TP2 16).

1. **Spread au rollover (21:00 UTC)** — cause NON corrigée de la perte EURSGD (−1,61 R au lieu de
   −1 R, −1 211 $). Le spread était normal à l'entrée : il a explosé à la sortie. Piste à mesurer
   avant de décider : compter les clôtures dans la fenêtre du rollover et leur surcoût, puis
   éventuellement interdire de porter les paires à spread large à travers cette fenêtre.
2. **Qualité des entrées** — 15 perdants sur 30 n'ont jamais dépassé +0,25 R (−9 708 $). Ce n'est
   pas un problème de stop (les gagnants ne descendent qu'à 0,40 R en moyenne) mais d'entrée.
   À traiter par la sélection statistique quand les agents auront 40 trades, ou par une revue des
   familles les plus fautives.
3. **Promotion des familles SHADOW** (N saisonnalité, O cycle long) — vérifier leurs stats dès
   qu'elles ont assez de trades ; c'est le test de l'hypothèse « moins de trades, plus longs ».
4. ~~Blue Guardian~~ **fonctionne depuis le 23/09** : 44 ordres « Request executed » dans la journée. Les
   refus restants sont « No money » (marge insuffisante sur ce compte, 62 fois entre 12 h et 14 h),
   « Invalid stops » et des ticks indisponibles : rien à changer côté identifiants.
   (ancien constat : compte 527246). **Facteur de taille passé de 1.0 à 0.5 le 23/09** (accord utilisateur) :
   les 62 « No money » concernaient SPX500 à 1,08 lot sur 5 k$ d'equity. — le broker refuse les ordres (`Trade disabled`). À vérifier
   côté utilisateur : mot de passe *trader* (pas *investisseur*) et challenge bien activé.
5. ~~D03~~ **suspendu le 2026-09-23 sur décision utilisateur** (7 trades, PF 0,11, −2 453 $) :
   `data/agent_status.json` + événement `agent_events`. Réactivation = décision utilisateur.
6. **Quotas LLM** — portés à **Opus 60/h, worker 150/h, budget volume 150 $/jour** le 2026-09-23
   (« monte le quota au besoin »). Mesuré avant : la demande réelle atteignait ~500 appels/h en
   session active, dont ~400 refusés faute de quota ; on sert désormais ~210 appels/h (~2,1x le
   poids hebdomadaire précédent). Fable reste hors du bot. **À surveiller sur `/usage`** : si le
   quota hebdomadaire se tend avant son reset, redescendre plutôt que de laisser couper.

---

## Décisions permanentes de l'utilisateur

- 2026-09-25 14:55 : **comptes démo remis à 500 000 $ par l'utilisateur** → état et statistiques de TOUS les comptes remis à zéro (`master_account_since` / `accounts_stats_since`, ancien état dans `archives/remise-a-zero_2026-09-25_1455`). Paliers de risque après profit plafonnés au risque de base (ne peuvent plus l'augmenter). Suite de tests à jour.

- 2026-09-25 : retour aux réglages du 23/09 puis choix ligne à ligne : risque 0,125 % (628 $), risque ouvert et perte jour 1 %, plafonds de lots FOXX désactivés, cohérence directe dès 0,3 %, hedging interdit, commission FOXX dans 07b, retraits auto 7 jours, suspension rapide, agents d'annonces en SHADOW, pause après perte désactivée, **paires exotiques retirées de l'univers** (32 paires). Sauvegardes : `archives/etat-complet-2026-09-25` + `archives/auto/` à chaque démarrage.

- 2026-09-24 : suspension automatique d'un agent à ≥ 10 trades et PF < 0,5 ; cohérence appliquée dès 0,3 % de profit  (règle rollover exotique annulée le même soir).

- 2026-09-24 : **nouveau compte maître à venir = compte DÉMO traité comme un compte financé FOXX** (retraits simulés, toutes les règles FOXX appliquées). Au changement : garder tout ce qui concerne les agents (learning.db, agent_status, recherche, shadow), réinitialiser l'état du compte et les stats du maître, archiver l'actuel IC Markets 53060800 sous le nom « compte test 1 ».

- 2026-09-24 : **risque par trade remis à 0,125 %** (fin du mode test à 0,05 %), règles FOXX inchangées.
- 2026-09-24 : **option « Modéré » choisie** : 0,25 % par trade, risque ouvert total 2 %, perte jour interne 2 %,
  plafonds cluster/devise/classe 0,5 / 0,6 / 1,0 %, paliers après profit 0,20 / 0,15 / 0,10 %. Règles FOXX inchangées.

- 2026-09-23 : **cohérence 25 % FOXX appliquée en direct** (seuil d'application 1 % de profit net du cycle) ; **D03
  suspendu** ; Blue Guardian `size_factor` 0.5.
- 2026-09-23 : **retrait de tous les bénéfices tous les 7 jours de trading** (14 pour le premier), comptes DEMO
  traités comme financés (retrait simulé automatique) ; règles FOXX de paiement appliquées par le bot.

- Jamais d'AUTO, de modification d'un seuil de risque ou du gate sans accord explicite.
- `.env` modifié seulement sur demande.
- On reste en **DEMO** : la bascule vers le réel n'aura lieu que sur demande explicite.
- Pas de clé API Anthropic tierce « illimitée » (revente de clés détournées).
