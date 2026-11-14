"""Inscription et état des comptes suiveurs (panneau de contrôle, 2026-09-22).

L'inscription écrit DEUX choses, côté PC uniquement :
- les identifiants dans ``.env`` (préfixe ``COPY<n>_``) — jamais relus par le panneau, jamais renvoyés ;
- la déclaration (nom, préfixe, facteur) dans ``config/copy_trading.yaml``.

La modification du YAML est textuelle (remplacement de ``followers: []`` ou ajout en fin de fichier,
où vit la liste) pour préserver les commentaires du fichier — structure que ce dépôt possède.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

# 2026-09-22, demande utilisateur : noms libres (accents, espaces, majuscules) jusqu'à 40 caractères.
# Restent exclus les caractères qui casseraient le YAML, la ligne de commande ou le journal (guillemets, / \ : …).
NAME_RE = re.compile(r"^[\w àâäéèêëïîôöùûüçÀÂÉÈÊËÏÎÔÖÙÛÜÇ.\-]{2,40}$")
ENV_KEYS = ("LOGIN", "PASSWORD", "SERVER", "TERMINAL_PATH")


def _cfg_file(home: Path) -> Path:
    return Path(home) / "config" / "copy_trading.yaml"


def load_cfg(home: Path) -> dict:
    try:
        return (yaml.safe_load(_cfg_file(home).read_text(encoding="utf-8")) or {}).get("copy_trading", {}) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _env_text(home: Path) -> str:
    try:
        return (Path(home) / ".env").read_text(encoding="utf-8")
    except OSError:
        return ""


def followers_status(home: Path) -> list[dict]:
    """État de chaque suiveur déclaré : identifiants présents ? terminal trouvé ? — sans jamais lire les valeurs secrètes."""
    env = _env_text(home)
    out = []
    for f in load_cfg(home).get("followers", []) or []:
        prefix = str(f.get("env_prefix", ""))
        creds = all(re.search(rf"^{re.escape(prefix)}_MT5_{k}=.+$", env, re.M) for k in ENV_KEYS)
        term = re.search(rf"^{re.escape(prefix)}_MT5_TERMINAL_PATH=(.+)$", env, re.M)
        term_ok = bool(term and Path(term.group(1).strip()).exists())
        out.append({"name": f.get("name"), "enabled": bool(f.get("enabled", False)),
                    "size_factor": float(f.get("size_factor", 1.0)), "env_prefix": prefix,
                    "creds_ok": creds, "terminal_ok": term_ok})
    return out


def ready_names(home: Path) -> list[str]:
    cfg = load_cfg(home)
    if not cfg.get("enabled", False):
        return []
    return [f["name"] for f in followers_status(home) if f["enabled"] and f["creds_ok"] and f["terminal_ok"]]


def _search_dirs() -> list[Path]:
    import os

    # copies portables préparées par l'assistant (C:\Claude-MT5-Trading\mt5-<nom>) en premier : elles sont
    # dédiées au copy trading, contrairement aux installations brandées (serveurs d'un seul broker)
    dirs = [Path(r"C:\Claude-MT5-Trading"), Path(r"C:\Program Files"), Path(r"C:\Program Files (x86)")]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "Programs")
    return dirs


def autodetect_terminal(home: Path, search_dirs: list[Path] | None = None) -> Path | None:
    """Trouve une installation MT5 LIBRE (ni celle du maître, ni celle d'un autre suiveur).

    2026-09-22, demande utilisateur (« chemin du terminal : à toi de gérer automatiquement ») :
    on balaie les emplacements d'installation standards à la recherche d'un ``terminal64.exe``
    non encore attribué. Aucun candidat → None (le message d'erreur guide l'utilisateur)."""
    env = _env_text(home)
    used = {m.strip().strip('"').lower()
            for m in re.findall(r"^(?:COPY\d+_)?MT5_TERMINAL_PATH=(.+)$", env, re.M)}
    for base in (search_dirs if search_dirs is not None else _search_dirs()):
        try:
            candidates = sorted(base.glob("*/terminal64.exe"))
        except OSError:
            continue
        for c in candidates:
            if str(c).lower() not in used:
                return c
    return None


def _slug(name: str) -> str:
    import unicodedata

    base = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-zA-Z0-9]+", "-", base).strip("-").lower()
    return base or "compte"


def _master_data_dir(master_exe: Path) -> Path | None:
    """Dossier de données (%APPDATA%\\MetaQuotes\\Terminal\\<hash>) de l'installation maître : son
    origin.txt contient le chemin d'installation."""
    import os

    root = Path(os.environ.get("APPDATA", "")) / "MetaQuotes" / "Terminal"
    want = str(master_exe.parent).lower()
    try:
        for d in root.iterdir():
            o = d / "origin.txt"
            if not o.exists():
                continue
            for enc in ("utf-16", "utf-8"):
                try:
                    if o.read_text(encoding=enc, errors="ignore").strip().lower() == want:
                        return d
                except OSError:
                    break
    except OSError:
        return None
    return None


def enable_algo_trading(terminal_dir: Path) -> bool:
    """Pré-active le bouton AutoTrading du terminal (2026-09-22, demande utilisateur) : clé
    ``[Experts] Enabled=1`` de ``config/common.ini`` (fichier UTF-16 chez MetaQuotes)."""
    ini = Path(terminal_dir) / "config" / "common.ini"
    try:
        raw = ini.read_bytes() if ini.exists() else b""
    except OSError:
        return False
    enc = "utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8"
    text = raw.decode(enc, errors="replace") if raw else ""
    # Enabled=1 + Account=0 / Profile=0 (2026-09-24) : MT5 coupe Algo Trading à chaque changement de compte ou de
    # profil quand ces options sont cochées (défaut) — c'est ce qui désactivait les nouveaux comptes
    if re.search(r"^\[Experts\]", text, re.M):
        for key, val in (("Enabled", "1"), ("Account", "0"), ("Profile", "0")):
            if re.search(rf"^{key}=.*$", text, re.M):
                text = re.sub(rf"^{key}=.*$", f"{key}={val}", text, count=1, flags=re.M)
            else:
                text = re.sub(r"^\[Experts\][ \t]*$", f"[Experts]\r\n{key}={val}", text, count=1, flags=re.M)
    else:
        text = text.rstrip("\r\n") + "\r\n[Experts]\r\nEnabled=1\r\nAccount=0\r\nProfile=0\r\nAllowDllImport=0\r\n"
    try:
        ini.parent.mkdir(parents=True, exist_ok=True)
        ini.write_bytes(text.encode("utf-16"))
        return True
    except OSError:
        return False


def prepare_portable_terminal(home: Path, name: str) -> Path | None:
    """Fabrique une copie portable DÉDIÉE du terminal maître (C:\\Claude-MT5-Trading\\mt5-<slug>) :
    binaire + profil configuré (l'assistant de premier lancement ne bloque plus l'IPC) + AutoTrading
    pré-activé. Retourne le terminal64.exe de la copie, ou None si le maître est introuvable."""
    import shutil

    env = _env_text(home)
    m = re.search(r"^MT5_TERMINAL_PATH=(.+)$", env, re.M)
    if not m:
        return None
    master_exe = Path(m.group(1).strip().strip('"'))
    if not master_exe.exists():
        return None
    root = Path(r"C:\Claude-MT5-Trading")
    dest = root / f"mt5-{_slug(name)}"
    n = 2
    while dest.exists():
        dest = root / f"mt5-{_slug(name)}-{n}"
        n += 1
    try:
        shutil.copytree(master_exe.parent, dest, ignore=shutil.ignore_patterns("logs", "*.log", "bases", "history"))
        data = _master_data_dir(master_exe)
        if data is not None and (data / "config").exists():
            shutil.copytree(data / "config", dest / "config", dirs_exist_ok=True)
    except OSError:
        return None
    enable_algo_trading(dest)
    return dest / "terminal64.exe"


def register_follower(home: Path, name: str, login: str, password: str, server: str,
                      terminal_path: str, size_factor: float,
                      search_dirs: list[Path] | None = None) -> dict:
    """Valide puis écrit .env + YAML. Lève ``ValueError`` avec un message utilisateur en cas de refus."""
    home = Path(home)
    name = str(name or "").strip()
    if not NAME_RE.match(name):
        raise ValueError("nom invalide : 2 à 40 caractères — lettres (accents ok), chiffres, espaces, points, tirets")
    if any(str(f.get("name", "")).casefold() == name.casefold() for f in load_cfg(home).get("followers", []) or []):
        raise ValueError(f"le suiveur « {name} » existe déjà")
    try:
        int(str(login).strip())
    except ValueError:
        raise ValueError("login MT5 invalide : un nombre est attendu") from None
    if not str(password or "").strip() or not str(server or "").strip():
        raise ValueError("mot de passe et serveur sont obligatoires")
    raw_term = str(terminal_path or "").strip().strip('"')
    if raw_term:
        term = Path(raw_term)
        if not term.exists() or term.suffix.lower() != ".exe":
            raise ValueError("chemin du terminal introuvable : indique le terminal64.exe de l'installation MT5 DÉDIÉE à ce compte, ou laisse vide pour la détection automatique")
    else:
        # 2026-09-22 : par défaut, copie portable DÉDIÉE (fiable quel que soit le broker, AutoTrading
        # pré-activé) ; à défaut, une installation libre existante
        term = prepare_portable_terminal(home, name) if search_dirs is None else None
        if term is None:
            term = autodetect_terminal(home, search_dirs)
        if term is None:
            raise ValueError("aucune installation MT5 utilisable sur ce PC : demande à l'assistant de préparer une copie dédiée à ce compte")
    try:
        sf = float(size_factor)
    except (TypeError, ValueError):
        raise ValueError("facteur de taille invalide") from None
    if not 0.01 <= sf <= 10.0:
        raise ValueError("facteur de taille hors bornes (0.01 à 10)")
    env = _env_text(home)
    used = set(re.findall(r"^COPY(\d+)_MT5_LOGIN=", env, re.M))
    n = 1
    while str(n) in used:
        n += 1
    prefix = f"COPY{n}"
    with open(home / ".env", "a", encoding="utf-8") as f:
        f.write(f"\n# Compte suiveur copy trading « {name} » (ajouté via le panneau)\n")
        f.write(f"{prefix}_MT5_LOGIN={str(login).strip()}\n")
        f.write(f"{prefix}_MT5_PASSWORD={str(password).strip()}\n")
        f.write(f"{prefix}_MT5_SERVER={str(server).strip()}\n")
        f.write(f"{prefix}_MT5_TERMINAL_PATH={term}\n")
    entry = (f"  - name: \"{name}\"\n"
             f"    env_prefix: {prefix}\n"
             f"    size_factor: {sf}\n"
             f"    enabled: true\n")
    cfgf = _cfg_file(home)
    text = cfgf.read_text(encoding="utf-8")
    if re.search(r"^  followers:\s*\[\]\s*$", text, re.M):
        text = re.sub(r"^  followers:\s*\[\]\s*$", "  followers:\n" + entry.rstrip("\n"), text, count=1, flags=re.M)
    else:
        text = text.rstrip("\n") + "\n" + entry
    cfgf.write_text(text, encoding="utf-8")
    return {"name": name, "env_prefix": prefix, "size_factor": sf, "terminal": str(term)}


def set_size_factor(home: Path, name: str, size_factor: float) -> dict:
    """Change le facteur de taille d'un suiveur (panneau, 2026-09-22). Le copieur relit la config à chaque
    cycle : la nouvelle valeur s'applique aux prochaines ouvertures sans redémarrage."""
    try:
        sf = float(size_factor)
    except (TypeError, ValueError):
        raise ValueError("facteur de taille invalide") from None
    if not 0.01 <= sf <= 10.0:
        raise ValueError("facteur de taille hors bornes (0.01 à 10)")
    cfgf = _cfg_file(Path(home))
    text = cfgf.read_text(encoding="utf-8")
    # bloc du suiveur : de sa ligne `- name:` jusqu'au prochain `- name:` (ou la fin)
    pat = re.compile(r'(^  - name: "?' + re.escape(str(name)) + r'"?\s*$)(.*?)(?=^  - name:|\Z)', re.M | re.S)
    m = pat.search(text)
    if not m:
        raise ValueError(f"suiveur inconnu : {name}")
    block = m.group(2)
    if re.search(r"^    size_factor:.*$", block, re.M):
        new_block = re.sub(r"^    size_factor:.*$", f"    size_factor: {sf}", block, count=1, flags=re.M)
    else:
        new_block = f"\n    size_factor: {sf}" + block
    text = text[:m.start(2)] + new_block + text[m.end(2):]
    cfgf.write_text(text, encoding="utf-8")
    return {"name": name, "size_factor": sf}


def remove_follower(home: Path, name: str, remove_env: bool = True) -> dict:
    """Supprime complètement un compte suiveur (panneau, demande utilisateur 2026-09-24) : bloc YAML, fichiers
    d'état du copieur (`copy_status/map/done/lock_<prefix>`) et, par défaut, ses lignes `<prefix>_MT5_*` du .env
    (symétrique de `register_follower`, qui les y a écrites). Ne ferme AUCUNE position : celles déjà copiées restent
    sur le compte avec leur stop et leur TP. Lève ``ValueError`` si le suiveur est inconnu."""
    home = Path(home)
    cfgf = _cfg_file(home)
    text = cfgf.read_text(encoding="utf-8")
    pat = re.compile(r'^  - name: "?' + re.escape(str(name)) + r'"?\s*$.*?(?=^  - name:|^  #|^\S|\Z)', re.M | re.S)
    m = pat.search(text)
    if not m:
        raise ValueError(f"suiveur inconnu : {name}")
    pm = re.search(r"^    env_prefix:\s*(\S+)\s*$", m.group(0), re.M)
    prefix = pm.group(1).strip('"') if pm else ""
    text = text[:m.start()] + text[m.end():]
    if not any(re.match(r"^  - name:", line) for line in text.splitlines()):
        text = re.sub(r"^  followers:\s*$", "  followers: []", text, count=1, flags=re.M)
    cfgf.write_text(text, encoding="utf-8")
    removed_files = []
    if prefix:
        state = home / "state"
        for fn in (f"copy_status_{prefix}.json", f"copy_map_{prefix}.json", f"copy_done_{prefix}.json", f"copy_{prefix}.lock"):
            f = state / fn
            if f.exists():
                try:
                    f.unlink()
                    removed_files.append(fn)
                except OSError:
                    pass
    env_lines = 0
    envf = home / ".env"
    if remove_env and prefix and envf.exists():
        lines = envf.read_text(encoding="utf-8").splitlines(keepends=True)
        keep = []
        for line in lines:
            if line.startswith(f"{prefix}_MT5_"):
                env_lines += 1
                continue
            if line.startswith("# Compte suiveur copy trading") and f"« {name} »" in line:
                continue
            keep.append(line)
        tmp = envf.with_suffix(".tmp")
        tmp.write_text("".join(keep), encoding="utf-8")
        import os as _os
        _os.replace(tmp, envf)
    return {"name": name, "env_prefix": prefix, "state_files_removed": removed_files, "env_lines_removed": env_lines}


def follower_size_factor(home: Path, name: str, default: float = 1.0) -> float:
    for f in load_cfg(home).get("followers", []) or []:
        if f.get("name") == name:
            try:
                return float(f.get("size_factor", default))
            except (TypeError, ValueError):
                return default
    return default
