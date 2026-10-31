import type { APIRequestContext, Browser, BrowserContext } from '@playwright/test';
import { expect, fillLogin, test } from './fixtures';

// Multi-seat bookings (#22 §3) end to end against a REAL, disposable local
// backend serving the production bundle. Same opt-in as the other live specs:
// E2E_LIVE_BACKEND=1 plus a loopback PLAYWRIGHT_BASE_URL. Never point this at a
// deployed host. All identity values are obviously fake.
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

async function register(request: APIRequestContext, name: string, email: string, cnic: string, role: 'passenger' | 'driver') {
  expect((await request.post('/auth/register', { data: { name, email, cnic, phone_number: '03001234567', password, role } })).status()).toBe(201);
}

async function signedIn(browser: Browser, email: string, home: '/passenger' | '/driver') {
  const context = await isolatedContext(browser);
  const page = await context.newPage();
  await page.goto('/login');
  await fillLogin(page, email, password);
  await expect(page).toHaveURL(home);
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem('rideshare.session.v1'))).not.toBeNull();
  return { context, page };
}

async function apiToken(request: APIRequestContext, email: string) {
  const login = await request.post('/auth/login', { data: { email, password } });
  expect(login.status()).toBe(200);
  return { Authorization: `Bearer ${(await login.json()).access_token}` };
}

test.describe('live disposable backend: multi-seat bookings', () => {
  test.skip(!enabled, 'Set E2E_LIVE_BACKEND=1 and a loopback PLAYWRIGHT_BASE_URL to run against a local backend.');

  test('2 of 3 seats → accept → 1 left → a 2-seat request cannot be accepted (disabled + 409) → cancel restores 2', async ({ browser, request }, testInfo) => {
    test.setTimeout(180_000);
    const run = `${Date.now()}`.slice(-8);
    const day = tomorrowPkt();
    const driverEmail = `e2e.ms.driver.${run}@example.com`;
    await register(request, 'E2E Seat Driver', driverEmail, `54321${run}`, 'driver');
    const driverAuth = await apiToken(request, driverEmail);
    const created = await request.post('/rides/', { headers: driverAuth, data: {
      origin: `E2E Lahore ${run}`, destination: `E2E Multan ${run}`, departure_time: `${day}T10:00:00+05:00`, available_seats: 3, fare_per_seat: 1800 } });
    expect(created.status()).toBe(201);
    const rideId: number = (await created.json()).id;
    const seatsLeft = async () => (await (await request.get(`/rides/${rideId}`, { headers: driverAuth })).json()).available_seats as number;

    const emails = [1, 2, 3].map(n => `e2e.ms.p${n}.${run}@example.com`);
    await register(request, 'Asha Seat', emails[0], `12345${run}`, 'passenger');
    await register(request, 'Bilal Seat', emails[1], `12346${run}`, 'passenger');
    await register(request, 'Chand Seat', emails[2], `12347${run}`, 'passenger');

    // 1. Passenger 1 requests 2 of 3 seats through the selector.
    const p1 = await signedIn(browser, emails[0], '/passenger');
    await p1.page.goto(`/passenger/rides/${rideId}`);
    const p1Seats = p1.page.getByRole('combobox', { name: 'Seats' });
    await expect(p1Seats.locator('option')).toHaveText(['1 seat', '2 seats', '3 seats']);
    await p1Seats.selectOption('2');
    await expect(p1.page.locator('#seat-total')).toHaveText('Total PKR 3,600 for 2 seats (PKR 1,800 each)');
    await p1.page.getByRole('button', { name: 'Request 2 seats' }).click();
    await expect(p1.page.getByText('2 seats requested.')).toBeVisible();
    expect(await seatsLeft()).toBe(3); // a request debits nothing

    // 2. Passenger 2 also asks for 2 seats while 3 are left (still allowed: requests don't debit).
    const p2 = await signedIn(browser, emails[1], '/passenger');
    await p2.page.goto(`/passenger/rides/${rideId}`);
    await p2.page.getByRole('combobox', { name: 'Seats' }).selectOption('2');
    await p2.page.getByRole('button', { name: 'Request 2 seats' }).click();
    await expect(p2.page.getByText('2 seats requested.')).toBeVisible();

    // Passenger 3 opens the page now (3 left) and picks 2, but submits only after the accept below.
    const p3 = await signedIn(browser, emails[2], '/passenger');
    await p3.page.goto(`/passenger/rides/${rideId}`);
    await p3.page.getByRole('combobox', { name: 'Seats' }).selectOption('2');

    // 3. The driver sees "2 seats" on both requests and accepts passenger 1.
    const driver = await signedIn(browser, driverEmail, '/driver');
    await driver.page.goto(`/driver/rides/${rideId}`);
    await expect(driver.page.getByRole('article', { name: 'Asha Seat' }).getByText('2 seats', { exact: true })).toBeVisible();
    await expect(driver.page.getByRole('article', { name: 'Bilal Seat' }).getByText('2 seats', { exact: true })).toBeVisible();
    await driver.page.getByRole('button', { name: 'Accept request from Asha Seat' }).click();
    await expect(driver.page.getByText('Accepted Asha Seat’s request. 2 seats are now taken.')).toBeVisible();
    await expect(driver.page.getByText('1 seat left')).toBeVisible();
    expect(await seatsLeft()).toBe(1);

    // 4. Passenger 2's 2-seat request no longer fits: accept is disabled with the reason…
    const bilalAccept = driver.page.getByRole('button', { name: 'Accept request from Bilal Seat' });
    await expect(bilalAccept).toBeDisabled();
    await expect(bilalAccept).toHaveAccessibleDescription('Needs 2 seats, but only 1 is left. Reject it, or wait until seats free up.');
    await testInfo.attach('driver-accept-disabled.png', { body: await driver.page.screenshot({ fullPage: true }), contentType: 'image/png' });
    // …and the server agrees (the button is only a courtesy): 409 "Not enough seats available".
    const bilalBooking = (await (await request.get(`/rides/${rideId}/bookings?status=pending`, { headers: driverAuth })).json())
      .find((b: { passenger: { name: string } }) => b.passenger.name === 'Bilal Seat');
    const forced = await request.patch(`/bookings/${bilalBooking.id}/respond?accept=true`, { headers: driverAuth });
    expect(forced.status()).toBe(409);
    expect(await forced.json()).toEqual({ detail: 'Not enough seats available' });
    expect(await seatsLeft()).toBe(1);

    // 5. Passenger 3's stale page submits 2 seats: fixed 409 copy, the ride refetches, the selector shrinks to 1.
    await p3.page.getByRole('button', { name: 'Request 2 seats' }).click();
    await expect(p3.page.getByRole('alert')).toHaveText('Not enough seats are left for that many. Choose fewer seats and try again.');
    await expect(p3.page.getByRole('combobox', { name: 'Seats' }).locator('option')).toHaveText(['1 seat']);
    await expect(p3.page.getByText('1 seat left')).toBeVisible();

    // 6. Passenger 1 cancels the accepted 2-seat booking: both seats come back.
    await p1.page.goto('/passenger/bookings');
    const card = p1.page.getByRole('article').filter({ hasText: `E2E Lahore ${run}` });
    await expect(card.getByText('2 seats', { exact: true })).toBeVisible();
    await expect(card).toContainText('PKR 3,600 (2 × PKR 1,800)');
    await card.getByRole('button', { name: /^Cancel booking:/ }).click();
    const dialog = p1.page.getByRole('alertdialog');
    await expect(dialog).toContainText('Your 2 confirmed seats will be released');
    await dialog.getByRole('button', { name: 'Cancel booking' }).click();
    await expect(p1.page.getByRole('status').filter({ hasText: 'Booking cancelled' })).toBeVisible();
    expect(await seatsLeft()).toBe(3);

    // 7. The driver refreshes: 3 left, and passenger 2's request can be accepted again.
    await driver.page.getByRole('button', { name: 'Refresh' }).click();
    await expect(driver.page.getByText('3 seats left')).toBeVisible();
    await expect(bilalAccept).toBeEnabled();

    for (const c of [p1, p2, p3, driver]) await c.context.close();
  });
});
