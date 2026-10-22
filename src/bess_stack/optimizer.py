"""Daily co-optimisation of day-ahead arbitrage and balancing capacity as a mixed-integer linear program (HiGHS via
scipy).

For one delivery day with quarter-hour energy prices p_t (PLN/MWh, interval length dt hours) and hourly balancing
capacity prices pi_{h,k} (PLN/MW per hour, product k, hour h(t) of interval t) the battery chooses charge c_t,
discharge d_t (MW) and the capacity r_{h,k} (MW) sold in every product to maximise

    sum_t p_t dt (d_t - c_t)  -  deg_cost sum_t dt d_t  +  sum_{h,k} pi_{h,k} r_{h,k}

subject to
    soc_t = soc_{t-1} + eta dt c_t - dt d_t / eta                         (energy balance, MWh)
    soc_min <= soc_t <= soc_max,  soc_end = soc_start                       (no free energy across days)
    0 <= c_t <= P u_t,  0 <= d_t <= P (1 - u_t),  u_t binary                (no simultaneous charge/discharge)
    sum_t dt d_t <= max_cycles * usable_energy                              (warranty-style throughput cap)
    d_t - c_t + sum_{k up} r_{h(t),k} <= P,   c_t - d_t + sum_{k down} r_{h(t),k} <= P      (power headroom)
    soc - sum_{k up} T_k r_{h,k} / eta >= soc_min,   soc + sum_{k down} T_k r_{h,k} eta <= soc_max   (SoC reserve)

The SoC reserve holds at the start and the end of every quarter-hour of hour h: a contracted MW of product k must be
deliverable at full power for T_k hours on top of the arbitrage schedule (``DURATION_REQUIREMENT``). Energy of
expected activations is not part of the balance (it is priced separately in ``activation.py``).

Everything is known in advance (perfect foresight). In reality balancing capacity is bought on the day before
delivery, in the morning before the day-ahead auction, so the result is an upper bound of what a battery could
have earned with these prices and rules.

Usage:
    from bess_stack.optimizer import BatteryParams, STRATEGIES, optimize_day
    res = optimize_day(prices, hour_of, reserve_prices, BatteryParams(energy_mwh=2), STRATEGIES["stacked"])
    res.total, res.reserve_revenue, res.schedule
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy import sparse
from scipy.optimize import Bounds, LinearConstraint, milp

from bess_stack.pse_api import DOWN, PRODUCTS, UP

# Hours of full contracted power a battery must hold in reserve per MW of each product (assumption, see README):
# FCR 15 min follows the default of SOGL art. 156(9) for units with limited energy reservoirs; for aFRR, mFRRd and
# RR the WDB sets no explicit duration, so a full one-hour procurement period is assumed.
DURATION_REQUIREMENT = {"fcr_up": 0.25, "fcr_down": 0.25, "afrr_up": 1.0, "afrr_down": 1.0,
                        "mfrr_up": 1.0, "mfrr_down": 1.0, "rr_up": 1.0}


@dataclass(frozen=True)
class BatteryParams:
    power_mw: float = 1.0
    energy_mwh: float = 2.0
    round_trip_efficiency: float = 0.87
    soc_min: float = 0.05          # fraction of energy_mwh
    soc_max: float = 0.95
    soc_start: float = 0.5         # also required at the end of each day
    max_cycles_per_day: float = 1.5
    degradation_cost: float = 30.0  # PLN per MWh discharged

    @property
    def usable_mwh(self) -> float:
        return self.energy_mwh * (self.soc_max - self.soc_min)

    @property
    def eta(self) -> float:
        """One-way efficiency, split symmetrically between charging and discharging."""
        return float(np.sqrt(self.round_trip_efficiency))


@dataclass(frozen=True)
class Strategy:
    name: str
    arbitrage: bool
    products: tuple[str, ...]


STRATEGIES = {
    "arbitrage": Strategy("arbitrage", True, ()),
    "reserves": Strategy("reserves", False, tuple(PRODUCTS)),
    "stacked": Strategy("stacked", True, tuple(PRODUCTS)),
}


@dataclass
class DayResult:
    total: float                   # PLN, net of degradation cost
    arbitrage: float               # PLN, energy bought and sold on the day-ahead market
    degradation: float             # PLN, degradation cost of arbitrage discharge
    reserve_revenue: dict[str, float]   # PLN per product
    discharged_mwh: float
    cycles: float
    schedule: pd.DataFrame         # per interval: price, charge_mw, discharge_mw, soc_mwh
    reserves: pd.DataFrame         # per hour: MW per product
    extras: dict = field(default_factory=dict)


def optimize_day(prices, hour_of, reserve_prices, params: BatteryParams, strategy: Strategy,
                 dt_hours: float = 0.25, duration_req: dict[str, float] | None = None,
                 reserve_limit=None, mip_rel_gap: float = 1e-5) -> DayResult:
    """Solve one day.

    prices          energy price per interval (n values, PLN/MWh)
    hour_of         index 0..H-1 of the capacity period (hour) of each interval (n values, non-decreasing)
    reserve_prices  H x len(PRODUCTS) capacity prices in PLN/MW per hour, columns in ``PRODUCTS`` order;
                    NaN means the product was not bought in that hour (the battery cannot sell it)
    reserve_limit   optional H x len(PRODUCTS) upper bound of the MW sold (for example the share of the offers
                    accepted when the market is saturated); defaults to the battery power
    """
    p = np.asarray(prices, dtype=float)
    hour_of = np.asarray(hour_of, dtype=int)
    if np.isnan(p).any():
        raise ValueError("prices contain NaN")
    n = len(p)
    pi = np.asarray(reserve_prices, dtype=float).reshape(-1, len(PRODUCTS))
    H, K = pi.shape
    if hour_of.min() < 0 or hour_of.max() >= H:
        raise ValueError("hour_of does not match reserve_prices")
    req = {**DURATION_REQUIREMENT, **(duration_req or {})}
    eta, P = params.eta, params.power_mw
    e_min, e_max = params.energy_mwh * params.soc_min, params.energy_mwh * params.soc_max
    soc0 = params.energy_mwh * params.soc_start

    # variable layout: [charge(n), discharge(n), soc(n), u(n), r(H*K) hour-major]
    ic, idd, isoc, iu, ir = 0, n, 2 * n, 3 * n, 4 * n
    nv = 4 * n + H * K
    cost = np.zeros(nv)
    cost[ic:ic + n] = p * dt_hours
    cost[idd:idd + n] = -(p - params.degradation_cost) * dt_hours
    allowed = np.array([k in strategy.products for k in PRODUCTS])
    sellable = ~np.isnan(pi) & allowed[None, :]
    cost[ir:] = -np.where(sellable, pi, 0.0).ravel()

    eye = sparse.identity(n, format="csr")
    shift = sparse.eye(n, k=-1, format="csr")          # soc_{t-1}
    zeros = sparse.csr_matrix((n, n))
    # A_up[t, ir + h(t)*K + k] = 1 for up products; same for down
    up_cols = np.array([k in UP for k in PRODUCTS])
    dn_cols = np.array([k in DOWN for k in PRODUCTS])
    tau = np.array([req[k] for k in PRODUCTS])

    def hour_map(weights: np.ndarray) -> sparse.csr_matrix:
        rows = np.repeat(np.arange(n), K)
        cols = (hour_of[:, None] * K + np.arange(K)[None, :]).ravel()
        vals = np.tile(weights, n)
        keep = vals != 0
        return sparse.csr_matrix((vals[keep], (rows[keep], cols[keep])), shape=(n, H * K))

    m_up = hour_map(up_cols.astype(float))
    m_dn = hour_map(dn_cols.astype(float))
    e_up = hour_map(np.where(up_cols, tau / eta, 0.0))
    e_dn = hour_map(np.where(dn_cols, tau * eta, 0.0))
    zr = sparse.csr_matrix((n, H * K))

    balance = sparse.hstack([-eta * dt_hours * eye, (dt_hours / eta) * eye, eye - shift, zeros, zr])
    rhs = np.zeros(n)
    rhs[0] = soc0
    end_row = sparse.csr_matrix(([1.0], ([0], [isoc + n - 1])), shape=(1, nv))
    ch_mode = sparse.hstack([eye, zeros, zeros, -P * eye, zr])
    dis_mode = sparse.hstack([zeros, eye, zeros, P * eye, zr])
    thr_row = sparse.csr_matrix((np.full(n, dt_hours), (np.zeros(n, int), np.arange(idd, idd + n))), shape=(1, nv))
    head_up = sparse.hstack([-eye, eye, zeros, zeros, m_up])
    head_dn = sparse.hstack([eye, -eye, zeros, zeros, m_dn])
    # SoC reserve at the end of each interval (soc_t) and at its start (soc_{t-1}; soc_{-1} = soc0 is a constant)
    res_up_end = sparse.hstack([zeros, zeros, eye, zeros, -e_up])
    res_dn_end = sparse.hstack([zeros, zeros, eye, zeros, e_dn])
    res_up_start = sparse.hstack([zeros, zeros, shift, zeros, -e_up])
    res_dn_start = sparse.hstack([zeros, zeros, shift, zeros, e_dn])
    start_lo = np.full(n, e_min)
    start_lo[0] = e_min - soc0
    start_hi = np.full(n, e_max)
    start_hi[0] = e_max - soc0

    constraints = [
        LinearConstraint(balance, rhs, rhs),
        LinearConstraint(end_row, soc0, soc0),
        LinearConstraint(ch_mode, -np.inf, 0.0),
        LinearConstraint(dis_mode, -np.inf, P),
        LinearConstraint(thr_row, 0.0, params.max_cycles_per_day * params.usable_mwh),
        LinearConstraint(head_up, -np.inf, P),
        LinearConstraint(head_dn, -np.inf, P),
        LinearConstraint(res_up_end, e_min, np.inf),
        LinearConstraint(res_dn_end, -np.inf, e_max),
        LinearConstraint(res_up_start, start_lo, np.inf),
        LinearConstraint(res_dn_start, -np.inf, start_hi),
    ]
    limit = np.full((H, K), P) if reserve_limit is None else np.clip(np.asarray(reserve_limit, float), 0.0, P)
    r_ub = np.where(sellable, limit, 0.0).ravel()
    arb_ub = P if strategy.arbitrage else 0.0
    lb = np.concatenate([np.zeros(2 * n), np.full(n, e_min), np.zeros(n), np.zeros(H * K)])
    ub = np.concatenate([np.full(2 * n, arb_ub), np.full(n, e_max), np.ones(n), r_ub])
    integrality = np.concatenate([np.zeros(3 * n), np.full(n, 1 if strategy.arbitrage else 0), np.zeros(H * K)])

    res = milp(cost, constraints=constraints, bounds=Bounds(lb, ub), integrality=integrality,
               options={"disp": False, "mip_rel_gap": mip_rel_gap})
    if not res.success:
        raise RuntimeError(f"MILP failed: {res.message}")
    x = res.x
    charge, discharge, soc = x[ic:ic + n].copy(), x[idd:idd + n].copy(), x[isoc:isoc + n]
    charge[charge < 1e-9] = 0.0
    discharge[discharge < 1e-9] = 0.0
    r = x[ir:].reshape(H, K).copy()
    r[r < 1e-9] = 0.0
    index = prices.index if isinstance(prices, pd.Series) else pd.RangeIndex(n)
    schedule = pd.DataFrame({"price": p, "charge_mw": charge, "discharge_mw": discharge, "soc_mwh": soc},
                            index=index)
    reserves = pd.DataFrame(r, columns=PRODUCTS)
    gross = float(np.sum(p * dt_hours * (discharge - charge)))
    discharged = float(np.sum(discharge) * dt_hours)
    rev = {k: float(np.sum(np.where(sellable[:, j], pi[:, j], 0.0) * r[:, j])) for j, k in enumerate(PRODUCTS)}
    degradation = params.degradation_cost * discharged
    return DayResult(
        total=gross - degradation + sum(rev.values()),
        arbitrage=gross,
        degradation=degradation,
        reserve_revenue=rev,
        discharged_mwh=discharged,
        cycles=discharged / params.usable_mwh if params.usable_mwh else 0.0,
        schedule=schedule,
        reserves=reserves,
    )
