"""2026-10-01 : le groupe Telegram de l'utilisateur a été converti en supergroupe ; tous les envois étaient refusés en
silence. Le notifieur suit désormais l'identifiant de migration renvoyé par Telegram et le mémorise (jamais dans .env)."""
from __future__ import annotations

import io
import json
import urllib.error

from tradinglab.monitoring import telegram_notifier as tn


class _Rep:
    def __init__(self, corps):
        self.corps = corps

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self.corps).encode()


def test_suit_la_migration_en_supergroupe(tmp_path, monkeypatch):
    appels = []

    def urlopen(req, timeout=0):
        chat = dict(x.split("=", 1) for x in req.data.decode().split("&"))["chat_id"]
        appels.append(chat)
        if chat == "-4000":
            corps = {"ok": False, "error_code": 400, "description": "Bad Request: group chat was upgraded to a supergroup chat",
                     "parameters": {"migrate_to_chat_id": -1004000}}
            raise urllib.error.HTTPError("https://api.telegram.org/botSECRET/sendMessage", 400, "Bad Request", {},
                                         io.BytesIO(json.dumps(corps).encode()))
        return _Rep({"ok": True})

    monkeypatch.setattr(tn.urllib.request, "urlopen", urlopen)
    etat = tmp_path / "telegram_chat.json"
    s = tn.TelegramSender("SECRET", "-4000", state_file=etat)
    assert s.send("bonjour") is True
    assert appels == ["-4000", "-1004000"] and s.chat_id == "-1004000"
    assert json.loads(etat.read_text())["chat_migre"] == {"-4000": "-1004000"}
    s2 = tn.TelegramSender("SECRET", "-4000", state_file=etat)          # redémarrage : nouvel identifiant d'emblée
    assert s2.chat_id == "-1004000" and s2.send("re") is True and appels[-1] == "-1004000"


def test_echec_journalise_sans_le_jeton(tmp_path, monkeypatch, capsys):
    def urlopen(req, timeout=0):
        raise urllib.error.HTTPError("https://api.telegram.org/botSECRET/sendMessage", 403, "Forbidden", {},
                                     io.BytesIO(json.dumps({"ok": False, "description": "Forbidden: bot was blocked by the user"}).encode()))

    monkeypatch.setattr(tn.urllib.request, "urlopen", urlopen)
    assert tn.TelegramSender("SECRET", "42", state_file=tmp_path / "x.json").send("x") is False
    err = capsys.readouterr().err
    assert "403" in err and "blocked" in err and "SECRET" not in err
