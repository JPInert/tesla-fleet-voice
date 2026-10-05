#!/usr/bin/env python3
"""Return a valid Fleet API access token, refreshing it first when near expiry.

Tesla rotates refresh tokens: each refresh consumes the old one, so the whole
response is written atomically before anything else uses it. Never prints tokens.
Refreshing needs only the client id, not the client secret.

    python3 tesla_auth.py           # refresh only if <15 min left; print status
    python3 tesla_auth.py --force   # refresh now
"""
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402

MARGIN = 15 * 60


def _expires_at(data: dict) -> float:
    return data['saved_at'] + data.get('expires_in', 0)


def save_tokens(data: dict) -> None:
    """Write the token JSON atomically with mode 600."""
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=config.CONFIG_DIR, prefix='.user-token-')
    with os.fdopen(fd, 'w') as temp:
        json.dump(data, temp)
        temp.write('\n')
    os.chmod(name, 0o600)
    os.replace(name, config.USER_TOKEN_FILE)


def refresh(data: dict) -> dict:
    form = urllib.parse.urlencode({
        'grant_type': 'refresh_token',
        'client_id': config.required('TESLA_CLIENT_ID'),
        'refresh_token': data['refresh_token'],
    }).encode()
    req = urllib.request.Request(config.AUTH_URL, data=form,
                                 headers={'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            new = json.load(response)
    except urllib.error.HTTPError as error:
        try:
            code = json.load(error).get('error')
        except Exception:
            code = None
        raise SystemExit(f'Tesla refresh HTTP {error.code}: {code or "unknown"}') from None
    except urllib.error.URLError:
        raise SystemExit('Tesla refresh failed to connect') from None
    if not new.get('access_token') or not new.get('refresh_token'):
        raise SystemExit('Tesla refresh response missing a token; old file left untouched')
    new['saved_at'] = time.time()
    save_tokens(new)
    return new


def access_token(force: bool = False) -> str:
    data = json.loads(config.USER_TOKEN_FILE.read_text())
    if force or time.time() > _expires_at(data) - MARGIN:
        data = refresh(data)
    return data['access_token']


if __name__ == '__main__':
    access_token(force='--force' in sys.argv)
    data = json.loads(config.USER_TOKEN_FILE.read_text())
    left = (_expires_at(data) - time.time()) / 3600
    print(f'access token valid for {left:.1f} h; saved {time.strftime("%Y-%m-%d %H:%M", time.localtime(data["saved_at"]))}')
