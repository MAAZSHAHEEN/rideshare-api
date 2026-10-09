import { test, expect } from '@playwright/test';
import type { Page } from '@playwright/test';

// Browser contract fixtures only: no accounts are created on a real server.
const identity = { id: 42, name: 'Ayesha Khan', email: 'ayesha@example.com', role: 'passenger' };
const token = () => `header.${Buffer.from(JSON.stringify({ exp: Math.floor(Date.now() / 1000) + 3600, role: 'admin' })).toString('base64url')}.signature`;
async function mockApi(page: Page, role = 'passenger', options: { failMe?: boolean; failList?: boolean; populated?: boolean } = {}) {
  await page.route('http://localhost:8000/**', async route => {
    const path = new URL(route.request().url()).pathname;
    let body: unknown = {};
    let status = 200;
    if (path === '/auth/login') body = { access_token: token(), token_type: 'bearer' };
    else if (path === '/me') {
      expect(route.request().headers().authorization).toContain('Bearer ');
      status = options.failMe ? 401 : 200;
      body = options.failMe ? { detail: 'Invalid token' } : { ...identity, role };
    } else if (path === '/auth/register') { status = 201; body = identity; }
    else if (path === '/rides/me' || path === '/bookings/me') {
      expect(route.request().headers().authorization).toContain('Bearer ');
      status = options.failList ? 503 : 200;
      const ride = { id: 7, driver_id: 42, origin: 'Peshawar', destination: 'Islamabad', departure_time: '2030-06-12T09:00:00Z', available_seats: 2, fare_per_seat: 500, status: 'active' };
      body = options.failList ? { detail: 'Database unavailable' } : options.populated ? (role === 'driver' ? [ride] : [{ id: 10, passenger_id: 42, ride_id: 7, status: 'pending', ride }]) : [];
    } else throw new Error(`Unexpected API request: ${path}`);
    await route.fulfill({ status, contentType: 'application/json', headers: { 'Access-Control-Allow-Origin': '*' }, body: JSON.stringify(body) });
  });
}
async function login(page: Page) {
  await page.goto('/login');
  await page.getByLabel('Email address').fill(identity.email);
  await page.getByLabel('Password', { exact: true }).fill('A strong password');
  await page.getByRole('button', { name: 'Log in', exact: true }).click();
}

test('landing desktop/mobile, navigation and registration visual review', async ({ page }, testInfo) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Your next journey. Better shared.' })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('landing-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: 'Open navigation' }).click();
  await page.getByRole('navigation', { name: 'Main navigation' }).getByRole('link', { name: 'Log in', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Welcome back.' })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('login-mobile.png'), fullPage: true });
  await page.getByRole('link', { name: 'Create an account', exact: true }).click();
  await expect(page.getByLabel('CNIC', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('registration-mobile.png'), fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.screenshot({ path: testInfo.outputPath('registration-desktop.png'), fullPage: true });
});

test('registration sends required fields and selected role, then redirects to login', async ({ page }) => {
  await mockApi(page);
  await page.goto('/register');
  await page.getByRole('radio', { name: 'Driver', exact: true }).check();
  await page.getByLabel('Full name').fill('Ayesha Khan');
  await page.getByLabel('Email address').fill(identity.email);
  await page.getByLabel('CNIC', { exact: true }).fill('1234512345671');
  await page.getByLabel('Phone number').fill('03001234567');
  await page.getByLabel('Password', { exact: true }).fill('  password with spaces  ');
  const request = page.waitForRequest(r => r.url().endsWith('/auth/register'));
  await page.getByRole('button', { name: 'Create account', exact: true }).click();
  expect((await request).postDataJSON()).toEqual({ name: 'Ayesha Khan', email: identity.email, cnic: '1234512345671', phone_number: '03001234567', password: '  password with spaces  ', role: 'driver' });
  await expect(page).toHaveURL('/login');
  await expect(page.getByRole('status')).toHaveText('Your account is ready. Log in to get started.');
});

test('invalid password fails locally; server validation and conflict are accessible', async ({ page }) => {
  await page.goto('/register');
  await page.getByLabel('Full name').fill('Ayesha');
  await page.getByLabel('Email address').fill(identity.email);
  await page.getByLabel('CNIC', { exact: true }).fill('1');
  await page.getByLabel('Phone number').fill('1');
  await page.getByLabel('Password', { exact: true }).fill('short');
  await page.getByRole('button', { name: 'Create account', exact: true }).click();
  await expect(page.getByText('Use at least 12 characters.', { exact: true })).toBeVisible();
  await expect(page.getByLabel('Password', { exact: true })).toHaveAttribute('aria-invalid', 'true');
  await page.route('http://localhost:8000/auth/register', route => route.fulfill({ status: 409, contentType: 'application/json', body: '{"detail":"Registration details already in use"}' }));
  await page.getByLabel('Password', { exact: true }).fill('A strong password');
  await page.getByRole('button', { name: 'Create account', exact: true }).click();
  await expect(page.getByRole('alert')).toHaveText('Registration details already in use');
  await expect(page.getByRole('alert')).toBeFocused();
});

test('passenger identity controls navigation; logout and reload end sessions', async ({ page }, testInfo) => {
  await mockApi(page);
  await page.goto('/passenger');
  await expect(page).toHaveURL('/login');
  await login(page);
  await expect(page).toHaveURL('/passenger');
  await expect(page.getByRole('heading', { name: 'Your journey is just beginning.' })).toBeVisible();
  // Client-side navigation preserves the memory session and exercises the role guard.
  await page.evaluate(() => { history.pushState({}, '', '/driver'); dispatchEvent(new PopStateEvent('popstate')); });
  await expect(page).toHaveURL('/passenger');
  await expect(page.locator('.skip-link')).not.toBeFocused();
  await page.screenshot({ path: testInfo.outputPath('passenger-desktop.png'), fullPage: true });
  await page.getByRole('link', { name: 'My account', exact: true }).click();
  await expect(page.getByText(identity.email)).toBeVisible();
  expect(await page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length }))).toEqual({ local: 0, session: 0 });
  await page.getByRole('main').getByRole('button', { name: 'Log out', exact: true }).click();
  await expect(page).toHaveURL('/login');
  await login(page);
  await expect(page).toHaveURL('/passenger');
  await page.reload();
  await expect(page).toHaveURL('/login');
});

test('driver dashboard displays real response shape and is responsive', async ({ page }, testInfo) => {
  await mockApi(page, 'driver', { populated: true });
  await login(page);
  await expect(page).toHaveURL('/driver');
  await expect(page.getByRole('heading', { name: 'Peshawar to Islamabad' })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('driver-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('driver-mobile.png'), fullPage: true });
});

test('unverified identity never unlocks dashboard', async ({ page }) => {
  await mockApi(page, 'passenger', { failMe: true });
  await login(page);
  await expect(page.getByRole('alert')).toHaveText('Invalid token');
  await expect(page).toHaveURL('/login');
});

test('history errors offer retry without pretending the list is empty', async ({ page }) => {
  await mockApi(page, 'passenger', { failList: true });
  await login(page);
  await expect(page.getByRole('heading', { name: 'We couldn’t load your journeys.' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Try again' })).toBeVisible();
  await expect(page.getByText('Your journey is just beginning.')).toHaveCount(0);
});

test('dashboard exposes a loading state until history resolves', async ({ page }) => {
  await mockApi(page);
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  await page.route('http://localhost:8000/bookings/me?**', async route => {
    await gate;
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
  });
  await login(page);
  await expect(page.getByRole('status')).toContainText('Loading your journeys');
  release();
  await expect(page.getByRole('heading', { name: 'Your journey is just beginning.' })).toBeVisible();
});

test('admin has a verified account page, never passenger or driver privileges', async ({ page }) => {
  await mockApi(page, 'admin');
  await login(page);
  await expect(page).toHaveURL('/account');
  await expect(page.getByText('This frontend does not provide an admin console.', { exact: false })).toBeVisible();
});
