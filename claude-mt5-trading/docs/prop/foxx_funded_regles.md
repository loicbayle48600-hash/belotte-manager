# FOXX Funded — règles officielles relevées

- **Source** : https://www.foxx-funded.com/fr/faqs (FAQ française du site officiel)
- **Date du relevé** : 2026-09-19 (UTC)
- **Méthode** : la page est une application monopage ; le contenu de la FAQ a été extrait du bundle
  applicatif servi par le site (`/assets/index-Bg6px8Ah.js`). Le bundle contient aussi les constantes
  du programme : `{consistency: 25, dailyLoss: 4, totalLoss: 8, challengeDays: 5, riskPerTrade: 2, target: 7}`.
- **Portée** : programme « Défi en 1 phase » (celui visé par `config/prop_firms.yaml`). Les valeurs du
  programme en 2 phases sont indiquées quand elles diffèrent.

> Ce document est une transcription de travail, pas un document contractuel. En cas de doute, la
> documentation officielle de la prop firm fait foi. Toute divergence doit bloquer l'automatisation prop.

## Objectifs et limites

| Règle | Valeur (1 phase) | Détail |
|---|---|---|
| Objectif de profit | **7 %** | 10 % en 2 phases. Atteint après fermeture de toutes les positions. |
| Perte quotidienne maximale | **4 % du solde initial** | Base de calcul : au changement de jour à **17:00 EST (heure de New York)**, on prend **le plus élevé du solde ou de l'equity**, et on en soustrait 4 % du **solde initial** pour fixer le plancher d'equity du jour. |
| Perte totale maximale | **8 % du solde initial** | Equity flottante incluse ; l'equity ne doit jamais atteindre 92 % du solde initial. |
| Type de drawdown | **statique** | Ni trailing en 1 phase, ni en 2 phases. |
| Perte maximale par idée de trade | **2 % du solde initial** | Plusieurs positions sur la même idée sont agrégées. Fermer puis rouvrir dans le même sens sur le même instrument **sous 10 minutes** reste la même idée. |
| Profit maximal par trade | **1,5 %** (tableau du site) | Lié au respect de la cohérence. |
| Jours de trading minimum | **5 jours** | Pas de limite de temps : période de trading illimitée. |
| Activité minimale | **au moins une transaction par semaine** | Évaluation et compte financé ; l'inactivité peut faire perdre le compte. |

## Règle de cohérence

- Aucune idée de trade ne peut représenter **plus de 25 % du profit total** du compte pour le cycle.
- Vérifiée **au moment du paiement** uniquement ; une incohérence ne disqualifie pas, elle oblige à
  continuer de trader jusqu'au retour sous le seuil.
- Plusieurs positions dans le même sens prises pour contourner la règle sont **agrégées en une seule**.
- Contraintes de taille de lots : le nombre de lots ouverts doit rester proportionnel à la taille du compte.

## Stop loss, stratégies et outils

- **Stop loss obligatoire sur chaque transaction.** Trois transactions sans stop loss entraînent une
  **rupture immédiate** du compte. (Une option « sans stop loss » existe sur certains produits.)
- **Martingale interdite** : ouvrir une ou plusieurs positions supplémentaires dans le même sens qu'une
  position perdante pour améliorer le prix d'entrée moyen.
- **Hedging autorisé** dans le même compte, s'il relève d'une stratégie légitime ; l'abus (multiplication
  de petites positions pour exploiter la plateforme) est interdit.
- **Expert Advisors autorisés**, mais une **approbation explicite est requise après l'achat du défi** :
  il faut contacter le support avec le numéro de compte et une description de l'EA.
- **Trading haute fréquence strictement interdit.**
- **Copy trading autorisé** entre comptes personnels du titulaire.

## News

- News trading **autorisé pendant la phase d'évaluation**.
- News trading **strictement interdit sur les comptes financés** : ne pas ouvrir ni fermer de position
  **5 minutes avant et après** tout événement économique à fort impact (dossier rouge).
- Sanctions : annulation des profits réalisés pendant la période restreinte, déduction sur le compte
  financé, suspension du compte en cas de répétition.

## Sessions et week-end

- Trading du week-end autorisé **uniquement sur les cryptomonnaies**.

## Paiements

- Premier paiement après **14 jours de trading**, puis **tous les 7 jours de trading**.
- Fenêtre de demande de **24 heures** ; au-delà, le compteur de 7 jours repart après une nouvelle opération.
- Partage des profits : **70/30** au premier paiement, **80/20** au deuxième, **90/10** au troisième.
- Retrait maximal par cycle : **15 % du solde initial**.
- Éligibilité : solde supérieur au solde initial, aucune violation de règle, vérification KYC effectuée.

## Application dans le laboratoire

Les règles ci-dessus sont transcrites dans `config/prop_firms.yaml` et appliquées par
`src/tradinglab/risk/prop_guard.py`, `src/tradinglab/core/trading_day.py` et `src/tradinglab/core/state.py`.
Tests de non-régression : `tests/test_prop_foxx_rules.py`.

| Règle relevée | Implémentation | Contrôle |
|---|---|---|
| Journée à 17:00 America/New_York | `TradingDayCalendar` ; `SystemState.roll_day_if_needed(equity, balance, now, calendar)` prend un instant, plus une date UTC | bascule du suivi journalier |
| Plancher jour = max(solde, equity) au reset − 4 % du solde initial | `DailyStats.reference_equity`, `SystemState.prop_daily_loss_percent()` / `prop_daily_floor()` | `19_prop_hard_daily` (marge 25 % → 3 %) |
| Perte totale 8 % du solde initial, drawdown statique | `SystemState.initial_balance` figé à la première synchronisation ; `prop_overall_loss_percent()` / `prop_overall_floor()` | `19_prop_hard_overall` (marge 25 % → 6 %) |
| 2 % par idée de trade, agrégation 10 min | `SystemState.register_trade_idea()` / `active_trade_idea()` ; risque cumulé jamais décrémenté par une perte encaissée | `19_prop_trade_idea` (marge 25 % → 1,5 %) |
| Week-end : cryptomonnaies uniquement | `PropGuard.weekend_check()`, week-end borné par le reset 17:00 NY | `19_prop_weekend` |
| Cohérence 25 % du profit total | `SystemState.consistency_share_percent()`, `PropGuard.consistency_status()` | rapport de conformité, **non bloquant** (contrôlée au paiement) |
| 5 jours de trading, ≥ 1 transaction / semaine | `SystemState.record_trading_day()`, `PropGuard.activity_status()` | rapport de conformité, indicatif |
| News 5 min avant/après (compte financé) | fenêtres internes de `config/news_sources.yaml` (30 min avant / 15 après), volontairement **plus larges** | `08_news` |
| Stop loss obligatoire | déjà imposé : aucun ordre ne part sans SL, et une position sans SL après fill est fermée | `10_stop_loss`, `Executor` |
| Martingale interdite, pas de moyenne à la baisse | `RiskManager.check_limits` (`no_averaging_or_grid`, une position par symbole) | `14_*` |
| Trading haute fréquence interdit | cadence du scheduler + `17_existing_position` + plafond de positions | — |
| EA autorisé mais approbation requise | `prop.ea_approval_obtained` ; tant qu'il est faux, `PropGuard.prop_automation_allowed` est faux | `03_authorization` |

## Ce qui reste à faire par un humain

1. Acheter le défi, puis demander au support l'**approbation de l'EA** (numéro de compte + description).
   Passer ensuite `ea_approval_obtained: true` dans `config/prop_firms.yaml`.
2. Autoriser explicitement l'exécution prop (`user_explicitly_authorized_prop_automation: true`) — le code ne
   se l'accorde jamais tout seul.
3. Vérifier que `account_size` correspond bien au défi acheté (actuellement 500 000). En pratique, c'est le
   **solde réellement constaté** (`SystemState.initial_balance`) qui sert de base aux pourcentages.
4. Relire ce document à chaque mise à jour de la FAQ de la prop firm et remonter `rules_version` /
   `rules_verified_at`.
