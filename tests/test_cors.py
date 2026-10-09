"""Exercise the real configured app in a fresh process, without a database."""

import os
from pathlib import Path
import subprocess
import sys
import unittest


class CORSTests(unittest.TestCase):
    def run_app(self, origins, code):
        env = {**os.environ, "FRONTEND_ORIGINS": origins,
               "DATABASE_URL": "postgresql+asyncpg://localhost/cors_test",
               "SECRET_KEY": "cors-tests-only-not-a-production-signing-key"}
        result = subprocess.run([sys.executable, "-B", "-c", code], env=env,
                                cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_explicit_origins_preflight_and_http_error_headers(self):
        self.run_app("http://localhost:5173, https://frontend.example.com", """
import asyncio
from httpx import ASGITransport, AsyncClient
from main import app
async def check():
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        for origin in ('http://localhost:5173', 'https://frontend.example.com'):
            for method in ('GET', 'POST', 'PATCH'):
                response = await client.options('/rides/', headers={
                    'Origin': origin, 'Access-Control-Request-Method': method,
                    'Access-Control-Request-Headers': 'authorization,content-type'})
                assert response.status_code == 200
                assert response.headers['access-control-allow-origin'] == origin
                assert 'access-control-allow-credentials' not in response.headers
            response = await client.get('/me', headers={'Origin': origin})
            assert response.status_code == 401
            assert response.headers['access-control-allow-origin'] == origin
            assert response.headers['www-authenticate'] == 'Bearer'
        for origin, method, headers in (
            ('https://untrusted.example.com', 'GET', 'authorization'),
            ('http://localhost:5174', 'GET', 'authorization'),
            ('http://localhost:5173', 'DELETE', 'authorization'),
            ('http://localhost:5173', 'GET', 'x-unapproved-header'),
        ):
            response = await client.options('/rides/', headers={
                'Origin': origin, 'Access-Control-Request-Method': method,
                'Access-Control-Request-Headers': headers})
            assert response.status_code == 400
        response = await client.get('/', headers={'Origin': 'https://untrusted.example.com'})
        assert 'access-control-allow-origin' not in response.headers
asyncio.run(check())
""")

    def test_empty_allowlist_disables_cross_origin_access(self):
        self.run_app("", """
import asyncio
from httpx import ASGITransport, AsyncClient
from main import app
async def check():
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        response = await client.options('/rides/', headers={
            'Origin': 'http://localhost:5173', 'Access-Control-Request-Method': 'GET'})
        assert response.status_code == 400
        assert 'access-control-allow-origin' not in response.headers
asyncio.run(check())
""")

    def test_invalid_origin_configuration_fails_fast(self):
        self.run_app("", """
import importlib, os, config
for value in ('*', 'https://*.example.com', 'ftp://example.com', 'https://example.com/path',
              'https://example.com/', 'https://example.com?x=1', 'https://user:pass@example.com',
              'https://example.com:bad', 'not-an-origin'):
    os.environ['FRONTEND_ORIGINS'] = value
    try:
        importlib.reload(config)
    except RuntimeError:
        pass
    else:
        raise AssertionError('Invalid origin accepted')
""")
