#!/usr/bin/env python3
"""Setup step: fetch the authorization code the callback stored, exchange it for tokens, and
save them (mode 600, atomic). Refuses a code older than five minutes. Never prints a token.
"""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
from tesla_auth import save_tokens  # noqa: E402


def main():
    state_dir = config.setting('CALLBACK_STATE_DIR', '/var/lib/tesla-fleet')
    try:
        result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                                 config.required('CALLBACK_SSH_HOST'), f'cat {state_dir}/callback.json'],
                                text=True, capture_output=True, check=True)
    except subprocess.CalledProcessError as e:
        raise SystemExit(f'could not read the callback receipt (ssh exit {e.returncode})') from None
    callback = json.loads(result.stdout)
    if time.time() - callback['received_at'] > 300:
        raise SystemExit('The Tesla authorization code is older than five minutes; authorize again')
    form = urllib.parse.urlencode({
        'grant_type': 'authorization_code',
        'client_id': config.required('TESLA_CLIENT_ID'),
        'client_secret': config.required('TESLA_CLIENT_SECRET'),
        'code': callback['code'],
        'audience': config.FLEET_URL,
        'redirect_uri': config.redirect_uri(),
    }).encode()
    req = urllib.request.Request(config.AUTH_URL, data=form,
                                 headers={'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            data = json.load(response)
    except urllib.error.HTTPError as error:
        try:
            payload = json.load(error)
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        code = payload.get('error') if isinstance(payload, dict) else None
        raise SystemExit(f'Tesla token exchange HTTP {error.code}: {code or "unknown"}') from None
    if not data.get('access_token') or not data.get('refresh_token'):
        raise SystemExit('Tesla token response was missing an access or refresh token')
    data['saved_at'] = time.time()
    save_tokens(data)
    print('Authorization complete; access and refresh tokens saved with mode 600')


if __name__ == '__main__':
    main()
