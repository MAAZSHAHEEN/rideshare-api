# Contributing to RideShare API

Thanks for your interest in improving RideShare API. This is a small, solo-maintained
project, so clear issues and focused pull requests help a lot.

## Ways to contribute
- Report bugs or confusing behavior with a [bug report](../../issues/new?template=bug_report.md).
- Propose features with a [feature request](../../issues/new?template=feature_request.md).
- Improve documentation, examples and tests.
- Pick up an issue labeled `good first issue` or `help wanted`.

Security problems should **not** be filed as public issues. Follow [SECURITY.md](SECURITY.md).

## Branches
- `rideshare-v2` is the active development branch. Open pull requests against it.
- `main` holds the original release and receives v2 through a reviewed merge.

## Development setup
Follow **Local Setup** in [README.md](README.md). In short:

```sh
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # set a random SECRET_KEY of 32+ bytes
docker compose up -d db
alembic upgrade head
uvicorn main:app --reload
```

Frontend (optional, Node 22.12+):

```sh
cd frontend
npm ci
npm run dev
```

## Running the checks
The backend suite is a PostgreSQL integration suite. Point it at a **disposable** database,
never a shared or production one.

```sh
export TEST_DATABASE_URL=postgresql+asyncpg://user:password@localhost:5433/rideshare_test
export DATABASE_URL=$TEST_DATABASE_URL
export SECRET_KEY=local-test-only-signing-key-at-least-32-bytes
export AUTH_RATE_LIMIT_ENABLED=false
python -m alembic upgrade head
python -B -m unittest discover -s tests -v
```

Rate-limiter integration tests also need `TEST_REDIS_URL` (for example
`redis://127.0.0.1:6379/0`); they are skipped without it.

Frontend:

```sh
cd frontend
npm run typecheck
npm test
```

GitHub Actions runs the same backend steps against PostgreSQL 15 and Redis on every
pull request.

## Pull request guidelines
- Keep each pull request to one logical change, and describe what and why.
- Add or update tests for behavior changes. Concurrency, locking and transaction
  changes need an integration test that exercises the race.
- Schema changes must come with an Alembic migration; never rely on `create_all`.
- Never commit secrets, `.env` files, real CNIC or phone numbers, or production data.
- Use clear commit messages; the history follows a `type: summary` style
  (`fix:`, `feat:`, `docs:`, `refactor:`, `ci:`).
- By contributing, you agree that your contributions are licensed under the
  project's [MIT License](LICENSE).

## Code of conduct
Be respectful and constructive. Harassment or abusive behavior is not tolerated, and
the maintainer may remove comments or block participants who engage in it.
