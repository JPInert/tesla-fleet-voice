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
    POST /stop      {"q": "coffee"}   ADD A STOP before the current destination
                                      (navigation_gps_request order 2; needs coordinates)
    POST /cmd/<name> [{"arg": "70f"}]   name in ALLOWED below; no lock/unlock/trunk/windows by design
    POST body {"wake": false}    an asleep car answers {"asleep": true} and is NOT woken, so the
                                 caller can ask "wake it?" first
    POST body {"dry": true}      /nav and /stop: resolve the place and say what WOULD be sent;
                                 nothing is sent to the car (the resolver may read its location)
    any  ?say=1      also speak the result through SAY_URL

Never wakes the car for a READ. A COMMAND wakes it if needed, unless the body says "wake": false.
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
from collections import namedtuple
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import speech  # noqa: E402
from tesla_auth import access_token  # noqa: E402

CHARGE, CLIMATE = config.CACHE / 'last-charge.json', config.CACHE / 'last-climate.json'
LAST_POS = config.CACHE / 'last-position.json'      # the car's last GPS fix (mode 600): search centre when asleep
NAV_HISTORY = config.CACHE / 'nav-history.json'     # every destination / stop the car TOOK, newest last, 300 kept
GEO_CACHE = config.CACHE / 'geocode-cache.json'     # address -> [lat, lon]; Nominatim allows 1 request/s
ALLOWED = {            # url name -> tesla-control argv (arg appended when given)
    'climate-on': ['climate-on'], 'climate-off': ['climate-off'],
    'climate-set-temp': ['climate-set-temp'],
    'media-toggle': ['media-toggle-playback'], 'media-next': ['media-next-track'],
    'media-prev': ['media-previous-track'], 'volume-up': ['media-volume-up'],
    'volume-down': ['media-volume-down'],
    'volume-set': ['media-set-volume'],   # handled by volume_set(): the car ignores the absolute command
    'honk': ['honk'], 'flash': ['flash-lights'],
    'sentry-on': ['sentry-mode', 'on'], 'sentry-off': ['sentry-mode', 'off'],
    'ping': ['ping'],
}
FILLER_WORDS = r'\b(?:near me|nearby|closest|nearest|the one)\b'
FILLER_JOINERS = r'\s+(?:on|at|by|near|off|in)\s+'
BACKGROUND_GEOCODE = True   # record_nav geocodes a new destination off-thread (tests turn it off)
FEW_HITS = 5            # fewer OSM hits than this = OSM is thin on that chain; ask Google for more branches
VOLUME_STEP = 1 / 3     # one media-volume-up/down moves the car's 0-10 scale by about 0.333
_ctx = threading.local()                             # per-request search centre (one thread per request)
Hit = namedtuple('Hit', 'name street town state postcode lat lon')


def spoken(name):
    n = config.spoken_name()
    return {'climate-on': f'Cooling {n} down.', 'climate-off': 'Climate off.', 'honk': 'Honked.',
            'flash': 'Flashed the lights.', 'sentry-on': 'Sentry on.', 'sentry-off': 'Sentry off.',
            'volume-up': 'Volume up.', 'volume-down': 'Volume down.'}.get(name, 'Done.')


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


def asleep():
    return {'ok': False, 'asleep': True, 'sentence': f"{config.Spoken_name()}'s asleep."}


def atomic(path, data):
    """tmp + rename, mode 600: watchers (e.g. a phone widget publisher) only ever see whole files,
    and destinations / positions are private."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as fh:
        fh.write(json.dumps(data))
    tmp.replace(path)


def read_json(path, default):
    try:
        return json.loads(path.read_text()) if path.exists() else default
    except ValueError:
        return default


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


# ── where the car is: the centre for "nearest" ───────────────────────────

def origin():
    """Search centre for "nearest": where the CAR is, not where the house is. Lazy: only the first
    map search in a request pays for a location read; saved and remembered places never do."""
    if not hasattr(_ctx, 'origin'):
        log(f'origin: {locate()}')
    return _ctx.origin


def locate():
    """The car's position -> _ctx.origin, and returns how it was found. Awake: one
    drive_state;location_data read (needs the vehicle_location scope). Asleep or the read fails:
    the last fix (a sleeping car has not moved), else HOME_LAT/HOME_LON, else None. Never wakes."""
    pos, how = None, 'home'
    try:
        st, r = FLEET.call('GET', '/vehicle_data?endpoints=drive_state%3Blocation_data')
        d = (r.get('response') or {}).get('drive_state', {}) if st == 200 else {}
        if d.get('latitude') is not None and d.get('longitude') is not None:
            pos, how = (float(d['latitude']), float(d['longitude'])), 'live'
            atomic(LAST_POS, {'lat': pos[0], 'lon': pos[1], '_at': time.time()})
    except Exception as e:
        log(f'locate: {type(e).__name__}')
    if pos is None:
        j = read_json(LAST_POS, {})
        if 'lat' in j and 'lon' in j:
            pos, how = (j['lat'], j['lon']), 'last fix'
    if pos is None:
        pos = config.home_latlon()
        how = 'home' if pos else 'unknown'
    _ctx.origin = pos
    return how


def miles(la, lo):
    """Flat-earth miles from origin(); fine for "which branch is closer" within a city."""
    o = origin()
    if o is None:
        return float('inf')
    lat, lon = o
    return math.hypot(la - lat, (lo - lon) * math.cos(math.radians(lat))) * 69.0


def nearest(hits, home):
    lat, lon = home
    return min(hits, key=lambda h: math.hypot(float(h['lat']) - lat,
                                              (float(h['lon']) - lon) * math.cos(math.radians(lat))))


# ── cleaning what was said ───────────────────────────────────────────────

def car_names():
    """Words that mean the car: 'car', 'tesla', the spoken name, the app display name and any
    CAR_ALIASES (comma-separated, e.g. how speech-to-text misspells your car's name)."""
    names = {'car', 'tesla'}
    names |= {w for w in config.spoken_name().lower().split() if w != 'the'}
    if config.setting('CAR_DISPLAY_NAME'):
        names.add(config.setting('CAR_DISPLAY_NAME').lower())
    names |= {a.strip().lower() for a in config.setting('CAR_ALIASES', '').split(',') if a.strip()}
    return sorted(names, key=len, reverse=True)


def strip_car(key):
    """"pizza place on the car" -> "pizza place". The car's name in a request is which car to
    send it to, not part of the place; left in, it went into the map search and nothing matched."""
    alts = '|'.join(re.escape(n) for n in car_names())
    tail = re.compile(rf'[\s,]+(?:on|in|to|for|with|into)\s+(?:the\s+)?(?:{alts})\W*$', re.I)
    return tail.sub('', key.strip()).strip().lower()


def clean_query(key):
    """Spoken fillers break the map search ("pizza place on main street" = 0 hits, without
    "on" = the right one). Strip them; keep the original if nothing is left."""
    key = re.sub(FILLER_WORDS, ' ', key)
    key = ' '.join(re.sub(FILLER_JOINERS, ' ', f' {key} ').split()) or key
    return key.removeprefix('the ')


RECALL_TAIL = re.compile(
    r"\s+(?:that\s+|which\s+)?(?:i|we|you)\s+(?:asked\s+(?:you\s+)?to\s+|had\s+you\s+|just\s+)?"
    r"(?:map(?:ped)?|went\s+to|navigated\s+to|go\s+to|sent|were\s+at|was\s+at)(?:\s+(?:to\s+)?(?:it|there))?"
    r"(?:\s+(?:before|earlier|yesterday|today|last\s+time|the\s+other\s+day))?\s*$"
    r"|\s+(?:from\s+)?(?:before|earlier|yesterday|last\s+time|again)\s*$", re.I)
_HIST_STOP = {'the', 'that', 'this', 'a', 'an', 'place', 'spot', 'one', 'shop', 'store', 'restaurant', 'joint', 'thing'}


def recall_query(key):
    """"that ice cream place i mapped before" -> ("ice cream place", True). True = the speaker referred
    back to an earlier trip. A leading "that" alone is NOT a recall ("map that pizza place on X")."""
    k = key.strip().lower()
    back = bool(RECALL_TAIL.search(k))
    k = RECALL_TAIL.sub('', k).strip()
    k = re.sub(r'^(?:that|this|the)\s+', '', k)
    return k, back


# ── destination memory ───────────────────────────────────────────────────

def record_nav(query, label, addr, geocode=True):
    """Remember a destination the car accepted. Newest copy per ADDRESS: two branches of one chain
    are two entries (deduplicating by name lost which one was sent)."""
    h = [e for e in read_json(NAV_HISTORY, []) if e.get('address', '').lower() != addr.lower()]
    h.append({'q': recall_query(strip_car(query))[0] or query, 'label': label, 'address': addr, '_at': time.time()})
    atomic(NAV_HISTORY, h[-300:])
    if geocode and BACKGROUND_GEOCODE:               # coordinates for the "is it still near?" check, off-thread

        def bg():
            _ctx.origin = config.home_latlon()       # an address geocode needs no car read
            geocode_address(addr)
        threading.Thread(target=bg, daemon=True).start()


def history_match(key):
    """Newest past destination whose label or spoken words hold EVERY content word of `key` (prefix
    match both ways, so "ital" finds "Italian"). None when nothing fits."""
    words = [w for w in re.findall(r"[a-z0-9']+", key.lower()) if w not in _HIST_STOP]
    if not words:
        return None
    for e in reversed(read_json(NAV_HISTORY, [])):
        hay = re.findall(r"[a-z0-9']+", f"{e.get('label', '')} {e.get('q', '')}".lower())
        if all(any(t.startswith(w) or (w.startswith(t) and len(t) > 3) for t in hay) for w in words):
            return e
    return None


# ── the resolver: what was said -> (label, text the car can search) ──────

def resolve_place(body):
    """Order: an explicit address; a saved place (places.json); a remembered destination; a map search
    near the car (OpenStreetMap, then Google via Gemini if a key is set); else the raw text, which the
    car's own map search resolves."""
    places = read_json(config.PLACES, {})
    if body.get('address'):
        return body['address'], body['address']
    key, back = recall_query(strip_car(body.get('place') or body.get('q') or ''))
    if not key:
        raise ValueError('need q, place or address')
    for name in (key, key.removeprefix('the ')):
        if name in places:
            return name, places[name]
    if body.get('place'):
        raise ValueError(f'no saved place {key!r}')
    e = history_match(clean_query(key))
    if e and not back:
        c = read_json(GEO_CACHE, {}).get(e['address'])
        far = config.setting('HISTORY_NEAR_MI', '5')
        if c and miles(*c) > float(far):
            # far from the car: only a CHAIN is searched again (several branches near the car);
            # a one-off place is still the one that was meant
            near = geocode_near_home(key)
            if near is not None and _ctx.hits >= 2:
                log(f'nav {key!r}: remembered one is {miles(*c):.1f} mi away, {_ctx.hits} branches nearer; searching')
                e = None
    if e:
        log(f'nav {key!r} -> history: {e["label"]!r}')
        return e['label'], e['address']
    if back:
        raise ValueError(f"I don't have {key} in the places I've sent to {config.spoken_name()}")
    return search_near_home(key)


def search_near_home(key, fetch=None):
    """Nearest match to the car (origin()) from OpenStreetMap Nominatim: bounded ~25 km box, 40 hits,
    the closest wins. A miss asks Google once (gemini_place, if GEMINI_API_KEY is set; it also fixes
    spelling). When OSM knows only a few branches of a chain, Google's list is measured too and a
    closer branch wins. An address-looking query, or no hit anywhere: the text goes to the car as-is
    and the CAR's own search resolves it (that fallback found a small shop OSM did not know)."""
    if key[:1].isdigit():
        return key, key
    q = clean_query(key)
    g = geocode_near_home(key, fetch=fetch)
    if g is None:
        gp = gemini_place(q, timeout=9)
        if gp:
            return gp[0], f'{gp[0]}, {gp[1]}'
        return q, q
    if _ctx.hits < FEW_HITS and len(q.split()) <= 3:   # 4+ words is a qualified ask ("pizza place main street")
        # OSM's "nearest" is only the nearest branch OSM HAS. Google lists a few; each address is measured
        # here, because the model's own "closest" pick was wrong when checked.
        best = google_nearest(q)
        if best and best[2] + 0.2 < miles(g.lat, g.lon):
            log(f'nearest {key!r}: google {best[2]:.1f} mi beats osm {miles(g.lat, g.lon):.1f} mi')
            return best[0], f'{best[0]}, {best[1]}'
    if not g.street:
        return g.name, f'{g.name}, {g.lat:.6f},{g.lon:.6f}'
    # state comes from the map result (not a hardcoded one); see README: unproven on the car
    return g.name, ', '.join(x for x in (g.name, g.street, g.town, g.state, g.postcode) if x)


_nominatim_lock, _nominatim_last = threading.Lock(), [0.0]


def _nominatim_get(url):
    """One Nominatim request, paced to its usage policy (max 1 request per second)."""
    with _nominatim_lock:
        wait = _nominatim_last[0] + 1.05 - time.time()
        if wait > 0:
            time.sleep(wait)
        _nominatim_last[0] = time.time()
    req = urllib.request.Request(url, headers={'User-Agent': config.setting('MAP_USER_AGENT', 'tesla-fleet-voice/1.0')})
    return json.load(urllib.request.urlopen(req, timeout=8))


def geocode_near_home(key, bounded=True, clean=True, fetch=None):
    """The Nominatim hit nearest origin() as a Hit, or None. Sets _ctx.hits. clean=False for a full
    ADDRESS: the spoken-filler strip would eat "by"/"in" inside a town name."""
    o = origin()
    if clean:
        key = clean_query(key)
    params = {'q': key, 'format': 'json', 'limit': 40, 'addressdetails': 1,
              'countrycodes': config.setting('MAP_COUNTRY', 'us')}
    if o is not None:
        lat, lon = o
        params.update(bounded=int(bounded), viewbox=f'{lon - .25},{lat + .25},{lon + .25},{lat - .25}')
    elif bounded:                                     # nowhere to centre a "near" search
        _ctx.hits = 0
        return None
    try:
        hits = (fetch or _nominatim_get)('https://nominatim.openstreetmap.org/search?' + urllib.parse.urlencode(params))
    except Exception as e:
        log(f'nominatim: {type(e).__name__}')
        hits = []
    _ctx.hits = len(hits)
    if not hits:
        return None
    h = nearest(hits, o) if o is not None else hits[0]
    a = h.get('address', {})
    street = ' '.join(x for x in (a.get('house_number'), a.get('road')) if x)
    town = a.get('city') or a.get('town') or a.get('village') or a.get('suburb') or ''
    return Hit(h.get('name') or key, street, town, a.get('state'), a.get('postcode'), float(h['lat']), float(h['lon']))


def geocode_address(addr, fetch=None):
    """[lat, lon] for a full address or "Name, lat,lon" text, or None. Cached in GEO_CACHE."""
    c = read_json(GEO_CACHE, {})
    if addr in c:
        return c[addr]
    m = re.search(r'(-?\d{1,2}\.\d{4,}),\s*(-?\d{1,3}\.\d{4,})\s*$', addr)   # "Name, 40.1,-73.9"
    if m:
        c[addr] = [float(m.group(1)), float(m.group(2))]
    else:
        g = None
        # "Shop Name, 1 Main St, Town": Nominatim can't match the shop NAME glued to the address
        segs = [x.strip() for x in addr.split(',')]
        k = next((i for i, x in enumerate(segs) if x[:1].isdigit()), None)
        rest = ', '.join(segs[k:]) if k else (', '.join(segs[1:]) if len(segs) > 1 and not addr[:1].isdigit() else None)
        # OSM often lacks the HOUSE NUMBER: last try the street without it (within a block is fine for a
        # stop), dropping the state / ZIP that made those queries miss. The street word must still match.
        m2 = re.match(r'\s*\d+[a-z]?\s+(.+)', rest or addr, re.I)
        nonum = re.sub(r',\s*(?:[A-Z]{2}|\d{5})\b.*$', '', m2.group(1)) if m2 else None
        for q in (addr, rest, nonum):
            if q:
                g = geocode_near_home(q, bounded=False, clean=False, fetch=fetch)
                if g and q is nonum:
                    tok = re.search(r'\b(\d+(?:st|nd|rd|th)|[a-z]{4,})\b', q.split(',')[0], re.I)
                    if not (tok and tok.group(1).lower() in (g.street or '').lower()):
                        g = None                     # a different street, not "within a block"
                if g:
                    break
        if g is None and ',' not in addr:            # bare text the car searched itself: ask Google where
            gp = gemini_place(addr, timeout=30)
            if gp:
                g = geocode_near_home(gp[1], bounded=False, clean=False, fetch=fetch)
        if g is None:
            return None
        c[addr] = [g.lat, g.lon]
    atomic(GEO_CACHE, c)
    return c[addr]


# ── Google Maps through Gemini grounding (optional, needs GEMINI_API_KEY on a billed project) ──

def _gemini_post(body, timeout):
    """POST to Gemini generateContent; returns the reply text. The key goes in a header, so an error
    message (which carries the URL) never holds it."""
    model = config.setting('GEMINI_MODEL', 'gemini-3.8-flash')
    req = urllib.request.Request(
        f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent',
        json.dumps(body).encode(), {'Content-Type': 'application/json', 'x-goog-api-key': config.setting('GEMINI_API_KEY')})
    r = json.load(urllib.request.urlopen(req, timeout=timeout))
    return ''.join(p.get('text', '') for p in r['candidates'][0]['content']['parts'])


def _maps_ask(prompt, pattern, timeout):
    if not config.setting('GEMINI_API_KEY'):
        return None
    body = {'contents': [{'parts': [{'text': prompt}]}], 'tools': [{'googleMaps': {}}]}
    o = origin()
    if o is not None:
        body['toolConfig'] = {'retrievalConfig': {'latLng': {'latitude': o[0], 'longitude': o[1]}}}
    try:
        return json.loads(re.search(pattern, _gemini_post(body, timeout), re.S).group())
    except Exception as e:
        log(f'gemini maps: {type(e).__name__}')
        return None


def gemini_place(key, timeout=40):
    """(name, address) of the closest `key` per Google Maps, or None. Slow (seconds to tens of
    seconds), so it only runs after OpenStreetMap misses."""
    j = _maps_ask(f'Using Google Maps, find the {key} closest to the user. Reply with ONLY one line of '
                  'JSON: {"name": ..., "address": ...}', r'\{.*\}', timeout)
    return (j['name'], j['address']) if isinstance(j, dict) and j.get('name') and j.get('address') else None


def google_nearest(key, n=4, timeout=7):
    """(name, address, miles) of the closest of Google's n branches, measured by geocoding each address.
    The model's own "closest" answer is not trusted."""
    arr = _maps_ask(f'Using Google Maps, list the {n} "{key}" locations closest to the user. '
                    'Reply with ONLY a JSON array of {"name": ..., "address": ...}', r'\[.*\]', timeout)
    best = None
    for a in (arr if isinstance(arr, list) else [])[:n]:
        if not (isinstance(a, dict) and a.get('name') and a.get('address')):
            continue
        c = geocode_address(a['address'])
        if c and (best is None or miles(*c) < best[2]):
            best = (a['name'], a['address'], miles(*c))
    return best


# ── navigation ───────────────────────────────────────────────────────────

def nav_payload(text, now=None):
    """The body mimics the phone's "share to Tesla" intent; the car geocodes the text itself.
    Tesla allow-lists share sources: a plain web URL is refused (HTTP 400 src_not_supported)."""
    return {'type': 'share_ext_content_raw', 'value': {'android.intent.extra.TEXT': text},
            'locale': 'en-US', 'timestamp_ms': str(int((now or time.time()) * 1000))}


def nav(body):
    # navigation_request to an OFFLINE car returns 200 and Tesla wakes the car itself; no 408 comes
    # back. So "wake": false has to check the state BEFORE sending.
    if body.get('wake') is False and (s := state()) != 'online':
        log(f'nav {(body.get("q") or body.get("place"))!r} -> {s}, not waking (wake=false)')
        return asleep()
    _ctx.__dict__.pop('origin', None)        # threads are reused across requests: never inherit a stale centre
    t0 = time.time()
    label, addr = resolve_place(body)
    log(f'resolve {body.get("q") or body.get("place")!r}: {time.time() - t0:.1f}s')
    if body.get('dry'):
        return {'ok': True, 'dry': True, 'to': addr, 'sentence': f'Would send {label} to {config.spoken_name()}.'}
    payload = nav_payload(addr)
    st, r = FLEET.call('POST', '/command/navigation_request', payload)
    woke = False
    if st == 408:
        if body.get('wake') is False:
            return asleep()
        if not wake_and_wait():
            return {'ok': False, 'error': 'car did not wake', 'sentence': f"{config.Spoken_name()} wouldn't wake up."}
        woke = True
        st, r = FLEET.call('POST', '/command/navigation_request', payload)
    ok = st == 200 and r.get('response', {}).get('result') is True
    log(f'nav {label!r} -> http {st} ok={ok} woke={woke}')
    if ok:
        record_nav(body.get('q') or body.get('place') or label, label, addr)
    return {'ok': ok, 'http': st, 'to': addr, 'woke': woke,
            'sentence': f'Sent {label} to {config.spoken_name()}.' if ok
            else f"{config.Spoken_name()} didn't take the destination."}


# ── add a stop ───────────────────────────────────────────────────────────

def place_coords(body):
    """(label, street-or-'', lat, lon, how) for a stop, which needs coordinates. A saved place gives
    them as "<name>@": "lat,lon" in places.json (plain entries are text for the car's own search)."""
    places = read_json(config.PLACES, {})
    key = strip_car(body.get('q') or '').removeprefix('the ')
    if not key:
        raise ValueError('need q')
    if key in ('home', 'the house', 'my house', 'house'):
        key = 'home'
        if 'home@' not in places and config.home_latlon():
            return 'home', '', *config.home_latlon(), 'saved'
    if key + '@' in places:
        la, lo = map(float, places[key + '@'].split(','))
        return key, '', la, lo, 'saved'
    if key in places:                                   # a saved TEXT address: geocode it
        g = geocode_near_home(places[key], bounded=False, clean=False)
        if g:
            return key, '', g.lat, g.lon, 'saved'
    # the same ladder as nav (memory, OSM nearest the car, Google for thin chains and spelling), then
    # the chosen address is geocoded (cached)
    label, addr = resolve_place({'q': body.get('q')})
    c = geocode_address(addr) if addr != label.lower() or ',' in addr else None
    if not c:
        raise ValueError(f"couldn't find {key}")
    parts = [x.strip() for x in addr.split(',')]
    street = next((x for x in parts[1:] if x[:1].isdigit() and not re.fullmatch(r'-?[\d.]+', x)), '')
    return label, street, c[0], c[1], 'search'


def stop_payload(lat, lon):
    """navigation_gps_request order: 1 replaces the trip, 2 adds a stop BEFORE the destination, 3 after."""
    return {'lat': lat, 'lon': lon, 'order': 2}


def add_stop(body, speak=False):
    """Dry: resolve only, nothing sent. Asleep + "wake": false: answered at once, before the slow
    resolver, so a voice caller can ask first. Otherwise resolve + send run in a thread and this returns
    {"pending": true} straight away (the Google fallback can outlast a voice command's timeout); the
    thread speaks the result itself when speak is set."""
    if body.get('dry'):
        _ctx.__dict__.pop('origin', None)
        label, street, la, lo, how = place_coords(body)
        return {'ok': True, 'dry': True, 'how': how, 'lat': la, 'lon': lo,
                'sentence': f'Would add a stop at {label}' + (f' on {street}.' if street else '.')}
    if not (body.get('q') or '').strip():
        raise ValueError('need q')
    if body.get('wake') is False and (s := state()) != 'online':
        log(f'stop {body.get("q")!r} -> {s}, not waking (wake=false)')
        return asleep()

    def work():
        try:
            res = send_stop({**body, 'wake': True})    # the state said online: a late 408 wakes, not drops
        except ValueError as e:
            res = {'sentence': str(e).capitalize() + '.'}
        except Exception as e:
            log(f'error /stop: {type(e).__name__}: {e}')
            res = {'sentence': f'Something went wrong talking to {config.spoken_name()}.'}
        if speak and res.get('sentence') and not res.get('asleep'):
            speech.say(res['sentence'], log)
    threading.Thread(target=work, daemon=True).start()
    return {'ok': True, 'pending': True}


def send_stop(body):
    _ctx.__dict__.pop('origin', None)
    t0 = time.time()
    label, street, la, lo, how = place_coords(body)
    log(f'resolve stop {body.get("q")!r}: {time.time() - t0:.1f}s via {how}')
    payload = stop_payload(la, lo)
    st, r = FLEET.call('POST', '/command/navigation_gps_request', payload)
    woke = False
    if st == 408:                                        # unlike navigation_request, this one does 408
        if body.get('wake') is False:
            return asleep()
        if not wake_and_wait():
            return {'ok': False, 'error': 'car did not wake', 'sentence': f"{config.Spoken_name()} wouldn't wake up."}
        woke = True
        st, r = FLEET.call('POST', '/command/navigation_gps_request', payload)
    ok = st == 200 and r.get('response', {}).get('result') is True
    log(f'stop {label!r} ({how}) -> http {st} ok={ok} woke={woke}')
    if ok:
        record_nav(body.get('q') or label, label, f'{label}, {street}' if street else f'{label}, {la:.6f},{lo:.6f}')
    where = f'{label} on {street}' if street else label
    return {'ok': ok, 'http': st, 'how': how, 'woke': woke, 'error': r.get('error'),
            'sentence': f'Added a stop at {where}.' if ok else f"{config.Spoken_name()} didn't take the stop."}


# ── volume ───────────────────────────────────────────────────────────────

def media_volume():
    """The car's live volume on its 0-10 scale, 'asleep', or None."""
    st, r = FLEET.call('GET', '/vehicle_data?endpoints=vehicle_state')
    if st == 408:
        return 'asleep'
    return (r.get('response') or {}).get('vehicle_state', {}).get('media_info', {}).get('audio_volume') if st == 200 else None


def volume_arg(arg, cur=None):
    """'50%' / '50' -> 5.0 on the car's 0-10 scale; a bare '7' is 7 on that scale; '+2' / '-2' are
    relative to the live volume. Clamped to 0-10."""
    a = str(arg or '').strip().rstrip('%').strip()
    m = re.fullmatch(r'([+-])?(\d+(?:\.\d+)?)', a)
    if not m:
        raise ValueError(f'volume needs a number, got {arg!r}')
    n = float(m.group(2))
    if m.group(1):
        if cur is None:
            raise ValueError("couldn't read the car's volume")
        v = cur + (n if m.group(1) == '+' else -n)
    else:
        pct = str(arg).strip().endswith('%')
        v = n / 10 if pct or n > 10 else n
    return f'{max(0.0, min(10.0, v)):.1f}'


def volume_steps(cur, target):
    return round((target - cur) / VOLUME_STEP)


def volume_set(arg, wake=True, speak=False):
    """The car ignores tesla-control's absolute media-set-volume (it returns 0 and the volume does not
    move), while media-volume-up/down register. So: read the live volume, then step to the target in a
    thread (about 0.6 s a step) and answer at once."""
    cur = media_volume()
    if cur == 'asleep':
        if not wake:
            return asleep()
        if not wake_and_wait():
            return {'ok': False, 'error': 'car did not wake', 'sentence': f"{config.Spoken_name()} wouldn't wake up."}
        cur = media_volume()
    if cur is None or cur == 'asleep':
        return {'ok': False, 'sentence': f"Couldn't read {config.spoken_name()}'s volume."}
    target = float(volume_arg(arg, cur))
    steps = volume_steps(cur, target)
    if steps == 0:
        return {'ok': True, 'volume': cur, 'sentence': f'Volume is already at {round(cur * 10)} percent.'}
    name = 'volume-up' if steps > 0 else 'volume-down'

    def work():
        done = 0
        for _ in range(abs(steps)):
            if not signed(name, wake=False).get('ok'):
                break
            done += 1
        log(f'volume {cur:.2f} -> {target:.2f}: {done}/{abs(steps)} {name}')
        if speak:
            speech.say(f'Volume {round(target * 10)} percent.' if done == abs(steps)
                       else f'{config.Spoken_name()} stopped taking volume changes.', log)
    threading.Thread(target=work, daemon=True).start()
    return {'ok': True, 'pending': True, 'from': cur, 'to': target, 'steps': steps}


# ── signed commands ──────────────────────────────────────────────────────

def signed(name, arg=None, wake=True):
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
        if not wake:
            log(f'cmd {name} -> asleep, not waking (wake=false)')
            return asleep()
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
        say = q.get('say') == ['1']
        wake = body.get('wake') is not False
        try:
            if self.command == 'GET' and path == '/status':
                res = {'state': state()}
            elif self.command == 'GET' and path in ('/battery', '/temp'):
                res = read(path[1:].replace('temp', 'climate'))
            elif self.command == 'POST' and path == '/nav':
                res = nav(body)
            elif self.command == 'POST' and path == '/stop':
                res = add_stop(body, speak=say)
            elif self.command == 'POST' and path == '/cmd/volume-set':
                res = volume_set(body.get('arg'), wake=wake, speak=say)
            elif self.command == 'POST' and path.startswith('/cmd/') and path[5:] in ALLOWED:
                res = signed(path[5:], body.get('arg'), wake=wake)
            else:
                return self.reply(404, {'error': 'unknown endpoint'})
        except ValueError as e:
            res = {'ok': False, 'error': str(e), 'sentence': str(e).capitalize() + '.'}
        except Exception as e:
            log(f'error {path}: {type(e).__name__}: {e}')
            res = {'ok': False, 'error': type(e).__name__,
                   'sentence': f"Something went wrong talking to {config.spoken_name()}."}
        if say and res.get('sentence') and not res.get('asleep'):   # an asleep answer: the caller asks first
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
