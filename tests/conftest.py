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
