#!/usr/bin/env python3
"""The car's history DB (SQLite): written by watchdog.py and car_service.py, read by this CLI.

Two tables, both fed from calls the watchdog ALREADY makes (no extra API cost):
  readings  one row per vehicle_data read: battery, range, charging, odometer, temps, locks
  states    one row per online/asleep/offline CHANGE (the 15-min state checks, deduplicated)
  raw       the full JSON of every read (VIN stripped): the forward-compatible copy

Gaps are real: an asleep car is never read, so a night is two readings (last before sleep,
first after waking) and the drop between them is the overnight drain.

    history.py              # last 15 readings
    history.py days [N]     # per-day: battery min/max, miles driven, kWh-ish, drain asleep
    history.py states [N]   # last N state changes
    history.py csv          # every reading as CSV on stdout
"""
import csv
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402

DB = config.DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
  ts REAL PRIMARY KEY, state TEXT,
  battery_level INTEGER, usable_battery_level INTEGER, battery_range REAL, est_battery_range REAL,
  charging_state TEXT, charge_limit_soc INTEGER, charger_power REAL, charge_energy_added REAL,
  minutes_to_full_charge INTEGER, fast_charger_present INTEGER,
  odometer REAL, inside_temp REAL, outside_temp REAL, is_climate_on INTEGER,
  locked INTEGER, sentry_mode INTEGER, car_version TEXT
);
CREATE TABLE IF NOT EXISTS states (ts REAL PRIMARY KEY, state TEXT);
-- the WHOLE vehicle_data response (VIN stripped), so a field nobody extracted today can be
-- pulled out of every past reading later: SELECT json_extract(body,'$.vehicle_state.tpms_pressure_fl')
CREATE TABLE IF NOT EXISTS raw (ts REAL, source TEXT, body TEXT, PRIMARY KEY (ts, source));
"""


def connect(db=None):
    db = db or DB
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db, timeout=10)
    con.executescript(SCHEMA)
    return con


def record_reading(ts, state, c, v, k):
    """c/v/k = charge_state / vehicle_state / climate_state dicts (any may be empty)."""
    c, v, k = c or {}, v or {}, k or {}
    b = lambda x: None if x is None else int(bool(x))  # noqa: E731
    row = dict(ts=ts, state=state,
               battery_level=c.get('battery_level'), usable_battery_level=c.get('usable_battery_level'),
               battery_range=c.get('battery_range'), est_battery_range=c.get('est_battery_range'),
               charging_state=c.get('charging_state'), charge_limit_soc=c.get('charge_limit_soc'),
               charger_power=c.get('charger_power'), charge_energy_added=c.get('charge_energy_added'),
               minutes_to_full_charge=c.get('minutes_to_full_charge'),
               fast_charger_present=b(c.get('fast_charger_present')),
               odometer=v.get('odometer'), inside_temp=k.get('inside_temp'), outside_temp=k.get('outside_temp'),
               is_climate_on=b(k.get('is_climate_on')), locked=b(v.get('locked')),
               sentry_mode=b(v.get('sentry_mode')), car_version=v.get('car_version'))
    with connect() as con:
        con.execute(f"INSERT OR REPLACE INTO readings ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                    list(row.values()))


def _scrub(obj):
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items() if k not in ('vin', 'id', 'vehicle_id', 'id_s')}
    if isinstance(obj, list):
        return [_scrub(x) for x in obj]
    return obj


def record_raw(ts, source, response):
    """source = 'watchdog' | 'service' | ...; response = the vehicle_data 'response' dict."""
    import json
    with connect() as con:
        con.execute('INSERT OR REPLACE INTO raw VALUES (?, ?, ?)', (ts, source, json.dumps(_scrub(response))))


def record_state(ts, state):
    """Only a change is stored."""
    with connect() as con:
        last = con.execute('SELECT state FROM states ORDER BY ts DESC LIMIT 1').fetchone()
        if not last or last[0] != state:
            con.execute('INSERT OR REPLACE INTO states VALUES (?, ?)', (ts, state))


def _t(ts):
    return datetime.fromtimestamp(ts).strftime('%m-%d %H:%M')


def show_last(n=15):
    with connect() as con:
        rows = con.execute('SELECT ts, usable_battery_level, battery_level, battery_range, charging_state, '
                           'odometer, inside_temp, outside_temp, locked FROM readings '
                           'ORDER BY ts DESC LIMIT ?', (n,)).fetchall()
    print(f"{'when':11}  {'use%':>4} {'bat%':>4} {'range':>5}  {'charging':12} {'odo':>8} {'in°F':>4} {'out°F':>5} lock")
    f = lambda c: '' if c is None else round(c * 9 / 5 + 32)  # noqa: E731
    for ts, u, bl, rng, cs, odo, it, ot, lk in reversed(rows):
        print(f"{_t(ts):11}  {u if u is not None else '':>4} {bl if bl is not None else '':>4} "
              f"{round(rng) if rng else '':>5}  {cs or '':12} {round(odo, 1) if odo else '':>8} "
              f"{f(it):>4} {f(ot):>5} {'' if lk is None else ('Y' if lk else 'N')}")
    print(f'{len(rows)} rows shown · DB {DB}')


def show_days(n=14):
    with connect() as con:
        rows = con.execute('SELECT ts, usable_battery_level, odometer, charging_state, state FROM readings '
                           'WHERE ts > ? ORDER BY ts', (time.time() - n * 86400,)).fetchall()
    days = {}
    prev = None
    for ts, u, odo, cs, st in rows:
        d = days.setdefault(datetime.fromtimestamp(ts).strftime('%Y-%m-%d'),
                            dict(n=0, lo=None, hi=None, odo0=None, odo1=None, drain=0, charged=0))
        d['n'] += 1
        if u is not None:
            d['lo'] = u if d['lo'] is None else min(d['lo'], u)
            d['hi'] = u if d['hi'] is None else max(d['hi'], u)
        if odo is not None:
            d['odo0'] = odo if d['odo0'] is None else d['odo0']
            d['odo1'] = odo
        if prev and u is not None and prev[1] is not None:
            delta = u - prev[1]
            moved = odo is not None and prev[2] is not None and odo - prev[2] > 0.5
            if delta > 0:
                d['charged'] += delta
            elif not moved:
                d['drain'] += -delta          # lost % while parked = idle/sleep drain
        prev = (ts, u, odo)
    print(f"{'day':10}  {'n':>3}  {'min%':>4} {'max%':>4}  {'miles':>5}  {'+chg%':>5}  {'parked-drain%':>13}")
    for day, d in days.items():
        miles = round(d['odo1'] - d['odo0'], 1) if d['odo0'] is not None else ''
        print(f"{day:10}  {d['n']:>3}  {d['lo'] if d['lo'] is not None else '':>4} {d['hi'] if d['hi'] is not None else '':>4}"
              f"  {miles:>5}  {d['charged']:>5}  {d['drain']:>13}")
    print('n = readings that day; miles/charge from consecutive readings only, so a gap hides detail.')


def show_states(n=20):
    with connect() as con:
        rows = con.execute('SELECT ts, state FROM states ORDER BY ts DESC LIMIT ?', (n,)).fetchall()
    for ts, s in reversed(rows):
        print(_t(ts), s)


def dump_csv():
    with connect() as con:
        cur = con.execute('SELECT * FROM readings ORDER BY ts')
        w = csv.writer(sys.stdout)
        w.writerow(['time'] + [c[0] for c in cur.description])
        for r in cur:
            w.writerow([datetime.fromtimestamp(r[0]).isoformat(timespec='seconds')] + list(r))


if __name__ == '__main__':
    a = sys.argv[1:]
    cmd = a[0] if a else 'last'
    num = int(a[1]) if len(a) > 1 else None
    {'last': lambda: show_last(num or 15), 'days': lambda: show_days(num or 14),
     'states': lambda: show_states(num or 20), 'csv': dump_csv}[cmd]()
