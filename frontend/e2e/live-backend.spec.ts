import { expect, fillLogin, fillRegistration, test } from './fixtures';

// End-to-end contract check against a REAL, disposable local backend: FastAPI
// serving the production build (frontend/dist) on the CI PostgreSQL service.
// Opt-in only (E2E_LIVE_BACKEND=1 plus a loopback PLAYWRIGHT_BASE_URL), so a
// plain `npm run test:e2e` stays fully mocked. Never point this at a deployed host.
const baseURL = process.env.PLAYWRIGHT_BASE_URL ?? '';
const enabled = process.env.E2E_LIVE_BACKEND === '1' && /^http:\/\/(127\.0\.0\.1|localhost):\d+\/?$/.test(baseURL);

test.describe('live disposable backend', () => {
  test.skip(!enabled, 'Set E2E_LIVE_BACKEND=1 and a loopback PLAYWRIGHT_BASE_URL to run against a local backend.');

  test('register, duplicate 409, bad login, login, empty dashboard, reload signs out', async ({ page }) => {
    test.setTimeout(60_000);
    // Unique, obviously fake values per run: example.com email, 12345-prefixed CNIC.
    const run = `${Date.now()}`.slice(-8);
    const email = `e2e.${run}@example.com`;
    const cnic = `12345${run}`;
    const password = 'A strong e2e password';

    await page.goto('/register');
    await fillRegistration(page, { email, cnic, password });
    await expect(page).toHaveURL('/login');
    await expect(page.getByRole('status')).toHaveText('Your account is ready. Log in to get started.');

    await page.goto('/register');
    await fillRegistration(page, { email, cnic: `54321${run}`, password });
    await expect(page.getByRole('alert')).toHaveText('Registration details already in use');

    await page.goto('/login');
    await fillLogin(page, email, 'not the right password');
    await expect(page.getByRole('alert')).toHaveText('Invalid email or password');

    await fillLogin(page, email, password);
    await expect(page).toHaveURL('/passenger');
    await expect(page.getByRole('heading', { name: 'Your journey is just beginning.' })).toBeVisible();

    await page.reload();
    await expect(page).toHaveURL('/login');
  });

  test('API namespaces keep JSON errors and are never swallowed by the SPA', async ({ request }) => {
    const missing = await request.get('/rides/does-not-exist-e2e');
    expect(missing.status()).toBeGreaterThanOrEqual(400);
    expect(missing.headers()['content-type']).toContain('application/json');
    const me = await request.get('/me');
    expect([401, 403]).toContain(me.status());
  });
});
