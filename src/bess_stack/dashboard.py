"""Static HTML dashboard (Plotly.js from the official CDN) for GitHub Pages.

Figures are plain Plotly JSON with colour tokens ("@fcr", "@afrr", ...) that the page resolves for the light or dark
theme, so one file follows the reader's colour scheme. Every chart has a table with the same numbers.

Usage:
    write_dashboard(result, Path("docs"))
"""
from __future__ import annotations

import html
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from bess_stack.analysis import Analysis
from bess_stack.figures import FAMILIES, FAMILY_LABEL, family, sensitivity_table
from bess_stack.market import LABELS
from bess_stack.pse_api import DOWN, PRODUCTS, UP

PLOTLY_JS = "https://cdn.plot.ly/plotly-2.35.2.min.js"
REPO_URL = "https://github.com/kwwaleszczynski-del/pl-bess-revenue-stack"
HOUR_AXIS = {"tickvals": list(range(0, 24, 3)), "ticktext": [f"{h:02d}:00" for h in range(0, 24, 3)]}


def _num(values, nd: int = 2) -> list:
    out = []
    for v in values:
        try:
            f = float(v)
        except (TypeError, ValueError):
            out.append(None)
            continue
        out.append(None if math.isnan(f) else round(f, nd))
    return out


def _x_months(index) -> list[str]:
    return [f"{m}-01" for m in index]


def _line(x, y, name, colour, dash="solid", **kw) -> dict:
    return {"type": "scatter", "mode": "lines+markers", "x": x, "y": _num(y), "name": name,
            "line": {"color": colour, "width": 2, "dash": dash}, "marker": {"size": 6, "color": colour}, **kw}


def _segments(row: pd.Series) -> dict[str, float]:
    out = {"arbitrage": float(row["arbitrage"] - row["degradation"])}
    for f in FAMILIES:
        out[f] = float(sum(row.get(f"rev_{k}", 0.0) for k in PRODUCTS if family(k) == f))
    return out


def _stack_traces(labels: list[str], segs: list[dict[str, float]], keys=None) -> list[dict]:
    keys = keys or ["arbitrage"] + FAMILIES
    return [{"type": "bar", "x": labels, "y": _num([s.get(k, 0.0) for s in segs], 0), "name": FAMILY_LABEL[k],
             "marker": {"color": f"@{k}", "line": {"color": "@surface", "width": 2}},
             "hovertemplate": "%{y:,.0f} PLN/MW/yr<extra>" + FAMILY_LABEL[k] + "</extra>"} for k in keys]


def figures(r: Analysis) -> dict[str, dict]:
    t = r.tables
    figs = {}
    m = t["market_monthly_prices"]
    x = _x_months(m.index)
    for key, group in (("prices_up", UP), ("prices_down", DOWN)):
        figs[key] = {"data": [_line(x, m[k], LABELS[k], f"@{family(k)}", hovertemplate="%{y:.0f} PLN/MW/h")
                              for k in group],
                     "layout": {"yaxis": {"title": {"text": "PLN per MW per hour"}, "rangemode": "tozero"},
                                "hovermode": "x unified"}}
    p = t["market_hour_profile_last12"]
    for key, group in (("profile_up", UP), ("profile_down", DOWN)):
        figs[key] = {"data": [_line(list(p.index), p[k], LABELS[k], f"@{family(k)}",
                                    hovertemplate="%{y:.0f} PLN/MW/h") for k in group],
                     "layout": {"yaxis": {"title": {"text": "PLN per MW per hour"}, "rangemode": "tozero"},
                                "xaxis": HOUR_AXIS, "hovermode": "x unified"}}
    d = t["market_depth"]
    xd = _x_months(d.index)
    for key, group in (("depth_up", UP), ("depth_down", DOWN)):
        figs[key] = {"data": [_line(xd, d[("cover", k)], LABELS[k], f"@{family(k)}",
                                    hovertemplate="%{y:.2f}x") for k in group],
                     "layout": {"yaxis": {"title": {"text": "MW offered / MW bought"}, "range": [0, 5]},
                                "hovermode": "x unified",
                                "shapes": [{"type": "line", "xref": "paper", "x0": 0, "x1": 1, "y0": 1, "y1": 1,
                                            "line": {"color": "@axis", "width": 1}}]}}
    rv = t["revenue_last12"]
    labels, segs = [], []
    for e in (1, 2, 4):
        for s in ("arbitrage", "reserves", "stacked"):
            cfg = f"{s}_{e}h"
            if cfg in rv.index:
                labels.append(f"{e} h {s}")
                segs.append(_segments(rv.loc[cfg]))
    figs["revenue"] = {"data": _stack_traces(labels, segs),
                       "layout": {"barmode": "relative", "bargap": 0.3,
                                  "yaxis": {"title": {"text": "PLN per MW per year"}}}}
    mr = t["monthly_revenue"]
    if "stacked_2h" in mr.index.get_level_values(0):
        ms = mr.loc["stacked_2h"]
        xm = _x_months(ms.index)
        traces = _stack_traces(xm, [_segments(row) for _, row in ms.iterrows()])
        for tr in traces:
            tr["hovertemplate"] = tr["hovertemplate"].replace("PLN/MW/yr", "PLN/MW/day")
        if "arbitrage_2h" in mr.index.get_level_values(0):
            ma = mr.loc["arbitrage_2h"]
            traces.append(_line(_x_months(ma.index), ma["total"], "Arbitrage only", "@ink",
                                hovertemplate="%{y:,.0f} PLN/MW/day"))
        figs["monthly"] = {"data": traces, "layout": {"barmode": "relative", "bargap": 0.15,
                                                      "yaxis": {"title": {"text": "PLN per MW per day"}}}}
    sens = sensitivity_table(r)
    if sens["stacked"].notna().sum() > 1:
        figs["sensitivity"] = {"data": [
            {"type": "bar", "orientation": "h", "y": list(sens.index), "x": _num(sens["stacked"], 0),
             "name": "Stacking", "marker": {"color": "@afrr"}, "hovertemplate": "%{x:,.0f} PLN/MW/yr<extra>Stacking</extra>"},
            {"type": "bar", "orientation": "h", "y": list(sens.index), "x": _num(sens["arbitrage"], 0),
             "name": "Arbitrage only", "marker": {"color": "@arbitrage"},
             "hovertemplate": "%{x:,.0f} PLN/MW/yr<extra>Arbitrage only</extra>"},
        ], "layout": {"barmode": "group", "yaxis": {"autorange": "reversed", "automargin": True},
                      "xaxis": {"title": {"text": "PLN per MW per year (2 h battery, last 12 months)"}},
                      "margin_l": 280}}
    if "saturation_curves" in t:
        c = t["saturation_curves"]
        for key, group in (("curves_up", UP), ("curves_down", DOWN)):
            figs[key] = {"data": [{"type": "scatter", "mode": "lines", "x": _num(c.loc[k].index, 0),
                                   "y": _num(c.loc[k, "mean_price"]), "name": LABELS[k],
                                   "line": {"color": f"@{family(k)}", "width": 2},
                                   "hovertemplate": "%{x} MW: %{y:.0f} PLN/MW/h"} for k in group],
                         "layout": {"xaxis": {"title": {"text": "battery MW offered every hour"}, "range": [0, 1500]},
                                    "yaxis": {"title": {"text": "mean clearing price, PLN/MW/h"},
                                              "rangemode": "tozero"}, "hovermode": "x unified"}}
    if "fleet" in t:
        f = t["fleet"]
        fl = [f"{int(v):,} MW" for v in f.index]
        traces = _stack_traces(fl, [_segments(f.loc[v]) for v in f.index])
        traces.append({"type": "scatter", "mode": "lines", "x": fl, "y": _num([f["arbitrage_only"].iloc[0]] * len(f), 0),
                       "name": "Arbitrage only", "line": {"color": "@ink", "width": 2},
                       "hovertemplate": "%{y:,.0f} PLN/MW/yr<extra>Arbitrage only</extra>"})
        figs["fleet"] = {"data": traces, "layout": {"barmode": "relative", "bargap": 0.3,
                                                    "xaxis": {"title": {"text": "fleet of identical 2 h batteries"}},
                                                    "yaxis": {"title": {"text": "PLN per MW per year"}}}}
    return figs


def _fmt(v, spec: str = ",.0f", suffix: str = "") -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return html.escape(str(v))
    return "n/a" if math.isnan(f) else f"{f:{spec}}{suffix}"


def table_html(frame: pd.DataFrame, cols: dict[str, tuple[str, str]], first: str) -> str:
    head = "".join(f"<th>{html.escape(label)}</th>" for label, _ in cols.values())
    rows = []
    for key, row in frame.iterrows():
        key = " ".join(str(k) for k in key) if isinstance(key, tuple) else str(key)
        cells = "".join(f"<td>{_fmt(row[c], spec)}</td>" for c, (_, spec) in cols.items())
        rows.append(f"<tr><th>{html.escape(key)}</th>{cells}</tr>")
    return f'<table><thead><tr><th>{html.escape(first)}</th>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table>'


def _tile(label: str, value: str, sub: str) -> str:
    return (f'<div class="tile"><div class="label">{html.escape(label)}</div><div class="value">{value}</div>'
            f'<div class="sub">{html.escape(sub)}</div></div>')


def kpi_tiles(r: Analysis) -> str:
    t = r.tables
    rv = t["revenue_last12"]
    cells = []
    if "stacked_2h" in rv.index:
        row = rv.loc["stacked_2h"]
        share = row["reserves"] / row["total"] if row["total"] else np.nan
        cells.append(_tile("2 h battery, stacking", _fmt(row["total"] / 1000, ",.0f", "k"),
                           f"PLN/MW/yr; {_fmt(100 * share, '.0f')}% from reserves"))
    if "arbitrage_2h" in rv.index:
        cells.append(_tile("2 h battery, arbitrage only", _fmt(rv.loc["arbitrage_2h", "total"] / 1000, ",.0f", "k"),
                           "PLN/MW/yr on the day-ahead price RCE"))
    if "market_room" in t:
        room = t["market_room"]["mean_mw"]
        cells.append(_tile("Reserve market size, upward", _fmt(room[UP].sum(), ",.0f", " MW"),
                           "mean MW bought per hour, FCR + aFRR + mFRRd + RR"))
    if "fleet" in t and len(t["fleet"]) > 1:
        f = t["fleet"]
        big = f.index[-1]
        cells.append(_tile(f"Stacking with a {int(big):,} MW fleet", _fmt(f.loc[big, "total"] / 1000, ",.0f", "k"),
                           f"PLN/MW/yr vs {_fmt(f.loc[0, 'total'] / 1000, ',.0f')}k as a lone price-taker"
                           if 0 in f.index else "PLN/MW/yr"))
    if "capacity_layer" in t:
        cl = t["capacity_layer"]
        y = cl.index.max()
        cells.append(_tile(f"Capacity market, {y} auction terms", _fmt(cl.loc[y, "revenue_pln_mw_year"] / 1000, ",.0f", "k"),
                           f"PLN/MW/yr ({_fmt(cl.loc[y, 'closing_price_pln_kw_year'], '.2f')} PLN/kW/yr x KWD "
                           f"{_fmt(100 * cl.loc[y, 'kwd_battery'], '.2f')}%)"))
    a, b = r.last12
    return (f'<div class="tile-group"><h3>Last 12 months <span>{a:%d %b %Y} to {b:%d %b %Y}, 1 MW battery, '
            f'perfect foresight</span></h3><div class="tiles">{"".join(cells)}</div></div>')


REV_COLS = {"total": ("Total", ",.0f"), "arbitrage_net": ("Arbitrage net", ",.0f"),
            "rev_fcr_up": ("FCR up", ",.0f"), "rev_fcr_down": ("FCR down", ",.0f"),
            "rev_afrr_up": ("aFRR up", ",.0f"), "rev_afrr_down": ("aFRR down", ",.0f"),
            "rev_mfrr_up": ("mFRRd up", ",.0f"), "rev_mfrr_down": ("mFRRd down", ",.0f"),
            "rev_rr_up": ("RR up", ",.0f"), "cycles_per_day": ("Cycles/day", ".2f")}


def revenue_tables(r: Analysis) -> tuple[str, str]:
    t = r.tables
    base = [f"{s}_{e}h" for e in (1, 2, 4) for s in ("arbitrage", "reserves", "stacked")]
    last = t["revenue_last12"].reindex([c for c in base if c in t["revenue_last12"].index]).copy()
    last["arbitrage_net"] = last["arbitrage"] - last["degradation"]
    ann = t["annual"].copy()
    ann = ann[ann.index.get_level_values(0).isin(base)]
    view = pd.DataFrame({c: ann[f"{c}_per_year"] for c in ["total", "arbitrage"] + [f"rev_{k}" for k in PRODUCTS]})
    view["arbitrage_net"] = ann["arbitrage_per_year"] - ann["degradation_per_year"]
    view["cycles_per_day"] = ann["cycles_per_day"]
    view["days"] = ann["days"]
    cols = {**REV_COLS, "days": ("Days", ",.0f")}
    return table_html(last, REV_COLS, "Configuration"), table_html(view, cols, "Configuration, year")


def write_dashboard(r: Analysis, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    t = r.tables
    last_table, annual_table = revenue_tables(r)
    ma = t["market_annual"].reset_index()
    ma["product"] = ma["product"].map(LABELS)
    market_table = table_html(ma.set_index(["year", "product"]),
                              {"mean_price": ("Mean price PLN/MW/h", ",.1f"), "mean_mw": ("Mean MW bought", ",.0f"),
                               "max_mw": ("Max MW", ",.0f"), "cost_mpln": ("Cost, PLN million", ",.1f")}, "Year, product")
    corr = t["market_correlation"].reset_index()
    corr["product"] = corr["product"].map(LABELS)
    corr_table = table_html(corr.set_index(["year", "product"]), {"pearson": ("Pearson", ".2f"),
                                                                    "spearman": ("Spearman", ".2f"), "n": ("Hours", ",.0f")},
                            "Year, product")
    depth_table = table_html(t["market_depth_last12"].rename(index=LABELS),
                             {"bought_mw": ("MW bought, basic", ",.0f"),
                              "supplementary_mw": ("MW bought, supplementary", ",.0f"),
                              "offered_mw": ("MW offered, basic", ",.0f"), "cover": ("Offered / bought", ".2f")},
                             "Product")
    tr = t["market_trend"].rename(index=LABELS)
    trend_table = table_html(tr, {"pln_per_year": ("PLN/MW/h per year", ",.1f"), "pct_per_year": ("% per year", ".0f"),
                                  "first": ("First month", ",.0f"), "last": ("Last full month", ",.0f")}, "Product")
    room_table = sat_note = fleet_table = ""
    if "market_room" in t:
        room = t["market_room"].rename(index=LABELS)
        room_table = table_html(room, {"mean_mw": ("Mean MW bought", ",.0f"), "p05_mw": ("P5 MW", ",.0f"),
                                       "p95_mw": ("P95 MW", ",.0f"), "price_now": ("Mean price now", ",.0f"),
                                       "mw_half_price": ("Battery MW: price halves", ",.0f"),
                                       "mw_tenth_price": ("Battery MW: price / 10", ",.0f")}, "Product")
        val = t["saturation_validation"]
        sat_note = (f"Cutting the published offer stacks at the MW bought reproduces the published clearing price "
                    f"(within 0.01 PLN) in {100 * val['match_share'].min():.1f}-{100 * val['match_share'].max():.1f}% "
                    f"of hours, depending on the product.")
    if "fleet" in t:
        f = t["fleet"].copy()
        f["arbitrage_net"] = f["arbitrage"] - f["degradation"]
        fleet_table = table_html(f, {"total": ("Total", ",.0f"), "arbitrage_net": ("Arbitrage net", ",.0f"),
                                     "reserves": ("Reserves", ",.0f"), "cycles_per_day": ("Cycles/day", ".2f")},
                                 "Fleet MW")
    cap_table = ""
    if "capacity_layer" in t:
        cap_table = table_html(t["capacity_layer"], {"closing_price_pln_kw_year": ("Closing price PLN/kW/yr", ",.2f"),
                                                     "kwd_battery": ("KWD batteries", ".4f"),
                                                     "revenue_pln_mw_year": ("PLN/MW/yr", ",.0f")}, "Delivery year")
    act_table = ""
    if "activation" in t:
        act_table = table_html(t["activation"], {"days": ("Days", ",.0f"),
                                                 "afrr_capacity_per_year": ("aFRR capacity PLN/MW/yr", ",.0f"),
                                                 "activation_margin_per_year": ("Activation margin PLN/MW/yr", ",.0f"),
                                                 "energy_up_mwh_per_year": ("MWh up / yr", ",.0f"),
                                                 "energy_down_mwh_per_year": ("MWh down / yr", ",.0f")}, "Configuration")
    status = r.meta.get("status", {})
    replacements = {
        "%%PLOTLY%%": PLOTLY_JS,
        "%%REPO%%": REPO_URL,
        "%%KPIS%%": kpi_tiles(r),
        "%%LAST12%%": last_table,
        "%%ANNUAL%%": annual_table,
        "%%MARKET%%": market_table,
        "%%CORR%%": corr_table,
        "%%TREND%%": trend_table,
        "%%DEPTH%%": depth_table,
        "%%ROOM%%": room_table,
        "%%SATNOTE%%": html.escape(sat_note),
        "%%FLEET%%": fleet_table,
        "%%CAPACITY%%": cap_table,
        "%%ACTIVATION%%": act_table,
        "%%STATUS%%": html.escape("; ".join(f"{k}: {v}" for k, v in status.items())),
        "%%GENERATED%%": html.escape(str(r.meta.get("generated_utc", ""))),
        "%%DOWNLOADED%%": html.escape(str(r.meta.get("data_downloaded", ""))),
        "%%PERIOD%%": f"{r.period[0]:%d %B %Y} to {r.period[1]:%d %B %Y}",
        "%%FIGURES%%": json.dumps(figures(r), separators=(",", ":")).replace("</", "<\\/"),
    }
    page = TEMPLATE
    for key, value in replacements.items():
        page = page.replace(key, value)
    path = out_dir / "index.html"
    path.write_text(page, encoding="utf-8")
    t["revenue_last12"].round(1).to_csv(out_dir / "revenue_last12.csv")
    t["annual"].round(1).to_csv(out_dir / "revenue_annual.csv")
    t["market_monthly_prices"].round(2).to_csv(out_dir / "capacity_prices_monthly.csv")
    return path


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Poland Battery Revenue Stack</title>
<meta name="description" content="How much a battery can earn in Poland by stacking day-ahead arbitrage with balancing capacity, and how many MW of batteries the reserve markets can absorb. PSE open data.">
<script src="%%PLOTLY%%" charset="utf-8"></script>
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --border: rgba(11,11,11,0.10); --accent: #2a78d6;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --border: rgba(255,255,255,0.10); --accent: #3987e5;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --border: rgba(255,255,255,0.10); --accent: #3987e5;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1180px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 26px; margin: 0 0 4px; }
h2 { font-size: 18px; margin: 0 0 4px; }
h3 { font-size: 15px; margin: 0 0 8px; }
h3 span { color: var(--muted); font-weight: 400; }
p { margin: 4px 0 12px; color: var(--ink2); }
a { color: var(--accent); }
.lead { max-width: 860px; }
.meta { font-size: 13px; color: var(--muted); }
.tile-group { margin: 20px 0 8px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; }
.tile { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; }
.tile .label { font-size: 13px; color: var(--ink2); }
.tile .value { font-size: 28px; font-weight: 600; margin: 2px 0; }
.tile .sub { font-size: 12px; color: var(--muted); }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(460px, 1fr)); gap: 16px; margin-top: 16px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 14px 14px 6px;
  min-width: 0; }
.card.wide { grid-column: 1 / -1; }
.chart { width: 100%; height: 340px; }
.note { font-size: 13px; color: var(--muted); }
.table-wrap { overflow-x: auto; }
table { border-collapse: collapse; font-size: 13px; font-variant-numeric: tabular-nums; width: 100%; }
th, td { padding: 4px 8px; border-bottom: 1px solid var(--grid); text-align: right; white-space: nowrap; }
thead th { color: var(--ink2); font-weight: 600; }
tbody th { text-align: left; font-weight: 600; }
details summary { cursor: pointer; color: var(--accent); margin: 8px 0; }
h2.section { margin: 32px 0 0; font-size: 20px; }
footer { margin-top: 32px; font-size: 13px; color: var(--muted); }
@media (max-width: 520px) { .grid { grid-template-columns: 1fr; } .chart { height: 300px; } }
</style>
</head>
<body>
<main>
<h1>What can a battery earn in Poland?</h1>
<p class="lead">A 1 MW battery that sells day-ahead arbitrage and balancing capacity (FCR, aFRR, mFRRd, RR) in the
same hours, optimised day by day with perfect foresight on public PSE data (%%PERIOD%%), and what happens to reserve
prices when a fleet of batteries fills the shallow Polish reserve markets. An upper bound, not a forecast.</p>
<p class="meta">Generated %%GENERATED%% UTC. <a href="%%REPO%%">Code, method and assumptions</a></p>
%%KPIS%%

<h2 class="section">Balancing capacity market</h2>
<div class="grid">
<section class="card"><h2>Capacity prices, upward</h2><p>Monthly mean of the hourly clearing price of the basic
(day-ahead) procurement, PLN per MW per hour.</p><div id="prices_up" class="chart"></div></section>
<section class="card"><h2>Capacity prices, downward</h2><p>RR is only bought upwards.</p>
<div id="prices_down" class="chart"></div></section>
<section class="card"><h2>By hour of day, upward</h2><p>Last 12 months, local time.</p>
<div id="profile_up" class="chart"></div></section>
<section class="card"><h2>By hour of day, downward</h2><p>Last 12 months, local time.</p>
<div id="profile_down" class="chart"></div></section>
<section class="card"><h2>Market depth, upward</h2><p>MW offered in the basic procurement divided by MW bought
(monthly mean of hours). Below 1 the market is short of offers.</p><div id="depth_up" class="chart"></div></section>
<section class="card"><h2>Market depth, downward</h2><p>Same ratio for the downward products. Months in which
almost no mFRRd down was bought go off the scale (hover for the value).</p>
<div id="depth_down" class="chart"></div></section>
</div>
<section class="card wide" style="margin-top:16px"><h2>Prices, volumes and cost per year</h2>
<p class="note">Cost is the sum of price x MW over the hours of the basic procurement (supplementary procurement not
included). Partial years: 2024 from 14 June, the current year to date.</p>
<div class="table-wrap">%%MARKET%%</div>
<details><summary>Depth over the last 12 months (mean MW per hour)</summary><div class="table-wrap">%%DEPTH%%</div></details>
<details><summary>Trend of monthly prices</summary><div class="table-wrap">%%TREND%%</div></details>
<details><summary>Correlation of hourly capacity prices with the day-ahead price</summary>
<div class="table-wrap">%%CORR%%</div></details></section>

<h2 class="section">Revenue of a 1 MW battery</h2>
<div class="grid">
<section class="card wide"><h2>Strategies and durations, last 12 months</h2><p>Arbitrage is net of the degradation
cost; reserves are capacity payments only.</p><div id="revenue" class="chart" style="height:380px"></div>
<div class="table-wrap">%%LAST12%%</div></section>
<section class="card wide"><h2>Month by month, 2 h battery</h2><p>Stacking strategy by source, with the
arbitrage-only strategy of the same battery for comparison.</p><div id="monthly" class="chart" style="height:380px"></div></section>
<section class="card wide"><h2>Sensitivity, 2 h battery</h2><p>Efficiency, degradation cost and the energy that has to
be held per MW of reserve.</p><div id="sensitivity" class="chart" style="height:360px"></div></section>
</div>
<section class="card wide" style="margin-top:16px"><h2>Per year</h2>
<p class="note">PLN per MW per year; partial years scaled by days covered (sum / days x 365).
Download: <a href="revenue_annual.csv">revenue_annual.csv</a>, <a href="revenue_last12.csv">revenue_last12.csv</a>,
<a href="capacity_prices_monthly.csv">capacity_prices_monthly.csv</a>.</p>
<div class="table-wrap">%%ANNUAL%%</div>
<details><summary>Variant: aFRR activation energy on top of the capacity payment</summary>
<p class="note">Activated pro rata to the aFRR capacity bought, paid the platform price and replaced at the day-ahead
price with losses; only since the connection to the European aFRR platform.</p>
<div class="table-wrap">%%ACTIVATION%%</div></details></section>

<h2 class="section">How many batteries fit?</h2>
<div class="grid">
<section class="card"><h2>One product, rising battery volume, upward</h2><p>Mean clearing price when a block of
battery MW bidding 0 enters the published offer stack of every hour.</p><div id="curves_up" class="chart"></div></section>
<section class="card"><h2>One product, rising battery volume, downward</h2><p>Same for the downward products.</p>
<div id="curves_down" class="chart"></div></section>
<section class="card wide"><h2>A fleet of stacking batteries</h2><p>Each battery of the fleet runs the same optimiser;
their offers lower the clearing prices, the loop is repeated until it settles. Energy prices are kept as observed.</p>
<div id="fleet" class="chart" style="height:380px"></div><div class="table-wrap">%%FLEET%%</div></section>
</div>
<section class="card wide" style="margin-top:16px"><h2>Market room</h2><p class="note">%%SATNOTE%%</p>
<div class="table-wrap">%%ROOM%%</div></section>

<h2 class="section">Capacity market layer</h2>
<section class="card wide" style="margin-top:12px"><p>A capacity contract pays the auction clearing price for the
de-rated power (KWD for batteries, one value for every storage duration). Values from the auction parameter
regulations and the URE announcements of final results; to be verified before reuse, they change every auction.</p>
<div class="table-wrap">%%CAPACITY%%</div></section>

<footer>
<p>Source: information obtained from www.pse.pl (PSE S.A. public reports, api.raporty.pse.pl: rce-pln, cmbp-tp, mbp-tp,
cmbu-tu, mbu-tu, popmb-rmb, eb-rozl, crb-rozl), state as of %%DOWNLOADED%%. The information has been processed in
whole or in part (aggregated, combined and used in optimisation models); it is not an official PSE publication. Data
status: %%STATUS%%.</p>
<p>Market rules change often (for example the balancing capacity price caps and the competition criterion of the
balancing rules from 31 July and 1 December 2026, or the capacity market parameters of each auction) and need to be
checked against current sources. Perfect foresight results are upper bounds. Nothing here is investment advice.</p>
</footer>
</main>
<script>
const FIGS = %%FIGURES%%;
const THEMES = {
  light: { surface: "#fcfcfb", ink: "#0b0b0b", ink2: "#52514e", muted: "#898781", grid: "#e1e0d9", axis: "#c3c2b7",
    arbitrage: "#2a78d6", fcr: "#eb6834", afrr: "#1baf7a", mfrr: "#eda100", rr: "#e87ba4", capacity: "#008300" },
  dark: { surface: "#1a1a19", ink: "#f0efec", ink2: "#c3c2b7", muted: "#898781", grid: "#2c2c2a", axis: "#383835",
    arbitrage: "#3987e5", fcr: "#d95926", afrr: "#199e70", mfrr: "#c98500", rr: "#d55181", capacity: "#008300" },
};
function themeName() {
  const forced = document.documentElement.dataset.theme;
  if (forced === "light" || forced === "dark") return forced;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}
function resolve(value, t) {
  if (typeof value === "string" && value.startsWith("@") && value.slice(1) in t) return t[value.slice(1)];
  if (Array.isArray(value)) return value.map(v => resolve(v, t));
  if (value && typeof value === "object") {
    const out = {};
    for (const k in value) out[k] = resolve(value[k], t);
    return out;
  }
  return value;
}
function axis(t, extra) {
  return Object.assign({ gridcolor: t.grid, linecolor: t.axis, zerolinecolor: t.axis, tickfont: { color: t.muted },
    title: { font: { color: t.ink2, size: 12 } }, automargin: true }, extra || {});
}
function render() {
  if (!window.Plotly) return;
  const t = THEMES[themeName()];
  for (const id in FIGS) {
    const el = document.getElementById(id);
    if (!el) continue;
    const fig = resolve(FIGS[id], t);
    const lay = Object.assign({}, fig.layout || {});
    const left = lay.margin_l || 56;
    delete lay.margin_l;
    const xTitle = lay.xaxis && lay.xaxis.title;      // leave room for the x-axis title below the plot
    const layout = Object.assign({}, lay, {
      paper_bgcolor: t.surface, plot_bgcolor: t.surface,
      font: { family: 'system-ui, -apple-system, "Segoe UI", sans-serif', color: t.ink, size: 12 },
      margin: { l: left, r: 16, t: 8, b: xTitle ? 56 : 40 },
      legend: { orientation: "h", y: xTitle ? -0.32 : -0.2, font: { color: t.ink2 } },
      hoverlabel: { bgcolor: t.surface, bordercolor: t.axis, font: { color: t.ink } },
      xaxis: axis(t, lay.xaxis), yaxis: axis(t, lay.yaxis),
    });
    Plotly.react(el, fig.data, layout, { responsive: true, displaylogo: false });
  }
}
render();
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", render);
</script>
</body>
</html>
"""
