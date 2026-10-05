#!/usr/bin/env python3
"""car_service: the ONE path to the car for voice, phone and scripts. Home network only.

Listens on SERVICE_PORT (default 7795). Every request needs
`Authorization: Bearer <SERVICE_TOKEN>` AND a source address in loopback or ALLOWED_NETS
(your LAN / VPN subnets). Nothing here is meant to face the internet.

Why a daemon: it keeps the HTTPS connection to Fleet API open (no TLS handshake per call) and
sends commands straight away, with no "is it awake?" pre-check; only a 408 (asleep) triggers
wake + retry. Signed commands go through Tesla's tesla-control with a session cache.

    GET  /status                 {"state": "online"}            (no wake, no data call)
    GET  /battery   /temp        reading + "sentence"; live if awake, else last cached + age
    POST /nav       {"q": "coffee"} | {"place": "home"} | {"address": "..."}
    POST /cmd/<name> [{"arg": "70f"}]   name in ALLOWED below; no lock/unlock/trunk/windows by design
    any  ?say=1      also speak the result through SAY_URL

Never wakes the car for a READ. A COMMAND wakes it if needed (that is what was asked for).
"""
import hmac
import http.client
import ipaddress
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import speech  # noqa: E402
from tesla_auth import access_token  # noqa: E402

CHARGE, CLIMATE = config.CACHE / 'last-charge.json', config.CACHE / 'last-climate.json'
ALLOWED = {            # url name -> tesla-control argv (arg appended when given)
    'climate-on': ['climate-on'], 'climate-off': ['climate-off'],
    'climate-set-temp': ['climate-set-temp'],
    'media-toggle': ['media-toggle-playback'], 'media-next': ['media-next-track'],
    'media-prev': ['media-previous-track'], 'volume-up': ['media-volume-up'],
    'volume-down': ['media-volume-down'],
    'honk': ['honk'], 'flash': ['flash-lights'],
    'sentry-on': ['sentry-mode', 'on'], 'sentry-off': ['sentry-mode', 'off'],
    'ping': ['ping'],
}
FILLER_WORDS = r'\b(?:near me|nearby|closest|nearest|the one)\b'
FILLER_JOINERS = r'\s+(?:on|at|by|near|off|in)\s+'


def spoken(name):
    n = config.spoken_name()
    return {'climate-on': f'Cooling {n} down.', 'climate-off': 'Climate off.', 'honk': 'Honked.',
            'flash': 'Flashed the lights.', 'sentry-on': 'Sentry on.', 'sentry-off': 'Sentry off.'}.get(name, 'Done.')


def log(msg):
    print(time.strftime('%m-%d %H:%M:%S'), msg, flush=True)


def authorized(ip, auth_header, service_secret, nets):
    """Both conditions: source address in an allowed network AND the exact bearer value."""
    good = hmac.compare_digest(auth_header.encode(), ('Bearer ' + service_secret).encode())
    return good and any(ipaddress.ip_address(ip) in n for n in nets)


class Fleet:
    """One kept-alive HTTPS connection to Fleet API; reconnects on any socket error."""

    def __init__(self):
        self.lock, self.conn = threading.Lock(), None
        self._qvin = None

    @property
    def qvin(self):
        if self._qvin is None:
            self._qvin = urllib.parse.quote(config.vin(), safe='')
        return self._qvin

    def call(self, method, path, body=None, timeout=30):
        data = None if body is None else json.dumps(body)
        hdrs = {'Authorization': 'Bearer ' + access_token(), 'Content-Type': 'application/json'}
        with self.lock:
            for attempt in (1, 2):
                try:
                    if self.conn is None:
                        self.conn = http.client.HTTPSConnection(config.FLEET_HOST, timeout=timeout)
                    self.conn.request(method, '/api/1/vehicles/' + self.qvin + path, data, hdrs)
                    r = self.conn.getresponse()
                    raw = r.read()
                    try:
                        payload = json.loads(raw or b'{}')
                    except ValueError:
                        payload = {}
                    return r.status, payload
                except (http.client.HTTPException, OSError):
                    if self.conn:
                        self.conn.close()
                    self.conn = None
                    if attempt == 2:
                        raise


FLEET = Fleet()


def state():
    st, r = FLEET.call('GET', '')
    return r.get('response', {}).get('state') if st == 200 else f'http {st}'


def wake_and_wait(limit=60):
    t0 = time.time()
    FLEET.call('POST', '/wake_up')
    while time.time() - t0 < limit:
        if state() == 'online':
            log(f'woke in {time.time() - t0:.1f}s')
            return True
        time.sleep(2)
    return False


def atomic(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data))
    tmp.replace(path)          # watchers (e.g. a phone widget publisher) only ever see whole files


# ── reads ────────────────────────────────────────────────────────────────

def read(kind):
    s = state()
    name = config.Spoken_name()
    if s == 'online':
        st, r = FLEET.call('GET', '/vehicle_data?endpoints=charge_state%3Bclimate_state')
        if st == 200:
            resp = r['response']
            c, k = resp['charge_state'], resp.get('climate_state') or {}
            now = time.time()
            c['_read_at'] = k['_read_at'] = now
            config.CACHE.mkdir(parents=True, exist_ok=True)
            atomic(CHARGE, c)
            if len(k) > 1:
                atomic(CLIMATE, k)
            try:
                import history
                history.record_reading(now, 'online', c, {}, k)
                history.record_raw(now, 'service', resp)
            except Exception as e:
                log(f'history: {type(e).__name__}')
            data = c if kind == 'battery' else k
            text = speech.battery_sentence(c) if kind == 'battery' else speech.temp_sentence(k)
            return {'state': s, 'live': True, 'sentence': text, **{x: data.get(x) for x in data if not x.startswith('_')}}
        if st != 408:
            return {'state': s, 'error': f'tesla http {st}', 'sentence': f"Couldn't reach {config.spoken_name()}."}
        s = 'asleep'
    path = CHARGE if kind == 'battery' else CLIMATE
    if not path.exists():
        return {'state': s, 'live': False, 'sentence': f"{name}'s {s}, and I don't have an earlier reading."}
    d = json.loads(path.read_text())
    text = speech.battery_sentence(d) if kind == 'battery' else speech.temp_sentence(d)
    return {'state': s, 'live': False, 'read_at': d['_read_at'],
            'sentence': f"{name}'s {s}, not waking it. Last reading, {speech.ago(d['_read_at'])}: {text}"}


# ── commands ─────────────────────────────────────────────────────────────

def resolve_place(body):
    places = json.loads(config.PLACES.read_text()) if config.PLACES.exists() else {}
    if body.get('address'):
        return body['address'], body['address']
    key = (body.get('place') or body.get('q') or '').strip().lower()
    if not key:
        raise ValueError('need q, place or address')
    for name in (key, key.removeprefix('the ')):
        if name in places:
            return name, places[name]
    if body.get('place'):
        raise ValueError(f'no saved place {key!r}')
    return search_near_home(key)


def clean_query(key):
    """Spoken fillers break the map search ("pizza place on main street" = 0 hits, without
    "on" = the right one). Strip them; keep the original if nothing is left."""
    key = re.sub(FILLER_WORDS, ' ', key)
    key = ' '.join(re.sub(FILLER_JOINERS, ' ', f' {key} ').split()) or key
    return key.removeprefix('the ')


def nearest(hits, home):
    lat, lon = home
    return min(hits, key=lambda h: math.hypot(float(h['lat']) - lat,
                                              (float(h['lon']) - lon) * math.cos(math.radians(lat))))


def search_near_home(key, fetch=None):
    """Nearest match to home from OpenStreetMap Nominatim (bounded ~25 km box, 40 hits, closest wins).
    An address-looking query, no HOME_LAT/HOME_LON, or no hit: the text goes to the car as-is and
    the CAR's own search resolves it (that fallback found a small shop OSM did not know)."""
    home = config.home_latlon()
    if key[:1].isdigit() or home is None:
        return key, key
    key = clean_query(key)
    lat, lon = home
    url = 'https://nominatim.openstreetmap.org/search?' + urllib.parse.urlencode({
        'q': key, 'format': 'json', 'limit': 40, 'addressdetails': 1,
        'countrycodes': config.setting('MAP_COUNTRY', 'us'), 'bounded': 1,
        'viewbox': f'{lon - .25},{lat + .25},{lon + .25},{lat - .25}'})
    fetch = fetch or (lambda u: json.load(urllib.request.urlopen(urllib.request.Request(
        u, headers={'User-Agent': config.setting('MAP_USER_AGENT', 'tesla-fleet-voice/1.0')}), timeout=8)))
    try:
        hits = fetch(url)
    except Exception as e:
        log(f'nominatim: {type(e).__name__}')
        hits = []
    if not hits:
        return key, key
    h = nearest(hits, home)
    a = h.get('address', {})
    street = ' '.join(x for x in (a.get('house_number'), a.get('road')) if x)
    town = a.get('city') or a.get('town') or a.get('village') or a.get('suburb') or ''
    name = h.get('name') or key
    if not street:
        return name, f'{name}, {float(h["lat"]):.6f},{float(h["lon"]):.6f}'
    return name, ', '.join(x for x in (name, street, town, a.get('state'), a.get('postcode')) if x)


def nav_payload(text, now=None):
    """The body mimics the phone's "share to Tesla" intent; the car geocodes the text itself."""
    return {'type': 'share_ext_content_raw', 'value': {'android.intent.extra.TEXT': text},
            'locale': 'en-US', 'timestamp_ms': str(int((now or time.time()) * 1000))}


def nav(body):
    label, addr = resolve_place(body)
    payload = nav_payload(addr)
    st, r = FLEET.call('POST', '/command/navigation_request', payload)
    woke = False
    if st == 408:
        if not wake_and_wait():
            return {'ok': False, 'error': 'car did not wake', 'sentence': f"{config.Spoken_name()} wouldn't wake up."}
        woke = True
        st, r = FLEET.call('POST', '/command/navigation_request', payload)
    ok = st == 200 and r.get('response', {}).get('result') is True
    log(f'nav {label!r} -> http {st} ok={ok} woke={woke}')
    return {'ok': ok, 'http': st, 'to': addr, 'woke': woke,
            'sentence': f'Sent {label} to {config.spoken_name()}.' if ok
            else f"{config.Spoken_name()} didn't take the destination."}


def signed(name, arg=None):
    argv = ALLOWED[name] + ([str(arg)] if arg is not None else [])
    tok = access_token()
    vin = config.vin()
    fd = os.open(config.RAW_ACCESS_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as fh:
        fh.write(tok)
    env = dict(os.environ, TESLA_VIN=vin, TESLA_TOKEN_FILE=str(config.RAW_ACCESS_FILE),
               TESLA_KEY_FILE=str(config.PRIVATE_KEY),
               TESLA_CACHE_FILE=str(config.CACHE / 'session-cache.json'))

    def run():
        try:
            p = subprocess.run([str(config.TESLA_CONTROL), *argv], env=env, capture_output=True, text=True, timeout=45)
        except subprocess.SubprocessError as e:
            return 1, type(e).__name__
        return p.returncode, (p.stdout + p.stderr).replace(vin, '<VIN>').replace(tok, '<TOKEN>').strip()

    t0 = time.time()
    rc, out = run()
    woke = False
    if rc and any(w in out.lower() for w in ('asleep', 'offline', '408', 'unavailable')):
        if not wake_and_wait():
            return {'ok': False, 'out': out, 'sentence': f"{config.Spoken_name()} wouldn't wake up."}
        woke = True
        rc, out = run()
    log(f'cmd {name} rc={rc} {time.time() - t0:.1f}s woke={woke} {out[:120]!r}')
    if rc == 0:
        text = spoken(name)
    elif 'ingear' in out:
        text = f"{config.Spoken_name()}'s in gear, it won't do that while driving."
    else:
        text = f"{config.Spoken_name()} didn't do it."
    return {'ok': rc == 0, 'woke': woke, 'out': out, 'sentence': text}


# ── http ─────────────────────────────────────────────────────────────────

class H(BaseHTTPRequestHandler):
    server_version, sys_version = 'car-service', ''
    service_secret = ''
    nets = []

    def log_message(self, *a):
        pass

    def reply(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def allowed(self):
        ip = self.client_address[0]
        if not authorized(ip, self.headers.get('Authorization', ''), self.service_secret, self.nets):
            log(f'DENIED {ip} {self.command} {self.path.split("?")[0]}')
            self.reply(403, {'error': 'forbidden'})
            return False
        return True

    def handle_any(self, body):
        url = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(url.query)
        path = url.path.rstrip('/')
        try:
            if self.command == 'GET' and path == '/status':
                res = {'state': state()}
            elif self.command == 'GET' and path in ('/battery', '/temp'):
                res = read(path[1:].replace('temp', 'climate'))
            elif self.command == 'POST' and path == '/nav':
                res = nav(body)
            elif self.command == 'POST' and path.startswith('/cmd/') and path[5:] in ALLOWED:
                res = signed(path[5:], body.get('arg'))
            else:
                return self.reply(404, {'error': 'unknown endpoint'})
        except ValueError as e:
            res = {'ok': False, 'error': str(e), 'sentence': str(e).capitalize() + '.'}
        except Exception as e:
            log(f'error {path}: {type(e).__name__}: {e}')
            res = {'ok': False, 'error': type(e).__name__,
                   'sentence': f"Something went wrong talking to {config.spoken_name()}."}
        if q.get('say') == ['1'] and res.get('sentence'):
            speech.say(res['sentence'], log)
        self.reply(200, res)

    def do_GET(self):
        if self.allowed():
            self.handle_any({})

    def do_POST(self):
        if not self.allowed():
            return
        n = min(int(self.headers.get('Content-Length') or 0), 4096)
        try:
            body = json.loads(self.rfile.read(n) or b'{}')
        except ValueError:
            body = {}
        self.handle_any(body if isinstance(body, dict) else {})


def main():
    H.service_secret = config.required('SERVICE_TOKEN')
    H.nets = config.allowed_networks()
    config.vin()                       # fail at start, not on the first request
    log(f'car_service on :{config.SERVICE_PORT}, nets {[str(n) for n in H.nets]}')
    ThreadingHTTPServer((config.setting('SERVICE_BIND', '0.0.0.0'), config.SERVICE_PORT), H).serve_forever()


if __name__ == '__main__':
    main()
