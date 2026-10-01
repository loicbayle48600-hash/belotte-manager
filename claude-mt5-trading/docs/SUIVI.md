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

- **Cycles de 87 et 168 s (25/09, 19 h 36-19 h 47 UTC)** : la suite de tests (12-15 min de calcul) tournait sur le même
  PC et ralentissait la boucle. L'orchestrateur et le watchdog démarrent désormais en priorité haute (`start_all`),
  appliquée aussi aux processus en cours. Les suites de tests sont lancées en priorité basse.

- **Validation des 68 agents LIVE qui tradent (25/09, plan pro point 3)** : aucun ne passe les 4 étapes, ni sur 3 000 barres
  (~1 mois M15) ni sur 10 000 (~3,5 mois, `reports/validation_live_10000.json`). Sur 10 000 barres : 6 passent le
  backtest (C09, E02, L03, B10, L07, C01) puis échouent hors échantillon ; sur 41 agents avec ≥ 20 trades, 7 ont un
  PF > 1, PF médian 0,7. Les 9 agents mis en avant ont tous un PF < 1 (E05 : 0,61 sur 109 trades). Limites : le backtest
  sort au SL/TP fixe (sans break-even, partiels ni stop suiveur), sans revue IA ni gate ; coûts par défaut. Aucun noyau
  « validé » ne peut être choisi sur cette base : décision laissée à l'utilisateur (gel en cours).

- **Aucun trade crypto le samedi 26/09 (signalé par l'utilisateur)** : SAFE_MODE de vendredi 22 h 58 à samedi 10 h 37
  (heure de Paris). Le watchdog ne surveillait qu'EURUSD : l'ancrage des cryptos passait avant la connexion au
  terminal (liste de symboles vide) et n'était jamais retenté. Forex gelé → « aucun marché suivi ne cote » → SAFE_MODE.
  Corrigé (`tests/test_watchdog_horloge.py`), bot revenu en AUTO, candidats crypto de nouveau analysés. Reste : au
  redémarrage un week-end, les cryptos fraîchement sélectionnées n'ont pas encore de tick (~9 min de SAFE_MODE).

- **Samedi 26/09, journal de l'après-midi** : score minimal à 55 pour la journée (demande utilisateur). Un trade :
  BTCUSD BUY (C01, cassure du range asiatique) à 13 h 50, stop à 0,16 % (84 170,75 → 84 032,95), stoppé à 14 h 41,
  −617 $ (−0,995 R). Non copié chez les suiveurs : stop trop serré pour leur broker (élargissement plafonné à ×2) ;
  retenté chaque minute pendant 50 min → délai croissant appliqué (`test_stop_trop_proche_retente_avec_delai_croissant`,
  effectif au prochain redémarrage). SAFE_MODE 14 h 59-15 h 06 (aucune crypto n'a coté pendant plus de 60 s) puis
  15 h 09-15 h 12 (MT5 « Authorization failed », connexion du terminal perdue puis revenue) : watchdog dans son rôle.
  La revue IA refuse depuis surtout des stops « dans le bruit » (0,3-0,5 ATR) et POLUSD (spread 3,2 ATR > stop).

- **Revue IA hors délai = entrée validée sans l'IA (26/09, corrigé)** : le BTCUSD perdant de 13 h 50 avait été mis en
  attente par l'IA à 13 h 45 (stop fragile) puis APPROUVÉ par le repli déterministe (score 63 ≥ 55) quand la revue a
  dépassé 25 s. Désormais une revue IA sans verdict (hors délai, erreur, arbitre muet) ne valide jamais d'entrée.
- **Stops serrés — rejeu des 7 derniers jours (26/09)** : réglage actuel (stop ≥ 0,25 ATR H1) : 98 trades, −34,9 R.
  Refuser les stops < 0,75 ATR : 54 trades, −21,6 R ; < 1,0 ATR : 20 trades, −3,3 R (DD 6 R au lieu de 35).
  ÉLARGIR les stops serrés au lieu de les refuser est pire (−42,6 à −50,7 R) : l'objectif reste au même endroit, le
  gain en R fond. En attente de la décision de l'utilisateur (seuil du gate, gel lab-v1).

- **Trader 7 j/7 : famille P crypto (26/09, décision utilisateur, LIVE directement sans SHADOW)** : 15 agents crypto.
  P01-P11 : screeners éprouvés en entrée H1 / tendance H4, stop ≥ 1,5 ATR H1. P12-P15 : stratégies issues de la
  recherche (`agents/crypto_strategies.py`) — Donchian 20 et 55 barres H4 (momentum de série temporelle), RSI(2) de
  Connors sur BTC/ETH, fenêtre de saisonnalité du week-end (samedi et dimanche 15-17 h UTC, dimanche 23 h UTC).
- **Revue IA (26/09, décision utilisateur)** : note « crypto 24 h/24 » ajoutée au dossier de l'IA le week-end (elle
  jugeait « incohérente » une session Londres/New York un samedi) ; au-delà des 3 créneaux IA, un APPROVE sur le seul
  score devient WAIT (jamais d'entrée sans l'avis de l'IA).

- **Backtest informatif de la famille P (26/09, ~4 mois H1/H4, 3 cryptos par agent)** : P01-P11 (screeners
  génériques) : seul P05 (retour à la moyenne en range) passe 2 étapes (PF 1,27) ; les autres ont un PF de 0,55 à 1,06.
  Stratégies de la recherche : P13 Donchian 55 H4 — 2 étapes, 95 trades, PF 1,24, +0,17 R/trade (échoue au
  walk-forward) ; P15 fenêtre du week-end — PF 1,56, +0,35 R sur 26 trades (échoue hors échantillon) ; P12 Donchian 20
  — PF 1,20 sur 153 trades ; P14 RSI(2) BTC/ETH — PF 0,75 (perdant). Tous restent LIVE (décision utilisateur).
  Premiers candidats P en attente : l'IA hésite sur des agents sans historique (sample_size = 0) et sur les spreads.

- **Trading des annonces (26/09, décision utilisateur, option news FOXX)** : seuls les agents qui tradent les annonces
  (`news_trader`) passent la fenêtre de blocage et le choc de news : K03-K07 (suivi, retournement, cassure de range,
  reprise de tendance, dérive après banque centrale) passent LIVE, et K08-K12, leurs versions crypto (stops plus
  larges), sont créés en LIVE. Tous les autres agents gardent la fenêtre 30/15 min (60/30 banques centrales).
  Calendrier indisponible → toujours bloqué. `prop_firms.yaml` : `news_trading_allowed_on_funded: true`.

- **P14 (RSI(2) BTC/ETH) en SHADOW** (26/09, décision utilisateur) : PF 0,75 en backtest.

- **Commission crypto (26/09, corrigé)** : la commission FOXX crypto (3 $/lot) appliquée PAR LOT valait 150 % (SOL) à
  16 700 % (XRP) du risque, car un lot crypto va d'un BTC à un seul XRP. Le contrôle 07b bloquait toute crypto hors BTC
  et le break-even crypto était impossible à placer. Elle est comptée en % de la valeur du trade (0,004 %, soit ~3 $
  sur un lot BTC) : `crypto_commission_percent_of_notional`, `tests/test_commission_crypto_2026_09_26.py`.
  Restent bloquées par leur SPREAD seul (> 20 % du risque sur des stops H1) : ADA, DOT, POL, KSM, souvent LNK et AVX.

- **Crypto multi-unités de temps (26/09, demande utilisateur)** : P16-P31, 4 approches (Donchian, retour à la moyenne
  en range, repli dans la tendance, expansion de volatilité) en M5, M15, H4 et D1, en LIVE. M5/M15 limités à BTC, ETH,
  XRP, SOL (spread faible) ; D1 avec stop 0,75 ATR D1 (maximum du gate : 4 ATR H1) ; Donchian H4 à 30 barres (P12 = 20).

- **Backtest P16-P31 (26/09, 3 000 barres : ~10 jours en M5, ~1 mois en M15, ~1,4 an en H4, ~8 ans en D1)** : meilleurs
  P28 Donchian D1 (240 trades, PF 1,49, +0,31 R, 2 étapes), P31 expansion D1 (PF 1,70, 2 étapes), P27 expansion H4
  (PF 1,40, 2 étapes) ; M5 prometteurs mais sur ~10 jours seulement (P18 PF 2,59, P19 PF 1,71). Perdants (PF < 0,8) :
  P17, P20, P22, P23, P26, P30 — mise en SHADOW proposée à l'utilisateur.

- **P17, P20, P22, P23, P26, P30 en SHADOW** (27/09, décision utilisateur) : PF < 0,8 en backtest.

- **L'IA confondait points et prix (27/09, corrigé)** : elle comparait `spread_points` (500 points = 5 $ sur le BTC) à
  la distance du stop en prix (91,8 $) et jugeait « spread supérieur au stop » : 76 candidats BTC dans la nuit, aucun
  approuvé. Le dossier de l'IA donne désormais le coût en prix et en % du stop (`cout_entree`), sans `spread_points`.

- **Positions hors crypto le week-end (27/09, corrigé)** : `weekend_holding_allowed: CRYPTO_ONLY` (FOXX) n'était
  appliqué nulle part ; GBPAUD, NZDJPY et NETH25 sont restées ouvertes pendant la fermeture (risque d'écart à la
  réouverture, violation FOXX sur compte financé). Désormais : aucune entrée hors crypto dans l'heure qui précède le
  week-end (`19_prop_weekend_holding`), fermeture des positions hors crypto 15 min avant le reset du vendredi
  17:00 New York (`weekend_close`). Les 3 positions actuelles seront gérées à la réouverture (SL en place).

- **Tenue des positions le week-end : désactivée (27/09, décision utilisateur)** : le relevé FOXX interdit de TRADER hors
  crypto le week-end, pas de GARDER une position ouverte (« CRYPTO_ONLY » venait du modèle de départ du 18/09).
  `weekend_holding_allowed: ALL` ; fermeture d'avant week-end et blocage de la dernière heure conservés mais inactifs.
  Risque assumé : écart de prix à la réouverture (le stop s'exécute au premier prix disponible).

- **Audit « des trades crypto dans la journée » (27/09)** : relevé chez IC Markets — DOTUSD, XLMUSD, LNKUSD, POLUSD en
  « clôture seulement » (85 candidats en une matinée, jamais ouvrables) → retirés de l'univers ; spreads relatifs :
  BTC 0,006 %, XRP 0,026 %, ETH 0,11 %, SOL 0,15 % (tradables), BNB/BCH/UNI/AVX 0,4-0,5 %, DATA 0,75 %, ADA 1,2 %,
  LTC 1,6 %, XTZ 2,2 %, DOG 2,9 %, KSM 9,6 % (refusés par le coût sur des stops H1). Pré-filtre déterministe AVANT la
  revue IA : symbole non ouvrable ou coût > plafond du stop → refusé sans consommer un créneau IA (même règle que le
  gate). Dossier IA : `historique_note` sous 10 trades (44 refus citaient « sample_size=0 »). En attente de décision :
  contrôle 07 (spread ≤ 0,15 ATR H1) qui refuse SOL à 0,185 ATR alors que 07b (coût / stop) est déjà satisfait.

- **« Je veux des trades » (27/09, accord explicite de l'utilisateur, exceptions au gel lab-v1)** : plafond spread / ATR
  H1 du contrôle 07 propre à la crypto (`max_spread_atr_ratio_by_class: {crypto: 0.25}`, 07b reste le garde-fou du
  coût) ; score minimal 55 reconduit pour la journée du dimanche (retour à 65 à 17 h New York) ; consigne à l'arbitre
  IA : WAIT réservé à une confirmation précise et nommée, sinon APPROVE ou REJECT.

- **Mini-trades crypto conservés (27/09, décision utilisateur)** : IC Markets plafonne XRPUSD à 1 000 lots (1 lot = 1 XRP,
  ~1 540 $ de position) et SOLUSD à 100 lots → un trade XRP ne porte que 28 $ de risque sur 628 $ visés (constaté sur le
  XRPUSD BUY P09 de 12 h 28), SOL ~250 $ au mieux ; seuls BTC et ETH portent le risque complet. Le contrôle 11b
  « risque utile » reste désactivé (retour au 19/09) : l'utilisateur préfère garder ces trades (jours de trading FOXX).
  Copieur Moneta arrêté : « Authorization failed » (identifiants COPY2 dans .env ou connexion manuelle du terminal, à
  faire par l'utilisateur).

- **Suiveur Moneta « 10k change » (27/09)** : compte de challenge Moneta Funded ajouté par l'utilisateur (login et serveur
  dans .env, à jour). `allow_real: true` ajouté sur sa demande explicite. Terminal `mt5-moneta-10k-change` = copie du
  terminal IC : il ne connaît pas le serveur MonetaFunded-Live (« Authorization failed ») → l'utilisateur doit y ouvrir
  le compte une fois (Fichier > Ouvrir un compte > Moneta Funded), puis relancer le copieur.

- **Copie : seulement les positions ouvertes en même temps (27/09, décision utilisateur)** : une position du maître
  ouverte plus de 10 min avant le démarrage d'un suiveur n'est jamais copiée (marquée traitée, journalisée). Cas
  Moneta connecté le dimanche : NZDJPY et NETH25 du vendredi auraient été copiés lundi à un prix sans rapport.
  L'export du maître porte désormais `time_open`. Copieur demo 1 : verrou réparé (SystemError Windows sur PID mort).

- **Améliorations validées par l'utilisateur (27/09 soir)** :
  2. Le backtest reproduit la gestion du bot (`backtest/engine.py` : break-even, TP partiels 1,5 R / 2,5 R, stop
     suiveur, R sur le risque initial) ; branché par le pipeline via `risk.profit_management`
     (`backtest.use_position_management`). Test : un trade monté à +1,6 R puis revenu au stop vaut +0,49 R au lieu de −1 R.
  3. Recherche dans un processus séparé (`research/worker.py`, composant `research` de start_all, priorité basse,
     20 agents par passage toutes les 30 min) ; le registre n'a qu'un écrivain, l'orchestrateur applique les
     demandes de statut (`state/agent_status_requests.jsonl`). `learning.research_external: true`.
  4. Watchdog : tolérance de 15 min au démarrage pour les symboles sans tick (fin des 10 min de SAFE_MODE le week-end).
  5. Terminal Moneta : copie du terminal IC, le compte maître reste dans sa liste de comptes enregistrés
     (`config/accounts.dat`, binaire) — à retirer à la main dans MT5 (Navigateur > Comptes > Supprimer).
  6. Rapport de 17 h : écarts maître / suiveur par compte (positions non portées, copies refusées et motifs).
  7. Journal : `llm_skipped` une fois par (rôle, motif) et par 10 min (2 900 lignes/jour avant).
  8. Revues IA par cycle 3 → 5 (`execution.max_llm_reviews_per_cycle`, jusqu'à 10 si besoin) ; quotas 60 → 100 Opus
     et 150 → 300 worker par heure (mesuré ~200 appels/h avec 3 revues).
  9. Stop minimal 0,75 ATR H1 hors crypto (`execution.min_sl_atr_ratio_by_class`), rejeu : +13 R sur la semaine ;
     crypto inchangée (agents M5/M15 à stops de 1,5 ATR de leur unité de temps).
  Reporté à la demande de l'utilisateur : garde de risque côté suiveur pour le challenge Moneta (point 1).

- **Fermeture du forex avant le week-end réactivée (27/09 soir, décision utilisateur)** : `weekend_holding_allowed:
  CRYPTO_ONLY` — les positions hors crypto (forex, indices, métaux, énergie) sont fermées 15 min avant le reset du
  vendredi 17:00 New York (22:45 Paris en été) et aucune entrée hors crypto n'est prise dans l'heure qui précède.
  Les positions crypto sont conservées.

- **Horaires forex en heure serveur (27/09 soir, corrigé)** : le bot croyait le forex fermé du dimanche 22 h UTC au
  vendredi 21 h UTC ; IC Markets ouvre le lundi 00:05 et ferme le vendredi 23:55 en heure serveur (UTC+3 l'été, UTC+2
  l'hiver) — les stops de GBPAUD (−2,2 R, −1 366 $) et NZDJPY (−1,21 R, −755 $) ont sauté dès 21:01 UTC, à
  l'ouverture réelle (écart du week-end). `forex_market_open` utilise le décalage serveur mesuré, sinon Europe/Athens.

- **Famille R, session Asie (28/09, décision utilisateur, LIVE directement)** : 12 agents actifs 00:00-08:00 UTC sur les
  paires en yen, AUD/NZD, JP225 / AUS200 / HK50 / CHINA50 et l'or (retour à la moyenne, rejet S/R, repli de tendance,
  momentum d'ouverture de Tokyo, balayage de liquidité, cassure de l'ouverture du Nikkei, structure or). Stops ≥ 0,75
  ATR H1 par construction. Backtest informatif lancé ; perdants → SHADOW sur validation de l'utilisateur.

- **R07 (tendance AUD/NZD en Asie) en SHADOW** (28/09, décision utilisateur) : PF 0,68 en backtest.

- **Optimiseur systématique d'agents (28/09, feu vert utilisateur : « les meilleurs agents »)** :
  `research/optimizer.py` — grille 12 stratégies × 4 unités de temps × 3 stops × 4 objectifs × 4 classes (2 304
  configurations), chacune jugée sur 2 à 4 symboles à la fois, avec la gestion réelle du bot ; tri large (PF ≥ 1,2,
  ≥ 40 trades, ≥ 0,10 R, DD ≤ 15 R) puis walk-forward anchoré 4 plis (robustesse ≥ 0,5, hors échantillon > 0).
  Score = espérance rétrécie (n/(n+30)) × √n. Multiprocessing 20 cœurs. L'utilisateur voulait une priorité
  « moyenne » : essayée, elle portait les cycles du bot à 37–70 s et faisait dépasser le délai des revues IA ; les
  calculs tournent en **priorité basse** (santé du bot d'abord, ils gardent les cœurs libres). GPU non utilisé :
  moteur barre par barre. Les retenus deviennent des propositions (`state/agent_proposals.jsonl`) que l'orchestrateur
  ajoute (famille X, SHADOW par défaut, `--status LIVE` possible).
  - Lectures du terminal MT5 en douceur (`research/rates_cache.py`) : le premier essai (52 × 10 000 barres d'affilée)
    a figé l'orchestrateur 7 minutes ; désormais cache disque `data/cache/rates`, pause 2 s, arrêt si le bot ne
    boucle plus depuis 90 s.
  - **Premier passage réel (28/09, 00:58 → 05:48, 4 h 52)** : 2 304 configurations, 0 erreur, 160 survivantes au tri
    large, **20 retenues après walk-forward, toutes or/argent en H4 (tendance D1)** : `breakout_retest` (PF 1,37–1,41,
    186–212 trades, robustesse WF 2,9–3,0, hors échantillon +0,26 à +0,37 R), `macd_momentum` (PF 2,37, 40 trades,
    robustesse 2,7–2,8, HE +0,46 à +0,51 R), `ema_trend` (PF 2,04, 85 trades, robustesse 0,97, HE +0,38 R). Aucune
    configuration forex, indices ou crypto, ni aucune en M15/H1, n'a passé le walk-forward.
    Les 20 retenues n'étaient que **6 configurations distinctes** : avec la gestion de position (TP partiels, stop
    suiveur), la cible finale `rr` ne change presque rien. Propositions réduites à la main à X01–X06 avant leur
    application ; `dedupe()` ajouté à l'optimiseur (un agent par stratégie × TF × classe × stop) et le rapport garde
    désormais toutes les survivantes avec leur walk-forward (`etape_2`). Rapport : `reports/optimizer_2026-09-28_0348.json`.
    Coût : `liquidity_sweep` et `structure_bos` ≈ 100 s par backtest de 5 000 barres contre 13 s pour `ema_trend`.
  - **Fil de recherche bloqué (28/09)** : les propositions ont attendu 7 h, seul le fil de recherche les appliquait et il ne
    rendait plus la main sans trace. Les files (statuts du worker, propositions de l'optimiseur) sont maintenant traitées
    dans la boucle principale (`_apply_research_files`) ; un fil encore occupé à l'échéance suivante est journalisé avec
    sa pile d'appels (« cycle de recherche toujours en cours »). X01–X06 ajoutés au registre le 28/09 à 15 h 12.
- **Accélérer les 40 trades par agent (28/09, décision utilisateur « 1 et 2 »)**. Constat : 180 trades live en 8 jours sur
  43 agents, aucun à 40 (C06 : 20) ; rythme tombé à 1–8 par jour ; 188 candidats refusés le 28/09 par le seul verrou
  « 3 pertes consécutives » (entrées fermées de 03 h 50 à 09 h 08), puis plafonds de concentration et « 1 position par
  symbole ». Les créneaux ne manquent pas (6 positions sur 30).
  1. **Trades papier des agents LIVE** (`orchestrator._papier`, `shadow` mode « paper ») : un signal LIVE non pris pour une
     raison de CAPACITÉ (entrées verrouillées, symbole déjà porté, au-delà des N revues IA, plafond d'entrées du cycle,
     gate refusé uniquement sur des contrôles de capacité — `CONTROLES_CAPACITE`) est suivi en papier au prix réel jusqu'au
     SL/TP, raison conservée (`features.paper_reason`, événement `paper_trade`). Jamais un refus de qualité (revue, spread,
     stop, news), jamais une idée exécutée. `store.trades(mode="live+paper")` pour juger un agent sur tout ce qu'il a
     signalé ; la dégradation / suspension automatique reste sur le live (à changer sur demande).
  2. **TEMPORAIRE, à remettre à la fin de la période de test** : `risk_per_trade_percent` 0,125 → **0,05 %**,
     `max_consecutive_losses` 3 → **5**. Plafonds de concentration inchangés en % d'equity (même argent exposé par
     cluster / devise / classe, composé de plus de tickets — comme le mode test du 23/09) ; budget total 1 % inchangé,
     soit 20 positions au plus. Score requis inchangé (65).
- **Bouton break-even = stop en profit + stop suiveur (28/09, demande utilisateur)** : `move_to_break_even` place le stop
  juste au-dessus de l'entrée EN PROFIT (entrée + 0,05 R + commission ; si ce niveau est trop près du prix pour le
  broker, au plus près accepté — référence bid/ask + 1 tick — tant que cela reste au-dessus de l'entrée) et arme le stop
  suiveur immédiatement (`plan.trailing_forced`, sans attendre +1,05 R) ; stop déjà au-delà → seul le suivi est armé ;
  prix pas encore en profit → refus propre. Bouton « Armer partout » : même comportement position par position.
- **Profit protégé automatiquement (28/09, demande utilisateur « à +100 $, stop suiveur pour rester en positif », puis
  « adapte : plus de positions avec moins de lots »)** : `profit_management.protect_profit_risk_ratio: 0.16` — dès que
  le meilleur R atteint 0,16 R (= 100 $ pour les 625 $ risqués à 0,125 %, 40 $ avec le 0,05 % temporaire, et
  proportionnel chez les suiveurs), break-even et stop suiveur sont armés quel que soit le R (`profit_protection` au
  journal). `protect_profit_money` : seuil absolu optionnel, 0 = non utilisé.
  **Verrou** (« si ça monte à 40 $, pas sous 35 $ ») : `protect_profit_lock_ratio: 0.14` — une fois la protection
  déclenchée, le stop se pose à 0,14 R (35 $ pour 250 $ risqués) et le suivi ne redescend jamais sous ce plancher
  (`_floor_level`) ; niveau trop près du prix pour le broker → stop au plus près accepté, en profit, sans attendre.
  **28/09 au soir, décision utilisateur « seuil plus haut »** : mesuré de 18 h à 18 h 30 avec 0,16 R / verrou 0,14 R, 7
  trades protégés sur 8 coupés entre +0,02 et +0,13 R sur un simple retour du prix. Nouveau réglage : protection à
  **0,5 R**, verrou = max(0,25 R, **moitié du meilleur profit atteint**) (`protect_profit_lock_fraction: 0.5`,
  `_lock_r`) — le trade respire, et une fois à +0,5 R il ne peut plus finir en perte.
  **Verrou logiciel** : si le stop broker n'a pas pu être posé au verrou (distance minimale) et que le prix repasse
  sous 0,14 R, la position est fermée au marché (`profit_lock_exit`) ; le stop broker au plus près reste le filet.
  **Bouton break-even** (« les positions qui ont un gros profit : le stop juste sous le prix dès qu'on clique ») : le
  bouton verrouille le profit ACTUEL — stop au plus près du prix accepté par le broker — s'il est au-delà du break-even.
  Constat USTEC 17 h 43 : bouton cliqué 30 s après l'entrée, à +5 points, stop posé au plus près, sorti à +8 $ sur le
  bruit : le bouton est fait pour un profit déjà installé.
- **Récap Telegram détaillé (28/09, demande utilisateur)** : à la clôture, brut (deals), commission (entrée + sortie),
  swap, spread à l'entrée estimé (points × valeur du point × volume, compris dans le brut) et **net** — `close_costs`
  dans `features`, champs `brut/commission/swap/spread_cost/volume/exit_reason` dans `post_trade_review`.
- **Revue par trois agents (28/09 au soir) et décisions utilisateur (« tout sauf 5 »)**. Chiffres sur 8 jours (197 trades
  live) : forex 113 trades −14 842 $ (−0,18 R/trade, commission ≈ 5 % du risque) contre crypto +0,19 R, indices +0,08 R,
  métaux +0,08 R ; M15 120 trades −0,25 R/trade, H1 −0,01 ; famille C −10,6 R/44 ; 31 trades passés par +0,5 R puis
  finis au stop plein (−20 796 $) ; 11 agents revus ≥ 15 fois par l'IA sans un APPROVE (B06 829/0, C05 1 121/1) ; 40
  agents LIVE sans aucun candidat ; trades papier depuis 16 h 28 : 33 clos −20,4 R (famille C surtout).
  Correctifs de code (bugs confirmés) : (A1) l'ombre oublie un signal exécuté en réel (`shadow.forget`, clé
  d'idempotence dans `ShadowPosition`) ; (A2) papier seulement sur APPROVE déterministe ; (A3) copieur : élargissement
  du spread seulement tant que le stop maître est du côté perdant (sinon le « break-even » du suiveur perdait) ; (A4)
  verrou logiciel : fermeture refusée → temporisation 60 s → 15 min, journal à la 1re puis toutes les 5 ; (A5)
  `_floor_level`/`_lock_r` lisent le réglage du régime courant ; (A6) fichiers de propositions/statuts inaccessibles →
  avertissement au journal.
  Décisions appliquées : (C1) C01, C06, D01, K02 SUSPENDED ; (C2) `llm_skip_never_approved_after: 30` — un agent revu
  30 fois sans APPROVE (compteur `state.llm_review_stats` par version) garde le verdict déterministe et libère les
  créneaux IA ; (C3) `forex_short_term_paper_only: true` — les signaux forex M1/M5/M15 vont en papier, jamais exécutés
  (les agents mixtes gardent indices/métaux/crypto) ; (C4) `risk_per_trade_by_class: {forex: 0.025}` — risque forex à
  moitié ; (C6) dégradation / suspension automatique sur `live+paper` ; (C7) K03–K12 et M02–M14 (0 candidat) en SHADOW
  — les K dépendent du calendrier économique, signalé « dégradé » dans l'état : à vérifier ; (C8) N06, N12, CH157 à
  proposer LIVE à 20 trades ; (C9) heures forex interdites : finalement **non** — l'utilisateur a précisé « s'il peut trader » ;
  `forex_blocked_hours_utc: []` (le réglage reste disponible). Refusé : (C5) une position par devise.
  Statuts déposés dans `state/agent_status_requests.jsonl`, appliqués au premier cycle de recherche (30 min après
  démarrage). Note : `llm_call` n'a ni agent ni symbole — à ajouter pour attribuer le coût IA (505 $ sur 8 jours).
- **Ordres en attente — jumeaux SHADOW, famille Q (28/09 au soir, demande utilisateur « ordre d'achat / de vente à tel
  prix avec TP et SL » → « tout en SHADOW et que ça complète notre pipeline »)**. Le candidat porte `entry_kind`
  (MARKET / LIMIT / STOP), `order_price`, `expiry_bars` ; `screeners.apply_pending_entry` transforme le signal d'une
  stratégie générique en ordre limite (retour au niveau, 0,2–0,3 ATR en retrait, distance au stop raccourcie) ou stop
  (confirmation, 0,15 ATR au-delà), annulé après 3 barres ; RR recalculé sur le prix de l'ordre, cibles inchangées.
  Simulation : l'ombre remplit l'ordre dès qu'une barre clôturée touche le prix (événements `pending_filled` /
  `pending_expired`, suivi à partir de la barre suivante) ; le moteur de backtest fait de même (gap → rempli à l'open,
  `BTResult.expired_orders`), donc les jumeaux passent par tout le pipeline. Q01–Q10 en SHADOW : breakout_retest (limite,
  métaux H4 / indices H1), sr_rejection (limite, forex H1), liquidity_sweep (limite, crypto H1), structure_bos (limite,
  métaux H1), donchian (stop, crypto H4 / métaux D1), atr_expansion (stop, indices H1), ema_trend et macd_momentum
  (stop, métaux H4). À juger sur l'espérance PAR SIGNAL (remplis + ratés) contre les jumeaux au marché. L'exécution réelle
  d'un ordre en attente n'est pas branchée : un tel candidat d'agent LIVE est refusé avant revue (« SHADOW seulement »).
  Reste à faire pour le réel : gate sur le prix de l'ordre, gestionnaire des ordres (durée de vie, annulation, une attente
  par symbole, adoption de la position), copie des positions une fois ouvertes.
- **Boost des tests en ombre (28/09 au soir, demande utilisateur « continue les tests en shadow, booste-les »)**. Bilan
  à 22 h : 154 agents SHADOW, 293 trades d'ombre clos, 57 ouverts ; meilleur CH157 +19,4 R sur 16 (+1,21 R/trade), puis
  CH179, CH158, CH101, O01 ; pires CH135 −17 R, P20 −15,5 R, O03 −13,8 R ; 115 agents SHADOW sans aucun trade d'ombre
  (challengers surtout) ; pipeline : 124 échouent au backtest, 27 l'ont passé, 4 jusqu'au Monte-Carlo ; les passages du
  worker ne traitaient plus que 0 à 10 agents (file vide, nouvel essai à 24 h). Réglages : worker 40 agents par passage
  toutes les 15 min (`research_interval_sec` 900), nouvel essai après 8 h, ombre 600 positions / 6 par agent. 15
  challengers (CH201–CH215, paramètres ±20 %) des cinq meilleurs agents d'ombre déposés en SHADOW via
  `state/agent_proposals.jsonl`. Aucun seuil de risque ni de validation touché (100 trades d'ombre toujours requis).
- **29/09 matin** : (1) agents d'annonce vérifiés sur la décision RBA (04h30 UTC) — K03 (news_follow) 6 positions d'ombre à
  04h35, K05 (news_range_break) 6 à 04h45, K04 (news_fade) 4 à 05h10–05h20 : le déclencheur fonctionne, le silence des
  8 jours venait de l'absence d'annonce HIGH depuis le 26/09 ; à juger sur leurs résultats d'ombre. (2) **Rapport de
  17 h NY réparé** : `_copy_gaps` appelait `.get` sur des `BotPositionPlan` → « rapport quotidien impossible » les 27 et
  28/09 (test de régression ajouté). (3) Nuit : SAFE_MODE 41 s à 23h11 (cotations figées 62 s au rollover, retour AUTO
  automatique) ; 11 trades clos −343 $ (8 gagnants dont 7 protégés, 3 stops pleins) ; ordres Q : 5 remplis, 2 expirés.
- **29/09, décision utilisateur (« je valide »)** : **CH157 → LIVE** (variante d'E01, 20 trades d'ombre, 10 gagnants,
  +15,4 R, +0,77 R/trade) ; P20 (−20,5 R/24), CH135 (−18 R/30), O03 (−13,8 R/30), CH137 (−8 R/8) SUSPENDED. Suivant sur
  la liste : CH158 (+7,3 R/25), non promu.
- **29/09, demande utilisateur « des X en M5, M15, H1 et D1 »** : X07–X22 en SHADOW, déclinaisons de X01, X02
  (breakout_retest), X03 (macd_momentum) et X06 (ema_trend) sur or/argent en M5 (tendance H1), M15 (H1), H1 (H4) et D1.
  Réserve : l'optimiseur du 28/09 n'avait rien retenu en M15/H1 (M5 absent de sa grille) ; le pipeline tranchera.
- **29/09, agents M1 en micro-positions (demande utilisateur, puis « toutes les stratégies sur tous les agents »)** :
  famille S, S01–S38 en SHADOW = 19 stratégies génériques (hors annonces / week-end) × {crypto BTC/ETH, indices US500 /
  USTEC / DE40 + or}, entrée M1, tendance M15, `risk_factor: 0.25` (micro-position : un quart du risque, appliqué par le
  gate via `GateContext.agent_risk_factor`). M1 chargé pour ces 6 symboles seulement (`system.extra_timeframes`).
  Règle FOXX « plus d'une minute » : le gestionnaire ne fait plus aucun TP partiel ni sortie du verrou avant 60 s
  (`PositionManager.MIN_HOLD_SEC`) ; le stop broker reste actif. L'ombre déduit le spread d'entrée des trades M1
  (`ShadowPosition.cost_r`), sinon le scalping paraîtrait gagnant. Limite connue pour un passage en réel : le gate
  exige un stop ≥ 0,75 ATR H1 hors crypto (`min_sl_atr_ratio_by_class`) et un coût ≤ 20 % du stop — un stop M1 sur
  indices / or serait refusé ; seule la crypto passerait tel quel.
  Optimiseur étendu aux 19 stratégies et au M5 (RR réduit à 2,0 : 1 140 configurations), relancé en priorité basse.
  **Résultat (29/09, 14h51 → 18h10)** : 151 survivantes au tri large, 20 retenues ; 8 écartées (déjà au registre — les
  X01–X06 d'hier retrouvés — ou résultats identiques pour un autre stop) ; **12 nouvelles X23–X34 en SHADOW** :
  breakout_retest M15 crypto, donchian D1 métaux, ema_pullback H4 métaux (×3), rsi_divergence H1 métaux, ema_trend H4
  métaux (stops 1,5 et 2), compression_expansion H4 indices, structure_bos D1 métaux, failed_breakout D1 crypto (×2).
  Toujours aucune configuration forex ni M5 retenue ; les métaux H4 dominent. Rapport : reports/optimizer_2026-09-29_1610.json.
- **29/09, recherche forex M5 (demande utilisateur)** : optimiseur en mode ciblé (`--classes forex8 --timeframes
  M5:H1,M5:M15 --sl-atr 1,1.5,2,3 --bars M5=20000`) : 8 paires, ~70 jours de M5, 19 stratégies, 152 configurations.
  **Décision utilisateur « oui, lève »** : les configurations forex M1/M5/M15 retenues par le walk-forward de l'optimiseur
  portent `params.forex_short_term_ok` et ne sont PAS mises en papier seulement (`_forex_court_terme`) ; elles naissent
  quand même en SHADOW et ne passent LIVE que sur décision. Le passage lancé à 18h20 utilise l'ancien code : le drapeau
  sera ajouté à ses propositions à la main.
- **29/09, accélération des backtests (demande utilisateur, « puis le GPU si c'est mieux »)** : (1) `swing_points`
  vectorisé (60 % du temps) ; (2) session testée d'abord, cadre de tendance et régime mis en cache ; (3) RSI(2) précalculé ;
  (4) **signaux vectorisés** `backtest/fastsig.py` + `fastsig_g1/g2/g3.py` : 18 stratégies génériques sur 19 (daily_hl_breakout
  garde le chemin lent) calculent en une fois le signal de chaque bougie ; `run_backtest` lit `signal_fn.signal_at(i)` ;
  caches par processus des indicateurs et du régime. Équivalence EXACTE bougie par bougie et trades identiques
  (tests/test_fastsig_2026_09_29.py, 208 tests). Gains, une configuration sur 5 000 bougies : structure_bos 9,5 s →
  0,06 s, rsi_divergence 12,3 → 0,05, bollinger_mr 10,6 → 0,04 ; ~100× une fois les indicateurs en cache. Les stratégies
  propres des agents (E05, B02…) gardent le screener ; `make_signal_fn(..., fast=False)` force l'ancien chemin.
- **29/09, alerte « données périmées — aucun marché suivi ne cote » (demande utilisateur)** : le 28/09 à 23h11, un
  seul contrôle a vu toutes les cotations figées depuis 62 s (cryptos comprises), revenues 15 s plus tard → SAFE_MODE
  41 s. Le watchdog exige désormais `system.stale_confirm_sec` (30 s) de contrôles périmés consécutifs avant l'alerte
  et le SAFE_MODE ; un flux réellement mort est toujours détecté (tests).
- **29–30/09, recherche sur la 3090 (demande utilisateur « portage GPU, fais tout »)** : CUDA (numba-cuda, cupy-cuda12x)
  dans le venv ; simulation compilée (`backtest/gpu_sim.py`, CPU Numba + noyau CUDA, métriques identiques à run_backtest) ;
  signaux par PAQUETS (`sl_atr`, `rr` en colonnes) sur numpy ou cupy pour les 18 stratégies (`fastsig`, `FastCtx.on(xp)`),
  parties « données » en numpy mises en cache ; recherche en masse `research/massive.py --device gpu --univers --sessions`
  (73 marchés, 5 variantes de session, période de contrôle 30 %, seuil √(2 ln M), plateau). 308 tests verts (équivalence
  CPU, paquet = appels séparés sur CPU et GPU, sorties GPU = CPU). Paquet de 24 configurations sur 20 000 bougies :
  12–25 ms CPU → 0,7–4 ms GPU. Premier passage (20 640 configurations, 4 familles) : aucune retenue (1 significative,
  échouée au contrôle).
- **30/09, grand passage de la recherche en masse** (73 marchés, 5 variantes de session, 129 000 configurations, 12 cœurs,
  1 h 31 de calcul après 45 min de lecture des historiques) : seuil t ≥ 4,85 ; 279 significatives, 279 avec plateau, 235
  confirmées sur la période jamais vue. Les 20 premières sont TOUTES la même idée : **exhaustion (rsi_ext 25), forex 28
  paires, session de New York, M5 et M15** — ex. M15 stop 0,8 ATR : apprentissage 327 trades +0,42 R PF 2,24 (t 6,6),
  contrôle 161 trades +0,55 R PF 2,82. Vérifié avec le spread médian RÉEL de chaque paire + commission : résultat
  identique (+0,57 R, PF 2,97 au contrôle). Propositions réduites à 8 distinctes (unité de temps × stop) : X35–X42 en
  SHADOW, sessions NEWYORK, `forex_short_term_ok` (exception accordée le 29/09). Rapport : reports/massive_2026-09-29_2328.json.
  À noter : E03 (exhaustion LIVE, rsi_ext 20, toutes sessions) n'a qu'un trade réel.
- **Nuit du 29 au 30/09 sur la 3090** (research/nuit.py) : 7 passages (grille fine, unités de temps alternatives, contrôle 20 /
  30 / 40 %), ≈ 2,3 millions de configurations en 1 h 15. Toutes les idées proposées (X35–X58, SHADOW) rejouées avec les
  trois découpages : **25/25 tiennent 3/3**. Idée dominante : **exhaustion forex (28 paires), M5/M15, session de New York
  ou chevauchement** — PF 2,0 à 3,8, +0,38 à +0,64 R au contrôle (coûts réels vérifiés). Idées indépendantes, coûts réels
  (spread médian + commission) : RSI(2) M15 indices New York (X48/X54, ~1 190 trades, +0,16 R, PF 1,33), bollinger_mr M15
  forex New York (X53, +0,29 R, PF 1,68), liquidity_sweep M15 indices New York (X56, +0,13 R, PF 1,27), Donchian D1 crypto
  (X49, +0,14 R, PF 1,26). Marginales avec coûts réels : X50 (exhaustion H1, PF 1,11) et X58 (RSI2 indices chevauchement,
  PF 1,14). Doublons de X35 : X43, X51. Décision LIVE à prendre après 2–3 semaines d'ombre.
- **Stop du suiveur élargi du surcroît de spread (28/09, décision utilisateur)** : SILVER chez les démos IC (spread 82
  points) contre XAGUSD chez le maître (11) — les quatre copies argent ont pris le stop à 61,807 sur un pic que le maître
  (stop 61,804, plus haut 61,776) n'a pas vu. Le maître exporte son spread ; à l'ouverture d'une copie, le stop du suiveur
  est élargi de (spread suiveur − spread maître) côté défavorable, mémorisé dans la table maître → suiveur
  (`spread_offset`) et les mises à jour du stop maître sont comparées après élargissement (`copier._widen_for_spread`).
  Risque de la copie un peu plus grand que celui du maître : accepté par l'utilisateur (option choisie parmi trois).
  - **Décision utilisateur 28/09 (« oui monte en live »)** : X01 et X02 (`breakout_retest` H4 or/argent, stops 1,0 et
    1,5 ATR, les deux mieux classés et les plus fournis en trades) ajoutés **LIVE** ; X03–X06 en SHADOW.

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

- 30/09 : régime vectorisé (`regime_series`) pour la recherche en masse : 7 s → 0,01 s par marché, équivalence testée. **Incident 09:41** : le terminal maître a été connecté au compte 53076986 (nouveau suiveur COPY3) → ACCOUNT_MISMATCH, SAFE_MODE. À faire : reconnecter le terminal maître sur 53068680, puis redémarrer en AUTO (copieurs COPY4-7 retirés encore actifs, COPY3 à lancer).
- 30/09 10:52 : maître 53068680 reconnecté, AUTO. Copieurs actifs : blueguardian, demo1 new (53076986). Moneta en attente : son terminal est connecté au 53068680, à reconnecter au compte Moneta par l utilisateur plus tard.
- 30/09 soir : page dashboard « Shadow & recherche » (/shadow). Règles shadow : suspension auto à 50 trades si PF < 0,8 ; famille N signalée pour revue live à 50 trades si PF >= 1,3. 11 agents perdants suspendus (S13 S12 S03 S08 S19 S27 S04 S38 S23 CH101 K01). Recherche continue gardée toute la journée.
- À venir (demande utilisateur) : trader chaque session (Londres, Sydney, Asie, USA) de façon indépendante, avec des règles propres, puis lancer le bot sur toutes.
- 30/09 nuit : recherche sur toutes les unités de temps (M1, M5, M15, H1, H2, H4, D1, W1 ; MN1 en tendance seulement, alignée sur le mois calendaire). Le bot sert automatiquement l unité de temps d un agent sur ses marchés (_sync_feed_timeframes). Option --toutes-ut de la recherche en masse.
- 30/09 nuit : algorithme or OR01 (cassure du range asiatique H1, sens de la tendance D1, option trend_only du screener session_breakout) en SHADOW. Backtest 2023-2026 : +0,29 R, PF 1,70, 192 trades, mais 90 % d achats pendant la hausse de l or et 0,00 R sur l argent : à juger en shadow. Fiche : config/agents_or.json.
- Propositions TP/SL (analyse du 30/09, rien appliqué) : cible finale 3R ; protection à 1R pour les agents prouvés ; ne jamais resserrer les stops ; garder BE 1R et partiels ; travailler coûts et entrées forex/crypto. En attente de décision utilisateur.
- 30/09 nuit, décision stops/cibles (« fais ce qui est le mieux ») : AUCUN changement de règle. Cible fixe 3R rejouée sur la semaine (106 trades réels) : -0,95 R → rejetée (option final_target_r codée mais désactivée). Protection à 1R : non appliquée (drawdown +18 %, contraire au souhait de protéger les gains). Stops élargis : gain dans le bruit. Break-even 1R et partiels gardés. Perdants forex/crypto déjà suspendus par les règles automatiques. Le rejeu simule maintenant la protection du profit. Gel lab-v1 inchangé (96/100).
- 01/10 : session SYDNEY (22:00-00:00 UTC, soit minuit-2 h à Paris l été ; 21:00-22:00 UTC reste OFF, changement de jour) visible partout ; aucun agent ne la trade encore (variante SYDNEY ajoutée à la recherche). Recherche alignée sur le live : en intraday, session jugée à la clôture de la bougie, filtre strict (plus de signaux dans la plage OFF) ; H4/D1 inchangés ; crypto en marché continu. Forex court terme en LIVE pendant la session ASIE (forex_short_term_live_sessions), papier ailleurs. Nouvelle version de réglages lab-v2 (gel de 100 trades depuis le 30/09 23:40 UTC ; lab-v1 close à 96/100).
- 01/10 (plus tard) : décision utilisateur « sur tous les marchés en réel » : forex court terme en LIVE dans toutes les sessions (forex_short_term_paper_only: false). Avertissement donné : papier Londres -12 R, New York -20 R. Les agents M1 métaux S22/S35/S32/S24 (positifs en shadow) restent en shadow : validation hors échantillon non passée. Gel lab-v2 redémarré le 30/09 23:55 UTC.
- 01/10 : trades semi-longs. Stop suiveur des agents H4/D1/W1 calé sur l ATR de leur unité (comme leurs backtests ; avant : ATR H1 pour tous, un trade D1 avait un suiveur à ≈ 0,2 R). Positions shadow H4/D1/W1 gardées 10/20/60 jours au lieu de 72 h ; durée des trades shadow enregistrée. Reste à décider (utilisateur) : garder ou non les positions semi-longues hors crypto le week-end (fermeture du vendredi 22:45, décision du 27/09).
- 01/10 : décision utilisateur, les positions semi-longues (agents H4/D1/W1) passent le week-end avec protection de l écart du lundi : gardées seulement si le stop est au moins au prix d entrée, moitié du volume fermée avant le week-end, perte au-delà du stop sur un écart « 1 week-end sur 100 » ≤ 0,25 % du capital (écarts mesurés : métaux 0,77, indices 1,15, forex 1,26, énergie 2,40 ATR journalier). Les autres positions hors crypto restent fermées le vendredi 22:45. À vérifier par l utilisateur : règles de week-end des comptes de challenge copiés (BlueGuardian, Moneta).
- 01/10 : sortie sans progression (décision utilisateur, 8 h) : un trade d agent M1/M5/M15 jamais monté à +0,2 R en 8 h est fermé au marché (rejeu 22/09-01/10 : +11,2 R au lieu de +10,5 R, stops pleins 29 % au lieu de 30 %). Agents H1 (36 trades rejoués) : la protection à 1,0 R au lieu de 0,5 R donne +8,1 R au lieu de +1,7 R, mais plus de stops pleins (28 % au lieu de 19 %) et un drawdown 6,4 R au lieu de 5,1 R : décision utilisateur en attente. Suiveur 1,0 ATR : gain faible ; 2,0-2,5 ATR, BE 1,5 R, sortie 8 h : pas mieux.
- 01/10 : décision utilisateur, protection du profit à 1,0 R pour les seuls agents H1 (protect_profit_risk_ratio_by_tf), 0,5 R pour les autres. Rejeu H1 : +8,1 R au lieu de +1,7 R, plus de stops pleins (28 % au lieu de 19 %).
- 01/10 : décisions utilisateur. Risque par trade remis à 0,125 % (forex 0,0625 %), verrou de pertes consécutives à 3, perte journalière interne 2,5 % (sous la limite FOXX de 3 % avec marge). Agents shadow S22, S24 (M1 indices/or), CH158 (Bollinger M15), CH115 (structure M15) passés en LIVE hors pipeline (shadow ≥ 20 trades, PF ≥ 1,2). E05 gardé en live : réels +3,8 R (PF 1,42), seul le papier perd. Version lab-v3.
- 01/10 : décision utilisateur, 24 agents mis en SHADOW : 13 qui étaient LIVE (D07 E03 B10 P09 F03 C05 C07 L03 L04 P12 P11 E07 P15) et 11 qui étaient SUSPENDED (D01 G04 C02 P16 G01 F02 B01 C01 F05 D03 C03). ADOPTED n est pas un agent (étiquette des positions adoptées).
- 01/10 : notifications Telegram absentes. Cause : le groupe a été converti en supergroupe (nouvel identifiant -1004344992778) et tous les envois étaient refusés sans trace. Le notifieur suit maintenant la migration (mémorisée dans state/telegram_chat.json) et journalise les échecs. À faire par l utilisateur : TELEGRAM_CHAT_ID=-1004344992778 dans .env.
