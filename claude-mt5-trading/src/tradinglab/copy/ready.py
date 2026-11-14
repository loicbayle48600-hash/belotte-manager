"""Imprime les suiveurs prêts, un par ligne « nom<TAB>chemin du terminal » — consommé par
scripts/start_all.ps1, qui lance chaque terminal dédié en mode /portable (profil dans son dossier)."""
from __future__ import annotations

import re

from ..core.config import project_home
from .registry import _env_text, load_cfg, ready_names

if __name__ == "__main__":
    home = project_home()
    ready = set(ready_names(home))
    env = _env_text(home)
    for f in load_cfg(home).get("followers", []) or []:
        if f.get("name") in ready:
            m = re.search(rf"^{re.escape(str(f.get('env_prefix', '')))}_MT5_TERMINAL_PATH=(.+)$", env, re.M)
            from .registry import _slug
            print(f"{f['name']}	{m.group(1).strip() if m else ''}	{f.get('env_prefix', '')}	{_slug(f['name'])}")
