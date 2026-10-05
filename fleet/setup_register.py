#!/usr/bin/env python3
"""Setup step: register your app's domain with Fleet API (partner account) and check that
Tesla can see your hosted public key. Needs TESLA_CLIENT_ID, TESLA_CLIENT_SECRET, APP_DOMAIN.

    setup_register.py               # POST /api/1/partner_accounts, then verify
    setup_register.py --verify-only # only GET /api/1/partner_accounts/public_key

Lesson from doing this: a partner token requested WITHOUT scopes registered fine but got 403 on
the public-key check. Requesting the scopes below for the partner token fixed the check.
Never prints a credential.
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402


def request(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        try:
            payload = json.load(error)
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        code = payload.get('error') if isinstance(payload, dict) else None
        print(f'HTTP {error.code}; Tesla error: {code or "unavailable"}', file=sys.stderr)
        raise SystemExit(1) from None


def main():
    domain = config.app_domain()
    form = urllib.parse.urlencode({
        'grant_type': 'client_credentials',
        'client_id': config.required('TESLA_CLIENT_ID'),
        'client_secret': config.required('TESLA_CLIENT_SECRET'),
        'audience': config.FLEET_URL,
        'scope': 'openid vehicle_device_data vehicle_cmds vehicle_charging_cmds',
    }).encode()
    status, data = request(config.AUTH_URL, form, {'Content-Type': 'application/x-www-form-urlencoded'})
    partner = data.get('access_token') if isinstance(data, dict) else None
    if status != 200 or not partner:
        raise SystemExit('Tesla did not issue a partner token')
    print('Partner token acquired')
    headers = {'Content-Type': 'application/json', 'Authorization': f'Bearer {partner}'}
    if '--verify-only' not in sys.argv:
        status, _ = request(config.FLEET_URL + '/api/1/partner_accounts',
                            json.dumps({'domain': domain}).encode(), headers)
        print(f'Partner registration HTTP {status}')
    status, result = request(config.FLEET_URL + '/api/1/partner_accounts/public_key?'
                             + urllib.parse.urlencode({'domain': domain}),
                             headers={'Authorization': f'Bearer {partner}'})
    response = result.get('response', {}) if isinstance(result, dict) else {}
    print(f'Registered public key lookup HTTP {status}; present: {bool(response)}')


if __name__ == '__main__':
    main()
