#!/usr/bin/env python3
"""Setup step: create a one-time Tesla authorization link for the account that will use the app.

Generates a random OAuth `state`, writes it over ssh to the web server that runs
server/oauth_callback.py (CALLBACK_SSH_HOST), and prints the authorization URL. Open it on the
phone that is signed into the Tesla app, with the SAME account. Each run invalidates the previous
pending link. Then run setup_exchange_oauth.py within five minutes.
"""
import secrets
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402


def main():
    state = secrets.token_urlsafe(32)
    state_dir = config.setting('CALLBACK_STATE_DIR', '/var/lib/tesla-fleet')
    subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', config.required('CALLBACK_SSH_HOST'),
                    f'umask 077; cat > {state_dir}/expected-state'],
                   input=state, text=True, check=True, stdout=subprocess.DEVNULL)
    print(config.AUTHORIZE_URL + '?' + urlencode({
        'response_type': 'code',
        'client_id': config.required('TESLA_CLIENT_ID'),
        'redirect_uri': config.redirect_uri(),
        'scope': config.SCOPES,
        'state': state,
    }))


if __name__ == '__main__':
    main()
