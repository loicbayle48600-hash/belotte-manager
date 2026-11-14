"""Stop du suiveur élargi du surcroît de spread (2026-09-28, décision utilisateur).

SILVER chez les démos IC : spread 82 points contre 11 sur XAGUSD du maître. Les quatre copies argent ont pris le stop
sur un pic que le maître n'a pas vu. Le stop du suiveur est élargi de (spread suiveur − spread maître), côté défavorable,
mémorisé à l'ouverture, et les mises à jour du stop maître sont comparées après élargissement (pas de boucle)."""
from __future__ import annotations

from types import SimpleNamespace

from tradinglab.copy.copier import COPY_COMMENT_PREFIX, CopyAction, CopyTrader, plan_sync
from tradinglab.core.types import Side


class _Spec:
    volume_min, volume_step, volume_max, digits, point = 0.01, 0.01, 100.0, 3, 0.001


class _FPos:
    def __init__(self, ticket, master_ticket, symbol, volume, sl, tp):
        self.ticket, self.symbol, self.volume, self.sl, self.tp = ticket, symbol, volume, sl, tp
        self.comment = f"{COPY_COMMENT_PREFIX}{master_ticket}"


def _copier(bid, ask):
    c = CopyTrader.__new__(CopyTrader)
    c.broker = SimpleNamespace(tick=lambda s: SimpleNamespace(bid=bid, ask=ask), symbol_info=lambda s: _Spec())
    c.logs = []
    c._log = lambda msg, **k: c.logs.append((msg, k))
    return c


def test_vente_stop_eleve_du_surcroit_de_spread():
    c = _copier(61.508, 61.590)                                  # SILVER : spread 0,082
    a = CopyAction("open", symbol="SILVER", master_symbol="XAGUSD", side=Side.SELL, sl=61.804, tp=60.07,
                   master_ticket=1, master_spread=0.011)          # XAGUSD : spread 0,011
    c._widen_for_spread(a)
    assert a.spread_offset == 0.071 and a.sl == 61.875 and a.tp == 60.07
    assert c.logs[0][0] == "stop élargi du surcroît de spread du suiveur"


def test_achat_stop_abaisse():
    c = _copier(61.508, 61.590)
    a = CopyAction("open", symbol="SILVER", side=Side.BUY, sl=60.90, tp=62.5, master_ticket=1, master_spread=0.011)
    c._widen_for_spread(a)
    assert a.sl == 60.829 and a.spread_offset == 0.071


def test_spread_suiveur_pas_plus_large_rien_ne_change():
    c = _copier(61.508, 61.519)                                  # même spread que le maître
    a = CopyAction("open", symbol="SILVER", side=Side.SELL, sl=61.804, master_ticket=1, master_spread=0.011)
    c._widen_for_spread(a)
    assert a.sl == 61.804 and a.spread_offset == 0.0 and not c.logs
    b = CopyAction("modify", symbol="SILVER", side=Side.SELL, sl=61.804, master_ticket=1, master_spread=0.011)
    _copier(61.508, 61.590)._widen_for_spread(b)                 # jamais à la modification : mémorisé à l'ouverture
    assert b.sl == 61.804


def test_mise_a_jour_du_stop_maitre_comparee_apres_elargissement():
    master = {"ts_utc": "2026-09-28T12:00:00+00:00", "equity": 500000.0, "magic": 51000,
              "positions": [{"ticket": 1, "symbol": "XAGUSD", "side": "SELL", "volume": 1.06, "sl": 61.804, "tp": 60.07,
                             "price_open": 61.226}]}
    specs = {"SILVER": _Spec()}
    mapping = {1: {"ticket": 9, "mv": 1.06, "fv": 0.21, "spread_offset": 0.071}}
    suiveur = [_FPos(9, 1, "SILVER", 0.21, 61.875, 60.07)]
    assert plan_sync(master, suiveur, 100000.0, 1.0, specs, mapping=mapping, sym_map={"XAGUSD": "SILVER"}) == []
    master["positions"][0]["sl"] = 61.30                         # le maître abaisse son stop
    acts = plan_sync(master, suiveur, 100000.0, 1.0, specs, mapping=mapping, sym_map={"XAGUSD": "SILVER"})
    assert [(a.kind, a.sl) for a in acts] == [("modify", 61.371)]
