"""Balancing capacity market statistics: prices per product and hour, seasonality, trend, depth and correlation with
day-ahead prices.

All inputs are frames from ``pse_api.load_series`` (UTC interval starts plus ``business_date``). Calendar periods
(months, years, hours of day) are Polish local time.

Usage:
    prices = monthly_prices(cap_price)
    depth = monthly_depth(cap_volume, sup_volume, offers)
    corr = correlations(cap_price, rce)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from bess_stack.pse_api import PRODUCTS

TZ = "Europe/Warsaw"
LABELS = {"fcr_up": "FCR up", "fcr_down": "FCR down", "afrr_up": "aFRR up", "afrr_down": "aFRR down",
          "mfrr_up": "mFRRd up", "mfrr_down": "mFRRd down", "rr_up": "RR up"}


def _month(frame: pd.DataFrame) -> np.ndarray:
    return np.asarray([f"{d:%Y-%m}" for d in frame["business_date"]])


def _year(frame: pd.DataFrame) -> np.ndarray:
    return np.asarray([d.year for d in frame["business_date"]])


def monthly_prices(cap_price: pd.DataFrame) -> pd.DataFrame:
    """Mean capacity price per month and product (PLN/MW per hour); hours without purchase are excluded."""
    return cap_price[PRODUCTS].groupby(_month(cap_price)).mean().rename_axis("month")


def hour_profile(cap_price: pd.DataFrame, start=None, end=None) -> pd.DataFrame:
    """Mean capacity price by local hour of day (0-23) and product over business dates start..end."""
    c = cap_price
    if start is not None:
        c = c[c["business_date"] >= start]
    if end is not None:
        c = c[c["business_date"] <= end]
    hour = np.asarray(c.index.tz_convert(TZ).hour)
    return c[PRODUCTS].groupby(hour).mean().rename_axis("hour")


def month_hour_heatmap(cap_price: pd.DataFrame, product: str) -> pd.DataFrame:
    """Mean price of one product by month (rows) and local hour (columns)."""
    c = cap_price
    hour = np.asarray(c.index.tz_convert(TZ).hour)
    return c[product].groupby([_month(c), hour]).mean().unstack()


def annual_market(cap_price: pd.DataFrame, cap_volume: pd.DataFrame) -> pd.DataFrame:
    """Per year and product: mean price, volume-weighted price, mean MW bought, hours and cost of the basic
    procurement (PLN million, sum of price x MW over the hours covered)."""
    p, v = cap_price[PRODUCTS].align(cap_volume[PRODUCTS], join="inner", axis=0)
    year = np.asarray([d.year for d in cap_price.loc[p.index, "business_date"]])
    rows = []
    for y in sorted(set(year)):
        m = year == y
        for k in PRODUCTS:
            pk, vk = p.loc[m, k], v.loc[m, k]
            both = pk.notna() & vk.notna()
            cost = float((pk[both] * vk[both]).sum())
            rows.append({"year": y, "product": k, "mean_price": pk.mean(),
                         "vw_price": cost / vk[both].sum() if vk[both].sum() > 0 else np.nan,
                         "mean_mw": vk.fillna(0.0).mean(), "max_mw": vk.max(), "hours": int(m.sum()),
                         "cost_mpln": cost / 1e6})
    return pd.DataFrame(rows).set_index(["year", "product"])


def hourly_offered(offers: pd.DataFrame) -> pd.DataFrame:
    """Total MW offered per hour and product in the basic procurement (sum over portfolio offers)."""
    if offers.empty:
        return pd.DataFrame(columns=PRODUCTS, dtype=float)
    out = offers.groupby([offers.index, offers["product"]])["mw"].sum().unstack()
    return out.reindex(columns=PRODUCTS)


def monthly_depth(cap_volume: pd.DataFrame, sup_volume: pd.DataFrame, offers: pd.DataFrame) -> pd.DataFrame:
    """Per month and product: mean MW bought in the basic and supplementary procurement, mean MW offered in the
    basic procurement and the cover ratio offered / bought (how deep the market is relative to demand)."""
    # a missing volume means nothing was bought (zero demand for that reserve type)
    bought = cap_volume[PRODUCTS].fillna(0.0).groupby(_month(cap_volume)).mean()
    sup = sup_volume.reindex(columns=PRODUCTS).fillna(0.0)
    sup = sup.groupby(_month(sup_volume)).mean() if len(sup_volume) else pd.DataFrame(columns=PRODUCTS)
    off = hourly_offered(offers)
    if len(off):
        bd = cap_volume["business_date"].reindex(off.index)
        off = off[bd.notna().to_numpy()]
        off = off.groupby(np.asarray([f"{d:%Y-%m}" for d in bd.dropna()])).mean()
    frames = {"bought": bought, "supplementary": sup, "offered": off.reindex(bought.index)}
    out = pd.concat(frames, axis=1)
    cover = out["offered"] / out["bought"].replace(0, np.nan)
    out = pd.concat([out, pd.concat({"cover": cover}, axis=1)], axis=1)
    return out.rename_axis("month")


def depth_summary(cap_volume: pd.DataFrame, sup_volume: pd.DataFrame, offers: pd.DataFrame, start, end) -> pd.DataFrame:
    """Per product over business dates start..end: mean MW bought (basic and supplementary procurement), mean MW
    offered in the basic procurement and the cover ratio offered / bought."""
    v = cap_volume[(cap_volume["business_date"] >= start) & (cap_volume["business_date"] <= end)]
    sup = sup_volume[(sup_volume["business_date"] >= start) & (sup_volume["business_date"] <= end)]
    if len(offers):
        off = hourly_offered(offers[(offers["business_date"] >= start) & (offers["business_date"] <= end)])
    else:
        off = pd.DataFrame(columns=PRODUCTS, dtype=float)
    out = pd.DataFrame({"bought_mw": v[PRODUCTS].fillna(0.0).mean(),
                        "supplementary_mw": sup.reindex(columns=PRODUCTS).fillna(0.0).mean(),
                        "offered_mw": off.reindex(index=v.index)[PRODUCTS].mean() if len(off) else np.nan})
    out["cover"] = out["offered_mw"] / out["bought_mw"].replace(0, np.nan)
    return out.rename_axis("product")


def hourly_energy_price(energy: pd.DataFrame) -> pd.Series:
    """Mean of the quarter-hour prices of each UTC hour."""
    return energy["price"].groupby(energy.index.floor("h")).mean()


def correlations(cap_price: pd.DataFrame, energy: pd.DataFrame) -> pd.DataFrame:
    """Pearson and Spearman correlation of hourly capacity prices with the hourly day-ahead price, per year."""
    e = hourly_energy_price(energy)
    c = cap_price[PRODUCTS].join(e.rename("energy"), how="inner")
    year = np.asarray([ts.tz_convert(TZ).year for ts in c.index])
    rows = []
    for y in sorted(set(year)):
        sub = c[year == y]
        for k in PRODUCTS:
            pair = sub[[k, "energy"]].dropna()
            if len(pair) < 50:
                continue
            rows.append({"year": y, "product": k, "n": len(pair),
                         "pearson": pair[k].corr(pair["energy"]),
                         "spearman": pair[k].rank().corr(pair["energy"].rank())})
    return pd.DataFrame(rows, columns=["year", "product", "n", "pearson", "spearman"]).set_index(["year", "product"])


def trends(monthly: pd.DataFrame, min_months: int = 6) -> pd.DataFrame:
    """Linear trend of the monthly mean price per product: PLN/MW-h per year and log-linear change in % per year."""
    t = np.asarray([int(m[:4]) + (int(m[5:7]) - 1) / 12 for m in monthly.index])
    rows = []
    for k in PRODUCTS:
        y = monthly[k].to_numpy(float)
        ok = np.isfinite(y) & (y > 0)
        if ok.sum() < min_months:
            continue
        slope, intercept = np.polyfit(t[ok], y[ok], 1)
        lslope = np.polyfit(t[ok], np.log(y[ok]), 1)[0]
        rows.append({"product": k, "months": int(ok.sum()), "pln_per_year": slope,
                     "pct_per_year": 100 * (np.exp(lslope) - 1), "first": y[ok][0], "last": y[ok][-1]})
    cols = ["product", "months", "pln_per_year", "pct_per_year", "first", "last"]
    return pd.DataFrame(rows, columns=cols).set_index("product")
