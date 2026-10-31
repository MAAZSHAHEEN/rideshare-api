import AxeBuilder from '@axe-core/playwright';
import type { Page, Route } from '@playwright/test';
import { apiOrigin, expect, fakeToken, fakeUser, fillLogin, noHorizontalOverflow, test } from './fixtures';

// Passenger journey (AST-023) against a stateful, in-browser mock of the
// existing API contract. No request leaves the browser; data is obviously fake.
const HOUR = 3_600_000;
const iso = (hoursFromNow: number) => new Date(Date.now() + hoursFromNow * HOUR).toISOString();
type Ride = { id: number; driver_id: number; origin: string; destination: string; departure_time: string; available_seats: number; fare_per_seat: number; status: string };
type Booking = { id: number; ride_id: number; passenger_id: number; status: string };
type Reply = { status: number; body: unknown; headers?: Record<string, string> } | 'abort';

class MockApi {
  rides: Ride[] = [
    { id: 7, driver_id: 9, origin: 'Lahore', destination: 'Islamabad', departure_time: iso(30), available_seats: 3, fare_per_seat: 2500, status: 'active' },
    { id: 8, driver_id: 9, origin: 'Lahore', destination: 'Islamabad', departure_time: iso(54), available_seats: 1, fare_per_seat: 2200, status: 'active' },
    { id: 9, driver_id: 9, origin: 'Lahore', destination: 'Islamabad', departure_time: iso(-2), available_seats: 2, fare_per_seat: 2000, status: 'active' },
  ];
  bookings: Booking[] = [];
  searches: URL[] = [];
  bookingReads: URL[] = [];
  idempotencyKeys: (string | undefined)[] = [];
  /** Per "METHOD /path" one-shot or persistent override. */
  overrides = new Map<string, Reply | (() => Promise<Reply>)>();
  meStatus = 200;
  onRequest?: (key: string, headers: Record<string, string>) => void;

  async install(page: Page) {
    await page.route(url => url.origin === new URL(apiOrigin).origin && /^\/(auth\/|me$|rides\/|bookings\/)/.test(url.pathname), route => this.handle(route));
  }

  private async handle(route: Route) {
    const request = route.request();
    const url = new URL(request.url());
    const key = `${request.method()} ${url.pathname}`;
    this.onRequest?.(key, request.headers());
    const override = this.overrides.get(key);
    if (override) {
      const reply = typeof override === 'function' ? await override() : override;
      if (reply === 'abort') return route.abort('internetdisconnected');
      return this.reply(route, reply.status, reply.body, reply.headers);
    }
    const auth = request.headers().authorization;
    if (key === 'POST /auth/login') return this.reply(route, 200, { access_token: fakeToken(), token_type: 'bearer' });
    if (!auth?.startsWith('Bearer ')) return this.reply(route, 401, { detail: 'Not authenticated' });
    if (key === 'GET /me') return this.reply(route, this.meStatus, this.meStatus === 200 ? fakeUser : { detail: 'Invalid token' });
    if (key === 'GET /rides/') {
      this.searches.push(url);
      const q = url.searchParams;
      const from = q.get('departure_from'), before = q.get('departure_before');
      // G3: trimmed, case-insensitive whole-value match.
      const place = (value: string, wanted: string | null) => !wanted || value.trim().toLowerCase() === wanted.trim().toLowerCase();
      const found = this.rides.filter(r => r.status === 'active' && r.available_seats >= 1 && Date.parse(r.departure_time) > Date.now()
        && place(r.origin, q.get('origin')) && place(r.destination, q.get('destination'))
        && (!from || Date.parse(r.departure_time) >= Date.parse(from)) && (!before || Date.parse(r.departure_time) < Date.parse(before)));
      return this.reply(route, 200, found.slice(Number(q.get('offset') ?? 0), Number(q.get('offset') ?? 0) + Number(q.get('limit') ?? 20)), { 'X-Total-Count': String(found.length) });
    }
    let match = /^\/rides\/(\d+)$/.exec(url.pathname);
    if (match && request.method() === 'GET') {
      const ride = this.rides.find(r => r.id === Number(match![1]));
      return ride ? this.reply(route, 200, ride) : this.reply(route, 404, { detail: 'Ride not found' });
    }
    if (key === 'GET /bookings/me') {
      this.bookingReads.push(url);
      const status = url.searchParams.get('status');
      const rideId = url.searchParams.get('ride_id');
      const rows = this.bookings.filter(b => (!status || b.status === status) && (!rideId || b.ride_id === Number(rideId))).slice().reverse()
        .map(b => ({ ...b, ride: this.rides.find(r => r.id === b.ride_id) }));
      return this.reply(route, 200, rows.slice(Number(url.searchParams.get('offset') ?? 0), Number(url.searchParams.get('offset') ?? 0) + Number(url.searchParams.get('limit') ?? 20)), { 'X-Total-Count': String(rows.length) });
    }
    match = /^\/bookings\/(\d+)$/.exec(url.pathname);
    if (match && request.method() === 'POST') {
      this.idempotencyKeys.push(request.headers()['idempotency-key']);
      const ride = this.rides.find(r => r.id === Number(match![1]));
      if (!ride) return this.reply(route, 404, { detail: 'Ride not found' });
      if (this.bookings.some(b => b.ride_id === ride.id && ['pending', 'accepted'].includes(b.status))) return this.reply(route, 409, { detail: 'You already booked this ride' });
      const booking = { id: 100 + this.bookings.length, ride_id: ride.id, passenger_id: fakeUser.id, status: 'pending' };
      this.bookings.push(booking);
      return this.reply(route, 201, booking);
    }
    match = /^\/bookings\/(\d+)\/cancel$/.exec(url.pathname);
    if (match && request.method() === 'PATCH') {
      const booking = this.bookings.find(b => b.id === Number(match![1]));
      if (!booking) return this.reply(route, 404, { detail: 'Booking not found' });
      if (!['pending', 'accepted'].includes(booking.status)) return this.reply(route, 409, { detail: 'Booking cannot be cancelled' });
      booking.status = 'cancelled';
      return this.reply(route, 200, booking);
    }
    throw new Error(`Unexpected API request: ${key}`);
  }

  private reply(route: Route, status: number, body: unknown, headers: Record<string, string> = {}) {
    return route.fulfill({ status, contentType: 'application/json', headers: { 'Access-Control-Allow-Origin': '*', 'Access-Control-Expose-Headers': 'Retry-After, X-Total-Count', ...headers }, body: JSON.stringify(body) });
  }
}

async function signIn(page: Page, api: MockApi) {
  await api.install(page);
  await page.goto('/login');
  await fillLogin(page);
  // Wait until the session is established (and saved for this tab) before any navigation.
  await expect(page).toHaveURL('/passenger');
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem('rideshare.session.v1'))).not.toBeNull();
}
const pktDate = (hoursFromNow: number) => new Date(Date.now() + hoursFromNow * HOUR + 5 * HOUR).toISOString().slice(0, 10);
const main = (page: Page) => page.getByRole('main');

test.describe('passenger journey (mocked API)', () => {
  test('search with filters → details → request seat → pending → cancel → cancelled', async ({ page }) => {
    const api = new MockApi();
    await signIn(page, api);
    await expect(page).toHaveURL('/passenger');
    await main(page).getByRole('link', { name: 'Find a ride' }).first().click();
    await expect(page).toHaveURL('/passenger/search');
    await expect(page.getByRole('heading', { name: 'Where are you heading?' })).toBeFocused();
    const day = pktDate(30);
    await page.getByLabel('From', { exact: true }).fill('Lahore');
    await page.getByLabel('To', { exact: true }).fill('Islamabad');
    await page.getByLabel('Date (PKT)').fill(day);
    await page.getByRole('button', { name: 'Search rides' }).click();
    await expect(page.getByText('1 ride found.')).toBeVisible();
    const search = api.searches.at(-1)!;
    expect(search.searchParams.get('origin')).toBe('Lahore');
    expect(search.searchParams.get('departure_from')).toBe(`${day}T00:00:00+05:00`);
    const card = page.getByRole('article', { name: 'Lahore to Islamabad' });
    await expect(card).toContainText('PKR 2,500');
    await expect(card).toContainText('3 seats left');
    await expect(card).toContainText(/\b\d{1,2}:\d{2} (am|pm) PKT/);
    await card.getByRole('link', { name: /^View details: Lahore to Islamabad/ }).click();
    await expect(page).toHaveURL('/passenger/rides/7');
    await page.getByRole('button', { name: 'Request a seat' }).click();
    await expect(page.getByRole('status').filter({ hasText: 'Seat requested.' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Request a seat' })).toHaveCount(0);
    await page.getByRole('link', { name: 'View my bookings' }).click();
    await expect(page).toHaveURL('/passenger/bookings');
    await expect(page.getByRole('heading', { name: 'Pending (1)' })).toBeVisible();
    await page.getByRole('button', { name: /^Cancel booking: Lahore to Islamabad/ }).click();
    const dialog = page.getByRole('alertdialog', { name: 'Cancel this booking?' });
    await expect(dialog.getByRole('button', { name: 'Keep booking' })).toBeFocused();
    await dialog.getByRole('button', { name: 'Cancel booking' }).click();
    await expect(page.getByRole('status').filter({ hasText: 'Booking cancelled' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Cancelled (1)' })).toBeVisible();
    await expect(page.getByRole('heading', { name: /Pending/ })).toHaveCount(0);
    expect(api.bookings).toEqual([expect.objectContaining({ ride_id: 7, status: 'cancelled' })]);
  });

  test('search shows loading, empty and error-with-retry states', async ({ page }) => {
    const api = new MockApi();
    let release!: () => void;
    const gate = new Promise<void>(resolve => { release = resolve; });
    api.overrides.set('GET /rides/', async () => { await gate; return { status: 503, body: { detail: 'asyncpg pool exhausted' } }; });
    await signIn(page, api);
    await page.goto('/passenger/search');
    await expect(page.getByText('Looking for rides…')).toBeVisible();
    release();
    await expect(page.getByRole('heading', { name: 'We couldn’t load rides.' })).toBeVisible();
    await expect(page.getByRole('alert')).toHaveText('The service is temporarily unavailable. Please try again later.');
    await expect(page.getByText(/asyncpg/)).toHaveCount(0);
    api.overrides.set('GET /rides/', { status: 200, body: [] });
    await page.getByRole('button', { name: 'Try again' }).click();
    await expect(page.getByRole('heading', { name: 'No rides match yet.' })).toBeVisible();
  });

  test('client-side filter validation blocks past dates and identical places', async ({ page }) => {
    const api = new MockApi();
    await signIn(page, api);
    await page.goto('/passenger/search');
    await expect(page.getByText(/rides? found/)).toBeVisible();
    const before = api.searches.length;
    await page.getByLabel('From', { exact: true }).fill('Lahore');
    await page.getByLabel('To', { exact: true }).fill('LAHORE');
    await page.getByLabel('Date (PKT)').fill('2020-01-01');
    await page.getByRole('button', { name: 'Search rides' }).click();
    await expect(page.getByRole('alert')).toHaveText('Please check the highlighted search filters.');
    await expect(page.getByLabel('To', { exact: true })).toHaveAttribute('aria-invalid', 'true');
    await expect(page.getByLabel('Date (PKT)')).toHaveAttribute('aria-invalid', 'true');
    expect(api.searches.length).toBe(before);
  });

  const failures: [string, Reply, RegExp][] = [
    ['403 own ride', { status: 403, body: { detail: 'You cannot book your own ride' } }, /ride you are driving/],
    ['404 removed', { status: 404, body: { detail: 'Ride not found' } }, /couldn’t find this ride/],
    ['409 full', { status: 409, body: { detail: 'No seats available' } }, /This ride is full/],
    ['409 departed', { status: 409, body: { detail: 'Ride has already departed' } }, /already departed/],
    ['409 duplicate', { status: 409, body: { detail: 'You already booked this ride' } }, /already requested a seat/],
    ['409 inactive', { status: 409, body: { detail: 'Ride is not active' } }, /no longer taking requests/],
    ['422', { status: 422, body: { detail: [{ loc: ['path', 'ride_id'], msg: 'Input should be a valid integer' }] } }, /weren’t accepted/],
    ['429 Retry-After', { status: 429, body: { detail: 'Too many requests' }, headers: { 'Retry-After': '30' } }, /wait 30 seconds/],
    ['500', { status: 500, body: { detail: 'Traceback: sqlalchemy.exc.InternalError' } }, /temporarily unavailable/],
    ['network', 'abort', /Check your connection/],
  ];
  for (const [name, reply, message] of failures) {
    test(`request a seat: ${name} shows a specific, safe message`, async ({ page }) => {
      const api = new MockApi();
      api.overrides.set('POST /bookings/7', reply);
      await signIn(page, api);
      await page.goto('/passenger/rides/7');
      await page.getByRole('button', { name: 'Request a seat' }).click();
      const alert = page.getByRole('alert');
      await expect(alert).toHaveText(message);
      await expect(alert).toBeFocused();
      await expect(page.getByText(/Traceback|sqlalchemy|constraint/)).toHaveCount(0);
    });
  }

  test('401 while booking returns to sign-in, then back to the same ride', async ({ page }) => {
    const api = new MockApi();
    api.overrides.set('POST /bookings/7', { status: 401, body: { detail: 'Invalid token' } });
    await signIn(page, api);
    await page.goto('/passenger/rides/7');
    await page.getByRole('button', { name: 'Request a seat' }).click();
    await expect(page).toHaveURL('/login');
    await expect(page.getByRole('status')).toHaveText('Your session has ended. Please sign in again.');
    expect(await page.evaluate(() => sessionStorage.length)).toBe(0);
    api.overrides.delete('POST /bookings/7');
    await fillLogin(page);
    await expect(page).toHaveURL('/passenger/rides/7');
    await page.getByRole('button', { name: 'Request a seat' }).click();
    await expect(page.getByText('Seat requested.')).toBeVisible();
  });

  test('signed-out deep link to a ride signs in and returns there', async ({ page }) => {
    const api = new MockApi();
    await api.install(page);
    await page.goto('/passenger/rides/8');
    await expect(page).toHaveURL('/login');
    await fillLogin(page);
    await expect(page).toHaveURL('/passenger/rides/8');
    await expect(page.getByText('1 seat left').first()).toBeVisible();
  });

  test('a departed ride is labelled and cannot be booked; a missing ride shows not found', async ({ page }) => {
    const api = new MockApi();
    await signIn(page, api);
    await page.goto('/passenger/rides/9');
    await expect(page.getByText('Departed', { exact: true })).toBeVisible();
    await expect(page.getByText('Not available to book.')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Request a seat' })).toHaveCount(0);
    await page.goto('/passenger/rides/404');
    await expect(page.getByRole('heading', { name: 'Ride not found' })).toBeVisible();
  });

  test('my bookings: grouped statuses, departed label, cancel conflict and keyboard dialog', async ({ page }) => {
    const api = new MockApi();
    api.bookings = [
      { id: 1, ride_id: 7, passenger_id: 42, status: 'accepted' },
      { id: 2, ride_id: 8, passenger_id: 42, status: 'rejected' },
      { id: 3, ride_id: 9, passenger_id: 42, status: 'pending' },
      { id: 4, ride_id: 8, passenger_id: 42, status: 'cancelled' },
    ];
    api.overrides.set('PATCH /bookings/1/cancel', { status: 409, body: { detail: 'Ride is not active' } });
    await signIn(page, api);
    await page.goto('/passenger/bookings');
    await expect(page.getByRole('heading', { level: 3 })).toHaveText(['Pending (1)', 'Accepted (1)', 'Rejected (1)', 'Cancelled (1)']);
    await expect(page.locator('.booking-group').first().getByText('Departed')).toBeVisible();
    const accepted = page.locator('.booking-group').nth(1);
    const trigger = accepted.getByRole('button', { name: /^Cancel booking/ });
    await trigger.focus();
    await page.keyboard.press('Enter');
    const dialog = page.getByRole('alertdialog');
    await expect(dialog).toContainText('Your confirmed seat will be released');
    await page.keyboard.press('Escape');
    await expect(dialog).toHaveCount(0);
    await expect(trigger).toBeFocused();
    await trigger.click();
    await dialog.getByRole('button', { name: 'Cancel booking' }).click();
    await expect(dialog.getByRole('alert')).toHaveText('The ride is no longer active, so this booking can’t be cancelled.');
    await dialog.getByRole('button', { name: 'Keep booking' }).click();
    // Rejected and cancelled bookings never offer cancel.
    await expect(page.locator('.booking-group').nth(2).getByRole('button', { name: /^Cancel booking/ })).toHaveCount(0);
    await expect(page.locator('.booking-group').nth(3).getByRole('button', { name: /^Cancel booking/ })).toHaveCount(0);
    await page.getByRole('button', { name: /^Rejected/ }).click();
    await expect(page).toHaveURL('/passenger/bookings?status=rejected');
    await expect(page.getByRole('article')).toHaveCount(1);
  });

  test('session survives a reload on a passenger view; logout clears it', async ({ page }) => {
    const api = new MockApi();
    await signIn(page, api);
    await page.goto('/passenger/search?from=Lahore');
    await expect(page.getByLabel('From', { exact: true })).toHaveValue('Lahore');
    await page.reload();
    await expect(page.getByRole('heading', { name: 'Where are you heading?' })).toBeFocused();
    await expect(page).toHaveURL('/passenger/search?from=Lahore');
    await expect(page.getByLabel('From', { exact: true })).toHaveValue('Lahore');
    await page.getByRole('navigation', { name: 'Main navigation' }).getByRole('button', { name: 'Log out' }).click();
    await expect(page).toHaveURL('/login');
    // A deliberate logout keeps no return path: signing in again lands on the dashboard.
    await fillLogin(page);
    await expect(page).toHaveURL('/passenger');
    await page.getByRole('navigation', { name: 'Main navigation' }).getByRole('button', { name: 'Log out' }).click();
    await page.goto('/passenger/search');
    await expect(page).toHaveURL('/login');
  });

  test('search totals (G7) and case-insensitive places (G3): "LAHORE " finds "Lahore", Page X of Y', async ({ page }) => {
    const api = new MockApi();
    for (let i = 0; i < 22; i += 1) api.rides.push({ id: 300 + i, driver_id: 9, origin: 'Lahore', destination: 'Islamabad', departure_time: iso(60 + i), available_seats: 2, fare_per_seat: 1500, status: 'active' });
    // Another route, so the unfiltered total (25) differs from the filtered one (24).
    api.rides.push({ id: 400, driver_id: 9, origin: 'Multan', destination: 'Lahore', departure_time: iso(70), available_seats: 2, fare_per_seat: 1500, status: 'active' });
    await signIn(page, api);
    await page.goto('/passenger/search');
    await expect(page.getByText('25 rides found. Showing 1–20.')).toBeVisible();
    await page.getByLabel('From', { exact: true }).fill('LAHORE ');
    await page.getByLabel('To', { exact: true }).fill('islamabad');
    await page.getByRole('button', { name: 'Search rides' }).click();
    await expect(page.getByText('24 rides found. Showing 1–20.')).toBeVisible();
    expect(api.searches.at(-1)!.searchParams.get('origin')).toBe('LAHORE');
    expect(api.searches.at(-1)!.searchParams.get('destination')).toBe('islamabad');
    await expect(page.getByText('Page 1 of 2')).toBeVisible();
    await page.getByRole('button', { name: 'Next →' }).click();
    await expect(page).toHaveURL('/passenger/search?from=LAHORE&to=islamabad&page=1');
    await expect(page.getByText('24 rides found. Showing 21–24.')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Next →' })).toBeDisabled();
    await page.reload();
    await expect(page.getByText('24 rides found. Showing 21–24.')).toBeVisible();
    await expect(page.getByLabel('From', { exact: true })).toHaveValue('LAHORE');
  });

  test('ride details finds my booking with GET /bookings/me?ride_id= (G2), even beyond the newest 100', async ({ page }) => {
    const api = new MockApi();
    api.bookings = [{ id: 1, ride_id: 8, passenger_id: 42, status: 'pending' }];
    for (let i = 0; i < 120; i += 1) api.bookings.push({ id: 10 + i, ride_id: 7, passenger_id: 42, status: 'cancelled' });
    await signIn(page, api);
    await page.goto('/passenger/rides/8');
    await expect(page.getByText('You’ve already requested a seat on this ride.')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Request a seat' })).toHaveCount(0);
    const lookup = api.bookingReads.find(u => u.searchParams.get('ride_id') === '8')!;
    expect(Object.fromEntries(lookup.searchParams)).toEqual({ limit: '5', offset: '0', ride_id: '8' });
    expect(api.bookingReads.some(u => u.searchParams.get('limit') === '100')).toBe(false);
  });

  test('my bookings: tab counts and pagination use X-Total-Count; expired renders read-only', async ({ page }) => {
    const api = new MockApi();
    api.bookings = Array.from({ length: 23 }, (_, i) => ({ id: 1 + i, ride_id: 7, passenger_id: 42, status: i === 0 ? 'expired' : 'rejected' }));
    await signIn(page, api);
    await page.goto('/passenger/bookings');
    await expect(page.getByRole('button', { name: 'All (23)' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Rejected (22)' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Expired (1)' })).toBeVisible();
    await expect(page.getByText('Page 1 of 2')).toBeVisible();
    await expect(page.getByText(/Showing 1–20 of 23 bookings\./)).toBeVisible();
    await page.getByRole('button', { name: 'Next →' }).click();
    await expect(page).toHaveURL('/passenger/bookings?page=1');
    await expect(page.getByRole('article')).toHaveCount(3);
    await expect(page.getByRole('heading', { name: 'Expired (1)' })).toBeVisible();
    await expect(page.locator('.booking-group').filter({ hasText: 'Expired' }).getByRole('button', { name: /^Cancel/ })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Next →' })).toBeDisabled();
  });

  test('request a seat sends an Idempotency-Key only same-origin; a network retry reuses it', async ({ page, baseURL }) => {
    const api = new MockApi();
    const keys: (string | undefined)[] = [];
    let first = true;
    api.overrides.set('POST /bookings/7', async () => {
      if (first) { first = false; return 'abort'; }
      return { status: 201, body: { id: 500, ride_id: 7, passenger_id: 42, status: 'pending' } };
    });
    api.onRequest = (key, headers) => { if (key === 'POST /bookings/7') keys.push(headers['idempotency-key']); };
    await signIn(page, api);
    await page.goto('/passenger/rides/7');
    await page.getByRole('button', { name: 'Request a seat' }).click();
    await expect(page.getByRole('alert')).toHaveText(/Check your connection/);
    await page.getByRole('button', { name: 'Request a seat' }).click();
    await expect(page.getByText('Seat requested.')).toBeVisible();
    const sameOrigin = new URL(baseURL!).origin === new URL(apiOrigin).origin;
    expect(keys).toHaveLength(2);
    if (sameOrigin) { expect(keys[0]).toMatch(/^[0-9a-f-]{36}$/); expect(keys[1]).toBe(keys[0]); }
    else expect(keys).toEqual([undefined, undefined]);
  });

  test('old query links redirect to the path routes', async ({ page }) => {
    const api = new MockApi();
    await signIn(page, api);
    for (const [from, to] of [
      ['/passenger?view=search&from=Lahore', '/passenger/search?from=Lahore'],
      ['/passenger?ride=7', '/passenger/rides/7'],
      ['/passenger?view=bookings&status=pending', '/passenger/bookings?status=pending'],
    ]) {
      await page.goto(from);
      await expect(page).toHaveURL(to);
      await expect(page.locator('h1')).toBeVisible();
    }
  });

  test('refresh and deep link work on every passenger route', async ({ page }) => {
    const api = new MockApi();
    api.bookings = [{ id: 1, ride_id: 7, passenger_id: 42, status: 'pending' }];
    await signIn(page, api);
    for (const [path, heading] of [
      ['/passenger', /^Welcome/], ['/passenger/search?from=Lahore', 'Where are you heading?'], ['/passenger/rides/7', 'Lahore to Islamabad'],
      ['/passenger/bookings', 'Your seat requests'], ['/passenger/bookings?status=pending', 'Your seat requests'],
    ] as const) {
      await page.goto(path);
      await page.reload();
      await expect(page).toHaveURL(path);
      await expect(page.getByRole('heading', { level: 1, name: heading })).toBeVisible();
    }
    // A signed-out deep link signs in and returns to the path route.
    await page.getByRole('navigation', { name: 'Main navigation' }).getByRole('button', { name: 'Log out' }).click();
    await page.goto('/passenger/rides/8');
    await expect(page).toHaveURL('/login');
    await fillLogin(page);
    await expect(page).toHaveURL('/passenger/rides/8');
  });

  test('a saved session the server rejects on reload signs out cleanly', async ({ page }) => {
    const api = new MockApi();
    await signIn(page, api);
    await expect(page).toHaveURL('/passenger');
    api.meStatus = 401;
    await page.reload();
    await expect(page).toHaveURL('/login');
    await expect(page.getByRole('status')).toHaveText('Your session has ended. Please sign in again.');
    expect(await page.evaluate(() => sessionStorage.length)).toBe(0);
  });
});

test.describe('passenger journey at 390px', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  test('search, details, bookings and the dialog fit without horizontal scrolling', async ({ page }) => {
    const api = new MockApi();
    api.bookings = [{ id: 1, ride_id: 7, passenger_id: 42, status: 'pending' }];
    await signIn(page, api);
    for (const path of ['/passenger/search', '/passenger/rides/7', '/passenger/bookings']) {
      await page.goto(path);
      await expect(page.locator('h1')).toBeVisible();
      await page.waitForLoadState('networkidle');
      expect(await noHorizontalOverflow(page), path).toBe(true);
    }
    await page.getByRole('button', { name: /^Cancel booking/ }).click();
    await expect(page.getByRole('alertdialog')).toBeVisible();
    expect(await noHorizontalOverflow(page)).toBe(true);
    const box = await page.getByRole('button', { name: 'Keep booking' }).boundingBox();
    expect(box!.height).toBeGreaterThanOrEqual(44);
  });
});

test.describe('passenger accessibility (axe)', () => {
  for (const [name, path] of [['search', '/passenger/search'], ['details', '/passenger/rides/7'], ['bookings', '/passenger/bookings']] as const) {
    test(`${name} has no serious accessibility violations`, async ({ page }, testInfo) => {
      const api = new MockApi();
      api.bookings = [{ id: 1, ride_id: 7, passenger_id: 42, status: 'pending' }, { id: 2, ride_id: 8, passenger_id: 42, status: 'rejected' }, { id: 3, ride_id: 8, passenger_id: 42, status: 'expired' }];
      await signIn(page, api);
      await page.goto(path);
      await expect(page.locator('h1')).toBeVisible();
      await page.waitForLoadState('networkidle');
      await page.evaluate(() => document.fonts.ready.then(() => undefined));
      const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']).analyze();
      const summary = results.violations.map(v => ({ id: v.id, impact: v.impact, nodes: v.nodes.map(n => n.target.join(' ')) }));
      await testInfo.attach(`axe-passenger-${name}.json`, { body: JSON.stringify(summary, null, 2), contentType: 'application/json' });
      expect(summary.filter(v => v.impact === 'serious' || v.impact === 'critical')).toEqual([]);
    });
  }
});

// Screenshots for review: opt-in with PASSENGER_SCREENS_DIR=<absolute dir>.
const screensDir = process.env.PASSENGER_SCREENS_DIR;
test.describe('passenger screenshots', () => {
  test.skip(!screensDir, 'Set PASSENGER_SCREENS_DIR to capture review screenshots.');
  for (const [device, viewport] of [['mobile', { width: 390, height: 844 }], ['desktop', { width: 1440, height: 1000 }]] as const) {
    test(`key screens (${device})`, async ({ page }) => {
      test.setTimeout(60_000);
      const api = new MockApi();
      api.bookings = [
        { id: 1, ride_id: 8, passenger_id: 42, status: 'accepted' },
        { id: 3, ride_id: 8, passenger_id: 42, status: 'rejected' }, { id: 4, ride_id: 7, passenger_id: 42, status: 'cancelled' },
      ];
      await page.setViewportSize(viewport);
      await signIn(page, api);
      const shot = async (name: string) => {
        await page.waitForLoadState('networkidle');
        await page.evaluate(() => document.fonts.ready.then(() => undefined));
        await page.screenshot({ path: `${screensDir}/${device}-${name}.png`, fullPage: true });
      };
      await page.goto(`/passenger/search?from=Lahore&to=Islamabad`);
      await expect(page.getByText(/rides? found/)).toBeVisible();
      await shot('01-search-results');
      await page.goto('/passenger/search?from=Quetta');
      await expect(page.getByText('No rides match yet.')).toBeVisible();
      await shot('02-search-empty');
      await page.goto('/passenger/rides/7');
      await expect(page.getByRole('button', { name: 'Request a seat' })).toBeVisible();
      await shot('03-ride-details');
      await page.getByRole('button', { name: 'Request a seat' }).click();
      await expect(page.getByText('Seat requested.')).toBeVisible();
      await shot('04-seat-requested');
      await page.goto('/passenger/rides/9');
      await expect(page.getByText('Not available to book.')).toBeVisible();
      await shot('05-ride-departed');
      api.rides.push({ id: 10, driver_id: 9, origin: 'Islamabad', destination: 'Lahore', departure_time: iso(40), available_seats: 1, fare_per_seat: 2400, status: 'active' });
      api.overrides.set('POST /bookings/10', { status: 409, body: { detail: 'No seats available' } });
      await page.goto('/passenger/rides/10');
      await page.getByRole('button', { name: 'Request a seat' }).click();
      await expect(page.getByRole('alert')).toBeVisible();
      await shot('06-booking-409-full');
      api.bookings.push({ id: 2, ride_id: 9, passenger_id: 42, status: 'pending' });
      await page.goto('/passenger/bookings');
      await expect(page.getByRole('heading', { name: /Pending/ })).toBeVisible();
      await shot('07-my-bookings');
      await page.getByRole('button', { name: /^Cancel booking/ }).first().click();
      await expect(page.getByRole('alertdialog')).toBeVisible();
      await page.screenshot({ path: `${screensDir}/${device}-08-cancel-dialog.png` });
    });
  }
});
