import os
import sys
import tempfile
from pathlib import Path

# Point every config path at a throwaway dir before any module imports config.
_tmp = tempfile.mkdtemp(prefix='tfv-test-')
os.environ['TESLA_FLEET_DIR'] = _tmp
os.environ['TESLA_FLEET_CACHE'] = os.path.join(_tmp, 'cache')
os.environ['TESLA_FLEET_DB'] = os.path.join(_tmp, 'history.db')
for k in ('CAR_SPOKEN_NAME', 'HOME_LAT', 'HOME_LON', 'ALLOWED_NETS', 'SAY_URL', 'CAR_DISPLAY_NAME'):
    os.environ.pop(k, None)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'fleet'))
sys.path.insert(0, str(ROOT / 'examples'))

import pytest  # noqa: E402

import car_service  # noqa: E402
import config  # noqa: E402


class FakeFleet:
    """Stands in for the Fleet API connection. `routes` maps (method, path-prefix) -> (status, payload);
    anything unrouted answers 408 (asleep). Every call is recorded."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []

    def call(self, method, path, body=None, timeout=30):
        self.calls.append((method, path, body))
        for (m, prefix), res in self.routes.items():
            if m == method and path.startswith(prefix):
                return res
        return 408, {}

    def posts(self):
        return [c for c in self.calls if c[0] == 'POST']


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """No network and no car in any test: a fake Fleet, recording stubs for OpenStreetMap and Gemini,
    and a clean per-test state (search centre, saved places, memory, geocode cache)."""
    net = {'nominatim': [], 'gemini': []}
    fleet = FakeFleet()
    monkeypatch.setattr(car_service, 'FLEET', fleet)
    monkeypatch.setattr(car_service, 'BACKGROUND_GEOCODE', False)   # no thread outliving the fakes
    monkeypatch.setattr(car_service, '_nominatim_get', lambda url: net['nominatim'].append(url) or [])
    monkeypatch.setattr(car_service, '_gemini_post', lambda body, timeout: net['gemini'].append(body) or '')
    monkeypatch.setattr(car_service.speech, 'say', lambda *a, **k: None)
    car_service._ctx.__dict__.clear()
    for p in (config.PLACES, car_service.NAV_HISTORY, car_service.GEO_CACHE, car_service.LAST_POS):
        p.unlink(missing_ok=True)
    for k in ('GEMINI_API_KEY', 'CAR_ALIASES', 'HISTORY_NEAR_MI'):
        monkeypatch.delenv(k, raising=False)
    yield {'fleet': fleet, 'net': net}
    car_service._ctx.__dict__.clear()
