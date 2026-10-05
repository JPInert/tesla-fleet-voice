#!/usr/bin/env python3
"""Send a destination to the car's navigation (Fleet API navigation_request, plain REST;
tesla-control has no navigation command).

DRY by default: prints the exact request with the VIN masked and sends nothing.

    tesla_nav.py "Empire State Building, New York, NY"            # dry: show payload
    tesla_nav.py --send "Empire State Building, New York, NY"     # send (car must be awake)
    tesla_nav.py --send --wake "..."                              # wake first
    tesla_nav.py --send --place home                              # a name from places.json

The body mimics the phone's "share to Tesla" intent; the car geocodes the text itself, so a
full street address (or a Google Maps link) works best.
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
from car_service import nav_payload  # noqa: E402


def call(method, path, token, body=None):
    req = urllib.request.Request(config.VEHICLES_BASE + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            err = json.load(e)
        except Exception:
            err = {}
        return e.code, {'error': err.get('error'), 'error_description': err.get('error_description')}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if len(args) != 1:
        raise SystemExit(__doc__)
    if '--place' in sys.argv:
        places = json.loads(config.PLACES.read_text())
        if args[0].lower() not in places:
            raise SystemExit(f'no place {args[0]!r}; known: {", ".join(places)}')
        args[0] = places[args[0].lower()]
    body = nav_payload(args[0])
    print('POST /api/1/vehicles/<VIN>/command/navigation_request')
    print(json.dumps(body, indent=2))
    if '--send' not in sys.argv:
        print('(dry run: nothing sent; add --send)')
        return
    from tesla_auth import access_token
    vin = urllib.parse.quote(config.vin(), safe='')
    token = access_token()
    st, r = call('GET', vin, token)
    state = r.get('response', {}).get('state') if st == 200 else f'HTTP {st}'
    print('car state:', state)
    if state != 'online':
        if '--wake' not in sys.argv:
            raise SystemExit(f'the car is {state}; re-run with --wake')
        st, _ = call('POST', vin + '/wake_up', token)
        print('wake_up HTTP', st)
        for _ in range(30):
            time.sleep(3)
            st, r = call('GET', vin, token)
            if st == 200 and r['response'].get('state') == 'online':
                break
        else:
            raise SystemExit('the car did not come online within 90 s')
    st, r = call('POST', vin + '/command/navigation_request', token, body)
    print('navigation_request HTTP', st, json.dumps(r.get('response', r)))


if __name__ == '__main__':
    main()
