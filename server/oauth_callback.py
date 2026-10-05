#!/usr/bin/env python3
"""Receive a one-time Tesla OAuth code without logging it to the web server.

Runs on the web server behind nginx (see nginx-tesla.conf), bound to localhost. It checks the
`state` that setup_start_oauth.py wrote, then stores {code, state, received_at} atomically with
mode 600 for setup_exchange_oauth.py to collect. Access logging is off on purpose: the request
URL carries the code.

Known limits: it stores the code but does not exchange it, and it does not consume `state` on
success, so a repeated callback rewrites the receipt time. Exchange promptly.
"""

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


STATE_DIR = Path(os.environ.get('STATE_DIRECTORY', '/var/lib/tesla-fleet'))
EXPECTED = STATE_DIR / 'expected-state'
CALLBACK = STATE_DIR / 'callback.json'


class Handler(BaseHTTPRequestHandler):
    server_version = 'TeslaOAuth'
    sys_version = ''

    def log_message(self, *args):
        # The request URL contains a one-time authorization code.
        pass

    def reply(self, status, message):
        data = ('<!doctype html><html><title>Tesla authorization</title>'
                f'<body><p>{message}</p></body></html>').encode()
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'none'")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path != '/tesla/oauth/callback':
            return self.reply(404, 'Not found.')
        params = parse_qs(url.query)
        state = params.get('state', [''])[0]
        code = params.get('code', [''])[0]
        if not EXPECTED.is_file() or not state or state != EXPECTED.read_text().strip():
            return self.reply(400, 'Authorization state did not match.')
        if not code or len(code) > 4096:
            return self.reply(400, 'Tesla did not return an authorization code.')
        payload = json.dumps({'code': code, 'state': state, 'received_at': time.time()}).encode()
        temp = CALLBACK.with_suffix('.tmp')
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(payload)
        os.replace(temp, CALLBACK)
        self.reply(200, 'Tesla authorization received. You can close this page.')


if __name__ == '__main__':
    ThreadingHTTPServer(('127.0.0.1', int(os.environ.get('CALLBACK_PORT', '8093'))), Handler).serve_forever()
