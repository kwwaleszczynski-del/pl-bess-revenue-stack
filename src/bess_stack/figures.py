"""README figures (matplotlib, PNG).

Colours follow the product family everywhere (arbitrage, FCR, aFRR, mFRRd, RR, capacity market); up and down
products are drawn in separate panels, never told apart by colour alone.

Usage:
    write_figures(result, Path("docs/img"))
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.ticker  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from bess_stack.analysis import Analysis  # noqa: E402
from bess_stack.market import LABELS  # noqa: E402
from bess_stack.pse_api import DOWN, PRODUCTS, UP  # noqa: E402

SURFACE, INK, INK2, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9"
FAMILY_COLOUR = {"arbitrage": "#2a78d6", "fcr": "#eb6834", "afrr": "#1baf7a", "mfrr": "#eda100", "rr": "#e87ba4",
                 "capacity": "#008300"}
FAMILY_LABEL = {"arbitrage": "Arbitrage (net)", "fcr": "FCR", "afrr": "aFRR", "mfrr": "mFRRd", "rr": "RR",
                "capacity": "Capacity market"}
FAMILIES = ["fcr", "afrr", "mfrr", "rr"]


def family(product: str) -> str:
    return product.rsplit("_", 1)[0]


def _style():
    plt.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "axes.edgecolor": GRID, "axes.labelcolor": INK2, "axes.titlecolor": INK, "text.color": INK,
        "xtick.color": MUTED, "ytick.color": MUTED, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6,
        "axes.spines.top": False, "axes.spines.right": False, "font.size": 9.5, "axes.titlesize": 11,
        "axes.titleweight": "bold", "legend.frameon": False, "legend.fontsize": 8.5, "lines.linewidth": 2,
        "axes.axisbelow": True,
    })


def _kfmt(ax, axis="y"):
    fmt = matplotlib.ticker.FuncFormatter(lambda v, _: f"{v / 1000:,.0f}k" if abs(v) >= 1000 else f"{v:,.0f}")
    (ax.yaxis if axis == "y" else ax.xaxis).set_major_formatter(fmt)


def _months(index) -> pd.DatetimeIndex:
    return pd.to_datetime([f"{m}-01" for m in index])


def _month_axis(ax, months=(1, 4, 7, 10)):
    ax.xaxis.set_major_locator(mdates.MonthLocator(bymonth=list(months)))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))


def reserve_prices(r: Analysis, path: Path):
    m = r.tables["market_monthly_prices"]
    x = _months(m.index)
    fig, axes = plt.subplots(1, 2, figsize=(11, 3.8), sharey=True)
    for ax, group, title in ((axes[0], UP, "Upward reserves"), (axes[1], DOWN, "Downward reserves")):
        for k in group:
            ax.plot(x, m[k], color=FAMILY_COLOUR[family(k)], label=LABELS[k], marker="o", markersize=3.5)
        ax.set_title(title, loc="left")
        _month_axis(ax, (1, 7))
        ax.legend(loc="best", ncols=2)
    axes[0].set_ylabel("PLN per MW per hour (monthly mean)")
    fig.suptitle("Balancing capacity prices in the basic procurement (CMBP)", x=0.01, ha="left", fontweight="bold")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def hour_profile(r: Analysis, path: Path):
    p = r.tables["market_hour_profile_last12"]
    fig, axes = plt.subplots(1, 2, figsize=(11, 3.6), sharey=True)
    for ax, group, title in ((axes[0], UP, "Upward reserves"), (axes[1], DOWN, "Downward reserves")):
        for k in group:
            ax.plot(p.index, p[k], color=FAMILY_COLOUR[family(k)], label=LABELS[k], marker="o", markersize=3)
        ax.set_title(title, loc="left")
        ax.set_xticks(range(0, 24, 3), [f"{h:02d}:00" for h in range(0, 24, 3)])
        ax.legend(loc="upper left", ncols=2)
    axes[0].set_ylabel("PLN per MW per hour")
    a, b = r.last12
    fig.suptitle(f"Capacity price by hour of day (local time, {a:%d %b %Y} to {b:%d %b %Y})", x=0.01, ha="left",
                 fontweight="bold")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def market_depth(r: Analysis, path: Path):
    d = r.tables["market_depth"]
    x = _months(d.index)
    fig, axes = plt.subplots(1, 2, figsize=(11, 3.6), sharey=True)
    for ax, group, title in ((axes[0], UP, "Upward reserves"), (axes[1], DOWN, "Downward reserves")):
        for k in group:
            ax.plot(x, d[("cover", k)], color=FAMILY_COLOUR[family(k)], label=LABELS[k], marker="o", markersize=3.5)
        ax.axhline(1.0, color=MUTED, linewidth=1)
        _month_axis(ax, (1, 7))
        ax.set_title(title, loc="left")
        ax.legend(loc="upper left", ncols=2)
        ax.set_ylim(0, 5)
        over = d[[("cover", k) for k in group]].max(axis=1)
        for xi, v in zip(x, over):
            if v > 5:
                ax.annotate(f"{v:.0f}x", (xi, 4.9), xytext=(6, 0), textcoords="offset points", ha="left", va="top",
                            fontsize=8, color=INK2)
    axes[0].set_ylabel("MW offered / MW bought")
    fig.suptitle("Depth of the reserve markets: offered capacity relative to demand (monthly mean of hours)", x=0.01,
                 ha="left", fontweight="bold")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _segments(row: pd.Series) -> dict[str, float]:
    out = {"arbitrage": row["arbitrage"] - row["degradation"]}
    for f in FAMILIES:
        out[f] = sum(row.get(f"rev_{k}", 0.0) for k in PRODUCTS if family(k) == f)
    return out


def _stacked_bars(ax, labels, segs: list[dict[str, float]], width=0.7, keys=None):
    keys = keys or ["arbitrage"] + FAMILIES
    x = np.arange(len(labels))
    pos = np.zeros(len(labels))
    neg = np.zeros(len(labels))
    for key in keys:
        vals = np.array([s.get(key, 0.0) for s in segs])
        base = np.where(vals >= 0, pos, neg)
        ax.bar(x, vals, width, bottom=base, color=FAMILY_COLOUR[key], label=FAMILY_LABEL[key], edgecolor=SURFACE,
               linewidth=1.5)
        pos += np.where(vals >= 0, vals, 0)
        neg += np.where(vals < 0, vals, 0)
    for xi, s in zip(x, segs):
        total = sum(s.values())
        ax.text(xi, max(pos[xi], 0) * 1.01 + 1, f"{total / 1000:,.0f}k", ha="center", va="bottom", fontsize=8.5,
                color=INK2)
    ax.set_xticks(x, labels)
    ax.axhline(0, color=MUTED, linewidth=0.8)
    _kfmt(ax)


def revenue_stack(r: Analysis, path: Path):
    t = r.tables["revenue_last12"]
    labels, segs = [], []
    for e in (1, 2, 4):
        for s, name in (("arbitrage", "arbitrage"), ("reserves", "reserves"), ("stacked", "stacked")):
            cfg = f"{s}_{e}h"
            if cfg in t.index:
                labels.append(f"{e} h\n{name}")
                segs.append(_segments(t.loc[cfg]))
    fig, ax = plt.subplots(figsize=(11, 4.4))
    _stacked_bars(ax, labels, segs)
    a, b = r.last12
    ax.set_ylabel("PLN per MW per year")
    ax.set_title(f"Revenue of a 1 MW battery by strategy and duration ({a:%d %b %Y} to {b:%d %b %Y}, perfect "
                 "foresight)", loc="left")
    ax.legend(loc="upper left", ncols=5)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def monthly_stack(r: Analysis, path: Path, config: str = "stacked_2h"):
    mr = r.tables["monthly_revenue"]
    if config not in mr.index.get_level_values(0):
        return
    m = mr.loc[config]
    arb = mr.loc[config.replace("stacked", "arbitrage")] if config.replace("stacked", "arbitrage") in \
        mr.index.get_level_values(0) else None
    x = _months(m.index)
    fig, ax = plt.subplots(figsize=(11, 4.2))
    bottom_pos = np.zeros(len(m))
    bottom_neg = np.zeros(len(m))
    for key in ["arbitrage"] + FAMILIES:
        if key == "arbitrage":
            vals = (m["arbitrage"] - m["degradation"]).to_numpy()
        else:
            vals = sum(m[f"rev_{k}"] for k in PRODUCTS if family(k) == key).to_numpy()
        base = np.where(vals >= 0, bottom_pos, bottom_neg)
        ax.bar(x, vals, width=24, bottom=base, color=FAMILY_COLOUR[key], label=FAMILY_LABEL[key],
               edgecolor=SURFACE, linewidth=1.2)
        bottom_pos += np.where(vals >= 0, vals, 0)
        bottom_neg += np.where(vals < 0, vals, 0)
    if arb is not None:
        ax.plot(_months(arb.index), arb["total"], color=INK, linewidth=2, marker="o", markersize=3.5,
                label="Arbitrage only (same battery)")
    ax.axhline(0, color=MUTED, linewidth=0.8)
    _kfmt(ax)
    ax.set_ylabel("PLN per MW per day (monthly mean)")
    ax.set_title("Where the money came from: 2 h battery, stacking strategy, month by month", loc="left")
    _month_axis(ax)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.1), ncols=6)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def saturation(r: Analysis, path: Path):
    if "saturation_curves" not in r.tables:
        return
    c = r.tables["saturation_curves"]
    fig, axes = plt.subplots(1, 3, figsize=(13, 4.0), gridspec_kw={"width_ratios": [1, 1, 1.25]})
    for ax, group, title in ((axes[0], UP, "Upward reserves"), (axes[1], DOWN, "Downward reserves")):
        for k in group:
            cc = c.loc[k]
            ax.plot(cc.index, cc["mean_price"], color=FAMILY_COLOUR[family(k)], label=LABELS[k])
        ax.set_title(title, loc="left")
        ax.set_xlabel("battery MW offered in the product, every hour")
        ax.legend(loc="upper right")
        ax.set_xlim(0, 1500)
    axes[0].set_ylabel("mean clearing price, PLN/MW/h")
    f = r.tables.get("fleet")
    ax = axes[2]
    if f is not None:
        labels = [f"{int(v):,}" for v in f.index]
        segs = [_segments(f.loc[v]) for v in f.index]
        _stacked_bars(ax, labels, segs, width=0.65)
        ax.axhline(f["arbitrage_only"].iloc[0], color=INK, linewidth=1.5)
        ax.text(-0.45, f["arbitrage_only"].iloc[0], "arbitrage only", va="bottom", ha="left", fontsize=8, color=INK)
        ax.set_xlabel("fleet of identical 2 h batteries, MW")
        ax.set_ylabel("PLN per MW per year")
        ax.set_title("Stacking revenue per MW in a fleet", loc="left")
        ax.legend(loc="upper right", fontsize=7.5)
    fig.suptitle("Reserve saturation on the published offer stacks (last 12 months)", x=0.01, ha="left",
                 fontweight="bold")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


SENSITIVITY = [("rte80", "Round-trip efficiency 80%"), ("", "Base: RTE 87%, 30 PLN/MWh, reserve 15 min / 1 h"),
               ("rte92", "Round-trip efficiency 92%"), ("deg0", "Degradation cost 0 PLN/MWh"),
               ("deg60", "Degradation cost 60 PLN/MWh"), ("deg100", "Degradation cost 100 PLN/MWh"),
               ("req15min", "All reserves held for 15 min only"), ("fcr30min", "FCR held for 30 min")]


def sensitivity_table(r: Analysis, which: str = "revenue_last12") -> pd.DataFrame:
    t = r.tables[which]
    rows = []
    for suffix, label in SENSITIVITY:
        row = {"case": label}
        for s in ("arbitrage", "reserves", "stacked"):
            cfg = f"{s}_2h" + (f"_{suffix}" if suffix else "")
            row[s] = float(t.loc[cfg, "total"]) if cfg in t.index else np.nan
        rows.append(row)
    return pd.DataFrame(rows).set_index("case")


def sensitivity(r: Analysis, path: Path):
    s = sensitivity_table(r)
    if s["stacked"].notna().sum() < 2:
        return
    fig, ax = plt.subplots(figsize=(11, 4.3))
    y = np.arange(len(s))
    h = 0.38
    ax.barh(y - h / 2, s["stacked"], h, color=FAMILY_COLOUR["afrr"], label="Stacking", edgecolor=SURFACE)
    ax.barh(y + h / 2, s["arbitrage"], h, color=FAMILY_COLOUR["arbitrage"], label="Arbitrage only", edgecolor=SURFACE)
    for yi, v in zip(y, s["stacked"]):
        if np.isfinite(v):
            ax.text(v, yi - h / 2, f" {v / 1000:,.0f}k", va="center", fontsize=8, color=INK2)
    ax.set_yticks(y, s.index)
    ax.invert_yaxis()
    _kfmt(ax, "x")
    ax.set_xlabel("PLN per MW per year, 2 h battery, last 12 months")
    ax.set_title("Sensitivity to efficiency, degradation cost and the energy held for reserves", loc="left")
    ax.set_xlim(0, np.nanmax(s[["stacked", "arbitrage"]].to_numpy()) * 1.12)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.16), ncols=2)
    ax.grid(axis="y", visible=False)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def write_figures(r: Analysis, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    _style()
    paths = []
    for name, fn in (("reserve_prices", reserve_prices), ("hour_profile", hour_profile),
                     ("market_depth", market_depth), ("revenue_stack", revenue_stack),
                     ("monthly_stack", monthly_stack), ("saturation", saturation), ("sensitivity", sensitivity)):
        p = out_dir / f"{name}.png"
        fn(r, p)
        if p.exists():
            paths.append(p)
    return paths
