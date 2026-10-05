#!/usr/bin/env python3
"""Charging / left-open watchdog. Run every 5 min by car-watchdog.timer; most runs make NO API call.

Alerts (Pushover if configured; spoken via SAY_URL between 08:00 and 23:00):
  * charge complete: charging_state went Charging -> Complete (at a Supercharger, move the car
    before idle fees)
  * evening low: first run after 21:30, not charging and usable battery < 20 %
  * low, any time: a FRESH reading under 15 % while not charging. Once per discharge:
    latched in watchdog.json, cleared by charging or by reading back >= 20 %
    (no location scope, so this fires wherever the car is parked)
  * left open: a door, window, trunk or frunk open, or the car unlocked, on two reads
    >= 8 min apart while parked; and at the 21:30 check from the latest reading (an asleep
    car's cached state is still true: unlocking or opening it wakes it). Once per episode.

Cost model: NEVER wakes the car (a wake is the expensive call).
  asleep/offline        -> one GET /vehicles/{vin} (state) every 15 min, no data read
  online, charging      -> vehicle_data every 10 min, every 5 min in the last 10 minutes
  online, not charging  -> vehicle_data at most every 30 min, so polling never keeps it awake
  something open        -> re-read in 10 min to confirm (an open car does not sleep anyway)
  charge_state + vehicle_state + climate_state come from ONE vehicle_data request

    watchdog.py            # normal run (the timer)
    watchdog.py --dry      # do everything except alert; print what it would send
    watchdog.py --status   # print saved state, no API call
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import history  # noqa: E402
import speech  # noqa: E402
from tesla_auth import access_token  # noqa: E402

CHARGE = config.CACHE / 'last-charge.json'     # shared with car_service
STATE = config.CACHE / 'watchdog.json'
CLIMATE = config.CACHE / 'last-climate.json'
VEH = config.CACHE / 'last-vehicle.json'       # vehicle_state: locks, doors, windows
LOG = config.CACHE / 'watchdog.log'
LOW_PCT = 20
ANYTIME_LOW = 15
ANYTIME_RESET = 20
EVENING = (21, 30)
VOICE_HOURS = range(8, 23)
DRY = '--dry' in sys.argv


def log(msg):
    config.CACHE.mkdir(parents=True, exist_ok=True)
    with LOG.open('a') as f:
        f.write(f'{datetime.now():%m-%d %H:%M:%S} {msg}\n')
    if DRY or sys.stdout.isatty():
        print(msg)


def load(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def save(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


def alert(title, text):
    if DRY:
        log(f'DRY alert: {title}: {text}')
        return
    log(f'ALERT {title}: {text}')
    app, user = config.setting('PUSHOVER_TOKEN'), config.setting('PUSHOVER_USER')
    if app and user:
        try:
            body = urllib.parse.urlencode({'token': app, 'user': user, 'title': title, 'message': text}).encode()
            urllib.request.urlopen('https://api.pushover.net/1/messages.json', body, timeout=15).read()
        except Exception as e:                   # never let a URL/body with the key reach the log
            log(f'ALERT PATH BROKEN: pushover {type(e).__name__} {getattr(e, "code", "")}')
    if datetime.now().hour in VOICE_HOURS:
        speech.say(text, log)


def api(path, token):
    req = urllib.request.Request(config.VEHICLES_BASE + path, headers={'Authorization': 'Bearer ' + token})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)['response']


OPENINGS = {'df': 'driver door', 'pf': 'passenger door', 'dr': 'rear left door', 'pr': 'rear right door',
            'fd_window': 'driver window', 'fp_window': 'passenger window',
            'rd_window': 'rear left window', 'rp_window': 'rear right window',
            'ft': 'frunk', 'rt': 'trunk'}


def open_things(v):
    """What is open/unlocked in a vehicle_state, as spoken words. [] = secure."""
    out = [name for k, name in OPENINGS.items() if v.get(k)]
    if v.get('locked') is False:
        out.append('unlocked')
    return out


def say_list(items):
    return items[0] if len(items) == 1 else ', '.join(items[:-1]) + ' and ' + items[-1]


def phrase(opened):
    """['driver window', 'unlocked'] -> 'has the driver window open and is unlocked'"""
    things = [o for o in opened if o != 'unlocked']
    parts = []
    if things:
        parts.append(f'has the {say_list(things)} open')
    if 'unlocked' in opened:
        parts.append('is unlocked')
    return ' and '.join(parts)


def main():
    if '--status' in sys.argv:
        print(json.dumps(load(STATE, {}), indent=2))
        return
    name = config.Spoken_name()
    now = time.time()
    st = load(STATE, {})
    today = datetime.now().strftime('%Y-%m-%d')
    hm = datetime.now().timetuple()[3:5]
    evening_due = hm >= EVENING and st.get('evening_done') != today
    if now < st.get('next_check', 0) and not evening_due:
        return

    vin = urllib.parse.quote(config.vin(), safe='')
    prev = load(CHARGE, {})
    pv = load(VEH, {})
    c = v = k = r = None
    try:
        token = access_token()
        vstate = api(vin, token).get('state')
        stale = now - prev.get('_read_at', 0)
        want_data = vstate == 'online' and (
            prev.get('charging_state') in ('Charging', 'Starting') or stale >= 30 * 60 or evening_due
            or st.get('open_since'))
        if want_data:
            r = api(vin + '/vehicle_data?endpoints=charge_state%3Bvehicle_state%3Bclimate_state', token)
            c, v = r['charge_state'], r.get('vehicle_state') or {}
            c['_read_at'] = v['_read_at'] = now
            save(CHARGE, c)
            k = r.get('climate_state')
            if k:
                k['_read_at'] = now
                save(CLIMATE, k)
            if v:
                save(VEH, v)
                missing = [x for x in list(OPENINGS) + ['locked'] if x not in v]
                if missing:
                    log(f'vehicle_state lacks {missing}')
    except urllib.error.HTTPError as e:
        if e.code != 408:                      # 408 = went to sleep between the two calls
            log(f'tesla HTTP {e.code}')
            st['next_check'] = now + 15 * 60
            save(STATE, st)
            return
        vstate = 'asleep'
    except (urllib.error.URLError, TimeoutError, KeyError) as e:
        log(f'tesla unreachable: {type(e).__name__}')
        st['next_check'] = now + 10 * 60
        save(STATE, st)
        return

    cur = c or prev
    cs = cur.get('charging_state')
    if c is not None:
        log(f'{vstate} {cs} usable={c.get("usable_battery_level")} limit={c.get("charge_limit_soc")} '
            f'min_to_full={c.get("minutes_to_full_charge")}')
        if prev.get('charging_state') == 'Charging' and cs == 'Complete':
            alert(f'{name} done charging', f'{name} finished charging. ' + speech.cap(speech.describe(c)) + '.')
        pct = c.get('usable_battery_level')
        if isinstance(pct, int):
            if cs in ('Charging', 'Starting') or pct >= ANYTIME_RESET:
                st.pop('low_alerted', None)
            elif pct < ANYTIME_LOW and not st.get('low_alerted'):
                alert(f'{name} is low', f'Heads up, {speech.describe(c)}, and not charging.')
                st['low_alerted'] = True
    else:
        log(f'{vstate} (no data read)')
    try:                                       # history DB must never break the alerts
        if c is not None:
            history.record_reading(now, vstate, c, v, k)
            history.record_raw(now, 'watchdog', r)
        history.record_state(now, vstate)
    except Exception as e:
        log(f'history write failed: {type(e).__name__}: {e}')

    if v:
        opened = open_things(v)
        if opened:
            log(f'open: {opened}')
            if not st.get('open_since'):
                st['open_since'] = now
            elif now - st['open_since'] >= 8 * 60 and not st.get('open_alerted'):
                alert(f'{name} left open', f'{name} {phrase(opened)}.')
                st['open_alerted'] = True
        else:
            st.pop('open_since', None)
            st.pop('open_alerted', None)

    if evening_due:
        st['evening_done'] = today
        pct = cur.get('usable_battery_level')
        if pct is not None and pct < LOW_PCT and cs not in ('Charging', 'Starting'):
            age = '' if c else f' (as of {datetime.fromtimestamp(cur["_read_at"]):%-I:%M %p})'
            alert(f'{name} is low', f'Heads up, {speech.describe(cur)}{age}, and not charging.')
        vcur = v or pv
        opened = open_things(vcur) if vcur else []
        if opened and not st.get('open_alerted'):
            age = '' if v else f' as of {datetime.fromtimestamp(vcur["_read_at"]):%-I:%M %p}'
            alert(f'{name} left open', f"Night check{age}: {config.spoken_name()} {phrase(opened)}.")
            st['open_alerted'] = True

    if vstate != 'online':
        wait = 15
    elif st.get('open_since') and not st.get('open_alerted'):
        wait = 10
    elif cs in ('Charging', 'Starting'):
        mins = cur.get('minutes_to_full_charge') or 0
        wait = 5 if mins <= 10 else 10
    else:
        wait = 30
    st['next_check'] = now + wait * 60 - 30      # -30 s so the 5-min timer doesn't skip a beat
    st['last_state'] = vstate
    save(STATE, st)


if __name__ == '__main__':
    main()
