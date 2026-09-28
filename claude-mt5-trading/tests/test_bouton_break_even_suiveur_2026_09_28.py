"""Bouton break-even (2026-09-28, demande utilisateur) : stop juste au-dessus de l'entrée EN PROFIT, au plus près du
prix si le niveau normal est trop proche pour le broker, et stop suiveur armé dès maintenant (sans attendre +1,05 R)."""
from __future__ import annotations

from datetime import datetime, timezone

from tradinglab.core.journal import Journal
from tradinglab.core.state import BotPositionPlan, StateStore
from tradinglab.core.types import OrderRequest, Side
from tradinglab.execution.position_manager import MarketContext, PMConfig, PositionManager

FIXED_NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def _pm(broker, tmp_path):
    store = StateStore(tmp_path / "state")
    store.state.roll_day_if_needed(100000, 100000, FIXED_NOW.date())
    store.state.update_equity(100000, 100000)
    return PositionManager(broker, store, Journal(tmp_path / "logs", component="t"), PMConfig(), 51000), store


def _buy(broker, store, sl_dist=0.0030):
    t = broker.tick("EURUSD")
    r = broker.order_send(OrderRequest("EURUSD", Side.BUY, 0.3, t.ask - sl_dist, magic=51000))
    pos = broker.position(r.ticket)
    plan = BotPositionPlan(pos.ticket, "EURUSD", "BUY", "B01", "c1", pos.price_open, pos.sl, pos.volume, 300.0, 0.3,
                           opened_at=FIXED_NOW.isoformat(), last_sl=pos.sl, regime="TRENDING")
    store.state.bot_positions[str(pos.ticket)] = plan
    store.save()
    return pos, plan


def test_stop_en_profit_et_suivi_arme(broker, tmp_path):
    pm, store = _pm(broker, tmp_path)
    pos, plan = _buy(broker, store)
    spec = broker.symbol_info("EURUSD")
    dist = pos.price_open - pos.sl
    broker.set_price("EURUSD", pos.price_open + 0.6 * dist)          # +0,6 R : sous le seuil du suivi automatique (1,05 R)
    assert pm.move_to_break_even(pos.ticket, spec)
    sl = broker.position(pos.ticket).sl
    assert sl > pos.price_open and plan.break_even_done and plan.trailing_forced and plan.trailing_active
    # « juste en dessous du prix » : au plus près que le broker accepte (distance minimale + 1 tick)
    marge = max(spec.min_stop_distance, spec.tick_size) + spec.tick_size
    assert abs(sl - (broker.tick("EURUSD").bid - marge)) < 2 * spec.tick_size
    # le prix monte : le suivi agit sans attendre +1,05 R et ne descend jamais sous le break-even
    broker.set_price("EURUSD", pos.price_open + 0.9 * dist)
    acts = pm.manage(plan, broker.position(pos.ticket), spec, MarketContext(atr=0.0004, structure_ok=True))
    assert broker.position(pos.ticket).sl >= sl and any("SL" in a for a in acts)
    plan_recharge = BotPositionPlan(**{k: v for k, v in plan.__dict__.items() if k in BotPositionPlan.__dataclass_fields__})
    assert plan_recharge.trailing_forced


def test_prix_trop_pres_du_break_even_stop_au_plus_pres_mais_en_profit(broker, tmp_path):
    pm, store = _pm(broker, tmp_path)
    pos, plan = _buy(broker, store)
    spec = broker.symbol_info("EURUSD")
    dist = pos.price_open - pos.sl
    marge = max(spec.min_stop_distance, spec.tick_size)
    # profit net à peine au-dessus du break-even : le stop se pose au plus près du prix, au-dessus du BE
    broker.set_price("EURUSD", pos.price_open + 0.05 * dist + 1.5 * marge)
    assert pm.move_to_break_even(pos.ticket, spec)
    sl = broker.position(pos.ticket).sl
    assert pos.price_open + 0.05 * dist - 1e-9 <= sl < broker.tick("EURUSD").bid
    # sous le break-even net : refus, rien ne bouge
    pos2, plan2 = _buy(broker, store)
    broker.set_price("EURUSD", pos2.price_open + 0.02 * dist)
    assert pm.move_to_break_even(pos2.ticket, spec) is False and broker.position(pos2.ticket).sl == plan2.initial_sl


def test_pas_encore_en_profit_refus_propre(broker, tmp_path):
    pm, store = _pm(broker, tmp_path)
    pos, plan = _buy(broker, store)
    spec = broker.symbol_info("EURUSD")
    broker.set_price("EURUSD", pos.price_open - 0.0001)
    assert pm.move_to_break_even(pos.ticket, spec) is False
    assert broker.position(pos.ticket).sl == plan.initial_sl and not plan.trailing_forced


def test_stop_deja_en_profit_resserre_encore_et_arme_le_suivi(broker, tmp_path):
    """Un stop déjà en profit (+0,5 R) est resserré juste sous le prix (+1 R) : le bouton verrouille le profit actuel."""
    pm, store = _pm(broker, tmp_path)
    pos, plan = _buy(broker, store)
    spec = broker.symbol_info("EURUSD")
    dist = pos.price_open - pos.sl
    broker.set_price("EURUSD", pos.price_open + 1.0 * dist)
    haut = pos.price_open + 0.5 * dist
    assert broker.modify_position(pos.ticket, haut, 0.0).ok
    plan.last_sl = haut
    assert pm.move_to_break_even(pos.ticket, spec)
    assert broker.position(pos.ticket).sl > haut and plan.trailing_forced
    # stop déjà au plus près du prix : rien à resserrer, le suivi reste armé et rien n'est élargi
    sl = broker.position(pos.ticket).sl
    assert pm.move_to_break_even(pos.ticket, spec) and broker.position(pos.ticket).sl == sl


def test_profit_protege_a_0_16_r_break_even_et_suivi_armes(broker, tmp_path):
    """Demande utilisateur du 28/09 : « à +100 $, mettre directement le stop suiveur » puis « adapte : plus de positions
    avec moins de lots » → seuil en fraction du risque (0,16 R = 100 $ pour 625 $ risqués, 48 $ pour les 300 $ d'ici)."""
    pm, store = _pm(broker, tmp_path)
    pm.cfg = PMConfig(protect_profit_risk_ratio=0.16)
    pos, plan = _buy(broker, store)
    spec = broker.symbol_info("EURUSD")
    dist = pos.price_open - pos.sl
    broker.set_price("EURUSD", pos.price_open + 0.10 * dist)     # +0,10 R : rien
    pm.manage(plan, broker.position(pos.ticket), spec, MarketContext(atr=0.0004, structure_ok=True))
    assert not plan.trailing_forced and broker.position(pos.ticket).sl == plan.initial_sl
    broker.set_price("EURUSD", pos.price_open + 0.40 * dist)     # +0,40 R : protection (bien sous le break-even à 1,0 R)
    acts = pm.manage(plan, broker.position(pos.ticket), spec, MarketContext(atr=0.0004, structure_ok=True))
    assert plan.trailing_forced and plan.trailing_active and any("profit protégé" in a for a in acts)
    assert broker.position(pos.ticket).sl > pos.price_open        # stop en profit dès que le broker l'accepte
    broker.set_price("EURUSD", pos.price_open + 0.9 * dist)
    sl1 = broker.position(pos.ticket).sl
    pm.manage(plan, broker.position(pos.ticket), spec, MarketContext(atr=0.0004, structure_ok=True))
    assert broker.position(pos.ticket).sl >= sl1                  # le suivi ne recule jamais


def test_seuil_absolu_en_argent_optionnel(broker, tmp_path):
    pm, store = _pm(broker, tmp_path)
    pm.cfg = PMConfig(protect_profit_money=100.0)
    pos, plan = _buy(broker, store, sl_dist=0.0100)               # 0,3 lot × 100 pips = 300 $ de risque réel : +100 $ = +0,33 R
    spec = broker.symbol_info("EURUSD")
    dist = pos.price_open - pos.sl
    broker.set_price("EURUSD", pos.price_open + 0.20 * dist)     # ≈ +60 $
    pm.manage(plan, broker.position(pos.ticket), spec, MarketContext(atr=0.0004, structure_ok=True))
    assert not plan.trailing_forced
    broker.set_price("EURUSD", pos.price_open + 0.40 * dist)     # ≈ +120 $
    pm.manage(plan, broker.position(pos.ticket), spec, MarketContext(atr=0.0004, structure_ok=True))
    assert plan.trailing_forced


def test_seuils_a_zero_desactivent(broker, tmp_path):
    pm, store = _pm(broker, tmp_path)
    pm.cfg = PMConfig()
    pos, plan = _buy(broker, store)
    spec = broker.symbol_info("EURUSD")
    broker.set_price("EURUSD", pos.price_open + 0.40 * (pos.price_open - pos.sl))
    pm.manage(plan, broker.position(pos.ticket), spec, MarketContext(atr=0.0004, structure_ok=True))
    assert not plan.trailing_forced and broker.position(pos.ticket).sl == plan.initial_sl


def test_config_reelle_protege_a_0_16_r():
    import yaml
    cfg = yaml.safe_load(open("config/risk.yaml", encoding="utf-8"))["profit_management"]
    pmc = PMConfig.from_config(cfg)
    assert pmc.protect_profit_risk_ratio == 0.5 and pmc.protect_profit_money == 0     # 0,16 → 0,5 le 28/09 au soir


def test_verrou_de_profit_le_stop_ne_redescend_pas_sous_35_dollars(broker, tmp_path):
    """Demande utilisateur du 28/09 : « si ça monte à 40 $, je ne veux pas que ça redescende sous 35 $ ». Risque réel
    300 $ (0,3 lot × 100 pips) : seuil 0,16 R = 48 $, verrou 0,14 R = 42 $ ; le stop se pose au verrou dès le seuil."""
    pm, store = _pm(broker, tmp_path)
    pm.cfg = PMConfig(protect_profit_risk_ratio=0.16, protect_profit_lock_ratio=0.14)
    pos, plan = _buy(broker, store, sl_dist=0.0100)
    spec = broker.symbol_info("EURUSD")
    dist = pos.price_open - pos.sl
    broker.set_price("EURUSD", pos.price_open + 0.20 * dist)     # seuil atteint (0,16 R)
    acts = pm.manage(plan, broker.position(pos.ticket), spec, MarketContext(atr=0.0004, structure_ok=True))
    sl = broker.position(pos.ticket).sl
    assert plan.trailing_forced and sl >= pos.price_open + 0.14 * dist - 1e-9, acts
    broker.set_price("EURUSD", pos.price_open + 0.9 * dist)      # ça monte : le suivi suit, jamais sous le verrou
    pm.manage(plan, broker.position(pos.ticket), spec, MarketContext(atr=0.0004, structure_ok=True))
    assert broker.position(pos.ticket).sl >= sl


def test_verrou_trop_pres_du_prix_stop_au_plus_pres(broker, tmp_path):
    """Seuil à 0,16 R mais broker exigeant 20 points : à +0,165 R le verrou (0,14 R) est à 2,5 pips du prix, trop près.
    Plutôt que d'attendre, le stop se pose au plus près accepté, en profit."""
    pm, store = _pm(broker, tmp_path)
    pm.cfg = PMConfig(protect_profit_risk_ratio=0.16, protect_profit_lock_ratio=0.14)
    pos, plan = _buy(broker, store, sl_dist=0.0100)
    spec = broker.symbol_info("EURUSD")
    dist = pos.price_open - pos.sl
    broker.set_price("EURUSD", pos.price_open + 0.165 * dist)
    acts = pm.manage(plan, broker.position(pos.ticket), spec, MarketContext(atr=0.0004, structure_ok=True))
    sl = broker.position(pos.ticket).sl
    assert plan.trailing_forced and pos.price_open < sl < pos.price_open + 0.14 * dist, acts


def test_config_reelle_verrou_0_14_r():
    import yaml
    cfg = yaml.safe_load(open("config/risk.yaml", encoding="utf-8"))["profit_management"]
    assert PMConfig.from_config(cfg).protect_profit_lock_ratio == 0.25                # 0,14 → 0,25 le 28/09 au soir


def test_gros_profit_le_bouton_verrouille_le_profit_actuel(broker, tmp_path):
    """Demande utilisateur du 28/09 : « les positions qui ont un gros profit : le stop juste en dessous du prix dès qu'on
    clique sur le bouton »."""
    pm, store = _pm(broker, tmp_path)
    pos, plan = _buy(broker, store)
    spec = broker.symbol_info("EURUSD")
    dist = pos.price_open - pos.sl
    broker.set_price("EURUSD", pos.price_open + 2.5 * dist)           # +2,5 R
    assert pm.move_to_break_even(pos.ticket, spec)
    sl = broker.position(pos.ticket).sl
    marge = max(spec.min_stop_distance, spec.tick_size) + spec.tick_size
    assert sl >= broker.tick("EURUSD").bid - marge - 2 * spec.tick_size    # ≈ +2,4 R verrouillés, pas +0,05 R
    assert plan.trailing_forced
