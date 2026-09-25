"""Copy trading (2026-09-22, demande utilisateur) : réplication des positions du bot maître vers des
comptes MT5 suiveurs. Un processus copieur PAR compte suiveur (le package MetaTrader5 ne tient qu'une
connexion par processus) ; la source de vérité est l'export `state/master_positions.json` écrit par
l'orchestrateur à chaque cycle."""
