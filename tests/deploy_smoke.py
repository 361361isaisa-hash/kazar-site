#!/usr/bin/env python3
"""Local deployment test. Own Docker project/volumes, fake contacts, payments disabled."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, ProxyHandler, build_opener
import uuid

ROOT = Path(__file__).resolve().parents[1]
DOCKER = ['docker', '--host=unix:///var/run/docker.sock']


def main():
    environment = os.environ.copy()
    for name in list(environment):
        if name.startswith('KWAZAR_') or name in {'DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH'}:
            environment.pop(name)
    owner = secrets.token_urlsafe(36)
    environment.update(KWAZAR_DOMAIN='kwazar.example', KWAZAR_PAYMENTS_ENABLED='0', KWAZAR_SMOKE_OWNER_TOKEN=owner)
    project = 'kwazar-smoke-' + uuid.uuid4().hex[:10]
    opener = build_opener(ProxyHandler({}))  # loopback only, never used for external requests
    with tempfile.TemporaryDirectory(prefix='kwazar-deploy-smoke-') as folder:
        temporary = Path(folder)
        (temporary / 'Caddyfile').write_text('''{
    admin off
    auto_https off
}
:8080 {
    request_body {
        max_size 32KB
    }
    reverse_proxy app:8000
}
''')
        override = temporary / 'override.yaml'
        override.write_text('''services:
  app:
    env_file: !reset []
    environment:
      KWAZAR_PUBLIC_URL: http://kwazar.example
      KWAZAR_ADMIN_TOKEN: ${KWAZAR_SMOKE_OWNER_TOKEN}
      KWAZAR_PAYMENTS_ENABLED: "0"
  proxy:
    ports: !override ["127.0.0.1::8080"]
    volumes:
      - ''' + str(temporary / 'Caddyfile') + ''':/etc/caddy/Caddyfile:ro
''')
        compose = DOCKER + ['compose', '--project-name', project, '-f', str(ROOT / 'compose.yaml'), '-f', str(override)]

        def run(args):
            result = subprocess.run(compose + args, cwd=ROOT, env=environment, capture_output=True, text=True, timeout=120)
            if result.returncode:
                raise RuntimeError('Deployment command failed: ' + (result.stderr + result.stdout)[-6000:])
            return result.stdout.strip()

        def api(path, payload=None, token=None):
            headers = {'Host': 'kwazar.example', 'Origin': 'http://kwazar.example'}
            if token:
                headers['Authorization'] = 'Bearer ' + token
            if payload is not None:
                headers['Content-Type'] = 'application/json'
            request = Request(base + path, headers=headers, data=json.dumps(payload).encode() if payload is not None else None)
            try:
                with opener.open(request, timeout=8) as response:
                    body = response.read()
                    return response.status, json.loads(body) if response.headers.get_content_type() == 'application/json' else body
            except HTTPError as response:
                return response.code, None

        try:
            run(['up', '-d', '--wait', '--wait-timeout', '90'])
            port = run(['port', 'proxy', '8080']).rsplit(':', 1)[1]
            base = 'http://127.0.0.1:' + port
            ready = api('/healthz')
            assert ready == (200, {'status': 'ok'}), f'Proxy readiness response: {ready!r}'
            code, products = api('/catalog.json')
            assert code == 200 and len(products) == 53
            assert api('/api/orders')[0] == 401
            assert api('/server.py')[0] == 404
            assert api('/' + products[0]['image'])[0] == 200
            run(['exec', '-T', 'app', 'python3', '-c', 'import os; assert os.getuid() == 10001'])
            print('PASS: proxy, readiness, 53 products, product image, protected API, non-root app', flush=True)
            payload = {'request_id': str(uuid.uuid4()), 'name': 'Synthetic deployment test', 'contact': 'deploy-test@example.invalid',
                       'consent': True, 'items': [{'id': products[0]['id'], 'quantity': 1}]}
            code, order = api('/api/orders', payload)
            assert code == 201
            order_path = '/api/orders/' + order['id']
            run(['up', '-d', '--no-deps', '--force-recreate', 'app'])
            for _ in range(25):
                try:
                    if api('/healthz')[0] == 200:
                        break
                except (OSError, URLError):
                    pass
                time.sleep(0.5)
            code, restored = api(order_path, token=order['access_token'])
            assert code == 200 and restored['details']['items'][0]['wb_id'] == products[0]['wbId']
            assert restored['payment_available'] is False
            print('PASS: synthetic order survives container recreation; payments remain disabled', flush=True)
            run(['exec', '-T', 'backup', 'python3', 'backup_store.py', '--data-dir', '/var/lib/kwazar', '--output-dir', '/backups'])
            verify = """import os, pathlib, sqlite3
snapshots = sorted(pathlib.Path('/backups').glob('kwazar-*.sqlite3'), key=lambda p:p.stat().st_mtime_ns)
assert snapshots
with sqlite3.connect(snapshots[-1]) as source, sqlite3.connect('/tmp/restored-test.sqlite3') as restored:
    source.backup(restored)
    assert restored.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert restored.execute('SELECT COUNT(*) FROM orders WHERE id=?', (os.environ['KWAZAR_SMOKE_ORDER_ID'],)).fetchone()[0] == 1
"""
            run(['exec', '-T', '-e', 'KWAZAR_SMOKE_ORDER_ID=' + order['id'], 'backup', 'python3', '-c', verify])
            print('PASS: backup contains the committed order and restores without WAL sidecars', flush=True)
        except Exception:
            logs = subprocess.run(compose + ['logs', '--tail', '20'], cwd=ROOT, env=environment, capture_output=True, text=True, timeout=20)
            print(logs.stdout[-6000:], flush=True)
            raise
        finally:
            subprocess.run(compose + ['down', '--volumes', '--remove-orphans'], cwd=ROOT, env=environment, capture_output=True, timeout=90)


if __name__ == '__main__':
    main()
