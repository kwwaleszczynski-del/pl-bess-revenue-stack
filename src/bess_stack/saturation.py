"""How many megawatts of batteries can the Polish reserve markets absorb, and what happens to prices and revenue
when they fill up?

The model uses the anonymous offer stacks that PSE publishes for the basic (day-ahead) balancing capacity
procurement (report popmb-rmb): for every hour and product, the offered MW and offer prices. Sorting the offers
by price and cutting at the MW bought reproduces the published clearing price (pay-as-cleared) in almost every
hour; ``validate`` measures this.

Assumptions (all explicit, see README):
- demand per hour and product stays as bought in the reference period (no change of the dimensioning rules);
- a battery fleet of F MW enters the stack as one block bidding ``bid`` PLN/MW per hour, below the existing offers,
  which stay unchanged (nobody leaves the market or changes prices);
- products are cleared one by one (the joint optimisation of the procurement and substitution between aFRR and
  mFRRd for the FRR demand are ignored);
- when battery offers exceed what can be accepted, every battery gets the same pro-rata share;
- energy arbitrage prices are not affected by the fleet (arbitrage saturation is a separate question).

Two views:
1. ``product_curves``: a block of V MW offered in a single product every hour; mean clearing price and revenue per
   MW of the block as V grows.
2. ``fleet_equilibrium``: F MW of identical batteries that each run the stacking optimiser. Their offers lower the
   prices, which changes what each battery offers; the loop is repeated with damping until it settles.

Usage:
    st = build_stacks(offers, cap_volume, cap_price)
    curves = product_curves(st, np.arange(0, 2001, 50))
    eq = fleet_equilibrium(days, st, cfg, [0, 500, 1000, 2000])
"""
from __future__ import annotations

import logging
from concurrent.futures import ProcessPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass

import numpy as np
import pandas as pd

from bess_stack.backtest import Day, RunConfig, default_workers, run_configs
from bess_stack.pse_api import PRODUCTS

log = logging.getLogger(__name__)


@dataclass
class Stacks:
    hours: pd.DatetimeIndex                 # UTC hour starts (rows)
    price: dict[str, np.ndarray]            # product -> hours x M offer prices sorted ascending, padded with NaN
    cum: dict[str, np.ndarray]              # product -> hours x M cumulative MW, padded with the total
    demand: pd.DataFrame                    # hours x PRODUCTS, MW bought (0 when nothing was bought)
    actual: pd.DataFrame                    # hours x PRODUCTS, published clearing price


def build_stacks(offers: pd.DataFrame, cap_volume: pd.DataFrame, cap_price: pd.DataFrame) -> Stacks:
    """Offer stacks for the hours that have offers, bought volumes and prices."""
    hours = pd.DatetimeIndex(sorted(set(offers.index) & set(cap_volume.index) & set(cap_price.index)),
                             name="ts_utc")
    demand = cap_volume.reindex(hours)[PRODUCTS].fillna(0.0)
    actual = cap_price.reindex(hours)[PRODUCTS]
    price, cum = {}, {}
    pos = pd.Series(np.arange(len(hours)), index=hours)
    for k in PRODUCTS:
        o = offers[(offers["product"] == k) & (offers["mw"] > 0)]
        o = o[o.index.isin(hours)]
        if o.empty:
            price[k] = np.full((len(hours), 1), np.nan)
            cum[k] = np.zeros((len(hours), 1))
            continue
        row = pos.reindex(o.index).to_numpy()
        frame = pd.DataFrame({"row": row, "price": o["price"].to_numpy(float), "mw": o["mw"].to_numpy(float)})
        frame = frame.sort_values(["row", "price"], kind="stable")
        rank = frame.groupby("row").cumcount().to_numpy()
        m = int(rank.max()) + 1
        p = np.full((len(hours), m), np.nan)
        q = np.zeros((len(hours), m))
        p[frame["row"].to_numpy(), rank] = frame["price"].to_numpy()
        q[frame["row"].to_numpy(), rank] = frame["mw"].to_numpy()
        price[k], cum[k] = p, np.cumsum(q, axis=1)
    return Stacks(hours, price, cum, demand, actual)


def clear(st: Stacks, product: str, battery_mw, bid: float = 0.0, rows=None) -> tuple[np.ndarray, np.ndarray]:
    """Clearing price and accepted battery MW per hour when ``battery_mw`` (scalar or per hour) is offered at
    ``bid``. Hours without demand get price NaN and no acceptance; hours without enough offers clear at the most
    expensive offer."""
    rows = np.arange(len(st.hours)) if rows is None else np.asarray(rows)
    p, q = st.price[product][rows], st.cum[product][rows]
    d = st.demand[product].to_numpy()[rows]
    v = np.broadcast_to(np.asarray(battery_mw, float), d.shape)
    below = np.where(p < bid, np.diff(np.concatenate([np.zeros((len(rows), 1)), q], axis=1), axis=1), 0.0).sum(axis=1)
    x = d - v                                      # demand left for the existing offers
    covered = q >= (x[:, None] - 1e-9)
    has = covered.any(axis=1)
    idx = np.where(has, covered.argmax(axis=1), 0)
    marginal = p[np.arange(len(rows)), idx]
    last = np.nanmax(np.where(np.isnan(p), -np.inf, p), axis=1)
    marginal = np.where(has, marginal, last)       # shortage: most expensive offer sets the price
    saturated = x <= below
    price = np.where(saturated, bid, np.maximum(marginal, bid))
    accepted = np.where(saturated, np.clip(d - below, 0.0, v), v)
    none = d <= 0
    price = np.where(none, np.nan, price)
    accepted = np.where(none, 0.0, accepted)
    return price, accepted


def validate(st: Stacks, tol: float = 0.01) -> pd.DataFrame:
    """Share of hours in which the offer stack reproduces the published clearing price (within ``tol`` PLN)."""
    rows = []
    for k in PRODUCTS:
        model, _ = clear(st, k, 0.0)
        act = st.actual[k].to_numpy()
        ok = np.isfinite(act) & np.isfinite(model)
        diff = np.abs(model[ok] - act[ok])
        rows.append({"product": k, "hours": int(ok.sum()),
                     "match_share": float((diff <= tol).mean()) if ok.any() else np.nan,
                     "mean_abs_diff": float(diff.mean()) if ok.any() else np.nan})
    return pd.DataFrame(rows).set_index("product")


def product_curves(st: Stacks, volumes, bid: float = 0.0) -> pd.DataFrame:
    """For a block of V MW offered every hour in one product: mean clearing price (hours with demand), revenue per
    MW of the block (PLN/MW/year, hours without demand earn nothing) and the share of the block accepted."""
    rows = []
    for k in PRODUCTS:
        for vol in volumes:
            price, acc = clear(st, k, float(vol), bid)
            has = np.isfinite(price)
            if vol > 0:
                rev = np.where(has, price * acc / vol, 0.0)
                share = float(acc.sum() / (vol * len(acc)))
            else:
                rev, share = np.where(has, price, 0.0), 1.0
            rows.append({"product": k, "battery_mw": float(vol),
                         "mean_price": float(np.nanmean(price)) if has.any() else np.nan,
                         "revenue_per_mw_year": float(rev.mean() * 8760), "accepted_share": share})
    return pd.DataFrame(rows).set_index(["product", "battery_mw"])


def market_room(st: Stacks, curves: pd.DataFrame) -> pd.DataFrame:
    """Per product: MW bought (mean, 5th and 95th percentile over hours with demand) and the battery block at which
    the mean clearing price falls to half and to a tenth of today's level."""
    rows = []
    for k in PRODUCTS:
        d = st.demand[k]
        d = d[d > 0]
        c = curves.loc[k]
        p0 = c["mean_price"].iloc[0]

        def first_below(frac: float) -> float:
            hit = c.index[c["mean_price"] <= frac * p0]
            return float(hit[0]) if len(hit) else np.nan

        rows.append({"product": k, "mean_mw": float(d.mean()) if len(d) else 0.0,
                     "p05_mw": float(d.quantile(0.05)) if len(d) else 0.0,
                     "p95_mw": float(d.quantile(0.95)) if len(d) else 0.0,
                     "hours_with_demand": float(len(d) / len(st.demand)), "price_now": float(p0),
                     "mw_half_price": first_below(0.5), "mw_tenth_price": first_below(0.1)})
    return pd.DataFrame(rows).set_index("product")


def _day_rows(st: Stacks, days: list[Day]) -> dict:
    pos = pd.Series(np.arange(len(st.hours)), index=st.hours)
    out = {}
    for d in days:
        idx = pos.reindex(d.hours).to_numpy()
        if not np.isnan(idx).any():
            out[d.business_date] = idx.astype(int)
    return out


def fleet_equilibrium(days: list[Day], st: Stacks, cfg: RunConfig, fleets_mw, iterations: int = 8,
                      damping: float = 0.5, bid: float = 0.0,
                      workers: int | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Stacking revenue of one battery in a fleet of F identical batteries (per MW of battery power).

    Returns (summary per fleet size, convergence log). Each battery can be accepted for at most
    (MW bought - offers below the bid) / F MW of a product in an hour.
    """
    rows_of = _day_rows(st, days)
    ref = [d for d in days if d.business_date in rows_of]
    if not ref:
        raise ValueError("no days with offer stacks")
    log.info("fleet equilibrium on %d days, fleets %s MW", len(ref), list(fleets_mw))
    summary, conv = [], []
    workers = workers or default_workers()
    pool = nullcontext(None) if workers == 1 else ProcessPoolExecutor(max_workers=workers)
    with pool as ex:
        base = run_configs(ref, [cfg], workers=workers, keep_reserves=True, executor=ex)
        base_r = dict(zip(base["business_date"], base["reserves"]))
        for fleet in fleets_mw:
            r_bar = {k: v.copy() for k, v in base_r.items()}
            result = base
            for it in range(iterations if fleet > 0 else 0):
                prices, limits = {}, {}
                for d in ref:
                    rows = rows_of[d.business_date]
                    pr = np.full((len(rows), len(PRODUCTS)), np.nan)
                    lim = np.zeros((len(rows), len(PRODUCTS)))
                    for j, k in enumerate(PRODUCTS):
                        price, _ = clear(st, k, fleet * r_bar[d.business_date][:, j], bid, rows)
                        pr[:, j] = price
                        dem = st.demand[k].to_numpy()[rows]
                        below = np.where(st.price[k][rows] < bid, np.diff(np.concatenate(
                            [np.zeros((len(rows), 1)), st.cum[k][rows]], axis=1), axis=1), 0.0).sum(axis=1)
                        lim[:, j] = np.clip((dem - below) / fleet, 0.0, cfg.params.power_mw)
                    prices[d.business_date], limits[d.business_date] = pr, lim
                result = run_configs(ref, [cfg], workers=workers, keep_reserves=True, reserve_prices=prices,
                                     reserve_limits=limits, executor=ex)
                new_r = dict(zip(result["business_date"], result["reserves"]))
                change = float(np.mean([np.abs(new_r[k] - r_bar[k]).mean() for k in r_bar]))
                r_bar = {k: damping * new_r[k] + (1 - damping) * r_bar[k] for k in r_bar}
                mean_prices = {k: float(np.nanmean(np.concatenate([prices[d][:, j] for d in prices])))
                               for j, k in enumerate(PRODUCTS)}
                conv.append({"fleet_mw": fleet, "iteration": it + 1, "mean_abs_change_mw": change,
                             "total_per_year": float(result["total"].mean() * 365), **mean_prices})
                log.info("fleet %s MW, iteration %d: revenue %.0f PLN/MW/yr, schedule change %.4f MW", fleet, it + 1,
                         result["total"].mean() * 365, change)
            row = {"fleet_mw": fleet, "days": len(result)}
            for c in ["total", "arbitrage", "degradation"] + [f"rev_{k}" for k in PRODUCTS]:
                row[c] = float(result[c].mean() * 365)
            row["reserves"] = sum(row[f"rev_{k}"] for k in PRODUCTS)
            row.update({f"mw_{k}": float(result[f"mw_{k}"].mean()) for k in PRODUCTS})
            row["cycles_per_day"] = float(result["cycles"].mean())
            summary.append(row)
    return pd.DataFrame(summary).set_index("fleet_mw"), pd.DataFrame(conv)
