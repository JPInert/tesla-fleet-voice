#!/usr/bin/env python3
"""car: tiny client for car_service. This is what voice phrases call.

    car.py battery|temp|status [--say]
    car.py nav "coffee shop" [--say]          # saved place, remembered place, map search near the car
    car.py stop "coffee shop" [--say]         # ADD A STOP before the current destination
    car.py cmd climate-on [arg] [--say]
    car.py cmd volume-set 50%                 # stepped up/down to 50 %, see car_service.volume_set

--say makes the SERVICE speak the result via SAY_URL. Prints the JSON reply.
--dry (nav/stop only): resolve the place and report what WOULD be sent; nothing is sent to the car.
    Use it for every test.
--no-wake: an asleep car is NOT woken; exit code 3 and {"asleep": true}, so the caller can ask first.
Exit code 1 when the service reports ok=false.
"""
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402

FLAGS = ('--say', '--no-wake', '--dry')


def build(argv):
    """argv (without the program name) -> (method, path, body). Raises SystemExit on bad usage."""
    a = [x for x in argv if x not in FLAGS]
    q = '?say=1' if '--say' in argv else ''
    if not a:
        raise SystemExit(__doc__)
    if a[0] in ('battery', 'temp', 'status'):
        method, path, body = 'GET', '/' + a[0] + q, None
    elif a[0] in ('nav', 'stop') and len(a) == 2:
        method, path, body = 'POST', '/' + a[0] + q, {'q': a[1]}
    elif a[0] == 'cmd' and len(a) in (2, 3):
        method, path, body = 'POST', '/cmd/' + a[1] + q, ({'arg': a[2]} if len(a) == 3 else {})
    else:
        raise SystemExit(__doc__)
    if '--no-wake' in argv and body is not None:
        body['wake'] = False
    if '--dry' in argv:
        if a[0] not in ('nav', 'stop'):
            raise SystemExit('--dry is for nav and stop only')
        body['dry'] = True
    return method, path, body


def exit_code(res):
    return 3 if res.get('asleep') else 0 if res.get('ok', True) else 1


def main():
    method, path, body = build(sys.argv[1:])
    req = urllib.request.Request(config.SERVICE_URL + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={'Authorization': 'Bearer ' + config.required('SERVICE_TOKEN'),
                                          'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=120) as r:
        res = json.load(r)
    print(json.dumps(res))
    sys.exit(exit_code(res))


if __name__ == '__main__':
    main()
