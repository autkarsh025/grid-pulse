"""Extract: pull hourly grid data from the EIA Open Data API v2 and save it untouched.

Why save raw JSON first? If a transform bug is found later, you can re-run the
transform from the saved files without calling the API again ("replayability").
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src import config

log = logging.getLogger(__name__)


class ExtractError(RuntimeError):
    """Raised when the API returns something we can't use."""


def get_api_key() -> str:
    key = os.getenv(config.EIA_API_KEY_ENV)
    if not key:
        raise ExtractError(
            f"Set {config.EIA_API_KEY_ENV} in your environment or .env file "
            "(free key: https://www.eia.gov/opendata/)."
        )
    return key


def make_session() -> requests.Session:
    """A session that retries transient failures (rate limits, 5xx) with exponential backoff."""
    retry = Retry(
        total=5,
        backoff_factor=2,  # waits 2s, 4s, 8s, ...
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def fetch_hourly(
    route: str,
    facets: dict[str, list[str]],
    start: date,
    end: date,
    api_key: str,
    session: requests.Session | None = None,
) -> list[dict]:
    """Fetch every hourly row for [start, end] (inclusive, UTC dates), following pagination."""
    session = session or make_session()
    url = f"{config.EIA_BASE_URL}/{route}"
    params: dict[str, object] = {
        "api_key": api_key,
        "frequency": "hourly",
        "data[0]": "value",
        "start": f"{start.isoformat()}T00",
        "end": f"{end.isoformat()}T23",
        "sort[0][column]": "period",
        "sort[0][direction]": "asc",
        "length": config.PAGE_SIZE,
    }
    for name, values in facets.items():
        params[f"facets[{name}][]"] = values  # requests repeats the key for each value

    rows: list[dict] = []
    offset = 0
    while True:
        params["offset"] = offset
        resp = session.get(url, params=params, timeout=config.REQUEST_TIMEOUT_S)
        if resp.status_code != 200:
            raise ExtractError(f"EIA returned HTTP {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        if "error" in body:
            raise ExtractError(f"EIA error: {body['error']}")
        payload = body.get("response", {})
        page = payload.get("data", [])
        total = int(payload.get("total", 0))  # EIA sends this as a string
        rows.extend(page)
        log.info("%s: fetched %d/%d rows", route, len(rows), total)
        if not page or len(rows) >= total:
            break
        offset += len(page)
    return rows


def save_raw(rows: list[dict], dataset: str, start: date, end: date) -> Path:
    """Write rows to data/raw/<dataset>/<start>_<end>.json and return the path."""
    out_dir = config.RAW_DIR / dataset
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{start.isoformat()}_{end.isoformat()}.json"
    path.write_text(json.dumps(rows))
    return path


def extract_window(start: date, end: date, session: requests.Session | None = None) -> dict[str, Path]:
    """Extract demand and generation for all configured regions. Returns raw file paths."""
    api_key = get_api_key()
    session = session or make_session()
    regions = list(config.REGIONS)

    demand_rows = fetch_hourly(
        config.REGION_ROUTE,
        {"respondent": regions, "type": list(config.DEMAND_TYPES)},
        start, end, api_key, session,
    )
    # No fueltype facet: take every fuel so nothing is missed if EIA adds codes.
    generation_rows = fetch_hourly(
        config.FUEL_ROUTE, {"respondent": regions}, start, end, api_key, session,
    )
    return {
        "demand": save_raw(demand_rows, "demand", start, end),
        "generation": save_raw(generation_rows, "generation", start, end),
    }
