"""2026-10-01, demandes utilisateur :
- « la session Sydney je la vois jamais » : SYDNEY = 22:00-00:00 UTC en semaine (hors crypto) ; 21:00-22:00 UTC (changement
  de jour) reste OFF ; aucun agent existant ne liste SYDNEY (rien ne change en live) ;
- « aligne la recherche sur le live » : en intraday, la session se juge à la CLÔTURE de la bougie et le filtre est strict
  (plus aucun signal dans la plage OFF) ; H4 / D1 inchangés (pris au premier scan permis) ;
- « en Asie mets les agents en live, pas sur papier » : exception ASIE à « forex court terme en papier »."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd

from tradinglab.core.clock import current_session
from tradinglab.core.types import Session
from tradinglab.research.adapters import make_signal_fn, masque_sessions, sessions_bougies

from test_fastsig_2026_09_29 import SPEC_SYM, _spec, _synth

MERCREDI = datetime(2026, 9, 30, tzinfo=timezone.utc)


def test_session_sydney_en_semaine():
    assert current_session(MERCREDI.replace(hour=21, minute=30)) is Session.OFF, "changement de jour : jamais tradé"
    assert current_session(MERCREDI.replace(hour=22, minute=0)) is Session.SYDNEY
    assert current_session(MERCREDI.replace(hour=23, minute=59)) is Session.SYDNEY
    assert current_session(MERCREDI.replace(hour=0, minute=0)) is Session.ASIA
    assert current_session(MERCREDI.replace(hour=22, minute=30), round_the_clock=True) is Session.ASIA, "crypto inchangée"
    samedi = datetime(2026, 10, 3, 22, 30, tzinfo=timezone.utc)
    assert current_session(samedi) is Session.OFF, "week-end : forex fermé"


def test_aucun_agent_existant_ne_trade_sydney():
    from pathlib import Path
    from tradinglab.agents.registry import AgentRegistry

    reg = AgentRegistry(status_file=Path("__inexistant__.json"))
    assert not [a.agent_id for a in reg.agents.values() if Session.SYDNEY.value in (a.sessions or [])]


def test_session_jugee_a_la_cloture_et_filtre_strict():
    t = pd.DatetimeIndex([pd.Timestamp("2026-09-30 20:45", tz="UTC"), pd.Timestamp("2026-09-30 21:45", tz="UTC"),
                          pd.Timestamp("2026-09-30 23:45", tz="UTC"), pd.Timestamp("2026-09-30 21:00", tz="UTC")])
    m15 = sessions_bougies(t, "M15")
    assert list(m15) == ["OFF", "SYDNEY", "ASIA", "OFF"]            # clôtures 21:00, 22:00, 00:00, 21:15
    jour = ["ASIA", "LONDON", "NEWYORK", "OVERLAP_LDN_NY"]
    assert list(masque_sessions(m15, jour, "M15")) == [False, False, True, False]
    assert list(masque_sessions(m15, ["SYDNEY"], "M15")) == [False, True, False, False]
    d1 = sessions_bougies(pd.DatetimeIndex([pd.Timestamp("2026-09-29 21:00", tz="UTC")]), "D1")
    assert bool(masque_sessions(d1, jour, "D1")[0]), "D1 ouverte à 21 h UTC : prise au premier scan permis, comme avant"


def test_plus_aucun_signal_intraday_hors_session():
    df = _synth(2500, 6)
    fn = make_signal_fn(_spec("ema_trend", {}, "M15", "H1"), SPEC_SYM, "M15")
    fn.prepare(df)
    s = fn.fast_arrays()[0]
    sess = sessions_bougies(df["time"], "M15")
    assert not np.any(s[np.isin(sess, ["OFF", "SYDNEY"])] != 0)
    lent = make_signal_fn(_spec("ema_trend", {}, "M15", "H1"), SPEC_SYM, "M15", fast=False)
    lent.prepare(df)
    for i in np.nonzero(np.isin(sess, ["OFF", "SYDNEY"]))[0][:40]:
        if i >= fn.required_bars:
            assert lent(df.iloc[: i + 1]) is None


def _orch(heure_session: str):
    from tradinglab.orchestration.orchestrator import Orchestrator

    o = Orchestrator.__new__(Orchestrator)
    o.s = SimpleNamespace(execution={"forex_short_term_paper_only": True, "forex_short_term_live_sessions": ["ASIA"]})
    o.broker = SimpleNamespace(symbol_info=lambda s: SimpleNamespace(asset_class="forex", trade_allowed=True))
    o.registry = SimpleNamespace(get=lambda aid: None)
    c = SimpleNamespace(agent_id="B03", symbol="EURGBP", timeframes=["M15", "H1"], session=heure_session)
    return o, c


def test_forex_court_terme_en_live_pendant_l_asie_seulement():
    o, c = _orch("ASIA")
    assert not o._forex_court_terme(c), "Asie : en live"
    for ses in ("LONDON", "NEWYORK", "OVERLAP_LDN_NY", "SYDNEY"):
        o, c = _orch(ses)
        assert o._forex_court_terme(c), f"{ses} : toujours en papier"


def test_configuration_asie_en_live():
    import yaml

    ex = yaml.safe_load(open("config/system.yaml", encoding="utf-8"))["execution"]
    # 01/10 plus tard : « sur tous les marchés en réel » → règle papier levée partout (l'exception Asie reste pour un retour arrière)
    assert ex["forex_short_term_paper_only"] is False and ex["forex_short_term_live_sessions"] == ["ASIA"]


def test_config_reelle_forex_court_terme_en_live_partout():
    """01/10, « sur tous les marchés en réel » : avec la configuration du dépôt, plus aucun signal forex court terme en papier."""
    import yaml

    o, _ = _orch("LONDON")
    o.s = SimpleNamespace(execution=yaml.safe_load(open("config/system.yaml", encoding="utf-8"))["execution"])
    for ses in ("ASIA", "LONDON", "NEWYORK", "OVERLAP_LDN_NY"):
        c = SimpleNamespace(agent_id="B03", symbol="EURGBP", timeframes=["M5", "H1"], session=ses)
        assert not o._forex_court_terme(c), ses
