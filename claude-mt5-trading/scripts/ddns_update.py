"""Mise à jour DDNS No-IP pour tradingdu48.ddns.net (2026-09-22, accès extérieur au dashboard).

Remplace le client DUC de No-IP : appelé par une tâche planifiée Windows toutes les 10 minutes.
Identifiants = « DDNS Key » No-IP (droits limités à la mise à jour de ce nom d'hôte, rien d'autre).
Ne fait AUCUNE mise à jour si l'IP publique n'a pas changé depuis le dernier appel (cache disque),
pour respecter la politique anti-abus de No-IP.
"""
from __future__ import annotations

import base64
import sys
import urllib.request
from pathlib import Path

HOSTNAME = "tradingdu48.ddns.net"
DDNS_USER = "gcdp4eq"
DDNS_PASS = "9PFNqPXNsgyh"
CACHE = Path(__file__).resolve().parents[1] / "state" / "ddns_last_ip.txt"


def main() -> int:
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=15) as r:
            ip = r.read().decode().strip()
    except Exception as e:  # noqa: BLE001 - hors ligne : on réessaiera au prochain passage
        print(f"ip publique indisponible: {type(e).__name__}", file=sys.stderr)
        return 0
    last = CACHE.read_text(encoding="utf-8").strip() if CACHE.exists() else ""
    if ip == last:
        return 0
    auth = base64.b64encode(f"{DDNS_USER}:{DDNS_PASS}".encode()).decode()
    req = urllib.request.Request(
        f"https://dynupdate.no-ip.com/nic/update?hostname={HOSTNAME}&myip={ip}",
        headers={"Authorization": f"Basic {auth}", "User-Agent": "tradinglab-ddns/1.0 loic.bayle48600@gmail.com"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            resp = r.read().decode().strip()
    except Exception as e:  # noqa: BLE001
        print(f"mise à jour échouée: {type(e).__name__}", file=sys.stderr)
        return 1
    if resp.startswith(("good", "nochg")):
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(ip, encoding="utf-8")
        return 0
    print(f"réponse No-IP inattendue: {resp}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
