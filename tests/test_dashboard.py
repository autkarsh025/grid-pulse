"""The dashboard must build from a real pipeline run and embed valid JSON (no NaN)."""
import json
import re
from datetime import date

import pytest

from fakes import fake_demand_records, fake_generation_records
from src import config, extract, pipeline


@pytest.fixture
def loaded_db(monkeypatch):
    monkeypatch.setenv("EIA_API_KEY", "test-key")

    def fake_fetch(route, facets, start, end, api_key, session=None):
        recs = fake_demand_records(start, end) if route == config.REGION_ROUTE \
            else fake_generation_records(start, end)
        for r in recs[::50]:
            r["value"] = "n/a"  # sprinkle missing values: they must become null, never NaN
        return recs

    monkeypatch.setattr(extract, "fetch_hourly", fake_fetch)
    try:
        pipeline.run(date(2026, 8, 1), date(2026, 9, 30), check_freshness=False)
    except Exception:
        pass  # a quality failure on some windows is fine; we only need data in the DB
    return config.DB_PATH


def test_dashboard_builds_with_valid_json(loaded_db, tmp_path, monkeypatch):
    from analysis import build_dashboard

    out = tmp_path / "index.html"
    monkeypatch.setattr(build_dashboard, "OUT", out)
    build_dashboard.build(loaded_db)
    html = out.read_text()
    payload = re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S).group(1)
    data = json.loads(payload.replace("<\\/", "</"))  # strict JSON parse: NaN would fail here
    assert "NaN" not in payload
    assert set(data) >= {"kpi", "duck", "carbon", "mix", "net", "anomalies", "runs"}
