"""Fake EIA-shaped records for tests. NOT real data: never use these numbers in findings."""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta


def hours(start: date, end: date):
    t = datetime(start.year, start.month, start.day)
    stop = datetime(end.year, end.month, end.day, 23)
    while t <= stop:
        yield t
        t += timedelta(hours=1)


def fake_demand_records(start: date, end: date, regions=("CISO", "ERCO")) -> list[dict]:
    rows = []
    for region in regions:
        base = 25_000 if region == "CISO" else 30_000
        for t in hours(start, end):
            demand = base + 5_000 * math.sin((t.hour - 8) / 24 * 2 * math.pi)
            exports = 500.0  # fake grid exports 500 MWh every hour: NG = D + TI
            for typ, val in (("D", demand), ("DF", demand * 1.02), ("NG", demand + exports), ("TI", exports)):
                rows.append({
                    "period": t.strftime("%Y-%m-%dT%H"), "respondent": region,
                    "respondent-name": region, "type": typ, "type-name": typ,
                    "value": f"{val:.0f}",  # EIA sends strings
                    "value-units": "megawatthours",
                })
    return rows


def fake_generation_records(start: date, end: date, regions=("CISO", "ERCO")) -> list[dict]:
    rows = []
    for region in regions:
        offset = 8 if region == "CISO" else 6  # rough UTC offset, just for a plausible shape
        for t in hours(start, end):
            local = (t.hour - offset) % 24
            solar = max(0.0, 10_000 * math.sin((local - 6) / 12 * math.pi)) if 6 <= local <= 18 else 0.0
            battery = -2_000 if 10 <= local <= 15 else (3_000 if 17 <= local <= 21 else 0)
            for fuel, val in (("SUN", solar), ("BAT", battery), ("NG", 12_000)):
                rows.append({
                    "period": t.strftime("%Y-%m-%dT%H"), "respondent": region,
                    "respondent-name": region, "fueltype": fuel, "type-name": fuel,
                    "value": f"{val:.0f}", "value-units": "megawatthours",
                })
    return rows
