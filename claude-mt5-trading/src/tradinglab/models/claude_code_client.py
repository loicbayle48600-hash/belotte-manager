"""Backend LLM alternatif : Claude Code en mode headless, au lieu d'une clé API.

Utilise l'exécutable ``claude`` déjà installé et authentifié sur le poste : les appels
passent donc par l'abonnement et non par une clé ``ANTHROPIC_API_KEY``.

Le client expose la surface minimale que ``LLMClient`` attend d'un client SDK :
``.messages.create(...)`` et ``.models.list(...)``. Les outils de Claude Code
(lecture/écriture de fichiers, bash, web) sont **désactivés** : ce backend ne sait
que transformer un couple (system, user) en texte, comme l'API Messages.

Limites connues, assumées :
- chaque appel porte le prompt système du harnais (~22 500 tokens) : voir README ;
- ``max_tokens`` n'est pas transmis (le mode headless ne l'expose pas) ;
- le coût renvoyé est le tarif public calculé par Claude Code, pas une facturation réelle.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

BACKEND_ENV = "TRADINGLAB_LLM_BACKEND"          # "claude_code" | "api" (défaut)
TIMEOUT_ENV = "TRADINGLAB_CLAUDE_TIMEOUT_SEC"   # défaut 30 s : un appel bloqué ne doit pas geler la boucle (heartbeat 45 s)


class ClaudeCodeError(RuntimeError):
    """Échec d'un appel headless (exécutable absent, timeout, sortie illisible)."""


@dataclass
class _TextBlock:
    text: str
    type: str = "text"


@dataclass
class _Usage:
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class _Message:
    content: list[_TextBlock]
    usage: _Usage
    model: str
    cost_usd: float = 0.0
    stop_reason: str = "end_turn"


@dataclass
class _ModelRef:
    id: str


@dataclass
class _ModelPage:
    data: list[_ModelRef] = field(default_factory=list)


class _Models:
    """``models.list`` sans API : renvoie les modèles déclarés dans models.yaml.

    Le mode headless n'expose pas d'inventaire ; la vérification officielle
    d'existence n'est donc pas possible avec ce backend. Un identifiant erroné se
    manifestera à l'appel (erreur journalisée, repli déterministe).
    """

    def __init__(self, candidates: list[str]) -> None:
        self._candidates = [c for c in candidates if c and c != "python"]

    def list(self, limit: int = 100) -> _ModelPage:
        return _ModelPage(data=[_ModelRef(id=c) for c in self._candidates[:limit]])


class _Messages:
    def __init__(self, parent: "ClaudeCodeClient") -> None:
        self._p = parent

    def create(self, model: str, max_tokens: int = 800, system: str = "",
               messages: Optional[list[dict]] = None, **_ignored: Any) -> _Message:
        user = "\n\n".join(str(m.get("content", "")) for m in (messages or []))
        raw = self._p._run(model=model, system=system, user=user)
        if raw.get("is_error") or raw.get("subtype") != "success":
            raise ClaudeCodeError(f"claude -p a échoué : {raw.get('subtype')} {raw.get('api_error_status')}")
        u = raw.get("usage", {}) or {}
        return _Message(
            content=[_TextBlock(text=str(raw.get("result", "")))],
            usage=_Usage(input_tokens=int(u.get("input_tokens", 0)),
                         output_tokens=int(u.get("output_tokens", 0)),
                         cache_creation_input_tokens=int(u.get("cache_creation_input_tokens", 0)),
                         cache_read_input_tokens=int(u.get("cache_read_input_tokens", 0))),
            model=model,
            cost_usd=float(raw.get("total_cost_usd", 0.0) or 0.0),
            stop_reason=str(raw.get("stop_reason", "end_turn")),
        )


class ClaudeCodeClient:
    """Client compatible ``LLMClient`` adossé à l'exécutable ``claude``."""

    def __init__(self, candidates: Optional[list[str]] = None, timeout_sec: Optional[float] = None,
                 workdir: Optional[Path] = None) -> None:
        exe = shutil.which("claude") or shutil.which("claude.cmd")
        if not exe:
            raise ClaudeCodeError("exécutable 'claude' introuvable dans le PATH (Claude Code non installé ?)")
        self.exe = exe
        self.timeout = float(timeout_sec or os.environ.get(TIMEOUT_ENV, 30))
        # dossier neutre : aucun CLAUDE.md ni .claude/ du projet ne doit alourdir le prompt
        self.workdir = Path(workdir) if workdir else Path(tempfile.gettempdir()) / "tradinglab-claude"
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.messages = _Messages(self)
        self.models = _Models(candidates or [])

    def _run(self, model: str, system: str, user: str) -> dict:
        args = [self.exe, "-p", "--output-format", "json", "--model", model,
                "--exclude-dynamic-system-prompt-sections",
                "--allowed-tools", "", "--setting-sources", "", "--strict-mcp-config"]
        sys_file: Optional[Path] = None
        if system:
            # passer par un fichier : un prompt long dépasserait la limite de ligne de commande Windows
            sys_file = self.workdir / "system.txt"
            sys_file.write_text(system, encoding="utf-8")
            args += ["--system-prompt-file", str(sys_file)]
        env = dict(os.environ)
        # une clé API présente ferait basculer Claude Code hors abonnement
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        try:
            proc = subprocess.run(args, input=user, capture_output=True, text=True, encoding="utf-8",
                                  timeout=self.timeout, cwd=str(self.workdir), env=env)
        except subprocess.TimeoutExpired as e:
            raise ClaudeCodeError(f"timeout après {self.timeout:.0f}s") from e
        if proc.returncode != 0:
            raise ClaudeCodeError(f"code retour {proc.returncode} : {(proc.stderr or '').strip()[:300]}")
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise ClaudeCodeError(f"sortie JSON illisible : {(proc.stdout or '')[:200]}") from e


def claude_code_backend(settings) -> Optional[Any]:
    """Backend terminal SANS condition d'environnement : utilisé comme SECOURS quand la clé API n'a plus
    de crédit (2026-09-22, demande utilisateur). Renvoie None si l'exécutable ``claude`` est absent."""
    if not shutil.which("claude"):
        return None
    cands = [m for t in (settings.models.get("tiers", {}) or {}).values() for m in t.get("candidates", [])]
    return ClaudeCodeClient(candidates=cands)


def make_llm_backend(settings) -> Optional[Any]:
    """Client SDK à passer au routeur et à ``LLMClient``.

    - ``TRADINGLAB_LLM_BACKEND=claude_code`` → abonnement via l'exécutable ``claude`` ;
    - sinon ``None`` → comportement historique (SDK anthropic + ``ANTHROPIC_API_KEY``).
    """
    if os.environ.get(BACKEND_ENV, "api").strip().lower() != "claude_code":
        return None
    cands = [m for t in (settings.models.get("tiers", {}) or {}).values() for m in t.get("candidates", [])]
    return ClaudeCodeClient(candidates=cands)
