# Full-stack deployment review

The backend serves the compiled React application from `frontend/dist` under the
same HTTPS origin as the API. No database, authentication, CORS or production
environment changes are required by this packaging change. No deployment is
performed by the repository's CI workflow.

## Runtime contract

- `/` serves React when `frontend/dist/index.html` exists. Without a build it
  retains the backend's JSON greeting, so backend-only development still works.
- `/login`, `/register`, `/passenger`, `/driver` and `/account` serve the React
  entry point. These routes are explicit: add future client routes here and in
  `frontend_serving.py`. There is no global SPA fallback. Unknown API paths keep
  their JSON errors, and `/docs`, `/redoc` and `/openapi.json` remain available.
- HTML and unhashed public SVGs use `Cache-Control: no-cache`; generated Vite
  assets use `public, max-age=31536000, immutable`. Only content-hashed output
  belongs in `dist/assets`. Missing assets return 404, never HTML.
- Restart the backend after adding/removing a build. Application startup still
  checks PostgreSQL connectivity; it neither runs migrations nor creates tables.
- Production builds always use relative API URLs. The Vite build configuration
  overrides `VITE_API_BASE_URL` with `/`, including custom build modes, and the
  client ignores API overrides when `import.meta.env.PROD` is true. A developer's
  shell or `.env.local` cannot select a development backend for this build.
  Vite development still defaults to `http://localhost:8000` and supports an
  explicit public HTTP(S) override. Never put secrets in Vite variables: other
  `VITE_*` values can still be compiled into public JavaScript.

Same-origin production requests need no additional CORS origin. Preserve the
existing production `FRONTEND_ORIGINS` setting; do not add wildcards or disable
authorization. Separate local Vite/Uvicorn servers still need their exact local
origins in that existing setting. JWTs remain in memory and API calls continue to
omit cookie credentials. A browser reload requires signing in again.

## Local Docker build

From the repository root:

```sh
docker build -t rideshare-api:review .
```

The Node 22 builder runs `npm ci` and `npm run build`. The Python 3.11 runtime
receives only Python runtime files, Alembic files and the compiled frontend;
Node, frontend source, tests and `node_modules` are not copied into it. Build
context exclusions also keep environment files, virtual environments, Git and
local deployment metadata out. Build-time secrets are unnecessary.

To run locally, supply a private runtime environment file pointing to an already
migrated **development** PostgreSQL database reachable from the container:

```sh
docker run --rm --env-file <private-runtime-env-file> -p 8000:8000 rideshare-api:review
```

The original `uvicorn main:app --host 0.0.0.0` entry point is retained. Its port
defaults to 8000 and honors `PORT` when a container platform supplies it; adjust
the published port accordingly. `DATABASE_URL`, `SECRET_KEY`, `ALGORITHM`,
`ACCESS_TOKEN_EXPIRE_MINUTES` and any existing CORS configuration are runtime
configuration, never Docker build arguments. Do not point local tests at production.

## FastAPI Cloud: verify the build path before deploying

FastAPI Cloud's documented source build installs Python dependencies; it does
**not** run a frontend build, and its GitHub integration currently does not build
the frontend either. Do not assume that discovering this Dockerfile enables a
custom container build. See the official [frontend deployment guide](https://fastapicloud.site/docs/builds-and-deployments/frontend/)
and [existing-project guide](https://fastapicloud.site/docs/getting-started/existing-project/).

This repository supports the documented **prebuilt frontend source-upload** path:

1. Review the diff and validate a fresh checkout of `rideshare-v2`. Verify the
   intended app is `rideshare-api.fastapicloud.dev` and the repository is
   `MAAZSHAHEEN/rideshare-api`. Inspect the actual cloud app/repository/branch and
   automatic deployment settings in the dashboard. Local app-link metadata does
   not prove which GitHub branch or builder is configured. Do not enable automatic
   deployments until this is verified; an unprepared GitHub source build may
   publish a backend-only release with no frontend assets.
2. Retain the existing production environment values without copying them into
   source or build output. Confirm the deployed backend already supports Task
   #18's `GET /me` response with `allowed_modes`. This frontend fails closed with
   older profiles; roll out a compatible backend first if necessary.
3. Build locally with Node 22.12+ and the lockfile. `npm run build` enforces the
   same-origin API configuration regardless of a developer's API override:

   ```powershell
   cd frontend
   npm ci
   npm run typecheck
   npm test
   npm run build
   cd ..
   ```

   The same commands work in a POSIX shell. Verify the resulting build makes
   relative API requests using the production browser check below. Rebuild for
   every release; `.fastapicloudignore` includes assets but cannot detect a stale
   build copied from a different revision.
4. `.python-version` selects Python 3.11. The root `main.py` / `app` entry point and
   `requirements.txt` support the existing Python source build. `.fastapicloudignore`
   re-includes ignored `frontend/dist` but excludes frontend source/tooling,
   environment files, virtual environments and local cloud links. The official
   [ignore-file guide](https://fastapicloud.site/docs/builds-and-deployments/fastapicloudignore/)
   describes these overrides. Verify the upload preview includes `dist/index.html`
   and every referenced asset. Do not commit generated assets or secrets.
5. Only after manual approval, the operator can use the documented Cloud CLI
   source-upload path from the repository root. Authenticate separately and verify
   the existing app's ID in the dashboard. Supply that ID explicitly to avoid
   accidentally creating/selecting another app:

   ```sh
   fastapi deploy . --app-id <verified-existing-app-id>
   ```

   The ID is an app identifier, not a signing key or database credential. Do not
   change the existing app environment. Keep the default deployment wait/verification
   enabled; do not add `--no-wait`. This instruction is not an authorization to
   deploy automatically. No new production variables or migration
   commands are part of this release. If selecting a container deployment instead,
   confirm that the platform supports custom images and its runtime/port contract
   before relying on the Docker path.
6. After that separately approved deployment, verify HTTPS root and each deep
   link, login and verified profile, history, API 401/404s, docs/OpenAPI, and static
   assets. Inspect browser requests for the same origin and no localhost URLs.
   Verify the cloud proxy/CDN honors HTML revalidation and does not cache private
   API responses. Local header tests cannot establish proxy behavior.

## Validation before release

Run the complete PostgreSQL suite as documented in the main README against a
disposable test server, including concurrency and migration tests. Run frontend
TypeScript, unit, build and browser tests:

```sh
cd frontend
npm run typecheck
npm test
npm run build
npm run test:e2e
```

For the same browser contracts against the **built app**, start local Uvicorn
with a disposable migrated database at port 5184, then in a separate PowerShell:

```powershell
cd frontend
$env:PLAYWRIGHT_BASE_URL = 'http://127.0.0.1:5184'
$env:PLAYWRIGHT_API_ORIGIN = 'http://127.0.0.1:5184'
npm run test:e2e
```

Use a locally installed Playwright Chromium, or `PLAYWRIGHT_CHANNEL=chrome` for
installed Chrome. The browser suite intercepts API calls with contract fixtures;
it verifies the actual bundle/deep links/session UI but does not substitute for
the PostgreSQL API tests or a manual end-to-end smoke test. Clear these two
Playwright variables to return to the default Vite browser server.

## Rollback and limitations

Record the prior successful deployment/artifact before release. If a full-stack
release fails, select that deployment's **Actions -> Redeploy** in the dashboard,
or manually redeploy its exact reviewed source and matching built assets. Cloud's
[redeployment guide](https://fastapicloud.site/docs/builds-and-deployments/redeployments/)
states that this reuses the successful image with the app's **current** configuration;
it does not restore old environment values. A dedicated rollback feature is still
listed as coming soon. Prefer the last Task #18-compatible backend: older
single-role code would restrict dual-mode workflows. Do not rebuild an old
frontend against newer source by accident.

Keep production configuration and the database untouched during rollback. There
is no schema migration to reverse and no justification to downgrade Alembic or
rewrite users/roles/history. A backend-only rollback restores API availability
but intentionally removes frontend deep links; communicate that limitation.

Cloud source-build, branch integration, deployment health checks and CDN behavior
must be verified on the platform by the operator. This preparation does not alter
those settings or prove a live deployment. The generic Docker `PORT` support is
separate from the cloud's managed source-build startup contract.

An already-open browser may request an old hashed chunk during/after a rolling
deployment. A reload retrieves the new HTML/assets and, with memory-only tokens,
requires login again. This release does not introduce asset retention across
releases, session persistence, automatic deployment, or deployment rollback
automation. Dependencies and base-image patches should still be reviewed before
release; a successful local build alone is not a supply-chain audit.

## Future deployment automation (proposal only)

After the manual full-stack release succeeds, prefer one explicit GitHub Actions
deployment path using the Cloud CLI over the built-in GitHub source integration.
The official [frontend guide](https://fastapicloud.site/docs/builds-and-deployments/frontend/)
supports building Node assets before the CLI upload. Do not let both deployment
paths race or let a source-only push replace a full-stack release.

Before enabling it, extend validation to include frontend TypeScript, unit tests,
browser tests, production build and upload inventory alongside the existing full
PostgreSQL suite. Deploy only the exact validated commit from the approved branch
(`rideshare-v2` initially, subject to operator confirmation); never deploy pull
requests or fork-controlled code with credentials. A dependent deployment job in
the same workflow can use `needs` to wait for every required check. If using a
separate `workflow_run`, check success, repository and branch, check out the
validated `head_sha`, and consume artifacts only from that trusted run.

Build once from a clean checkout and retain the compiled `frontend/dist` artifact
with the source SHA and checksums. Restore that same artifact into the matching
backend checkout before the CLI upload, and verify all referenced files exist.
Use a protected GitHub production environment with manual approval for initial
releases. Store `FASTAPI_CLOUD_TOKEN` and `FASTAPI_CLOUD_APP_ID` there as documented
in the [deploy-token guide](https://fastapicloud.site/docs/advanced-features/deploy-tokens/);
never inject database credentials or JWT keys into the frontend build. Keep the
GitHub token read-only, avoid persisted checkout credentials, and make deployment
failure fail the job.

Serialize production deployments with a shared concurrency group. Avoid cancelling
a deployment already uploading/rolling out; reject a queued obsolete SHA rather
than releasing it after a newer successful version. Keep Cloud's deployment
verification enabled, record the deployment ID/artifact, and add HTTPS smoke checks
for HTML/deep links, assets, docs and API JSON errors. Preserve a known-good image
for manual redeployment. No workflow, secrets, branch settings or automation are
created or enabled by this proposal.
