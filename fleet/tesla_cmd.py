#!/usr/bin/env python3
"""Send one SIGNED command to the car through Tesla's tesla-control (Fleet API transport).

tesla-control (built from github.com/teslamotors/vehicle-command) signs with your virtual key
(PRIVATE_KEY in the config dir). It wants a raw access token in a file and the VIN in the
environment; this wrapper supplies both without printing either, and scrubs the VIN and token
out of anything tesla-control prints.

    tesla_cmd.py ping                     # signed round-trip, changes nothing (car must be awake)
    tesla_cmd.py --wake climate-on        # wake first if asleep, then send
    tesla_cmd.py climate-set-temp 70f
    tesla_cmd.py climate-off

Exit code is tesla-control's. Commands to an asleep car fail unless --wake.
"""
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
from tesla_auth import access_token  # noqa: E402


def state(v, token):
    req = urllib.request.Request(config.VEHICLES_BASE + urllib.parse.quote(v, safe=''),
                                 headers={'Authorization': 'Bearer ' + token})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)['response'].get('state')


def run(args, v, token, timeout=60):
    fd = os.open(config.RAW_ACCESS_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(token)
    env = dict(os.environ, TESLA_VIN=v, TESLA_TOKEN_FILE=str(config.RAW_ACCESS_FILE),
               TESLA_KEY_FILE=str(config.PRIVATE_KEY),
               TESLA_CACHE_FILE=str(config.CACHE / 'session-cache.json'))
    try:
        r = subprocess.run([str(config.TESLA_CONTROL), *args], env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.SubprocessError as e:        # argv holds no secret, but keep it terse anyway
        raise SystemExit(f'tesla-control {args[0]} failed: {type(e).__name__}') from None
    out = (r.stdout + r.stderr).replace(v, '<VIN>').replace(token, '<TOKEN>').strip()
    return r.returncode, out


def main():
    args = [a for a in sys.argv[1:] if a != '--wake']
    if not args:
        raise SystemExit(__doc__)
    v, token = config.vin(), access_token()
    if '--wake' in sys.argv and state(v, token) != 'online':
        rc, out = run(['wake'], v, token, timeout=90)
        print(f'wake rc={rc} {out}')
        for _ in range(30):
            if state(v, token) == 'online':
                break
            time.sleep(3)
        else:
            raise SystemExit('the car did not come online within 90 s')
    rc, out = run(args, v, token)
    print(f'{args[0]} rc={rc} {out}')
    sys.exit(rc)


if __name__ == '__main__':
    main()
