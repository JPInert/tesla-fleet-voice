"""Spoken sentences built from Fleet API state. Pure functions, no I/O except say()."""
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402


def f(celsius):
    """The API reports Celsius; the sentences say Fahrenheit."""
    return round(celsius * 9 / 5 + 32)


def battery_sentence(c):
    """c = charge_state. Usable percent first: that is the number the Tesla app shows
    (observed once: app 79, usable_battery_level 79, battery_level 80)."""
    pct, raw = c.get('usable_battery_level'), c.get('battery_level')
    s = f"{config.Spoken_name()}'s at {pct} percent" + (f' usable, {raw} total' if raw is not None and raw != pct else '')
    if c.get('battery_range') is not None:
        s += f', about {round(c["battery_range"])} miles'
    cs = c.get('charging_state')
    # Mention the plug only when it IS plugged in; "unplugged" every time was noise.
    if cs and cs != 'Disconnected':
        s += {'Charging': '. Charging', 'Complete': '. Plugged in, finished charging'}.get(cs, f'. Plugged in, {cs.lower()}')
    return s + '.'


def temp_sentence(k):
    """k = climate_state."""
    parts = []
    if k.get('inside_temp') is not None:
        parts.append(f'{f(k["inside_temp"])} degrees inside')
    if k.get('outside_temp') is not None:
        parts.append(f'{f(k["outside_temp"])} outside')
    if not parts:
        return f"{config.Spoken_name()} didn't report a temperature."
    s = f"{config.Spoken_name()}'s " + ', '.join(parts)
    if k.get('is_climate_on'):
        s += f', climate on, set to {f(k["driver_temp_setting"])}' if k.get('driver_temp_setting') else ', climate on'
    return s + '.'


def describe(c):
    """Short battery phrase for alerts, lower-case start: 'the car's at 12 percent'."""
    s = f"{config.spoken_name()}'s at {c.get('usable_battery_level')} percent"
    if c.get('battery_range') is not None:
        s += f', about {round(c["battery_range"])} miles'
    return s


def cap(s):
    return s[:1].upper() + s[1:]


def ago(ts, now=None):
    m = int(((now or time.time()) - ts) / 60)
    if m < 2:
        return 'just now'
    if m < 90:
        return f'{m} minutes ago'
    h = round(m / 60)
    return f'{h} hours ago' if h < 36 else f'{round(h / 24)} days ago'


def say(text, log=print):
    """POST the sentence to a voice daemon (SAY_URL). No-op when SAY_URL is unset."""
    if not config.SAY_URL:
        return
    try:
        req = urllib.request.Request(config.SAY_URL, json.dumps({'text': text}).encode(),
                                     {'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:
        log(f'say failed: {type(e).__name__}')
