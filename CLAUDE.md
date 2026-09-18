# Dépôt belotte-manager — repères pour Claude Code

Ce dépôt contient deux projets indépendants :

1. **Application HACCP cuisine** (racine : `index.html`, `js/`, `css/`, `mobile/`) — PWA + APK Android via
   `.github/workflows` (voir `README.md`).
2. **`claude-mt5-trading/`** — laboratoire de trading algorithmique multi-agents autonome relié à MetaTrader 5
   (compte DEMO d'abord). Toute session qui travaille sur ce projet doit lire `claude-mt5-trading/CLAUDE.md`
   et `claude-mt5-trading/README.md` avant d'agir. Tests : `cd claude-mt5-trading && python -m pytest`.

Ne pas mélanger les deux projets (dépendances, scripts, CI).
