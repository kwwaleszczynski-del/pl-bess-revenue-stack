"""Variant: revenue from aFRR balancing energy on top of the capacity payment (ex post, with assumptions).

The optimiser earns capacity payments only. A battery holding aFRR is also activated and paid for the energy. PSE
publishes, per quarter-hour, the aFRR energy activated on the European aFRR platform (eb-rozl, MWh) and its average
price up and down (crb-rozl, PLN/MWh); both exist only since the Polish connection to the platform in 2025.

Assumptions:
- the battery is activated pro rata to its share of the aFRR capacity bought (basic plus supplementary
  procurement): activation ratio alpha_t = activated MWh / (MW bought x 0.25 h), capped at 1;
- up activation delivers E = alpha_t r_up dt and is paid the aFRR up price; the energy is bought back at the
  day-ahead price of the same quarter-hour, with round-trip losses (E / RTE);
- down activation absorbs E = alpha_t r_down dt at the aFRR down price; the stored E x RTE is sold at the day-ahead
  price;
- degradation cost applies to the discharged energy; the state of charge effect is assumed to be rebalanced at the
  day-ahead price, so the arbitrage schedule is unchanged.

    margin_up   = E_up   (p_up - p_DA / RTE - deg)
    margin_down = E_down (p_DA RTE - p_down - deg RTE)

Usage:
    act = activation_ratio(afrr_energy, cap_volume, sup_volume)
    daily = activation_revenue(reserves_by_day, days, act, afrr_price, energy, params)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from bess_stack.backtest import Day
from bess_stack.optimizer import BatteryParams
from bess_stack.pse_api import PRODUCTS

DT = 0.25


def activation_ratio(afrr_energy: pd.DataFrame, cap_volume: pd.DataFrame, sup_volume: pd.DataFrame) -> pd.DataFrame:
    """Per quarter-hour: share of the bought aFRR capacity that was activated, up and down (0..1)."""
    q = afrr_energy.index
    basic = cap_volume[["afrr_up", "afrr_down"]].fillna(0.0).reindex(q.floor("h")).set_axis(q)
    sup = sup_volume.reindex(q)[["afrr_up", "afrr_down"]].fillna(0.0)
    bought = basic + sup
    up = afrr_energy["afrr_up_mwh"].abs() / (DT * bought["afrr_up"].replace(0, np.nan))
    down = afrr_energy["afrr_down_mwh"].abs() / (DT * bought["afrr_down"].replace(0, np.nan))
    out = pd.DataFrame({"alpha_up": up.clip(0, 1), "alpha_down": down.clip(0, 1)}, index=q)
    out["business_date"] = afrr_energy["business_date"]
    return out.dropna(subset=["alpha_up", "alpha_down"], how="all")


def activation_revenue(reserves_by_day: dict, days: list[Day], ratio: pd.DataFrame, afrr_price: pd.DataFrame,
                       energy: pd.DataFrame, params: BatteryParams) -> pd.DataFrame:
    """Daily activated energy and margin for the hourly aFRR schedules in ``reserves_by_day`` (business date ->
    hours x PRODUCTS array). Days without platform data are skipped."""
    j_up, j_dn = PRODUCTS.index("afrr_up"), PRODUCTS.index("afrr_down")
    rte, deg = params.round_trip_efficiency, params.degradation_cost
    a = ratio.reindex(energy.index)
    pr = afrr_price.reindex(energy.index)
    rows = []
    for d in days:
        if d.business_date not in reserves_by_day:
            continue
        sel = energy["business_date"].to_numpy() == d.business_date
        idx = energy.index[sel]
        au, ad = a.loc[idx, "alpha_up"].to_numpy(), a.loc[idx, "alpha_down"].to_numpy()
        if np.isnan(au).all() and np.isnan(ad).all():
            continue
        r = reserves_by_day[d.business_date]
        r_up, r_dn = r[d.hour_of, j_up], r[d.hour_of, j_dn]
        p_da = energy.loc[idx, "price"].to_numpy()
        p_up, p_dn = pr.loc[idx, "afrr_up_price"].to_numpy(), pr.loc[idx, "afrr_down_price"].to_numpy()
        e_up = np.where(np.isfinite(au) & np.isfinite(p_up), np.nan_to_num(au) * r_up * DT, 0.0)
        e_dn = np.where(np.isfinite(ad) & np.isfinite(p_dn), np.nan_to_num(ad) * r_dn * DT, 0.0)
        m_up = e_up * (np.nan_to_num(p_up) - p_da / rte - deg)
        m_dn = e_dn * (p_da * rte - np.nan_to_num(p_dn) - deg * rte)
        rows.append({"business_date": d.business_date, "energy_up_mwh": e_up.sum(), "energy_down_mwh": e_dn.sum(),
                     "margin_up": m_up.sum(), "margin_down": m_dn.sum(), "margin": m_up.sum() + m_dn.sum(),
                     "paid_up": float((e_up * np.nan_to_num(p_up)).sum()),
                     "paid_down": float((e_dn * np.nan_to_num(p_dn)).sum())})
    return pd.DataFrame(rows)
