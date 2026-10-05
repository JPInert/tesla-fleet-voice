#!/usr/bin/env python3
"""car: tiny client for car_service. This is what voice phrases call.

    car.py battery|temp|status [--say]
    car.py nav "coffee shop" [--say]          # saved place, else a map search near home
    car.py cmd climate-on [arg] [--say]

--say makes the SERVICE speak the result via SAY_URL. Prints the JSON reply.
Exit code 1 when the service reports ok=false.
"""
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402


def build(argv):
    """argv (without the program name) -> (method, path, body). Raises SystemExit on bad usage."""
    a = [x for x in argv if x != '--say']
    q = '?say=1' if '--say' in argv else ''
    if not a:
        raise SystemExit(__doc__)
    if a[0] in ('battery', 'temp', 'status'):
        return 'GET', '/' + a[0] + q, None
    if a[0] == 'nav' and len(a) == 2:
        return 'POST', '/nav' + q, {'q': a[1]}
    if a[0] == 'cmd' and len(a) in (2, 3):
        return 'POST', '/cmd/' + a[1] + q, ({'arg': a[2]} if len(a) == 3 else {})
    raise SystemExit(__doc__)


def main():
    method, path, body = build(sys.argv[1:])
    req = urllib.request.Request(config.SERVICE_URL + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={'Authorization': 'Bearer ' + config.required('SERVICE_TOKEN'),
                                          'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=120) as r:
        res = json.load(r)
    print(json.dumps(res))
    sys.exit(0 if res.get('ok', True) else 1)


if __name__ == '__main__':
    main()
