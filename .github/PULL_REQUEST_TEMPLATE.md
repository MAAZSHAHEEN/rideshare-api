## What and why

## How it was tested
- [ ] `python -B -m unittest discover -s tests -v` passes against a disposable PostgreSQL database
- [ ] Frontend `npm run typecheck` and `npm test` pass (if frontend changed)
- [ ] New or changed behavior has tests

## Checklist
- [ ] Schema changes include an Alembic migration
- [ ] No secrets, `.env` files or personal data committed
- [ ] Docs updated where behavior changed
