"""End-to-end run on synthetic data: tables, README figures and the dashboard are written (no network)."""
from __future__ import annotations

import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

from bess_stack.analysis import Data, run_analysis
from bess_stack.dashboard import write_dashboard
from bess_stack.figures import write_figures
from bess_stack.pse_api import SeriesStatus

from conftest import synthetic_capacity, synthetic_energy, synthetic_offers


@pytest.fixture(scope="module")
def result():
    start, end = date(2025, 7, 14), date(2025, 7, 18)
    energy = synthetic_energy(start, end)
    price, volume = synthetic_capacity(start, end)
    q = energy.index
    afrr_energy = pd.DataFrame({"afrr_up_mwh": 20.0, "afrr_down_mwh": -15.0, "business_date": energy["business_date"]},
                               index=q)
    afrr_price = pd.DataFrame({"afrr_up_price": energy["price"] + 150, "afrr_down_price": energy["price"] - 150,
                               "imbalance_price": energy["price"], "business_date": energy["business_date"]}, index=q)
    sup = pd.DataFrame({k: np.nan for k in ["fcr_up", "fcr_down", "afrr_up", "afrr_down", "mfrr_up", "mfrr_down"]},
                       index=q)
    sup["business_date"] = energy["business_date"]
    frames = {"rce": energy, "sdac": energy, "cap_price": price, "cap_volume": volume, "sup_price": sup,
              "sup_volume": sup, "afrr_energy": afrr_energy, "afrr_price": afrr_price,
              "offers": synthetic_offers(price, volume, n=8)}
    data = Data(frames, {k: SeriesStatus(k) for k in frames}, "18 July 2025")
    return run_analysis(data, workers=1, fleets_mw=(0, 2000), iterations=2)


def test_tables_and_summary(result, tmp_path):
    result.write_tables(tmp_path)
    s = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert s["period"] == ["2025-07-14", "2025-07-17"] and s["days"] == 4
    stacked, arb = s["revenue_last12"]["stacked_2h"], s["revenue_last12"]["arbitrage_2h"]
    assert stacked["total"] >= arb["total"]
    assert s["market_room_mw"]["fcr_up"] == 197
    assert s["fleet_total_pln_mw_year"]["2000"] <= s["fleet_total_pln_mw_year"]["0"] + 1
    for name in ("annual", "revenue_last12", "market_depth", "saturation_curves", "fleet", "activation",
                 "capacity_layer", "market_correlation"):
        assert (tmp_path / f"{name}.csv").exists(), name
    assert not (tmp_path / "_daily.csv").exists()


def test_figures_and_dashboard(result, tmp_path):
    paths = write_figures(result, tmp_path / "img")
    assert {p.name for p in paths} >= {"reserve_prices.png", "revenue_stack.png", "saturation.png", "sensitivity.png"}
    page = write_dashboard(result, tmp_path).read_text(encoding="utf-8")
    assert "%%" not in page and "cdn.plot.ly" in page
    assert '"fleet"' in page and "Capacity market" in page
    assert (tmp_path / "revenue_annual.csv").exists()
