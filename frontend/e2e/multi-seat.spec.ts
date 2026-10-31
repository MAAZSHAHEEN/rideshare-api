import AxeBuilder from '@axe-core/playwright';
import type { Page, Route, TestInfo } from '@playwright/test';
import { apiOrigin, expect, fakeToken, fillLogin, noHorizontalOverflow, test } from './fixtures';

// Multi-seat bookings (#22 §3) against a stateful in-browser mock of the
// c1a7e0b00003 contract. No request leaves the browser; data is obviously fake.
const HOUR = 3_600_000;
const iso = (h: number) => new Date(Date.now() + h * HOUR).toISOString();
const user = { id: 42, name: 'Test Person', email: 'test.person@example.com', role: 'driver', allowed_modes: ['passenger', 'driver'] };
type Ride = { id: number; driver_id: number; origin: string; destination: string; departure_time: string; available_seats: number; fare_per_seat: number; status: string };
type Req = { id: number; ride_id: number; passenger_id: number; status: string; seats_requested: number; passenger: { id: number; name: string } };

class SeatApi {
  passengerRide: Ride = { id: 7, driver_id: 9, origin: 'Lahore', destination: 'Islamabad', departure_time: iso(30), available_seats: 3, fare_per_seat: 2500, status: 'active' };
  driverRide: Ride = { id: 8, driver_id: 42, origin: 'Lahore', destination: 'Multan', departure_time: iso(30), available_seats: 2, fare_per_seat: 1800, status: 'active' };
  requests: Req[] = [
    { id: 81, ride_id: 8, passenger_id: 101, status: 'pending', seats_requested: 2, passenger: { id: 101, name: 'Two Seat Passenger' } },
    { id: 82, ride_id: 8, passenger_id: 102, status: 'pending', seats_requested: 3, passenger: { id: 102, name: 'Three Seat Passenger' } },
  ];
  posts: { body: string | null; key: string | undefined }[] = [];
  /** When set, the next POST /bookings/7 drops the ride to this many seats and answers 409. */
  raceTo: number | null = null;

  async install(page: Page) {
    await page.route(url => url.origin === new URL(apiOrigin).origin && /^\/(auth\/|me$|rides\/|bookings\/)/.test(url.pathname), route => this.handle(route));
  }
  private reply(route: Route, status: number, body: unknown, headers: Record<string, string> = {}) {
    return route.fulfill({ status, contentType: 'application/json', headers: { 'Access-Control-Allow-Origin': '*', 'Access-Control-Expose-Headers': 'X-Total-Count', ...headers }, body: JSON.stringify(body) });
  }
  private async handle(route: Route) {
    const request = route.request();
    const url = new URL(request.url());
    const key = `${request.method()} ${url.pathname}`;
    if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Headers': 'Authorization, Content-Type, Idempotency-Key', 'Access-Control-Allow-Methods': 'GET, POST, PATCH' } });
    if (key === 'POST /auth/login') return this.reply(route, 200, { access_token: fakeToken(), token_type: 'bearer' });
    if (key === 'GET /me') return this.reply(route, 200, user);
    if (key === 'GET /rides/7') return this.reply(route, 200, this.passengerRide);
    if (key === 'GET /rides/8') return this.reply(route, 200, this.driverRide);
    if (key === 'GET /bookings/me') return this.reply(route, 200, [], { 'X-Total-Count': '0' });
    if (key === 'GET /rides/me/booking-counts') return this.reply(route, 200, []);
    if (key === 'GET /rides/8/bookings') return this.reply(route, 200, this.requests, { 'X-Total-Count': String(this.requests.length) });
    if (key === 'POST /bookings/7') {
      this.posts.push({ body: request.postData(), key: request.headers()['idempotency-key'] });
      if (this.raceTo !== null) { this.passengerRide.available_seats = this.raceTo; this.raceTo = null; return this.reply(route, 409, { detail: 'Not enough seats available' }); }
      const seats = request.postData() ? JSON.parse(request.postData()!).seats : 1;
      return this.reply(route, 201, { id: 70, ride_id: 7, passenger_id: 42, status: 'pending', seats_requested: seats });
    }
    const respond = /^PATCH \/bookings\/(\d+)\/respond$/.exec(key);
    if (respond) {
      const req = this.requests.find(r => r.id === Number(respond[1]))!;
      if (req.seats_requested > this.driverRide.available_seats) return this.reply(route, 409, { detail: 'Not enough seats available' });
      req.status = 'accepted'; this.driverRide.available_seats -= req.seats_requested;
      return this.reply(route, 200, { id: req.id, ride_id: 8, passenger_id: req.passenger_id, status: 'accepted', seats_requested: req.seats_requested });
    }
    return this.reply(route, 404, { detail: 'Not Found' });
  }
}

async function signIn(page: Page, api: SeatApi) {
  await api.install(page);
  await page.goto('/login');
  await fillLogin(page, user.email);
  await expect(page).toHaveURL(/\/(passenger|driver)$/);
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem('rideshare.session.v1'))).not.toBeNull();
}

async function axe(page: Page, testInfo: TestInfo, name: string) {
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']).analyze();
  const blocking = results.violations.filter(v => v.impact === 'serious' || v.impact === 'critical');
  await testInfo.attach(`axe-${name}.json`, { body: JSON.stringify(results.violations.map(v => ({ id: v.id, impact: v.impact, nodes: v.nodes.length })), null, 2), contentType: 'application/json' });
  expect(blocking.map(v => v.id), `serious/critical axe violations on ${name}`).toEqual([]);
}

test.describe('multi-seat bookings (mocked API)', () => {
  test('passenger requests 2 seats: selector bounds, total, body and Idempotency-Key', async ({ page }, testInfo) => {
    const api = new SeatApi();
    await signIn(page, api);
    await page.goto('/passenger/rides/7');
    const seats = page.getByRole('combobox', { name: 'Seats' });
    await expect(seats).toHaveValue('1');
    await expect(seats.locator('option')).toHaveText(['1 seat', '2 seats', '3 seats']);
    await seats.selectOption('2');
    await expect(page.locator('#seat-total')).toHaveText('Total PKR 5,000 for 2 seats (PKR 2,500 each)');
    await axe(page, testInfo, 'ride-details-seat-selector');
    await page.getByRole('button', { name: 'Request 2 seats' }).click();
    await expect(page.getByText('2 seats requested.')).toBeVisible();
    expect(api.posts).toHaveLength(1);
    expect(JSON.parse(api.posts[0].body!)).toEqual({ seats: 2 });
    // Split-origin dev build: the key is sent by default now that CORS allows it.
    expect(api.posts[0].key).toMatch(/^[0-9a-f-]{36}$/);
  });

  test('409 "Not enough seats available": fixed copy, ride refetched, selector shrinks', async ({ page }) => {
    const api = new SeatApi();
    await signIn(page, api);
    await page.goto('/passenger/rides/7');
    await page.getByRole('combobox', { name: 'Seats' }).selectOption('3');
    api.raceTo = 1;
    await page.getByRole('button', { name: 'Request 3 seats' }).click();
    await expect(page.getByRole('alert')).toHaveText('Not enough seats are left for that many. Choose fewer seats and try again.');
    await expect(page.getByRole('combobox', { name: 'Seats' }).locator('option')).toHaveText(['1 seat']);
    await expect(page.getByText('1 seat left')).toBeVisible();
  });

  test('driver: "N seats" per request; accept disabled with a reason when it needs more than are left', async ({ page }, testInfo) => {
    const api = new SeatApi();
    await signIn(page, api);
    await page.goto('/driver/rides/8');
    const three = page.getByRole('button', { name: 'Accept request from Three Seat Passenger' });
    await expect(three).toBeDisabled();
    await expect(three).toHaveAccessibleDescription('Needs 3 seats, but only 2 are left. Reject it, or wait until seats free up.');
    await expect(page.getByRole('article', { name: 'Three Seat Passenger' }).getByText('3 seats', { exact: true })).toBeVisible();
    await axe(page, testInfo, 'driver-ride-multi-seat');
    await page.getByRole('button', { name: 'Accept request from Two Seat Passenger' }).click();
    await expect(page.getByText('Accepted Two Seat Passenger’s request. 2 seats are now taken.')).toBeVisible();
  });

  test('mobile 390x844: selector and driver requests fit without horizontal scrolling', async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 390, height: 844 });
    const api = new SeatApi();
    await signIn(page, api);
    await page.goto('/passenger/rides/7');
    await page.getByRole('combobox', { name: 'Seats' }).selectOption('3');
    await expect(page.getByRole('button', { name: 'Request 3 seats' })).toBeVisible();
    expect(await noHorizontalOverflow(page)).toBe(true);
    await axe(page, testInfo, 'mobile-ride-details-seat-selector');
    await testInfo.attach('mobile-seat-selector.png', { body: await page.screenshot({ fullPage: true }), contentType: 'image/png' });
    await page.goto('/driver/rides/8');
    await expect(page.getByRole('button', { name: 'Accept request from Three Seat Passenger' })).toBeDisabled();
    expect(await noHorizontalOverflow(page)).toBe(true);
    await testInfo.attach('mobile-driver-requests.png', { body: await page.screenshot({ fullPage: true }), contentType: 'image/png' });
  });
});
