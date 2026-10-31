import AxeBuilder from '@axe-core/playwright';
import type { Page, Route } from '@playwright/test';
import { expect, fakeUser, fillLogin, json, mockOnly, test } from './fixtures';

// D-RC-1 / D-RC-2 regressions (handover/RC_ACCEPTANCE.md §2). All API calls are
// mocked; the "server" clock is the test runner's real clock, the browser clock
// is faked with page.clock where the scenario needs a skewed device.
const HOUR = 3_600_000;
const KEY = 'rideshare.session.v1';
/** A token as the backend issues it: iat/exp on the server clock (`serverNow`), 60-minute lifetime. */
const serverToken = (serverNow = Date.now(), lifetimeSeconds = 3600) => {
  const iat = Math.floor(serverNow / 1000);
  return `header.${Buffer.from(JSON.stringify({ sub: '42', role: 'passenger', iat, exp: iat + lifetimeSeconds })).toString('base64url')}.signature`;
};

type Handler = (route: Route) => Promise<void> | void;
async function mockApi(page: Page, token = serverToken()) {
  const calls = { me: 0 };
  const handlers: Record<string, Handler> = {
    '/auth/login': route => json(route, 200, { access_token: token, token_type: 'bearer' }),
    '/me': route => { calls.me += 1; return json(route, 200, fakeUser); },
    '/rides/me': route => json(route, 200, []),
    '/bookings/me': route => json(route, 200, []),
  };
  await mockOnly(page, handlers);
  return { handlers, calls };
}
async function signIn(page: Page) {
  await page.goto('/login');
  await fillLogin(page);
  await expect(page).toHaveURL('/passenger');
  await expect(page.getByRole('heading', { name: 'Your journey is just beginning.' })).toBeVisible();
  await expect.poll(() => page.evaluate(k => sessionStorage.getItem(k), KEY)).not.toBeNull();
}
const stored = (page: Page) => page.evaluate(k => sessionStorage.getItem(k), KEY);

test.describe('D-RC-1: device clock skew', () => {
  test('browser clock 2 h fast: a valid token signs in, survives reload, and expires after its server lifetime', async ({ page }) => {
    await page.clock.install({ time: Date.now() + 2 * HOUR });
    const api = await mockApi(page);
    await signIn(page);
    await page.clock.fastForward('30:00');
    await page.reload();
    await expect(page.getByRole('heading', { name: 'Your journey is just beginning.' })).toBeVisible();
    await expect(page).toHaveURL('/passenger');
    expect(await stored(page)).not.toBeNull();
    const meBefore = api.calls.me;
    // The rest of the 60-minute lifetime (plus a margin) on the faked clock → expiry hint fires.
    await page.clock.fastForward('31:00');
    await expect(page).toHaveURL('/login');
    await expect(page.getByRole('status')).toHaveText('Your session has expired. Please sign in again.');
    expect(await page.evaluate(() => sessionStorage.length)).toBe(0);
    await page.waitForTimeout(1_000);
    expect(api.calls.me).toBe(meBefore); // expiry needs no identity call, and nothing loops
  });

  test('browser clock 2 h slow: the session still ends after the server lifetime', async ({ page }) => {
    await page.clock.install({ time: Date.now() - 2 * HOUR });
    await mockApi(page);
    await signIn(page);
    await page.clock.fastForward('59:00');
    await expect(page).toHaveURL('/passenger');
    await page.clock.fastForward('01:30');
    await expect(page).toHaveURL('/login');
    await expect(page.getByRole('status')).toHaveText('Your session has expired. Please sign in again.');
  });

  test('a really expired token on reload → /login with the expiry message (server 401 is the authority)', async ({ page }) => {
    const api = await mockApi(page);
    await page.goto('/login');
    // Issued 2 h ago with a 60-minute lifetime: expired at the server.
    await page.evaluate(([k, token]) => sessionStorage.setItem(k, JSON.stringify({ token, receivedAt: Date.now() - 2 * 3_600_000, mode: 'passenger' })), [KEY, serverToken(Date.now() - 2 * HOUR)] as const);
    api.handlers['/me'] = route => { api.calls.me += 1; return json(route, 401, { detail: 'Could not validate credentials' }); };
    await page.goto('/passenger/bookings');
    await expect(page).toHaveURL('/login');
    await expect(page.getByRole('status')).toHaveText('Your session has expired. Please sign in again.');
    expect(await page.evaluate(() => sessionStorage.length)).toBe(0);
    // One restore (the dev build's StrictMode may start it twice); nothing after that, no loop.
    const settled = api.calls.me;
    expect(settled).toBeGreaterThanOrEqual(1);
    expect(settled).toBeLessThanOrEqual(2);
    await page.waitForTimeout(1500);
    expect(api.calls.me).toBe(settled);
  });
});

test.describe('D-RC-2: API unreachable during a reload', () => {
  test('keeps the session, shows a retryable state without redirecting, and recovers on Retry', async ({ page }, testInfo) => {
    const api = await mockApi(page);
    await signIn(page);
    await page.goto('/passenger/bookings');
    await expect(page.getByRole('heading', { level: 1, name: 'Your seat requests' })).toBeVisible();
    const before = await stored(page);
    const up = api.handlers['/me'];
    let downCalls = 0;
    api.handlers['/me'] = route => { downCalls += 1; return route.abort('connectionrefused'); };
    await page.reload();
    await expect(page.getByRole('heading', { name: 'Can’t reach RideShare' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Retry' })).toBeVisible();
    await expect(page).toHaveURL('/passenger/bookings');
    expect(await stored(page)).toBe(before);
    // No private data from before the reload, no redirect loop.
    await expect(page.getByRole('heading', { name: 'Your seat requests' })).toHaveCount(0);
    // One restore attempt (the dev build's StrictMode may start it twice), then nothing: no loop.
    const settled = downCalls;
    expect(settled).toBeGreaterThanOrEqual(1);
    expect(settled).toBeLessThanOrEqual(2);
    await page.waitForTimeout(1500);
    expect(downCalls).toBe(settled);
    await expect(page).toHaveURL('/passenger/bookings');
    const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']).analyze();
    await testInfo.attach('axe-restore-failed.json', { body: JSON.stringify(results.violations, null, 2), contentType: 'application/json' });
    expect(results.violations.filter(v => v.impact === 'serious' || v.impact === 'critical').map(v => v.id)).toEqual([]);
    await page.screenshot({ path: testInfo.outputPath('restore-failed.png'), fullPage: true });
    // A failed retry keeps the state.
    await page.getByRole('button', { name: 'Retry' }).click();
    await expect.poll(() => downCalls).toBe(settled + 1);
    await expect(page.getByRole('heading', { name: 'Can’t reach RideShare' })).toBeVisible();
    // The API comes back.
    api.handlers['/me'] = up;
    await page.getByRole('button', { name: 'Retry' }).click();
    await expect(page.getByRole('heading', { level: 1, name: 'Your seat requests' })).toBeVisible();
    await expect(page).toHaveURL('/passenger/bookings');
    expect(await stored(page)).toBe(before);
  });

  test('R-6: the retry screen shows a neutral header (brand only), then the role nav after Retry', async ({ page }, testInfo) => {
    const api = await mockApi(page);
    await signIn(page);
    const header = page.locator('header.site-header');
    await expect(header.getByRole('link', { name: 'My dashboard' })).toBeVisible();
    const up = api.handlers['/me'];
    api.handlers['/me'] = route => route.abort('connectionrefused');
    await page.reload();
    const heading = page.getByRole('heading', { level: 1, name: 'Can’t reach RideShare' });
    await expect(heading).toBeVisible();
    await expect(heading).toBeFocused();
    await expect(page.getByRole('alert')).toContainText('You’re still signed in on this tab.');
    // Brand only: no sign-in / register links (the visitor is still signed in), no role or mode nav.
    await expect(header.getByRole('link', { name: 'RideShare home' })).toBeVisible();
    await expect(header.getByRole('link')).toHaveCount(1);
    await expect(header.getByRole('navigation')).toHaveCount(0);
    await expect(page.getByRole('link', { name: /log in|sign in/i })).toHaveCount(0);
    await expect(page.getByRole('link', { name: /get started|register|join rideshare/i })).toHaveCount(0);
    await expect(page.locator('a[href="/login"], a[href="/register"]')).toHaveCount(0);
    await expect(page.getByRole('navigation', { name: 'Travel mode' })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Retry' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
    const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']).analyze();
    await testInfo.attach('axe-restore-failed-neutral.json', { body: JSON.stringify(results.violations, null, 2), contentType: 'application/json' });
    expect(results.violations.map(v => v.id)).toEqual([]);
    await page.screenshot({ path: testInfo.outputPath('r6-neutral-header.png'), fullPage: true });
    // Keyboard: Tab from the focused heading reaches Retry.
    await page.keyboard.press('Tab');
    await expect(page.getByRole('button', { name: 'Retry' })).toBeFocused();
    api.handlers['/me'] = up;
    await page.keyboard.press('Enter');
    await expect(page.getByRole('heading', { name: 'Your journey is just beginning.' })).toBeVisible();
    await expect(header.getByRole('navigation', { name: 'Main navigation' }).getByRole('link', { name: 'My dashboard' })).toBeVisible();
    await expect(header.getByRole('button', { name: 'Log out' })).toBeVisible();
    await expect(page.getByRole('navigation', { name: 'Travel mode' })).toBeVisible();
    await expect(page.getByRole('link', { name: /log in/i })).toHaveCount(0);
    await expect(page.locator('#main-content')).toBeFocused();
    await page.screenshot({ path: testInfo.outputPath('r6-recovered.png'), fullPage: true });
  });

  test('a 503 during restore recovers on its own when the browser comes back online', async ({ page, context }) => {
    const api = await mockApi(page);
    await signIn(page);
    const up = api.handlers['/me'];
    api.handlers['/me'] = route => json(route, 503, { detail: 'Service Unavailable' });
    await page.reload();
    await expect(page.getByRole('heading', { name: 'Can’t reach RideShare' })).toBeVisible();
    api.handlers['/me'] = up;
    await context.setOffline(true);
    await context.setOffline(false); // fires `online` → automatic retry
    await expect(page.getByRole('heading', { name: 'Your journey is just beginning.' })).toBeVisible();
    await expect(page).toHaveURL('/passenger');
  });

  test('Sign out from the unreachable state goes to /login and clears the tab session', async ({ page }) => {
    const api = await mockApi(page);
    await signIn(page);
    api.handlers['/me'] = route => route.abort('connectionrefused');
    await page.reload();
    await page.getByRole('button', { name: 'Sign out' }).click();
    await expect(page).toHaveURL('/login');
    expect(await page.evaluate(() => sessionStorage.length)).toBe(0);
  });
});
