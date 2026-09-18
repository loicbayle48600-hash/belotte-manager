import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradinglab.core.config import load_settings  # noqa: E402
from tradinglab.mt5.mock_adapter import MockBroker  # noqa: E402

FIXED_NOW = datetime(2026, 1, 20, 10, 0, tzinfo=timezone.utc)  # mardi 10:00 UTC, session LONDON


@pytest.fixture
def home(tmp_path):
    shutil.copytree(ROOT / "config", tmp_path / "config")
    return tmp_path


@pytest.fixture
def settings(home, monkeypatch):
    monkeypatch.setenv("TRADINGLAB_BROKER", "mock")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    return load_settings(home)


@pytest.fixture
def broker():
    b = MockBroker(seed=7)
    b.connect()
    b.set_now(FIXED_NOW)
    return b
