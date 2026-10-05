"""Every path and setting in one place.

Settings come from the environment first, then from `fleet.env` in the config directory
(default ~/.config/tesla-fleet-voice, override with TESLA_FLEET_DIR). Credentials, tokens,
the private key, the vehicle list and saved places all live in that directory, never in
this repo. See fleet.example.env for every key.
"""
import ipaddress
import json
import os
from pathlib import Path

CONFIG_DIR = Path(os.environ.get('TESLA_FLEET_DIR', '~/.config/tesla-fleet-voice')).expanduser()
ENV_FILE = CONFIG_DIR / 'fleet.env'
USER_TOKEN_FILE = CONFIG_DIR / 'user-token.json'     # access + refresh token, mode 600
RAW_ACCESS_FILE = CONFIG_DIR / 'access-raw'          # bare access token for tesla-control, mode 600
VEHICLES_FILE = CONFIG_DIR / 'vehicles.json'         # cached vehicle list (holds the VIN), mode 600
PRIVATE_KEY = CONFIG_DIR / 'private-key.pem'         # virtual key, P-256, mode 600
PLACES = CONFIG_DIR / 'places.json'                  # saved places, see places.example.json
CACHE = Path(os.environ.get('TESLA_FLEET_CACHE', '~/.cache/tesla-fleet-voice')).expanduser()
DB = Path(os.environ.get('TESLA_FLEET_DB', '~/.local/share/tesla-fleet-voice/history.db')).expanduser()

AUTH_URL = 'https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token'
AUTHORIZE_URL = 'https://auth.tesla.com/oauth2/v3/authorize'
SCOPES = 'openid offline_access vehicle_device_data vehicle_cmds vehicle_charging_cmds'

_file_cache = None


def _file_values():
    global _file_cache
    if _file_cache is None:
        _file_cache = {}
        try:
            lines = ENV_FILE.read_text().splitlines()
        except OSError:
            lines = []
        for line in lines:
            key, sep, value = line.partition('=')
            if sep and not key.lstrip().startswith('#'):
                _file_cache[key.strip()] = value.strip().strip('"\'')
    return _file_cache


def setting(key, default=None):
    """Environment wins, then fleet.env, then the default. Empty counts as unset."""
    value = os.environ.get(key) or _file_values().get(key)
    return value if value else default


def required(key):
    value = setting(key)
    if not value:
        raise SystemExit(f'{key} is not set (environment or {ENV_FILE})')
    return value


FLEET_HOST = setting('FLEET_HOST', 'fleet-api.prd.na.vn.cloud.tesla.com')   # North America
FLEET_URL = 'https://' + FLEET_HOST
VEHICLES_BASE = FLEET_URL + '/api/1/vehicles/'
TESLA_CONTROL = Path(setting('TESLA_CONTROL', '~/.local/bin/tesla-control')).expanduser()
SAY_URL = setting('SAY_URL', '')             # e.g. a voice daemon's POST /say; empty = don't speak
SERVICE_PORT = int(setting('SERVICE_PORT', '7795'))
SERVICE_URL = setting('SERVICE_URL', f'http://127.0.0.1:{SERVICE_PORT}')


def spoken_name():
    """How sentences refer to the car, mid-sentence: 'the car' unless CAR_SPOKEN_NAME is set."""
    return setting('CAR_SPOKEN_NAME', 'the car')


def Spoken_name():
    """The same, capitalised for the start of a sentence."""
    s = spoken_name()
    return s[:1].upper() + s[1:]


def allowed_networks():
    """Loopback always; add your LAN / VPN subnets in ALLOWED_NETS (comma-separated CIDRs)."""
    nets = ['127.0.0.0/8'] + [n.strip() for n in setting('ALLOWED_NETS', '').split(',') if n.strip()]
    return [ipaddress.ip_network(n) for n in nets]


def home_latlon():
    """HOME_LAT/HOME_LON, or None. Only used to rank map-search hits by distance."""
    lat, lon = setting('HOME_LAT'), setting('HOME_LON')
    return (float(lat), float(lon)) if lat and lon else None


def vin():
    """The VIN to talk to: the vehicle named CAR_DISPLAY_NAME, else the only one in vehicles.json."""
    vehicles = json.loads(VEHICLES_FILE.read_text())
    want = setting('CAR_DISPLAY_NAME')
    matches = [v for v in vehicles if not want or v.get('display_name') == want]
    if len(matches) != 1:
        raise SystemExit(f'expected exactly one vehicle{" named " + want if want else ""} in {VEHICLES_FILE}, '
                         f'found {len(matches)}; set CAR_DISPLAY_NAME')
    return matches[0]['vin']


def app_domain():
    """The domain that hosts your public key and OAuth callback (registered with Tesla)."""
    return required('APP_DOMAIN')


def redirect_uri():
    return setting('REDIRECT_URI') or f'https://{app_domain()}/tesla/oauth/callback'
