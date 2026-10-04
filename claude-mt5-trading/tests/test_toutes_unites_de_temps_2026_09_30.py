"""2026-09-30, demande utilisateur : « continuer sur l'or en M1, M5, M15, H1, H2, H4, D1, W1, MN et pour tous les autres
aussi, sur tous les temps possibles ».

- horloge : barres H2 (alignées sur minuit serveur) et MN1 (1er du mois serveur, calendrier réel) ;
- recherche : la tendance mensuelle est clôturée au 1er du mois suivant, jamais après une durée fixe de 30 jours
  (sinon un mois de 31 jours serait lu un jour trop tôt : lookahead) ; tirages et passage « toutes UT » ;
- bot : le flux sert l'unité de temps d'un agent (ex. H2, W1) sur SES marchés seulement."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from tradinglab.backtest import fastsig
from tradinglab.core import clock
from tradinglab.research.adapters import make_signal_fn, required_bars

from test_fastsig_2026_09_29 import PARAMS, SPEC_SYM, _spec, _synth

UTC = timezone.utc


def test_horloge_h2_et_mois_calendaire():
    off = 3 * 3600                                                     # serveur UTC+3 (été)
    ts = datetime(2026, 9, 15, 12, 30, tzinfo=UTC)
    assert clock.bar_open_time(ts, "H2", off) == datetime(2026, 9, 15, 11, 0, tzinfo=UTC)
    assert clock.bar_open_time(ts, "MN1", off) == datetime(2026, 8, 31, 21, 0, tzinfo=UTC)
    assert clock.bar_open_time(datetime(2026, 8, 31, 22, 0, tzinfo=UTC), "MN1", off) == datetime(2026, 8, 31, 21, 0, tzinfo=UTC)
    assert clock.bar_open_time(datetime(2026, 8, 31, 20, 0, tzinfo=UTC), "MN1", off) == datetime(2026, 7, 31, 21, 0, tzinfo=UTC)
    # passage d'année, serveur UTC+2 (hiver)
    assert clock.next_month_open(datetime(2026, 11, 30, 22, 0, tzinfo=UTC), 2 * 3600) == datetime(2026, 12, 31, 22, 0, tzinfo=UTC)
    avant = clock.SERVER_UTC_OFFSET_SEC
    try:
        clock.set_server_utc_offset(off)
        now = datetime(2026, 9, 30, 20, 0, tzinfo=UTC)
        assert clock.seconds_until_next_bar("MN1", now) == 3600.0      # 1er octobre 00:00 serveur = 21:00 UTC
        nouveau, cur = clock.is_new_bar("MN1", datetime(2026, 8, 31, 21, 0, tzinfo=UTC), now)
        assert not nouveau and cur == datetime(2026, 8, 31, 21, 0, tzinfo=UTC)
        assert clock.is_new_bar("MN1", cur, now + timedelta(hours=2))[0]
    finally:
        clock.set_server_utc_offset(avant)


def _strat():
    return next(s for s in sorted(fastsig.FAST) if s in PARAMS)


def test_tendance_mensuelle_cloturee_au_premier_du_mois_suivant():
    df = _synth(3000, 7, "1D")
    spec = _spec(_strat(), PARAMS[_strat()][0], "D1", "MN1")
    fn = make_signal_fn(spec, SPEC_SYM, "D1", fast=True)
    fn.prepare(df)
    assert fn.data_error is None
    ctx = fn.fast_ctx()
    mois = pd.DatetimeIndex(df.set_index("time").resample("MS", label="left", closed="left")["close"].last().dropna().index)
    fins = mois + pd.offsets.MonthBegin(1)
    clot = pd.DatetimeIndex(df["time"]) + pd.Timedelta(days=1)          # clôture de chaque bougie D1
    attendu = fins.searchsorted(clot, side="right")
    assert np.array_equal(np.asarray(ctx.lt_idx) + 1, attendu)
    # aucune barre mensuelle lue avant sa vraie fin : ni un jour trop tôt dans les mois de 31 jours
    for i in range(len(df)):
        n = int(attendu[i])
        if n:
            assert fins[n - 1] <= clot[i]
    trente_et_un = [m for m in mois[:-1] if (m + pd.offsets.MonthBegin(1) - m).days == 31]
    assert trente_et_un
    m = trente_et_un[0]
    i = int(np.searchsorted(df["time"].values, np.datetime64((m + pd.Timedelta(days=29)).tz_convert(None))))   # bougie du 30
    k = int(mois.get_loc(m))
    assert ctx.lt_idx[i] < k, "le mois ne doit pas être clôturé avant la bougie du 31"


def test_equivalence_rapide_lente_sur_les_nouvelles_ut():
    s = _strat()
    for df, entry, trend in ((_synth(3200, 8, "4h"), "H4", "W1"), (_synth(2600, 9, "1D"), "D1", "MN1"),
                             (_synth(1600, 10, "2h"), "H2", "D1"), (_synth(1400, 11, "1min"), "M1", "M15")):
        spec = _spec(s, PARAMS[s][0], entry, trend)
        lent = make_signal_fn(spec, SPEC_SYM, entry, fast=False)
        rapide = make_signal_fn(spec, SPEC_SYM, entry, fast=True)
        lent.prepare(df)
        rapide.prepare(df)
        assert lent.data_error is None, (entry, trend, lent.data_error)
        ecarts = fastsig.compare_signals(lent, rapide, df, start=max(250, len(df) - 400))
        assert not ecarts, (entry, trend, ecarts[:1])


def test_recherche_toutes_ut():
    from tradinglab.research import massive as m

    assert "M1" not in {e for e, _ in m.TF_ALEATOIRES}, "pas de M1 en continu : téléchargements lourds sur le terminal du bot"
    assert "M1" in {e for e, _ in m.TF_TOUTES}
    for paires in (m.TF_ALEATOIRES, m.TF_TOUTES):
        entrees = {e for e, _ in paires}
        tendances = {t for _, t in paires}
        assert {"H2", "W1"} <= entrees and "MN1" in tendances
        assert "MN1" not in entrees, "jamais d'entrée mensuelle : trop peu d'historique"
        for e, t in paires:
            assert e in m.BARS, e
            assert required_bars(e, t) <= m.BARS[e], (e, t)
    for e, _ in m.TF_TOUTES:
        assert e in m.BARS
    assert max(m.BARS.values()) < 100000, "MT5 refuse une demande égale à sa limite de 100 000 barres"


def test_flux_sert_les_ut_des_agents(settings, broker):
    from tradinglab.agents.registry import AgentSpec
    from tradinglab.core.types import AgentStatus

    from test_review_market_agents_orchestration import make_orch

    o = make_orch(settings, broker)
    assert "H2" not in o.feed.timeframes
    o.registry.add(AgentSpec(agent_id="XUT1", family="X", name="ut", strategy=_strat(), markets=["EURUSD"],
                             sessions=["LONDON", "NEWYORK", "ASIA", "OVERLAP_LDN_NY", "OFF"],
                             timeframes={"entry": "H2", "trend": "W1"}, regimes=["TRENDING"], params={},
                             base_strategy=_strat()))
    o.registry.set_status("XUT1", AgentStatus.SHADOW)
    ajouts = o._sync_feed_timeframes()
    reel = o.universe["EURUSD"]
    assert reel in ajouts["H2"] and reel in o.feed.extra_timeframes["H2"] and reel in o.feed.extra_timeframes["W1"]
    autres = [r for k, r in o.universe.items() if k != "EURUSD"]
    assert not any(r in o.feed.extra_timeframes["H2"] for r in autres), "uniquement les marchés de l'agent"
    assert o._sync_feed_timeframes() == {}, "idempotent"
    snap = o.feed.snapshot(reel)
    assert "H2" in snap.frames and "W1" in snap.frames


def test_h2_et_w1_construits_sans_telechargement(tmp_path):
    """La recherche construit H2 depuis H1 et W1 depuis D1 : elle ne demande jamais H2 / W1 au terminal du bot."""
    from tradinglab.research import massive as m

    demandes = []

    class Faux:
        def rates(self, symbol, tf, n):
            demandes.append(tf)
            pas = {"H1": "1h", "D1": "1D", "H2": "2h", "W1": "7D"}[tf]
            return _synth(400, 3, pas)

    h2 = m.charger_ut(Faux(), "XAUUSD", "H2", tmp_path)
    w1 = m.charger_ut(Faux(), "XAUUSD", "W1", tmp_path)
    assert demandes == ["H1", "D1"]
    assert (h2["time"].diff().dropna() == pd.Timedelta(hours=2)).all() and len(h2) == 200
    assert (w1["time"].dt.dayofweek == 6).all(), "semaines du dimanche, comme MT5"
    m.charger_ut(Faux(), "XAUUSD", "H2", tmp_path / "b", reelles=True)
    assert demandes[-1] == "H2"
