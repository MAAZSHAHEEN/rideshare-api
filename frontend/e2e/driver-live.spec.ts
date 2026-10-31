import type { Browser, BrowserContext, Page } from '@playwright/test';
import { expect, fillLogin, fillRegistration, test } from './fixtures';

// M3 Definition of Done against a REAL, disposable local backend (CI PostgreSQL
// or a local throwaway database). Same opt-in as live-backend.spec.ts:
// E2E_LIVE_BACKEND=1 plus a loopback PLAYWRIGHT_BASE_URL. Never point this at a
// deployed host. All identity values are obviously fake.
const baseURL = process.env.PLAYWRIGHT_BASE_URL ?? '';
const enabled = process.env.E2E_LIVE_BACKEND === '1' && /^http:\/\/(127\.0\.0\.1|localhost):\d+\/?$/.test(baseURL);

/** A second, isolated browser (own cookies and sessionStorage) that can only reach the local server. */
async function isolatedContext(browser: Browser): Promise<BrowserContext> {
  const context = await browser.newContext({ baseURL });
  const allowed = new URL(baseURL).origin;
  await context.route(url => url.origin !== allowed && /^https?:$/.test(url.protocol), route => route.abort('blockedbyclient'));
  return context;
}

async function passengerRequestsSeat(browser: Browser, email: string, cnic: string, password: string, rideId: number) {
  const context = await isolatedContext(browser);
  const page = await context.newPage();
  await page.goto('/register');
  await fillRegistration(page, { name: email.startsWith('e2e.p1') ? 'Asha Test' : 'Bilal Test', email, cnic, password });
  await expect(page).toHaveURL('/login');
  await fillLogin(page, email, password);
  await expect(page).toHaveURL('/passenger');
  await page.goto(`/passenger/rides/${rideId}`);
  await page.getByRole('button', { name: 'Request a seat' }).click();
  await expect(page.getByText('Seat requested.')).toBeVisible();
  return { context, page };
}

async function refreshRequests(page: Page, summary: string) {
  await page.getByRole('button', { name: 'Refresh' }).click();
  await expect(page.getByText(summary)).toBeVisible();
}

test.describe('live disposable backend: driver journey', () => {
  test.skip(!enabled, 'Set E2E_LIVE_BACKEND=1 and a loopback PLAYWRIGHT_BASE_URL to run against a local backend.');

  test('driver publishes → passenger requests → driver accepts (seats drop) → second request → driver rejects', async ({ page, browser, request }) => {
    test.setTimeout(150_000);
    const run = `${Date.now()}`.slice(-8);
    const password = 'A strong e2e password';
    const origin = `E2E Lahore ${run}`;
    const destination = `E2E Islamabad ${run}`;
    const tomorrowPkt = new Date(Date.now() + 5 * 3_600_000 + 86_400_000).toISOString().slice(0, 10);

    // 1. Driver registers and logs in through the browser.
    const driverEmail = `e2e.driver.${run}@example.com`;
    await page.goto('/register');
    await page.getByRole('radio', { name: 'Driver', exact: true }).check();
    await fillRegistration(page, { name: 'E2E Driver', email: driverEmail, cnic: `54321${run}`, password });
    await expect(page).toHaveURL('/login');
    await fillLogin(page, driverEmail, password);
    await expect(page).toHaveURL('/driver');
    await expect(page.getByText('Driver documents not verified')).toBeVisible();

    // 2. Publishes a ride departing tomorrow at 10:00 PKT, 2 seats, PKR 1,800.
    await page.getByRole('navigation', { name: 'Dashboard navigation' }).getByRole('link', { name: 'Publish a ride' }).click();
    await expect(page).toHaveURL('/driver/rides/new');
    await page.getByLabel('From').fill(origin);
    await page.getByLabel('To').fill(destination);
    await page.getByLabel('Departure (PKT)').fill(`${tomorrowPkt}T10:00`);
    await page.getByLabel('Seats offered').fill('2');
    await page.getByLabel('Fare per seat (PKR)').fill('1800');
    await page.getByRole('button', { name: 'Publish ride' }).click();
    await expect(page).toHaveURL(/\/driver\/rides\/\d+$/);
    const rideId = Number(new URL(page.url()).pathname.split('/').at(-1));
    await expect(page.getByText('Ride published.')).toBeVisible();
    await expect(page.getByText('10:00 am PKT').first()).toBeVisible();
    await expect(page.getByText('2 seats left')).toBeVisible();
    await expect(page.getByRole('heading', { name: 'No requests yet.' })).toBeVisible();
    // Deep link reload is served by the backend (G1) and keeps the tab session.
    await page.reload();
    await expect(page).toHaveURL(`/driver/rides/${rideId}`);
    await expect(page.getByText('2 seats left')).toBeVisible();

    // 3. Passenger 1 (second browser context) requests a seat.
    const p1 = await passengerRequestsSeat(browser, `e2e.p1.${run}@example.com`, `12345${run}`, password, rideId);
    // 4. Driver sees the request and accepts it: seats drop from 2 to 1.
    await refreshRequests(page, '1 pending · 0 accepted');
    await page.getByRole('button', { name: 'Accept request from Asha Test' }).click();
    await expect(page.getByRole('status').filter({ hasText: 'Accepted Asha Test’s request.' })).toBeVisible();
    await expect(page.getByText('1 seat left')).toBeVisible();
    await expect(page.getByText('0 pending · 1 accepted')).toBeVisible();

    // 5. Passenger 2 (third context) requests; driver rejects after confirming.
    const p2 = await passengerRequestsSeat(browser, `e2e.p2.${run}@example.com`, `13579${run}`, password, rideId);
    await refreshRequests(page, '1 pending · 1 accepted');
    await page.getByRole('button', { name: 'Reject request from Bilal Test' }).click();
    await page.getByRole('alertdialog', { name: 'Reject this request?' }).getByRole('button', { name: 'Reject request' }).click();
    await expect(page.getByRole('status').filter({ hasText: 'Rejected Bilal Test’s request.' })).toBeVisible();
    await expect(page.getByRole('heading', { level: 3 })).toHaveText(['Accepted (1)', 'Rejected (1)']);
    await expect(page.getByText('1 seat left')).toBeVisible();

    // Passengers see the driver's answers.
    await p1.page.goto('/passenger/bookings');
    await expect(p1.page.getByRole('heading', { name: 'Accepted (1)' })).toBeVisible();
    await p2.page.goto('/passenger/bookings');
    await expect(p2.page.getByRole('heading', { name: 'Rejected (1)' })).toBeVisible();

    // 6. Server truth. Re-answering an already answered request is refused with 409
    // (the UI's handling of that 409 is covered in driver.spec.ts).
    const login = await request.post('/auth/login', { data: { email: driverEmail, password } });
    const auth = { Authorization: `Bearer ${(await login.json()).access_token}` };
    const bookings = await (await request.get(`/rides/${rideId}/bookings`, { headers: auth })).json();
    expect(bookings.map((b: { status: string; passenger: { name: string } }) => `${b.passenger.name}:${b.status}`).sort()).toEqual(['Asha Test:accepted', 'Bilal Test:rejected']);
    expect(Object.keys(bookings[0].passenger).sort()).toEqual(['id', 'name']);
    expect((await request.patch(`/bookings/${bookings[0].id}/respond?accept=true`, { headers: auth })).status()).toBe(409);
    const ride = await (await request.get(`/rides/${rideId}`, { headers: auth })).json();
    expect(ride).toMatchObject({ origin, destination, available_seats: 1, fare_per_seat: 1800, status: 'active' });
    expect(Date.parse(ride.departure_time)).toBe(Date.parse(`${tomorrowPkt}T10:00:00+05:00`));

    // My rides shows it under Upcoming.
    await page.goto('/driver/rides?filter=upcoming');
    await expect(page.getByRole('article', { name: `${origin} to ${destination}` })).toContainText('1 seat left');
    await p1.context.close();
    await p2.context.close();
  });

  test('server stays authoritative: a past departure sent directly is refused with 422', async ({ request }) => {
    const run = `${Date.now()}`.slice(-8);
    const password = 'A strong e2e password';
    const user = { name: 'E2E Driver Two', email: `e2e.driver2.${run}@example.com`, cnic: `24680${run}`, phone_number: '03001234567', password, role: 'driver' };
    expect((await request.post('/auth/register', { data: user })).status()).toBe(201);
    const token = (await (await request.post('/auth/login', { data: { email: user.email, password } })).json()).access_token;
    const response = await request.post('/rides/', { headers: { Authorization: `Bearer ${token}` }, data: {
      origin: 'Lahore', destination: 'Islamabad', departure_time: '2020-01-01T10:00:00+05:00', available_seats: 2, fare_per_seat: 1000,
    } });
    expect(response.status()).toBe(422);
  });
});
