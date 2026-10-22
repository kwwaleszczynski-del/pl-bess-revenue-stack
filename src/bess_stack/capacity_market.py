"""Capacity market layer: revenue of a battery from a Polish capacity market contract (rynek mocy).

A capacity contract pays the auction clearing price (PLN/kW/year) for the capacity obligation, which for a battery
is its net achievable power multiplied by the de-rating factor KWD (korekcyjny wspolczynnik dyspozycyjnosci) of its
technology group in that auction:

    revenue (PLN/MW/year) = closing price (PLN/kW/year) x 1000 x KWD

The regulations set a single KWD for "electricity storage in the form of batteries, flywheels and
supercapacitors", without distinguishing storage duration, so the layer is the same per MW for a 1 h, 2 h or 4 h
battery and halves per MWh with each doubling of duration. The obligation to deliver during system stress periods
is not modelled in the dispatch: a battery that holds a capacity contract must also keep energy for those periods.

All values were read from the primary sources listed below on 22 October 2026. Market rules and parameters change
from auction to auction and must be checked against the current regulations before reuse.

Usage:
    layer = capacity_layer()        # one row per main auction with price, KWD and PLN/MW/year
"""
from __future__ import annotations

import pandas as pd

# KWD for batteries in the main auction for each delivery year, from the ministerial regulation on auction parameters
KWD_BATTERY = {
    2025: (0.9611, "Dz.U. 2020 poz. 1355, par. 10 pkt 9"),
    2026: (0.9611, "Dz.U. 2021 poz. 1480, par. 10 pkt 9"),
    2027: (0.9500, "Dz.U. 2022 poz. 1690, par. 10 pkt 9"),
    2028: (0.9500, "Dz.U. 2023 poz. 1561, par. 10 pkt 9"),
    2029: (0.6130, "Dz.U. 2024 poz. 1154, par. 10 pkt 9"),
    2030: (0.1339, "Dz.U. 2025 poz. 1066, par. 10 pkt 9 (group shared with pumped storage)"),
}

# closing prices of the main auctions, from the final results announced by the President of URE
CLOSING_PRICE = {
    2029: (264.90, "auction of 12 Dec 2024, closed in round 7; URE Information No. 02/2025 of 3 Jan 2025"),
    2030: (465.02, "auction of 11 Dec 2025, closed in round 2; URE Information No. 1/2026 of 2 Jan 2026"),
}

SOURCES = {
    "Dz.U. 2025 poz. 1066": "https://api.sejm.gov.pl/eli/acts/DU/2025/1066/text.pdf",
    "Dz.U. 2024 poz. 1154": "https://api.sejm.gov.pl/eli/acts/DU/2024/1154/text.pdf",
    "Dz.U. 2023 poz. 1561": "https://api.sejm.gov.pl/eli/acts/DU/2023/1561/text.pdf",
    "Dz.U. 2022 poz. 1690": "https://api.sejm.gov.pl/eli/acts/DU/2022/1690/text.pdf",
    "Dz.U. 2021 poz. 1480": "https://api.sejm.gov.pl/eli/acts/DU/2021/1480/text.pdf",
    "Dz.U. 2020 poz. 1355": "https://api.sejm.gov.pl/eli/acts/DU/2020/1355/text.pdf",
    "URE Information No. 02/2025": "https://www.ure.gov.pl/download/9/15112/Ostateczneogloszeniewynikowaukcjiglownejna2029.pdf",
    "URE Information No. 1/2026": "https://www.ure.gov.pl/download/9/15928/InformacjaPrezesaUrzeduRegulacjiEnergetykiNr12026.pdf",
}


def capacity_revenue(closing_price_pln_kw_year: float, kwd: float) -> float:
    """PLN per MW of battery power per year."""
    return closing_price_pln_kw_year * 1000.0 * kwd


def capacity_layer() -> pd.DataFrame:
    """One row per main auction with a known closing price: delivery year, price, KWD and revenue per MW."""
    rows = []
    for year, (price, src) in CLOSING_PRICE.items():
        kwd, kwd_src = KWD_BATTERY[year]
        rows.append({"delivery_year": year, "closing_price_pln_kw_year": price, "kwd_battery": kwd,
                     "revenue_pln_mw_year": capacity_revenue(price, kwd), "price_source": src,
                     "kwd_source": kwd_src})
    return pd.DataFrame(rows).set_index("delivery_year")


def kwd_history() -> pd.DataFrame:
    return pd.DataFrame([{"delivery_year": y, "kwd_battery": k, "source": s} for y, (k, s) in KWD_BATTERY.items()]
                        ).set_index("delivery_year")
