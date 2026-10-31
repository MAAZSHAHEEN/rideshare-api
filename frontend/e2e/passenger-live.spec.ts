import { expect, fillLogin, fillRegistration, test } from './fixtures';

// AST-023 Definition of Done against a REAL, disposable local backend (the CI
// PostgreSQL service, or a local throwaway database). Same opt-in as
// live-backend.spec.ts: E2E_LIVE_BACKEND=1 plus a loopback PLAYWRIGHT_BASE_URL.
// Never point this at a deployed host. All identity values are obviously fake.
const baseURL = process.env.PLAYWRIGHT_BASE_URL ?? '';
const enabled = process.env.E2E_LIVE_BACKEND === '1' && /^http:\/\/(127\.0\.0\.1|localhost):\d+\/?$/.test(baseURL);

test.describe('live disposable backend: passenger journey', () => {
  test.skip(!enabled, 'Set E2E_LIVE_BACKEND=1 and a loopback PLAYWRIGHT_BASE_URL to run against a local backend.');

  test('register → login → search a seeded future ride → request → pending → reload → cancel → cancelled', async ({ page, request }) => {
    test.setTimeout(90_000);
    const run = `${Date.now()}`.slice(-8);
    const password = 'A strong e2e password';
    // Unique places per run, so leftovers from earlier runs never match this search.
    const origin = `E2E Lahore ${run}`;
    const destination = `E2E Islamabad ${run}`;

    // Seed: a driver account publishes a ride departing tomorrow at 10:00 PKT, via the API.
    const driver = { name: 'E2E Driver', email: `e2e.driver.${run}@example.com`, cnic: `54321${run}`, phone_number: '03001234567', password, role: 'driver' };
    expect((await request.post('/auth/register', { data: driver })).status()).toBe(201);
    const driverLogin = await request.post('/auth/login', { data: { email: driver.email, password } });
    expect(driverLogin.status()).toBe(200);
    const driverAuth = { Authorization: `Bearer ${(await driverLogin.json()).access_token}` };
    const tomorrowPkt = new Date(Date.now() + 5 * 3_600_000 + 86_400_000).toISOString().slice(0, 10);
    const created = await request.post('/rides/', { headers: driverAuth, data: {
      origin, destination, departure_time: `${tomorrowPkt}T10:00:00+05:00`, available_seats: 2, fare_per_seat: 1800,
    } });
    expect(created.status()).toBe(201);
    const ride = await created.json();

    // Passenger: register and log in through the browser.
    const email = `e2e.passenger.${run}@example.com`;
    await page.goto('/register');
    await fillRegistration(page, { email, cnic: `12345${run}`, password });
    await expect(page).toHaveURL('/login');
    await fillLogin(page, email, password);
    await expect(page).toHaveURL('/passenger');

    // Search with all three filters.
    await page.goto('/passenger/search');
    await page.getByLabel('From', { exact: true }).fill(origin);
    await page.getByLabel('To', { exact: true }).fill(destination);
    await page.getByLabel('Date (PKT)').fill(tomorrowPkt);
    await page.getByRole('button', { name: 'Search rides' }).click();
    await expect(page.getByText('1 ride found.')).toBeVisible();
    const card = page.getByRole('article', { name: `${origin} to ${destination}` });
    await expect(card).toContainText('PKR 1,800');
    await expect(card).toContainText('2 seats left');
    await expect(card).toContainText('10:00 am PKT');

    // Details → request a seat → pending.
    await card.getByRole('link', { name: /^View details/ }).click();
    await expect(page).toHaveURL(`/passenger/rides/${ride.id}`);
    await page.getByRole('button', { name: 'Request a seat' }).click();
    await expect(page.getByText('Seat requested.')).toBeVisible();

    // The tab session survives a reload, and the page knows about the request.
    await page.reload();
    await expect(page).toHaveURL(`/passenger/rides/${ride.id}`);
    await expect(page.getByText('You’ve already requested a seat on this ride.', { exact: false })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Request a seat' })).toHaveCount(0);

    // My bookings → pending → cancel (confirm) → cancelled.
    await page.getByRole('link', { name: 'Manage in My bookings' }).click();
    await expect(page.getByRole('heading', { name: 'Pending (1)' })).toBeVisible();
    await page.getByRole('button', { name: new RegExp(`^Cancel booking: ${origin}`) }).click();
    await page.getByRole('alertdialog').getByRole('button', { name: 'Cancel booking' }).click();
    await expect(page.getByRole('status').filter({ hasText: 'Booking cancelled' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Cancelled (1)' })).toBeVisible();
    await expect(page.getByRole('heading', { name: /^Pending/ })).toHaveCount(0);

    // Server truth: the driver sees the booking as cancelled, and the seats were never consumed.
    const driverView = await request.get(`/rides/${ride.id}/bookings`, { headers: driverAuth });
    expect(driverView.status()).toBe(200);
    expect((await driverView.json()).map((b: { status: string }) => b.status)).toEqual(['cancelled']);
    expect((await (await request.get(`/rides/${ride.id}`, { headers: driverAuth })).json()).available_seats).toBe(2);
  });
});
