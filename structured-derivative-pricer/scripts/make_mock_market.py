#!/usr/bin/env python
"""Write a MOCK market-data file (synthetic numbers, for development and tests only).

    make_mock_market.py OUT.yaml [--as-of 2026-09-28] [--ticker SPX] [--spot 5000]
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
from pathlib import Path

import yaml

TENORS = ["1M", "3M", "6M", "1Y", "2Y", "3Y", "5Y"]
MONEYNESS = [0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.4]


def mock_surface(atm=0.18, skew=-0.12, curvature=0.15, term_slope=0.01):
    """Equity-like smile: downside skew flattening with maturity; ATM rising slowly with tenor."""
    years = {"1M": 1 / 12, "3M": 0.25, "6M": 0.5, "1Y": 1, "2Y": 2, "3Y": 3, "5Y": 5}
    rows = []
    for t in TENORS:
        T = years[t]
        damp = 1 / math.sqrt(max(T, 0.5))
        rows.append([round(atm + term_slope * math.log1p(T) + damp * (skew * math.log(k) + curvature * math.log(k) ** 2), 4)
                     for k in MONEYNESS])
    return rows


SWAPTION_EXPIRIES = ["1M", "3M", "6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y"]
SWAPTION_TENORS = ["1Y", "2Y", "5Y", "10Y", "20Y"]


def mock_swaption_normal_vols(base=0.0080, hump=0.0025, decay=2.0, tenor_slope=0.0006):
    """USD-like ATM normal vols: a hump at short expiries decaying with expiry, lower for long swaps."""
    years = {"1M": 1 / 12, "3M": 0.25, "6M": 0.5, "1Y": 1, "2Y": 2, "3Y": 3, "5Y": 5, "7Y": 7, "10Y": 10, "20Y": 20}
    return [[round(base + hump * math.exp(-years[e] / decay) - tenor_slope * math.log1p(years[t]), 5)
             for t in SWAPTION_TENORS] for e in SWAPTION_EXPIRIES]


def build(as_of: dt.date, ticker: str, spot: float) -> dict:
    return {
        "_note": "MOCK / SYNTHETIC market data for development and testing. Not real market data.",
        "as_of": as_of,
        "curves": {
            "USD": {
                "type": "bootstrap", "currency": "USD",
                "deposit": [{"tenor": t, "rate": r} for t, r in
                            [("1M", 0.0430), ("3M", 0.0425), ("6M", 0.0415), ("9M", 0.0405)]],
                "swap": [{"tenor": t, "rate": r} for t, r in
                         [("1Y", 0.0395), ("2Y", 0.0375), ("3Y", 0.0365), ("5Y", 0.0360),
                          ("7Y", 0.0362), ("10Y", 0.0370)]],
            },
            "USD-FLAT": {"type": "flat", "rate": 0.04},
        },
        "equities": {
            ticker: {
                "spot": spot,
                "calendar": "UnitedStates.NYSE",
                "dividend": {"type": "flat", "rate": 0.015},
                "vol": {"type": "surface", "strike_mode": "relative", "tenors": TENORS,
                        "strikes": MONEYNESS, "vols": mock_surface()},
            },
            f"{ticker}-FLATVOL": {
                "spot": spot, "calendar": "UnitedStates.NYSE",
                "dividend": {"type": "flat", "rate": 0.01},
                "vol": {"type": "constant", "value": 0.20},
            },
        },
        "swaption_vols": {
            "USD": {"type": "matrix", "currency": "USD", "vol_type": "normal",
                    "expiries": SWAPTION_EXPIRIES, "tenors": SWAPTION_TENORS, "vols": mock_swaption_normal_vols()},
        },
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--as-of", default=dt.date.today().isoformat())
    ap.add_argument("--ticker", default="SPX")
    ap.add_argument("--spot", type=float, default=5000.0)
    a = ap.parse_args()
    doc = build(dt.date.fromisoformat(a.as_of), a.ticker, a.spot)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    with open(a.out, "w") as f:
        f.write("# MOCK market data - synthetic numbers for development/testing only.\n")
        yaml.safe_dump({k: v for k, v in doc.items() if k != "_note"}, f, sort_keys=False,
                       default_flow_style=None, width=120)
    print(f"wrote {a.out}")
