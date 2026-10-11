"""Exercise Cloud's installed upload walker without uploading or using a DB."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi_cloud_cli.commands.deploy.archive import _rignore_walk


ROOT = Path(__file__).resolve().parents[1]


class DeploymentPackagingTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / '.git').mkdir()
        for name in ('.gitignore', '.fastapicloudignore'):
            (self.root / name).write_bytes((ROOT / name).read_bytes())
        self.write('frontend/.gitignore', (ROOT / 'frontend/.gitignore').read_text(encoding='utf-8'))

    def write(self, name, content='fixture'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')

    def uploaded_files(self):
        return {path.relative_to(self.root).as_posix()
                for path in _rignore_walk(self.root) if path.is_file()}

    def test_complete_frontend_build_and_backend_runtime_are_included(self):
        expected = {
            'frontend/dist/index.html', 'frontend/dist/favicon.svg',
            'frontend/dist/journey.svg', 'frontend/dist/assets/index-Ab12.js',
            'frontend/dist/assets/index-Ab12.css',
            'frontend/dist/assets/fonts/travel-Ab12.woff2',
            'frontend/dist/assets/chunks/dashboard-Ab12.js',
        }
        expected.update(path.relative_to(ROOT).as_posix()
                        for directory in ('routers', 'services', 'alembic')
                        for path in (ROOT / directory).rglob('*')
                        if path.is_file() and '__pycache__' not in path.parts)
        expected.update(path.name for path in ROOT.glob('*.py'))
        expected.update(('requirements.txt', 'alembic.ini', '.python-version'))
        for name in expected:
            self.write(name)
        self.assertTrue(expected <= self.uploaded_files())

    def test_secrets_and_development_tooling_are_excluded_even_inside_dist(self):
        excluded = {
            '.env', '.env.production', '.aws/credentials', '.fastapicloud/cloud.json',
            '.codex/config.toml', 'venv/pyvenv.cfg', '.venv/pyvenv.cfg',
            'frontend/.env.local', 'frontend/node_modules/package/index.js',
            'frontend/src/main.tsx', 'frontend/package.json', 'frontend/package-lock.json',
            'frontend/e2e/foundation.spec.ts', 'frontend/test-results/result.json',
            'frontend/dist/.env', 'frontend/dist/nested/.env.production',
            'frontend/dist/private.key', 'frontend/dist/credentials.pem',
            'tests/test_example.py', '.github/workflows/ci.yml',
            'task19_review.txt', '__pycache__/main.pyc', 'database.backup',
        }
        for name in excluded:
            self.write(name)
        self.write('frontend/dist/index.html')
        included = self.uploaded_files()
        self.assertEqual(excluded & included, set())
        self.assertIn('frontend/dist/index.html', included)
