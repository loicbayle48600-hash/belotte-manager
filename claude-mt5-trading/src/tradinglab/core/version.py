"""Version du code en cours d'exécution (plan pro du 2026-09-25, point 1).

Chaque changement est enregistré dans Git et étiqueté `lab-vN` ; la version est notée sur chaque trade pour que les
résultats se comparent version par version. `-dirty` signale des modifications non enregistrées."""
from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def code_version() -> str:
    racine = Path(__file__).resolve().parents[3]
    try:
        out = subprocess.run(["git", "describe", "--tags", "--always", "--dirty"], cwd=racine, capture_output=True,
                             text=True, timeout=10)
        v = out.stdout.strip()
        return v or "inconnue"
    except (OSError, subprocess.SubprocessError):
        return "inconnue"
