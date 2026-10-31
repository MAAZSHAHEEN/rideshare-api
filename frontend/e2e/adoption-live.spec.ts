import type { APIRequestContext, Browser, BrowserContext, Page } from '@playwright/test';
import { expect, fillLogin, fillRegistration, test } from './fixtures';

// Milestone B frontend adoption (PR #17 API gaps + passenger path routes)
// against a REAL, disposable local backend serving the production bundle
// (frontend_serving). Same opt-in as the other live specs: E2E_LIVE_BACKEND=1
// plus a loopback PLAYWRIGHT_BASE_URL. Never point this at a deployed host.
// All identity values are obviously fake.
const baseURL = process.env.PLAYWRIGHT_BASE_URL ?? '';
const enabled = process.env.E2E_LIVE_BACKEND === '1' && /^http:\/\/(127\.0\.0\.1|localhost):\d+\/?$/.test(baseURL);
const password = 'A strong e2e password';
const tomorrowPkt = () => new Date(Date.now() + 5 * 3_600_000 + 86_400_000).toISOString().slice(0, 10);

async function isolatedContext(browser: Browser): Promise<BrowserContext> {
  const context = await browser.newContext({ baseURL });
  const allowed = new URL(baseURL).origin;
  await context.route(url => url.origin !== allowed && /^https?:$/.test(url.protocol), route => route.abort('blockedbyclient'));
  return context;
}

/** Registers through the API (fast) and signs in through the browser. */
async function signedInPassenger(browser: Browser, request: APIRequestContext, name: string, email: string, cnic: string) {
  expect((await request.post('/auth/register', { data: { name, email, cnic, phone_number: '03001234567', password, role: 'passenger' } })).status()).toBe(201);
  const context = await isolatedContext(browser);
  const page = await context.newPage();
  await page.goto('/login');
  await fillLogin(page, email, password);
  await expect(page).toHaveURL('/passenger');
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem('rideshare.session.v1'))).not.toBeNull();
  return { context, page };
}

async function apiToken(request: APIRequestContext, email: string) {
  const login = await request.post('/auth/login', { data: { email, password } });
  expect(login.status()).toBe(200);
  return { Authorization: `Bearer ${(await login.json()).access_token}` };
}

async function openRequests(page: Page, rideId: number, summary: string) {
  await page.goto(`/driver/rides/${rideId}`);
  await expect(page.getByText(summary)).toBeVisible();
}

test.describe('live disposable backend: API-gap adoption', () => {
  test.skip(!enabled, 'Set E2E_LIVE_BACKEND=1 and a loopback PLAYWRIGHT_BASE_URL to run against a local backend.');

  test('demo: publish → case-insensitive search → request → pending count → accept → accepted after refresh → withdraw (D7) → reject → seats', async ({ page, browser, request }) => {
    test.setTimeout(180_000);
    const run = `${Date.now()}`.slice(-8);
    const origin = `E2E Lahore ${run}`;
    const destination = `E2E Islamabad ${run}`;
    const day = tomorrowPkt();

    // 1. Driver registers + logs in through the browser and publishes (2 seats, PKR 1,800, tomorrow 10:00 PKT).
    const driverEmail = `e2e.adopt.driver.${run}@example.com`;
    await page.goto('/register');
    await page.getByRole('radio', { name: 'Driver', exact: true }).check();
    await fillRegistration(page, { name: 'E2E Driver', email: driverEmail, cnic: `54321${run}`, password });
    await expect(page).toHaveURL('/login');
    await fillLogin(page, driverEmail, password);
    await expect(page).toHaveURL('/driver');
    await page.goto('/driver/rides/new');
    await page.getByLabel('From').fill(origin);
    await page.getByLabel('To').fill(destination);
    await page.getByLabel('Departure (PKT)').fill(`${day}T10:00`);
    await page.getByLabel('Seats offered').fill('2');
    await page.getByLabel('Fare per seat (PKR)').fill('1800');
    await page.getByRole('button', { name: 'Publish ride' }).click();
    await expect(page).toHaveURL(/\/driver\/rides\/\d+$/);
    const rideId = Number(new URL(page.url()).pathname.split('/').at(-1));
    await expect(page.getByRole('heading', { name: 'No requests yet.' })).toBeVisible();

    // 2. Passenger 1 searches with different case and stray spaces (G3) and requests a seat.
    const p1 = await signedInPassenger(browser, request, 'Asha Test', `e2e.adopt.p1.${run}@example.com`, `12345${run}`);
    await p1.page.goto('/passenger/search');
    await p1.page.getByLabel('From', { exact: true }).fill(`  ${origin.toUpperCase()} `);
    await p1.page.getByLabel('To', { exact: true }).fill(destination.toLowerCase());
    await p1.page.getByLabel('Date (PKT)').fill(day);
    await p1.page.getByRole('button', { name: 'Search rides' }).click();
    await expect(p1.page.getByText('1 ride found.')).toBeVisible();
    await expect(p1.page.getByText('Page 1 of 1')).toBeVisible();
    await p1.page.getByRole('article', { name: `${origin} to ${destination}` }).getByRole('link', { name: /^View details/ }).click();
    await expect(p1.page).toHaveURL(`/passenger/rides/${rideId}`);
    await p1.page.getByRole('button', { name: 'Request a seat' }).click();
    await expect(p1.page.getByText('Seat requested.')).toBeVisible();

    // 3. Driver sees the pending count on My rides › Upcoming (D1 + D2), then on the ride.
    await page.goto('/driver/rides?filter=upcoming');
    const card = page.getByRole('article', { name: `${origin} to ${destination}` });
    await expect(card).toContainText('1 pending');
    await openRequests(page, rideId, '1 pending · 0 accepted');
    await page.getByRole('button', { name: 'Accept request from Asha Test' }).click();
    await expect(page.getByText('1 seat left')).toBeVisible();
    await expect(page.getByText('0 pending · 1 accepted')).toBeVisible();

    // 4. Passenger 1 refreshes the ride page: G2 lookup shows the accepted booking.
    await p1.page.reload();
    await expect(p1.page).toHaveURL(`/passenger/rides/${rideId}`);
    const mine = p1.page.getByRole('status').filter({ hasText: 'already requested a seat on this ride' });
    await expect(mine).toContainText('accepted');
    await expect(p1.page.getByRole('button', { name: 'Request a seat' })).toHaveCount(0);

    // 5. Passenger 2 requests, the driver's page shows it, then passenger 2 withdraws before the driver answers.
    const p2 = await signedInPassenger(browser, request, 'Bilal Test', `e2e.adopt.p2.${run}@example.com`, `13579${run}`);
    await p2.page.goto(`/passenger/rides/${rideId}`);
    await p2.page.getByRole('button', { name: 'Request a seat' }).click();
    await expect(p2.page.getByText('Seat requested.')).toBeVisible();
    await page.getByRole('button', { name: 'Refresh' }).click();
    await expect(page.getByText('1 pending · 1 accepted')).toBeVisible();
    await p2.page.goto('/passenger/bookings');
    await p2.page.getByRole('button', { name: new RegExp(`^Cancel booking: ${origin}`) }).click();
    await p2.page.getByRole('alertdialog').getByRole('button', { name: 'Cancel booking' }).click();
    await expect(p2.page.getByRole('status').filter({ hasText: 'Booking cancelled' })).toBeVisible();
    // The driver's list is stale (still shows Bilal pending): accepting now gets the D7 409.
    await page.getByRole('button', { name: 'Accept request from Bilal Test' }).click();
    await expect(page.getByRole('alert')).toHaveText('This booking is no longer available.');
    await expect(page.getByText('0 pending · 1 accepted')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Accept request from Bilal Test' })).toHaveCount(0);

    // 6. Passenger 3 requests and the driver rejects.
    const p3 = await signedInPassenger(browser, request, 'Chand Test', `e2e.adopt.p3.${run}@example.com`, `11223${run}`);
    await p3.page.goto(`/passenger/rides/${rideId}`);
    await p3.page.getByRole('button', { name: 'Request a seat' }).click();
    await expect(p3.page.getByText('Seat requested.')).toBeVisible();
    await page.getByRole('button', { name: 'Refresh' }).click();
    await expect(page.getByText('1 pending · 1 accepted')).toBeVisible();
    await page.getByRole('button', { name: 'Reject request from Chand Test' }).click();
    await page.getByRole('alertdialog', { name: 'Reject this request?' }).getByRole('button', { name: 'Reject request' }).click();
    await expect(page.getByRole('heading', { level: 3 })).toHaveText(['Accepted (1)', 'Rejected (1)', 'Cancelled (1)']);
    await expect(page.getByText('1 seat left')).toBeVisible();
    await p3.page.goto('/passenger/bookings');
    await expect(p3.page.getByRole('heading', { name: 'Rejected (1)' })).toBeVisible();
    await expect(p3.page.getByRole('button', { name: 'Rejected (1)' })).toBeVisible();

    // 7. Server truth: seat accounting, statuses, totals and counts.
    const auth = await apiToken(request, driverEmail);
    const ride = await (await request.get(`/rides/${rideId}`, { headers: auth })).json();
    expect(ride).toMatchObject({ available_seats: 1, status: 'active' });
    const list = await request.get(`/rides/${rideId}/bookings`, { headers: auth });
    expect(list.headers()['x-total-count']).toBe('3');
    expect((await list.json()).map((b: { status: string; passenger: { name: string } }) => `${b.passenger.name}:${b.status}`).sort())
      .toEqual(['Asha Test:accepted', 'Bilal Test:cancelled', 'Chand Test:rejected']);
    const counts = await (await request.get(`/rides/me/booking-counts?ride_id=${rideId}`, { headers: auth })).json();
    expect(counts).toEqual([{ ride_id: rideId, pending: 0, accepted: 1, rejected: 1, cancelled: 1 }]);
    for (const p of [p1, p2, p3]) await p.context.close();
  });

  test('pagination totals over 21 seeded rides, and refresh/deep link on every route against the production bundle', async ({ page, browser, request }) => {
    test.setTimeout(120_000);
    const run = `${Date.now()}`.slice(-8);
    const origin = `E2E Multan ${run}`;
    const destination = `E2E Quetta ${run}`;
    const driverEmail = `e2e.adopt.pager.${run}@example.com`;
    expect((await request.post('/auth/register', { data: { name: 'E2E Pager', email: driverEmail, cnic: `24680${run}`, phone_number: '03001234567', password, role: 'driver' } })).status()).toBe(201);
    const auth = await apiToken(request, driverEmail);
    const day = tomorrowPkt();
    let firstRide = 0;
    for (let i = 0; i < 21; i += 1) {
      const hh = String(1 + Math.floor(i / 2)).padStart(2, '0');
      const created = await request.post('/rides/', { headers: auth, data: { origin, destination, departure_time: `${day}T${hh}:${i % 2 ? '30' : '00'}:00+05:00`, available_seats: 1, fare_per_seat: 1000 } });
      expect(created.status()).toBe(201);
      if (!firstRide) firstRide = (await created.json()).id;
    }

    // Driver browser: My rides › Upcoming shows the accurate total and two pages.
    await page.goto('/login');
    await fillLogin(page, driverEmail, password);
    await expect(page).toHaveURL('/driver');
    await page.goto('/driver/rides?filter=upcoming');
    await expect(page.getByText('Page 1 of 2')).toBeVisible();
    await expect(page.getByText(/Showing 1–20 of 21 rides\./)).toBeVisible();
    await expect(page.getByRole('article').first()).toContainText('1:00 am PKT');
    await page.getByRole('button', { name: 'Next →' }).click();
    await expect(page.getByRole('article')).toHaveCount(1);
    await expect(page.getByRole('button', { name: 'Next →' })).toBeDisabled();

    // Driver deep links: reload each against the backend-served bundle.
    for (const path of ['/driver', '/driver/rides', '/driver/rides?filter=departed&page=0', '/driver/rides/new', `/driver/rides/${firstRide}`]) {
      await page.goto(path);
      await page.reload();
      await expect(page).toHaveURL(path);
      await expect(page.locator('h1')).toBeVisible();
    }

    // Passenger: case-insensitive search total across pages, then each passenger route survives reload.
    const p = await signedInPassenger(browser, request, 'Dua Test', `e2e.adopt.p4.${run}@example.com`, `11335${run}`);
    await p.page.goto(`/passenger/search?from=${encodeURIComponent(origin.toLowerCase())}&to=${encodeURIComponent(destination.toUpperCase())}`);
    await expect(p.page.getByText('21 rides found. Showing 1–20.')).toBeVisible();
    await p.page.getByRole('button', { name: 'Next →' }).click();
    await expect(p.page.getByText('21 rides found. Showing 21–21.')).toBeVisible();
    for (const path of ['/passenger', '/passenger/search', `/passenger/search?from=${encodeURIComponent(origin)}&page=1`, `/passenger/rides/${firstRide}`, '/passenger/bookings', '/passenger/bookings?status=pending']) {
      await p.page.goto(path);
      await p.page.reload();
      await expect(p.page).toHaveURL(path);
      await expect(p.page.locator('h1')).toBeVisible();
    }
    // Old query links still land on the path routes.
    await p.page.goto(`/passenger?ride=${firstRide}`);
    await expect(p.page).toHaveURL(`/passenger/rides/${firstRide}`);
    // The server answers every deep link with the SPA (no-cache), never JSON 404.
    for (const path of ['/passenger/search', `/passenger/rides/${firstRide}`, '/passenger/bookings', `/driver/rides/${firstRide}`]) {
      const response = await request.get(path);
      expect(response.status(), path).toBe(200);
      expect(response.headers()['content-type'], path).toContain('text/html');
    }
    await p.context.close();
  });
});
