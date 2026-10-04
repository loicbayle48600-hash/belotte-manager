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



def test_shadow_semi_long_garde_le_temps_de_son_unite(tmp_path):
    """01/10, « trades semi-longs » : une position shadow H4 / D1 / W1 n'est plus coupée à 72 h comme un trade intraday."""
    from tradinglab.shadow.shadow import ShadowPosition, delai_max_heures

    assert delai_max_heures("M15") == 72.0 and delai_max_heures("H1") == 72.0 and delai_max_heures("") == 72.0
    assert delai_max_heures("H4") == 240.0 and delai_max_heures("D1") == 480.0 and delai_max_heures("W1") == 1440.0
    ancienne = ShadowPosition(id="x", agent_id="O01", symbol="XAUUSD", side="BUY", entry=1.0, sl=0.9, tp=1.2,
                              opened_at="2026-09-30T00:00:00+00:00", regime="TRENDING", session="LONDON", setup_score=70.0)
    assert ancienne.entry_tf == "", "positions déjà enregistrées : relues sans erreur, délai intraday par défaut"


def test_stop_suiveur_semi_long_sur_l_atr_de_son_unite():
    """01/10 : agents H4 / D1 / W1 → stop suiveur calé sur l'ATR de leur unité (comme leurs backtests) ; intraday → ATR H1."""
    from tradinglab.orchestration.orchestrator import Orchestrator

    o = Orchestrator.__new__(Orchestrator)
    tfs = {"SW1": "D1", "IN1": "M15"}
    o.registry = SimpleNamespace(get=lambda aid: SimpleNamespace(timeframes={"entry": tfs[aid]}) if aid in tfs else None)
    d1 = pd.DataFrame({"atr14": [9.0, 9.5, 10.0, 11.0]})
    snap = SimpleNamespace(frames={"D1": d1}, atr_h1=2.0)
    plan = lambda aid: SimpleNamespace(agent_id=aid, entry=100.0, initial_sl=85.0)      # noqa: E731
    assert o._atr_gestion(plan("SW1"), snap) == 10.0, "dernière bougie D1 clôturée"
    assert o._atr_gestion(plan("IN1"), snap) == 2.0, "intraday : ATR H1 inchangé"
    assert o._atr_gestion(plan("SW1"), SimpleNamespace(frames={}, atr_h1=2.0)) == 2.0, "unité absente : repli ATR H1"
    assert o._atr_gestion(plan("SW1"), None) == 15.0, "sans données : distance du stop initial"
