import { test as base, expect } from '@playwright/test';
import type { Page, Route } from '@playwright/test';

// The API origin the bundle under test calls. The Vite dev server targets the
// local FastAPI default; a production bundle served by FastAPI is same-origin.
export const apiOrigin = process.env.PLAYWRIGHT_API_ORIGIN || 'http://localhost:8000';

/**
 * Every test runs offline apart from the app under test and its API origin.
 * Third-party requests (for example the Google Fonts stylesheet) are aborted so
 * results never depend on an external network, and so no test can reach a
 * production host by accident.
 */
export const test = base.extend({
  context: async ({ context, baseURL }, use) => {
    const allowed = new Set([new URL(baseURL ?? 'http://127.0.0.1:5183').origin, new URL(apiOrigin).origin]);
    await context.route(url => !allowed.has(url.origin) && /^https?:$/.test(url.protocol), route => route.abort('blockedbyclient'));
    await use(context);
  },
});
export { expect };

// Obviously fake identity data; never real CNIC, phone or personal details.
export const fakeUser = {
  id: 42, name: 'Test Passenger', email: 'test.passenger@example.com', role: 'passenger',
  allowed_modes: ['passenger', 'driver'],
};
export const fakeToken = () => `header.${Buffer.from(JSON.stringify({ exp: Math.floor(Date.now() / 1000) + 3600 })).toString('base64url')}.signature`;

export function json(route: Route, status: number, body: unknown) {
  return route.fulfill({ status, contentType: 'application/json', headers: { 'Access-Control-Allow-Origin': '*' }, body: JSON.stringify(body) });
}

/** Mocks one API path; any other API call fails the test instead of leaking to a server. */
export async function mockOnly(page: Page, handlers: Record<string, (route: Route) => Promise<void> | void>) {
  await page.route(url => url.origin === new URL(apiOrigin).origin && /^\/(auth\/|me$|rides\/|bookings\/)/.test(url.pathname), async route => {
    const path = new URL(route.request().url()).pathname;
    const handler = handlers[path];
    if (!handler) throw new Error(`Unexpected API request: ${route.request().method()} ${path}`);
    await handler(route);
  });
}

/** Signed-in passenger with empty histories. */
export async function mockSignedIn(page: Page) {
  await mockOnly(page, {
    '/auth/login': route => json(route, 200, { access_token: fakeToken(), token_type: 'bearer' }),
    '/me': route => json(route, 200, fakeUser),
    '/rides/me': route => json(route, 200, []),
    '/bookings/me': route => json(route, 200, []),
  });
}

export async function fillLogin(page: Page, email = fakeUser.email, password = 'A strong password') {
  await page.getByLabel('Email address').fill(email);
  await page.getByLabel('Password', { exact: true }).fill(password);
  await page.getByRole('button', { name: 'Log in', exact: true }).click();
}

export async function fillRegistration(page: Page, overrides: Partial<Record<'name' | 'email' | 'cnic' | 'phone' | 'password', string>> = {}) {
  await page.getByLabel('Full name').fill(overrides.name ?? 'Test Passenger');
  await page.getByLabel('Email address').fill(overrides.email ?? fakeUser.email);
  await page.getByLabel('CNIC', { exact: true }).fill(overrides.cnic ?? '1234512345671');
  await page.getByLabel('Phone number').fill(overrides.phone ?? '03001234567');
  await page.getByLabel('Password', { exact: true }).fill(overrides.password ?? 'A strong password');
  await page.getByRole('button', { name: 'Create account', exact: true }).click();
}

/**
 * True when the page has no horizontal scrolling at the configured viewport.
 * Compares with the configured device width, not innerWidth: with isMobile the
 * browser widens the layout viewport to fit overflowing content, which would hide it.
 */
export const noHorizontalOverflow = (page: Page) => {
  const width = page.viewportSize()?.width ?? 0;
  return page.evaluate(w => document.documentElement.scrollWidth <= w && document.body.scrollWidth <= w, width);
};
