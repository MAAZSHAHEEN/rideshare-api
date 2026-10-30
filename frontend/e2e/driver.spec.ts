import AxeBuilder from '@axe-core/playwright';
import type { Page, Route, TestInfo } from '@playwright/test';
import { apiOrigin, expect, fakeToken, fillLogin, noHorizontalOverflow, test } from './fixtures';

// Driver journey (M3) against a stateful, in-browser mock of the existing API
// contract (services/rides.py + services/bookings.py rules). No request leaves
// the browser; identities are obviously fake.
const HOUR = 3_600_000;
const iso = (hoursFromNow: number) => new Date(Date.now() + hoursFromNow * HOUR).toISOString();
const pktLocal = (hoursFromNow: number) => new Date(Date.now() + hoursFromNow * HOUR + 5 * HOUR).toISOString().slice(0, 16);
const driverUser = { id: 42, name: 'Test Driver', email: 'test.driver@example.com', role: 'driver', allowed_modes: ['passenger', 'driver'] };
type Ride = { id: number; driver_id: number; origin: string; destination: string; departure_time: string; available_seats: number; fare_per_seat: number; status: string };
type Booking = { id: number; ride_id: number; passenger_id: number; status: string; passenger: { id: number; name: string } };
type Reply = { status: number; body: unknown; headers?: Record<string, string> } | 'abort';

class MockDriverApi {
  user: Record<string, unknown> = driverUser;
  rides: Ride[] = [
    { id: 7, driver_id: 42, origin: 'Lahore', destination: 'Islamabad', departure_time: iso(30), available_seats: 2, fare_per_seat: 2500, status: 'active' },
    { id: 8, driver_id: 42, origin: 'Islamabad', destination: 'Lahore', departure_time: iso(-3), available_seats: 1, fare_per_seat: 2200, status: 'active' },
    { id: 9, driver_id: 42, origin: 'Lahore', destination: 'Rawalpindi', departure_time: iso(-50), available_seats: 0, fare_per_seat: 2000, status: 'completed' },
    { id: 10, driver_id: 42, origin: 'Rawalpindi', destination: 'Lahore', departure_time: iso(20), available_seats: 3, fare_per_seat: 1900, status: 'cancelled' },
    { id: 11, driver_id: 99, origin: 'Multan', destination: 'Lahore', departure_time: iso(40), available_seats: 3, fare_per_seat: 1500, status: 'active' },
  ];
  bookings: Booking[] = [
    { id: 70, ride_id: 7, passenger_id: 101, status: 'pending', passenger: { id: 101, name: 'Asha Test' } },
    { id: 71, ride_id: 7, passenger_id: 102, status: 'pending', passenger: { id: 102, name: 'Bilal Test' } },
    { id: 72, ride_id: 7, passenger_id: 103, status: 'cancelled', passenger: { id: 103, name: 'Chand Test' } },
    { id: 80, ride_id: 8, passenger_id: 104, status: 'pending', passenger: { id: 104, name: 'Dua Test' } },
  ];
  created: unknown[] = [];
  reads: string[] = [];
  overrides = new Map<string, Reply | (() => Promise<Reply>)>();

  async install(page: Page) {
    await page.route(url => url.origin === new URL(apiOrigin).origin && /^\/(auth\/|me$|rides\/|bookings\/)/.test(url.pathname), route => this.handle(route));
  }

  private async handle(route: Route) {
    const request = route.request();
    const url = new URL(request.url());
    const key = `${request.method()} ${url.pathname}`;
    if (request.method() === 'GET') this.reads.push(url.pathname);
    const override = this.overrides.get(key);
    if (override) {
      const reply = typeof override === 'function' ? await override() : override;
      if (reply === 'abort') return route.abort('internetdisconnected');
      return this.reply(route, reply.status, reply.body, reply.headers);
    }
    if (key === 'POST /auth/login') return this.reply(route, 200, { access_token: fakeToken(), token_type: 'bearer' });
    if (!request.headers().authorization?.startsWith('Bearer ')) return this.reply(route, 401, { detail: 'Not authenticated' });
    if (key === 'GET /me') return this.reply(route, 200, this.user);
    if (key === 'GET /rides/me') {
      const status = url.searchParams.get('status');
      const rows = this.rides.filter(r => r.driver_id === 42 && (!status || r.status === status)).sort((a, b) => Date.parse(b.departure_time) - Date.parse(a.departure_time));
      const offset = Number(url.searchParams.get('offset') ?? 0);
      return this.reply(route, 200, rows.slice(offset, offset + Number(url.searchParams.get('limit') ?? 20)));
    }
    if (key === 'POST /rides/') {
      const body = request.postDataJSON();
      this.created.push(body);
      const ride = { id: 100 + this.created.length, driver_id: 42, ...body, departure_time: new Date(body.departure_time).toISOString(), status: 'active' };
      this.rides.push(ride);
      return this.reply(route, 201, ride);
    }
    let match = /^\/rides\/(\d+)(\/bookings|\/cancel|\/complete)?$/.exec(url.pathname);
    if (match) {
      const ride = this.rides.find(r => r.id === Number(match![1]));
      if (!ride) return this.reply(route, 404, { detail: 'Ride not found' });
      if (!match[2] && request.method() === 'GET') return this.reply(route, 200, ride);
      if (ride.driver_id !== 42) return this.reply(route, 403, { detail: 'This is not your ride' });
      if (match[2] === '/bookings') return this.reply(route, 200, this.bookings.filter(b => b.ride_id === ride.id).slice().reverse());
      if (ride.status !== 'active') return this.reply(route, 409, { detail: 'Ride is not active' });
      for (const b of this.bookings.filter(b => b.ride_id === ride.id)) {
        if (match[2] === '/cancel' && (b.status === 'pending' || b.status === 'accepted')) { if (b.status === 'accepted') ride.available_seats += 1; b.status = 'cancelled'; }
        if (match[2] === '/complete' && b.status === 'pending') b.status = 'cancelled';
      }
      ride.status = match[2] === '/cancel' ? 'cancelled' : 'completed';
      return this.reply(route, 200, ride);
    }
    match = /^\/bookings\/(\d+)\/respond$/.exec(url.pathname);
    if (match && request.method() === 'PATCH') {
      const booking = this.bookings.find(b => b.id === Number(match![1]));
      if (!booking) return this.reply(route, 404, { detail: 'Booking not found' });
      const ride = this.rides.find(r => r.id === booking.ride_id)!;
      if (booking.status !== 'pending') return this.reply(route, 409, { detail: 'Booking already responded to' });
      if (ride.status !== 'active') return this.reply(route, 409, { detail: 'Ride is not active' });
      if (url.searchParams.get('accept') === 'true') {
        if (Date.parse(ride.departure_time) <= Date.now()) return this.reply(route, 409, { detail: 'Ride has already departed' });
        if (ride.available_seats < 1) return this.reply(route, 409, { detail: 'No seats available' });
        booking.status = 'accepted'; ride.available_seats -= 1;
      } else booking.status = 'rejected';
      const { passenger: _, ...plain } = booking;
      return this.reply(route, 200, plain);
    }
    throw new Error(`Unexpected API request: ${key}`);
  }

  private reply(route: Route, status: number, body: unknown, headers: Record<string, string> = {}) {
    return route.fulfill({ status, contentType: 'application/json', headers: { 'Access-Control-Allow-Origin': '*', 'Access-Control-Expose-Headers': 'Retry-After', ...headers }, body: JSON.stringify(body) });
  }
}

async function signIn(page: Page, api: MockDriverApi) {
  await api.install(page);
  await page.goto('/login');
  await fillLogin(page, driverUser.email);
  await expect(page).toHaveURL('/driver');
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem('rideshare.session.v1'))).not.toBeNull();
}
const requestCard = (page: Page, name: string) => page.getByRole('article', { name });
const settle = async (page: Page) => { await page.waitForLoadState('networkidle'); await page.evaluate(() => document.fonts.ready.then(() => undefined)); };

test.describe('driver journey (mocked API)', () => {
  test('dashboard → publish → ride page → accept (seats drop) → reject → my rides', async ({ page }) => {
    const api = new MockDriverApi();
    await signIn(page, api);
    await expect(page.getByText('Driver documents not verified')).toBeVisible();
    await page.getByRole('main').getByRole('link', { name: 'Publish a ride' }).first().click();
    await expect(page).toHaveURL('/driver/rides/new');
    await expect(page.getByRole('heading', { name: 'Offer seats on a scheduled ride' })).toBeFocused();
    const departure = pktLocal(26);
    await page.getByLabel('From').fill('Lahore');
    await page.getByLabel('To').fill('Islamabad');
    await page.getByLabel('Departure (PKT)').fill(departure);
    await page.getByLabel('Seats offered').fill('2');
    await page.getByLabel('Fare per seat (PKR)').fill('1800');
    await expect(page.getByText(/^Departs .* PKT\.$/)).toBeVisible();
    await page.getByRole('button', { name: 'Publish ride' }).click();
    await expect(page).toHaveURL('/driver/rides/101');
    expect(api.created).toEqual([{ origin: 'Lahore', destination: 'Islamabad', departure_time: `${departure}:00+05:00`, available_seats: 2, fare_per_seat: 1800 }]);
    await expect(page.getByText('Ride published.')).toBeVisible();
    await expect(page.getByRole('heading', { name: 'No requests yet.' })).toBeVisible();

    // A ride with incoming requests: accept one, reject another.
    await page.goto('/driver/rides/7');
    await expect(page.getByText('2 pending · 0 accepted')).toBeVisible();
    await expect(page.getByText('2 seats left')).toBeVisible();
    await requestCard(page, 'Asha Test').getByRole('button', { name: 'Accept request from Asha Test' }).click();
    await expect(page.getByRole('status').filter({ hasText: 'Accepted Asha Test’s request.' })).toBeVisible();
    await expect(page.getByText('1 seat left')).toBeVisible();
    await expect(page.getByText('1 pending · 1 accepted')).toBeVisible();
    await requestCard(page, 'Bilal Test').getByRole('button', { name: 'Reject request from Bilal Test' }).click();
    const dialog = page.getByRole('alertdialog', { name: 'Reject this request?' });
    await expect(dialog.getByRole('button', { name: 'Keep request' })).toBeFocused();
    await dialog.getByRole('button', { name: 'Reject request' }).click();
    await expect(page.getByRole('status').filter({ hasText: 'Rejected Bilal Test’s request.' })).toBeVisible();
    await expect(page.getByRole('heading', { level: 3 })).toHaveText(['Accepted (1)', 'Rejected (1)', 'Cancelled (1)']);
    expect(api.bookings.filter(b => b.ride_id === 7).map(b => b.status)).toEqual(['accepted', 'rejected', 'cancelled']);
    expect(api.rides.find(r => r.id === 7)!.available_seats).toBe(1);

    // My rides lists the new ride under Upcoming.
    await page.getByRole('navigation', { name: 'Dashboard navigation' }).getByRole('link', { name: 'My rides' }).click();
    await expect(page).toHaveURL('/driver/rides');
    await page.getByRole('button', { name: 'Upcoming', exact: true }).click();
    await expect(page).toHaveURL('/driver/rides?filter=upcoming');
    await expect(page.getByRole('article')).toHaveCount(2);
  });

  test('ride form validation blocks bad input without calling the API', async ({ page }) => {
    const api = new MockDriverApi();
    await signIn(page, api);
    await page.goto('/driver/rides/new');
    await page.getByLabel('From').fill('Lahore');
    await page.getByLabel('To').fill('LAHORE');
    await page.getByLabel('Departure (PKT)').fill(pktLocal(-2));
    await page.getByLabel('Seats offered').fill('0');
    await page.getByLabel('Fare per seat (PKR)').fill('250000');
    await page.getByRole('button', { name: 'Publish ride' }).click();
    await expect(page.getByRole('alert')).toHaveText('Please fix the highlighted ride details.');
    await expect(page.getByRole('alert')).toBeFocused();
    for (const label of ['To', 'Departure (PKT)', 'Seats offered', 'Fare per seat (PKR)']) await expect(page.getByLabel(label)).toHaveAttribute('aria-invalid', 'true');
    expect(api.created).toEqual([]);
  });

  test('server 422 on publish is mapped to the field, never shown raw', async ({ page }) => {
    const api = new MockDriverApi();
    api.overrides.set('POST /rides/', { status: 422, body: { detail: [{ loc: ['body', 'fare_per_seat'], msg: 'Input should be less than or equal to 100000 <b>raw</b>' }] } });
    await signIn(page, api);
    await page.goto('/driver/rides/new');
    await page.getByLabel('From').fill('Lahore');
    await page.getByLabel('To').fill('Islamabad');
    await page.getByLabel('Departure (PKT)').fill(pktLocal(10));
    await page.getByLabel('Fare per seat (PKR)').fill('2000');
    await page.getByRole('button', { name: 'Publish ride' }).click();
    await expect(page.getByRole('alert')).toHaveText('Some ride details weren’t accepted. Check the highlighted fields.');
    await expect(page.getByLabel('Fare per seat (PKR)')).toHaveAttribute('aria-invalid', 'true');
    await expect(page.getByText(/raw|Input should/)).toHaveCount(0);
  });

  const conflicts: [string, string, RegExp][] = [
    ['already responded', 'Booking already responded to', /already answered, or the passenger withdrew it/],
    ['insufficient seats', 'No seats available', /No seats are left on this ride/],
    ['departed', 'Ride has already departed', /already departed, so requests can no longer be accepted/],
    ['concurrent conflict', 'could not serialize access', /changed at the same time/],
  ];
  for (const [name, detail, message] of conflicts) {
    test(`accept 409 (${name}) shows a specific message and re-fetches state`, async ({ page }) => {
      const api = new MockDriverApi();
      api.overrides.set('PATCH /bookings/70/respond', { status: 409, body: { detail } });
      await signIn(page, api);
      await page.goto('/driver/rides/7');
      await expect(page.getByText('2 pending · 0 accepted')).toBeVisible();
      const before = api.reads.filter(p => p === '/rides/7/bookings').length;
      await page.getByRole('button', { name: 'Accept request from Asha Test' }).click();
      const alert = page.getByRole('alert');
      await expect(alert).toHaveText(message);
      await expect(alert).toBeFocused();
      await expect.poll(() => api.reads.filter(p => p === '/rides/7/bookings').length).toBeGreaterThan(before);
      await expect(page.getByText(/serialize/)).toHaveCount(0);
    });
  }

  test('real race: another tab answered first → 409, refreshed list shows the truth', async ({ page }) => {
    const api = new MockDriverApi();
    await signIn(page, api);
    await page.goto('/driver/rides/7');
    await expect(page.getByRole('button', { name: 'Accept request from Asha Test' })).toBeVisible();
    api.bookings.find(b => b.id === 70)!.status = 'rejected';
    await page.getByRole('button', { name: 'Accept request from Asha Test' }).click();
    await expect(page.getByRole('alert')).toContainText('already answered');
    await expect(page.getByRole('heading', { name: 'Rejected (1)' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Accept request from Asha Test' })).toHaveCount(0);
  });

  test('departed ride: no accept, reject allowed, complete with confirm', async ({ page }) => {
    const api = new MockDriverApi();
    await signIn(page, api);
    await page.goto('/driver/rides/8');
    await expect(page.getByText('Accepting is paused.')).toBeVisible();
    await expect(page.getByRole('button', { name: /^Accept request/ })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Reject request from Dua Test' })).toBeEnabled();
    await page.getByRole('button', { name: 'Mark ride completed' }).click();
    const dialog = page.getByRole('alertdialog', { name: 'Mark this ride completed?' });
    await expect(dialog).toContainText('1 pending request will be cancelled.');
    await dialog.getByRole('button', { name: 'Mark completed' }).click();
    await expect(page.getByRole('status').filter({ hasText: 'Ride marked as completed.' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Mark ride completed' })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Cancel ride' })).toHaveCount(0);
  });

  test('cancel ride: keyboard dialog, then cancelled with its requests', async ({ page }) => {
    const api = new MockDriverApi();
    await signIn(page, api);
    await page.goto('/driver/rides/7');
    const trigger = page.getByRole('button', { name: 'Cancel ride' });
    await trigger.focus();
    await page.keyboard.press('Enter');
    const dialog = page.getByRole('alertdialog', { name: 'Cancel this ride?' });
    await expect(dialog).toContainText('0 accepted passengers and 2 pending requests will be cancelled.');
    await page.keyboard.press('Escape');
    await expect(dialog).toHaveCount(0);
    await expect(trigger).toBeFocused();
    await trigger.click();
    await dialog.getByRole('button', { name: 'Cancel ride' }).click();
    await expect(page.getByRole('status').filter({ hasText: 'Ride cancelled.' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Cancelled (3)' })).toBeVisible();
    expect(api.rides.find(r => r.id === 7)!.status).toBe('cancelled');
  });

  test('my rides filters: upcoming, departed, completed, cancelled; empty and error states', async ({ page }) => {
    const api = new MockDriverApi();
    await signIn(page, api);
    await page.goto('/driver/rides');
    await expect(page.getByRole('article')).toHaveCount(4);
    for (const [tab, name] of [['Upcoming', 'Lahore to Islamabad'], ['Departed', 'Islamabad to Lahore'], ['Completed', 'Lahore to Rawalpindi'], ['Cancelled', 'Rawalpindi to Lahore']]) {
      await page.getByRole('button', { name: tab, exact: true }).click();
      await expect(page.getByRole('article')).toHaveCount(1);
      await expect(page.getByRole('article', { name })).toContainText(tab);
    }
    api.overrides.set('GET /rides/me', { status: 503, body: { detail: 'db down' } });
    await page.getByRole('button', { name: 'Refresh' }).click();
    await expect(page.getByText('We couldn’t load your rides.')).toBeVisible();
    api.overrides.set('GET /rides/me', { status: 200, body: [] });
    await page.getByRole('button', { name: 'Try again' }).click();
    await expect(page.getByRole('heading', { name: 'No cancelled rides.' })).toBeVisible();
  });

  test('requests loading, then error with retry', async ({ page }) => {
    const api = new MockDriverApi();
    let release!: () => void;
    const gate = new Promise<void>(resolve => { release = resolve; });
    api.overrides.set('GET /rides/7/bookings', async () => { await gate; return { status: 500, body: { detail: 'Traceback' } }; });
    await signIn(page, api);
    await page.goto('/driver/rides/7');
    await expect(page.getByText('Loading requests…')).toBeVisible();
    release();
    await expect(page.getByRole('heading', { name: 'We couldn’t load the requests.' })).toBeVisible();
    await expect(page.getByText(/Traceback/)).toHaveCount(0);
    api.overrides.delete('GET /rides/7/bookings');
    await page.getByRole('button', { name: 'Try again' }).click();
    await expect(page.getByText('2 pending · 0 accepted')).toBeVisible();
  });

  const failures: [string, Reply, RegExp][] = [
    ['403', { status: 403, body: { detail: 'This is not your ride' } }, /belongs to another driver/],
    ['404', { status: 404, body: { detail: 'Booking not found' } }, /couldn’t find that request/],
    ['422', { status: 422, body: { detail: [{ loc: ['query', 'accept'], msg: 'x' }] } }, /weren’t accepted/],
    ['429', { status: 429, body: { detail: 'Too many' }, headers: { 'Retry-After': '30' } }, /wait 30 seconds/],
    ['500', { status: 500, body: { detail: 'sqlalchemy.exc' } }, /temporarily unavailable/],
    ['network', 'abort', /Check your connection/],
  ];
  for (const [name, reply, message] of failures) {
    test(`accept ${name} shows a safe message`, async ({ page }) => {
      const api = new MockDriverApi();
      api.overrides.set('PATCH /bookings/70/respond', reply);
      await signIn(page, api);
      await page.goto('/driver/rides/7');
      await page.getByRole('button', { name: 'Accept request from Asha Test' }).click();
      await expect(page.getByRole('alert')).toHaveText(message);
      await expect(page.getByText(/sqlalchemy/)).toHaveCount(0);
    });
  }

  test('401 while accepting signs out, then returns to the same ride after login', async ({ page }) => {
    const api = new MockDriverApi();
    api.overrides.set('PATCH /bookings/70/respond', { status: 401, body: { detail: 'Invalid token' } });
    await signIn(page, api);
    await page.goto('/driver/rides/7');
    await page.getByRole('button', { name: 'Accept request from Asha Test' }).click();
    await expect(page).toHaveURL('/login');
    await expect(page.getByRole('status')).toHaveText('Your session has ended. Please sign in again.');
    api.overrides.delete('PATCH /bookings/70/respond');
    await fillLogin(page, driverUser.email);
    await expect(page).toHaveURL('/driver/rides/7');
    await page.getByRole('button', { name: 'Accept request from Asha Test' }).click();
    await expect(page.getByText('1 seat left')).toBeVisible();
  });

  test('deep links survive a reload (G1) and signed-out links return after login', async ({ page }) => {
    const api = new MockDriverApi();
    await api.install(page);
    await page.goto('/driver/rides/7');
    await expect(page).toHaveURL('/login');
    await fillLogin(page, driverUser.email);
    await expect(page).toHaveURL('/driver/rides/7');
    await expect(page.getByText('2 pending · 0 accepted')).toBeVisible();
    for (const path of ['/driver/rides/7', '/driver/rides/new', '/driver/rides?filter=departed']) {
      await page.goto(path);
      await page.reload();
      await expect(page).toHaveURL(path);
      await expect(page.locator('h1')).toBeVisible();
    }
  });

  test('another driver’s ride and unknown ids are refused in the UI', async ({ page }) => {
    const api = new MockDriverApi();
    await signIn(page, api);
    await page.goto('/driver/rides/11');
    await expect(page.getByRole('heading', { name: 'Not your ride' })).toBeVisible();
    expect(api.reads).not.toContain('/rides/11/bookings');
    await page.goto('/driver/rides/404');
    await expect(page.getByRole('heading', { name: 'Ride not found' })).toBeVisible();
  });

  test('admin cannot open driver views (UI guard; the API also refuses)', async ({ page }) => {
    const api = new MockDriverApi();
    api.user = { ...driverUser, role: 'admin', allowed_modes: [] };
    await api.install(page);
    await page.goto('/login');
    await fillLogin(page, driverUser.email);
    await expect(page).toHaveURL('/account');
    for (const path of ['/driver/rides/new', '/driver/rides', '/driver/rides/7']) {
      await page.evaluate(p => { history.pushState({}, '', p); dispatchEvent(new PopStateEvent('popstate')); }, path);
      await expect(page).toHaveURL('/account');
    }
  });
});

test.describe('driver journey at 390px', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  test('dashboard, publish, my rides, ride page and dialog fit without horizontal scrolling', async ({ page }) => {
    const api = new MockDriverApi();
    await signIn(page, api);
    for (const path of ['/driver', '/driver/rides/new', '/driver/rides', '/driver/rides/7', '/driver/rides/8']) {
      await page.goto(path);
      await expect(page.locator('h1')).toBeVisible();
      await settle(page);
      expect(await noHorizontalOverflow(page), path).toBe(true);
    }
    await page.goto('/driver/rides/7');
    for (const name of ['Accept request from Asha Test', 'Reject request from Asha Test', 'Cancel ride']) {
      const box = await page.getByRole('button', { name }).boundingBox();
      expect(box!.height, name).toBeGreaterThanOrEqual(44);
    }
    await page.getByRole('button', { name: 'Cancel ride' }).click();
    await expect(page.getByRole('alertdialog')).toBeVisible();
    expect(await noHorizontalOverflow(page)).toBe(true);
  });
});

async function axe(page: Page, testInfo: TestInfo, name: string) {
  await settle(page);
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']).analyze();
  const summary = results.violations.map(v => ({ id: v.id, impact: v.impact, nodes: v.nodes.map(n => n.target.join(' ')) }));
  await testInfo.attach(`axe-driver-${name}.json`, { body: JSON.stringify(summary, null, 2), contentType: 'application/json' });
  expect(summary.filter(v => v.impact === 'serious' || v.impact === 'critical'), name).toEqual([]);
}

test.describe('driver accessibility (axe)', () => {
  test('publish form (with errors), my rides, ride requests and dialog', async ({ page }, testInfo) => {
    const api = new MockDriverApi();
    await signIn(page, api);
    await axe(page, testInfo, 'dashboard');
    await page.goto('/driver/rides/new');
    await expect(page.locator('h1')).toBeVisible();
    await axe(page, testInfo, 'publish');
    await page.getByRole('button', { name: 'Publish ride' }).click();
    await expect(page.getByRole('alert')).toBeVisible();
    await axe(page, testInfo, 'publish-errors');
    await page.goto('/driver/rides');
    await expect(page.getByRole('article').first()).toBeVisible();
    await axe(page, testInfo, 'rides');
    await page.goto('/driver/rides/7');
    await expect(page.getByText('2 pending · 0 accepted')).toBeVisible();
    await axe(page, testInfo, 'ride');
    await page.goto('/driver/rides/8');
    await expect(page.getByText('Accepting is paused.')).toBeVisible();
    await axe(page, testInfo, 'ride-departed');
    await page.getByRole('button', { name: 'Cancel ride' }).click();
    await expect(page.getByRole('alertdialog')).toBeVisible();
    await axe(page, testInfo, 'cancel-dialog');
  });
});

// Review screenshots: opt-in with DRIVER_SCREENS_DIR=<absolute dir>.
const screensDir = process.env.DRIVER_SCREENS_DIR;
test.describe('driver screenshots', () => {
  test.skip(!screensDir, 'Set DRIVER_SCREENS_DIR to capture review screenshots.');
  for (const [device, viewport] of [['mobile', { width: 390, height: 844 }], ['desktop', { width: 1440, height: 1000 }]] as const) {
    test(`key screens (${device})`, async ({ page }) => {
      test.setTimeout(90_000);
      const api = new MockDriverApi();
      await page.setViewportSize(viewport);
      await signIn(page, api);
      const shot = async (name: string, fullPage = true) => { await settle(page); await page.screenshot({ path: `${screensDir}/${device}-${name}.png`, fullPage }); };
      await shot('01-dashboard');
      await page.goto('/driver/rides/new');
      await page.getByLabel('From').fill('Lahore');
      await page.getByLabel('To').fill('Islamabad');
      await page.getByLabel('Departure (PKT)').fill(pktLocal(26));
      await page.getByLabel('Fare per seat (PKR)').fill('1800');
      await shot('02-publish-form');
      await page.getByLabel('To').fill('lahore');
      await page.getByLabel('Departure (PKT)').fill(pktLocal(-2));
      await page.getByRole('button', { name: 'Publish ride' }).click();
      await expect(page.getByRole('alert')).toBeVisible();
      await shot('03-publish-validation');
      await page.goto('/driver/rides');
      await expect(page.getByRole('article').first()).toBeVisible();
      await shot('04-my-rides');
      await page.goto('/driver/rides/7');
      await expect(page.getByText('2 pending · 0 accepted')).toBeVisible();
      await shot('05-ride-requests');
      await page.getByRole('button', { name: 'Accept request from Asha Test' }).click();
      await expect(page.getByText('1 seat left')).toBeVisible();
      await shot('06-request-accepted');
      api.overrides.set('PATCH /bookings/71/respond', { status: 409, body: { detail: 'No seats available' } });
      await page.getByRole('button', { name: 'Accept request from Bilal Test' }).click();
      await expect(page.getByRole('alert')).toBeVisible();
      await shot('07-accept-409-no-seats');
      await page.getByRole('button', { name: 'Reject request from Bilal Test' }).click();
      await expect(page.getByRole('alertdialog')).toBeVisible();
      await shot('08-reject-dialog', false);
      await page.keyboard.press('Escape');
      await page.goto('/driver/rides/8');
      await expect(page.getByText('Accepting is paused.')).toBeVisible();
      await shot('09-departed-ride');
      await page.getByRole('button', { name: 'Cancel ride' }).click();
      await expect(page.getByRole('alertdialog')).toBeVisible();
      await shot('10-cancel-ride-dialog', false);
    });
  }
});
