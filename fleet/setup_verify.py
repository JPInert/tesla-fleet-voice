#!/usr/bin/env python3
"""Setup step: list the vehicles this account can see, cache the list (mode 600, it holds the
VIN), then ask fleet_status whether your virtual key is paired. Prints names, states and pairing
counts only; never a VIN or token. Sends no command and does not wake the car.

Listing a vehicle does NOT prove the key is paired; fleet_status does.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
from tesla_auth import access_token  # noqa: E402


def request(path, token, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(config.FLEET_URL + path, data=data, headers={
        'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=25) as reply:
            return json.load(reply)
    except urllib.error.HTTPError as error:
        raise SystemExit(f'Tesla access check HTTP {error.code}') from None


def main():
    token = access_token()
    vehicles = request('/api/1/vehicles', token).get('response', [])
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(config.VEHICLES_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(vehicles, stream)
    print('Vehicles accessible:', len(vehicles))
    for vehicle in vehicles:
        print('Vehicle:', vehicle.get('display_name') or '(unnamed)', '| state:', vehicle.get('state'))
    vins = [v['vin'] for v in vehicles if v.get('vin')]
    if vins:
        status = request('/api/1/vehicles/fleet_status', token, {'vins': vins}).get('response', {})
        for key in ('key_paired_vins', 'unpaired_vins'):
            if key in status:
                print(key + ' count:', len(status[key]))
        for key in ('vehicle_command_protocol_required', 'total_number_of_keys'):
            if key in status:
                print(key + ':', status[key])


if __name__ == '__main__':
    main()
