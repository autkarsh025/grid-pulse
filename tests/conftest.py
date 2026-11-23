import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Point every test at a temporary data folder so tests never touch your real database."""
    from src import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(config, "STAGING_DIR", tmp_path / "staging")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    # Tests use a two-grid world (CISO + ERCO) so expected row counts stay simple.
    two = ("CISO", "ERCO")
    monkeypatch.setattr(config, "REGIONS", {k: v for k, v in config.REGIONS.items() if k in two})
    monkeypatch.setattr(config, "REGION_TZ", {k: v for k, v in config.REGION_TZ.items() if k in two})
    monkeypatch.setattr(config, "MAX_DEMAND_MWH", {k: v for k, v in config.MAX_DEMAND_MWH.items() if k in two})

    # Tests never touch the network: weather calls get a session that fails instantly
    # (tests that exercise weather pass their own fake session).
    import requests
    from src import weather

    class Offline:
        def get(self, *a, **k):
            raise requests.ConnectionError("offline in tests")

    monkeypatch.setattr(weather, "make_session", lambda: Offline())
    return tmp_path
