import AxeBuilder from '@axe-core/playwright';
import { execFileSync } from 'node:child_process';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import type { APIRequestContext, Browser, BrowserContext, Page, TestInfo } from '@playwright/test';
import { expect, fillLogin, noHorizontalOverflow, test } from './fixtures';

// Milestone 4 (RC) browser acceptance on the INTEGRATED build: the production
// bundle served by uvicorn with a live, disposable local backend and database.
//
// Opt-in only. Needs everything the other live specs need (E2E_LIVE_BACKEND=1,
// loopback PLAYWRIGHT_BASE_URL) plus E2E_RC_ACCEPTANCE=1 and E2E_RC_PSQL_URL, a
// libpq URL of the SAME disposable database the server uses. psql is used for
// the three things the public API can't do on purpose:
//   1. promote a freshly registered synthetic user to admin (suspension test);
//   2. back-date one pending request's expires_at (expiry test, = TTL elapsed);
//   3. nothing else: every other step goes through the UI or the public API.
// The URL must be loopback and name a database starting with rs_ or e2e.
// Optional E2E_RC_SWEEP_CMD runs `python -m jobs.expire_bookings` for the same DB.
// Optional E2E_RC_SCREENS_DIR receives the evidence screenshots.
//
// Data is synthetic: rc*@example.com emails (the API rejects the reserved .test TLD), +92300000xxxx-style fake phones,
// fake CNIC 12345-…. Traces are NOT recorded by this spec: a trace keeps request
// headers and the login response, i.e. bearer tokens. Video records pixels only,
// and the UI never renders a token.
const baseURL = process.env.PLAYWRIGHT_BASE_URL ?? '';
const psqlUrl = process.env.E2E_RC_PSQL_URL ?? '';
const loopback = (url: string) => /^(https?|postgres(ql)?):\/\/([^@/]*@)?(127\.0\.0\.1|localhost)(:\d+)?\//.test(url + '/');
const enabled = process.env.E2E_LIVE_BACKEND === '1' && process.env.E2E_RC_ACCEPTANCE === '1'
  && /^http:\/\/(127\.0\.0\.1|localhost):\d+\/?$/.test(baseURL)
  && loopback(psqlUrl) && /\/(rs_|e2e)[a-z0-9_]*$/.test(psqlUrl);
const screensDir = process.env.E2E_RC_SCREENS_DIR ?? '';
const password = 'A strong rc-acceptance password';
const tomorrowPkt = () => new Date(Date.now() + 5 * 3_600_000 + 86_400_000).toISOString().slice(0, 10);
const run = `${Date.now()}`.slice(-7);
let seq = 0;
/** Fresh synthetic identity: unique per call, obviously fake. */
function person(label: string) {
  seq += 1;
  const n = `${run}${seq}`.padStart(8, '0').slice(-8);
  return {
    name: `${label} Rc`,
    email: `${label.toLowerCase().replace(/\s+/g, '.')}.rc${n}@example.com`,
    cnic: `12345${n}`,
    phone_number: `+92300${n.slice(-7)}`,
  };
}

test.use({ trace: 'off' });
// Short evidence videos (W1 only) go to E2E_RC_VIDEO_DIR when set.
const videoDir = process.env.E2E_RC_VIDEO_DIR ?? '';

function psql(sql: string): string {
  return execFileSync('psql', [psqlUrl, '-X', '-q', '-At', '-v', 'ON_ERROR_STOP=1', '-c', sql], { encoding: 'utf8' }).trim();
}

async function shot(page: Page, testInfo: TestInfo, name: string, fullPage = true) {
  const body = await page.screenshot({ fullPage });
  await testInfo.attach(`${name}.png`, { body, contentType: 'image/png' });
  if (screensDir) {
    mkdirSync(screensDir, { recursive: true });
    await page.screenshot({ path: join(screensDir, `${name}.png`), fullPage });
  }
}

async function axe(page: Page, testInfo: TestInfo, name: string) {
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze();
  const serious = results.violations.filter(v => v.impact === 'serious' || v.impact === 'critical');
  await testInfo.attach(`axe-rc-${name}.json`, {
    body: JSON.stringify(serious.map(v => ({ id: v.id, impact: v.impact, nodes: v.nodes.map(n => n.target) })), null, 2),
    contentType: 'application/json',
  });
  expect(serious, `serious/critical axe violations on ${name}`).toEqual([]);
}

async function isolatedContext(browser: Browser, options: Parameters<Browser['newContext']>[0] = {}, video = ''): Promise<BrowserContext> {
  const context = await browser.newContext({ baseURL, ...options, ...(video && videoDir ? { recordVideo: { dir: join(videoDir, video), size: { width: 1280, height: 800 } } } : {}) });
  const allowed = new URL(baseURL).origin;
  await context.route(url => url.origin !== allowed && /^https?:$/.test(url.protocol), route => route.abort('blockedbyclient'));
  return context;
}

async function registerApi(request: APIRequestContext, who: ReturnType<typeof person>, role: 'passenger' | 'driver') {
  const response = await request.post('/auth/register', { data: { ...who, password, role } });
  expect(response.status(), await response.text()).toBe(201);
  return (await response.json()).id as number;
}

/** Authorization header for API-side steps. Held in memory only, never logged or attached. */
async function apiAuth(request: APIRequestContext, email: string) {
  const login = await request.post('/auth/login', { data: { email, password } });
  expect(login.status()).toBe(200);
  return { Authorization: `Bearer ${(await login.json()).access_token}` };
}

async function uiLogin(page: Page, email: string, home: string) {
  await page.goto('/login');
  await fillLogin(page, email, password);
  await expect(page).toHaveURL(home);
}

async function publishApi(request: APIRequestContext, auth: Record<string, string>, origin: string, destination: string, seats = 3, fare = 1500) {
  const created = await request.post('/rides/', { headers: auth, data: {
    origin, destination, departure_time: `${tomorrowPkt()}T10:00:00+05:00`, available_seats: seats, fare_per_seat: fare } });
  expect(created.status(), await created.text()).toBe(201);
  return (await created.json()).id as number;
}

const seatsLeft = async (request: APIRequestContext, auth: Record<string, string>, rideId: number) =>
  (await (await request.get(`/rides/${rideId}`, { headers: auth })).json()).available_seats as number;

test.describe('RC acceptance (integrated build, live disposable backend)', () => {
  test.skip(!enabled, 'Opt-in: E2E_LIVE_BACKEND=1, E2E_RC_ACCEPTANCE=1, loopback PLAYWRIGHT_BASE_URL and a loopback disposable E2E_RC_PSQL_URL.');

  test('W1 core journey: register → case-insensitive search → book 2 seats → pending → driver accepts → confirmed, 1 left → cancel → 3 restored', async ({ browser, request }, testInfo) => {
    test.setTimeout(300_000);
    const tag = `${run}w1`;
    const origin = `RC Lahore ${tag}`;
    const destination = `RC Islamabad ${tag}`;

    // Driver registers in the UI and publishes a 3-seat ride beforehand.
    const d = person('Driver One');
    const driverCtx = await isolatedContext(browser, { viewport: { width: 1280, height: 800 } }, 'w1-driver');
    const driver = await driverCtx.newPage();
    await driver.goto('/register');
    await driver.getByRole('radio', { name: 'Driver', exact: true }).check();
    await driver.getByLabel('Full name').fill(d.name);
    await driver.getByLabel('Email address').fill(d.email);
    await driver.getByLabel('CNIC', { exact: true }).fill(d.cnic);
    await driver.getByLabel('Phone number').fill(d.phone_number);
    await driver.getByLabel('Password', { exact: true }).fill(password);
    await driver.getByRole('button', { name: 'Create account', exact: true }).click();
    await expect(driver).toHaveURL('/login');
    await fillLogin(driver, d.email, password);
    await expect(driver).toHaveURL('/driver');
    await driver.goto('/driver/rides/new');
    await driver.getByLabel('From').fill(origin);
    await driver.getByLabel('To').fill(destination);
    await driver.getByLabel('Departure (PKT)').fill(`${tomorrowPkt()}T10:00`);
    await driver.getByLabel('Seats offered').fill('3');
    await driver.getByLabel('Fare per seat (PKR)').fill('1500');
    await shot(driver, testInfo, 'w1-01-driver-publish-form');
    await driver.getByRole('button', { name: 'Publish ride' }).click();
    await expect(driver.getByText('Ride published.')).toBeVisible();
    await expect(driver.getByText('3 seats left')).toBeVisible();
    await expect(driver).toHaveURL(/\/driver\/rides\/\d+$/);
    const rideId = Number(new URL(driver.url()).pathname.split('/').pop());
    await shot(driver, testInfo, 'w1-02-driver-ride-published');
    const driverAuth = await apiAuth(request, d.email);

    // Passenger registers in the UI.
    const p = person('Passenger One');
    const pCtx = await isolatedContext(browser, { viewport: { width: 1280, height: 800 } }, 'w1-passenger');
    const page = await pCtx.newPage();
    await page.goto('/register');
    await page.getByLabel('Full name').fill(p.name);
    await page.getByLabel('Email address').fill(p.email);
    await page.getByLabel('CNIC', { exact: true }).fill(p.cnic);
    await page.getByLabel('Phone number').fill(p.phone_number);
    await page.getByLabel('Password', { exact: true }).fill(password);
    await shot(page, testInfo, 'w1-03-passenger-register');
    await page.getByRole('button', { name: 'Create account', exact: true }).click();
    await expect(page).toHaveURL('/login');
    await expect(page.getByText('Your account is ready. Log in to get started.')).toBeVisible();
    await fillLogin(page, p.email, password);
    await expect(page).toHaveURL('/passenger');

    // Case-insensitive search: lower-case "from", upper-case "to".
    await page.goto('/passenger/search');
    await page.getByLabel('From', { exact: true }).fill(origin.toLowerCase());
    await page.getByLabel('To', { exact: true }).fill(destination.toUpperCase());
    await page.getByLabel('Date (PKT)').fill(tomorrowPkt());
    await page.getByRole('button', { name: 'Search rides' }).click();
    await expect(page.getByText('1 ride found.')).toBeVisible();
    const card = page.getByRole('article', { name: `${origin} to ${destination}` });
    await expect(card).toContainText('3 seats left');
    await expect(card).toContainText('PKR 1,500');
    await shot(page, testInfo, 'w1-04-search-case-insensitive');

    // Book two seats.
    await card.getByRole('link', { name: /^View details/ }).click();
    await expect(page).toHaveURL(`/passenger/rides/${rideId}`);
    await page.getByRole('combobox', { name: 'Seats' }).selectOption('2');
    await expect(page.locator('#seat-total')).toHaveText('Total PKR 3,000 for 2 seats (PKR 1,500 each)');
    await page.getByRole('button', { name: 'Request 2 seats' }).click();
    await expect(page.getByText('2 seats requested.')).toBeVisible();
    expect(await seatsLeft(request, driverAuth, rideId)).toBe(3); // a request debits nothing
    await shot(page, testInfo, 'w1-05-two-seats-requested');

    // Pending in My bookings.
    await page.goto('/passenger/bookings');
    await expect(page.getByRole('heading', { name: 'Pending (1)' })).toBeVisible();
    const booking = page.getByRole('article').filter({ hasText: origin });
    await expect(booking.getByText('2 seats', { exact: true })).toBeVisible();
    await expect(booking).toContainText('PKR 3,000 (2 × PKR 1,500)');
    await shot(page, testInfo, 'w1-06-pending');

    // Driver accepts.
    await driver.getByRole('button', { name: 'Refresh' }).click();
    const request2 = driver.getByRole('article', { name: p.name });
    await expect(request2.getByText('2 seats', { exact: true })).toBeVisible();
    await driver.getByRole('button', { name: `Accept request from ${p.name}` }).click();
    await expect(driver.getByText(`Accepted ${p.name}’s request. 2 seats are now taken.`)).toBeVisible();
    await expect(driver.getByText('1 seat left')).toBeVisible();
    await shot(driver, testInfo, 'w1-07-driver-accepted');

    // Passenger reloads: confirmed, 1 seat left on the ride.
    await page.reload();
    await expect(page.getByRole('heading', { name: 'Accepted (1)' })).toBeVisible();
    await expect(page.getByText('Your seat is confirmed by the driver.')).toBeVisible();
    await shot(page, testInfo, 'w1-08-passenger-confirmed');
    await page.goto(`/passenger/rides/${rideId}`);
    await expect(page.getByText('1 seat left')).toBeVisible();
    expect(await seatsLeft(request, driverAuth, rideId)).toBe(1);
    await shot(page, testInfo, 'w1-09-ride-one-seat-left');

    // Passenger cancels before departure: all 3 seats restored.
    await page.goto('/passenger/bookings');
    await booking.getByRole('button', { name: /^Cancel booking:/ }).click();
    const dialog = page.getByRole('alertdialog');
    await expect(dialog).toContainText('Your 2 confirmed seats will be released');
    await shot(page, testInfo, 'w1-10-cancel-dialog', false);
    await dialog.getByRole('button', { name: 'Cancel booking' }).click();
    await expect(page.getByRole('status').filter({ hasText: 'Booking cancelled' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Cancelled (1)' })).toBeVisible();
    expect(await seatsLeft(request, driverAuth, rideId)).toBe(3);
    await shot(page, testInfo, 'w1-11-cancelled');
    await driver.getByRole('button', { name: 'Refresh' }).click();
    await expect(driver.getByText('3 seats left')).toBeVisible();
    await shot(driver, testInfo, 'w1-12-driver-seats-restored');
    await pCtx.close();
    await driverCtx.close();
  });

  test('W2 deep links + reload on every route, axe on each page, mobile 390×844 layouts', async ({ browser, request }, testInfo) => {
    test.setTimeout(420_000);
    const tag = `${run}w2`;
    const d = person('Driver Two');
    const p = person('Passenger Two');
    await registerApi(request, d, 'driver');
    await registerApi(request, p, 'passenger');
    const driverAuth = await apiAuth(request, d.email);
    const rideId = await publishApi(request, driverAuth, `RC Multan ${tag}`, `RC Faisalabad ${tag}`);
    const pAuth = await apiAuth(request, p.email);
    expect((await request.post(`/bookings/${rideId}`, { headers: pAuth })).status()).toBe(201);

    const routes: Array<{ who: 'passenger' | 'driver'; path: string; ready: (page: Page) => Promise<void> }> = [
      { who: 'passenger', path: '/passenger', ready: page => expect(page.getByRole('heading', { level: 1 }).first()).toBeVisible() },
      { who: 'passenger', path: `/passenger/search?from=rc+multan+${tag}`, ready: page => expect(page.getByText('1 ride found.')).toBeVisible() },
      { who: 'passenger', path: '/passenger/bookings', ready: page => expect(page.getByRole('heading', { name: 'Pending (1)' })).toBeVisible() },
      { who: 'passenger', path: `/passenger/rides/${rideId}`, ready: page => expect(page.getByText('You’ve already requested a seat on this ride.', { exact: false })).toBeVisible() },
      { who: 'passenger', path: '/account', ready: page => expect(page.getByRole('heading', { level: 1 }).first()).toBeVisible() },
      { who: 'driver', path: '/driver', ready: page => expect(page.getByRole('heading', { level: 1 }).first()).toBeVisible() },
      { who: 'driver', path: '/driver/rides', ready: page => expect(page.getByRole('article', { name: `RC Multan ${tag} to RC Faisalabad ${tag}` })).toBeVisible() },
      { who: 'driver', path: '/driver/rides/new', ready: page => expect(page.getByRole('button', { name: 'Publish ride' })).toBeVisible() },
      { who: 'driver', path: `/driver/rides/${rideId}`, ready: page => expect(page.getByRole('button', { name: `Accept request from ${p.name}` })).toBeVisible() },
    ];
    const slug = (path: string) => path.replace(/\?.*/, '').replace(/\/\d+$/, '/id').replace(/^\//, '').replace(/\//g, '-');

    for (const [label, viewport] of [['desktop', { width: 1280, height: 800 }], ['mobile', { width: 390, height: 844 }]] as const) {
      const contexts = {
        passenger: await isolatedContext(browser, { viewport, isMobile: label === 'mobile', hasTouch: label === 'mobile' }),
        driver: await isolatedContext(browser, { viewport, isMobile: label === 'mobile', hasTouch: label === 'mobile' }),
      };
      const pages = { passenger: await contexts.passenger.newPage(), driver: await contexts.driver.newPage() };
      await uiLogin(pages.passenger, p.email, '/passenger');
      await uiLogin(pages.driver, d.email, '/driver');
      for (const route of routes) {
        const page = pages[route.who];
        await page.goto(route.path);          // deep link (full document load)
        await route.ready(page);
        await page.reload();                  // reload keeps the session and the route
        await expect.poll(() => new URL(page.url()).pathname).toBe(route.path.replace(/\?.*/, ''));
        await route.ready(page);
        if (label === 'desktop') await axe(page, testInfo, `${route.who}-${slug(route.path)}`);
        else expect(await noHorizontalOverflow(page), `${route.path} overflows at 390 px`).toBe(true);
        await shot(page, testInfo, `w2-${label}-${route.who}-${slug(route.path) || 'home'}`);
      }
      await contexts.passenger.close();
      await contexts.driver.close();
    }

    // Public pages, and a signed-out deep link that returns to its route after login.
    const anon = await isolatedContext(browser, { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
    const page = await anon.newPage();
    for (const path of ['/', '/login', '/register']) {
      await page.goto(path);
      expect(await noHorizontalOverflow(page), `${path} overflows at 390 px`).toBe(true);
      await axe(page, testInfo, `public-${path.slice(1) || 'home'}`);
      await shot(page, testInfo, `w2-mobile-public-${path.slice(1) || 'home'}`);
    }
    await page.goto('/passenger/bookings');
    await expect(page).toHaveURL(/\/login/);
    await fillLogin(page, p.email, password);
    await expect(page).toHaveURL('/passenger/bookings');
    await expect(page.getByRole('heading', { name: 'Pending (1)' })).toBeVisible();
    await anon.close();
  });

  test('W3 session expiry: expired token → login with a message, no redirect loop', async ({ browser, request }, testInfo) => {
    test.setTimeout(120_000);
    const p = person('Passenger Three');
    await registerApi(request, p, 'passenger');
    const context = await isolatedContext(browser);
    const page = await context.newPage();
    // Fake the BROWSER clock only (the server keeps real time), so the real
    // 60-minute token is seen as expired by the client without forging one.
    await page.clock.install();
    await uiLogin(page, p.email, '/passenger');
    await page.goto('/passenger/bookings');
    // Let the reload's session restore (GET /me) and the bookings read settle first:
    // jumping the fake clock while they are in flight would fire their client timeouts.
    await expect(page.getByRole('heading', { name: 'No bookings yet.' })).toBeVisible();
    await page.waitForLoadState('networkidle');
    let meCalls = 0;
    page.on('request', r => { if (new URL(r.url()).pathname === '/me') meCalls += 1; });
    await page.clock.fastForward('01:01:00');
    await expect(page).toHaveURL(/\/login/);
    await expect(page.getByText('Your session has expired. Please sign in again.')).toBeVisible();
    await shot(page, testInfo, 'w3-01-session-expired');
    expect(await page.evaluate(() => sessionStorage.getItem('rideshare.session.v1'))).toBeNull();
    // No loop: the page stays on /login and makes no further identity calls.
    await page.clock.runFor(5_000);
    await page.waitForTimeout(1_500);
    await expect(page).toHaveURL(/\/login/);
    expect(meCalls).toBe(0);
    // A reload after expiry does not resurrect the session either.
    await page.goto('/passenger/bookings');
    await expect(page).toHaveURL(/\/login/);
    expect(meCalls).toBe(0);
    // D-RC-1 (documented in handover/RC_ACCEPTANCE.md): the client checks the token's exp
    // against the DEVICE clock, so while this browser's clock is still 61 min fast a fresh
    // token already looks expired and sign-in fails closed (clear copy, no loop).
    await fillLogin(page, p.email, password);
    await expect(page.getByRole('alert')).toHaveText('The sign-in session is invalid or expired. Please sign in again.');
    await shot(page, testInfo, 'w3-02-clock-skew-sign-in-refused');
    // With the device clock correct again, signing in returns to the protected route.
    await page.clock.setSystemTime(new Date());
    await fillLogin(page, p.email, password);
    await expect(page).toHaveURL('/passenger/bookings');
    await context.close();
  });

  test('W4 error handling: API down, 500, 429 and a real 409 get fixed copy (never raw server text)', async ({ browser, request }, testInfo) => {
    test.setTimeout(180_000);
    const tag = `${run}w4`;
    const d = person('Driver Four');
    const a = person('Passenger Four A');
    const b = person('Passenger Four B');
    await registerApi(request, d, 'driver');
    await registerApi(request, a, 'passenger');
    await registerApi(request, b, 'passenger');
    const driverAuth = await apiAuth(request, d.email);
    const rideId = await publishApi(request, driverAuth, `RC Sialkot ${tag}`, `RC Gujrat ${tag}`, 1, 900);

    const context = await isolatedContext(browser);
    const page = await context.newPage();
    await uiLogin(page, b.email, '/passenger');
    const searchUrl = `/passenger/search?from=rc+sialkot+${tag}`;
    const searchApi = (url: URL) => url.pathname === '/rides/';

    await page.route(searchApi, route => route.abort('connectionrefused'));
    await page.goto(searchUrl);
    await expect(page.getByRole('alert')).toHaveText('We couldn’t reach RideShare. Check your connection and try again.');
    await shot(page, testInfo, 'w4-01-api-down');
    await page.unroute(searchApi);

    await page.route(searchApi, route => route.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"Internal server error: secret stack"}' }));
    await page.reload();
    await expect(page.getByRole('alert')).toHaveText('The service is temporarily unavailable. Please try again later.');
    await expect(page.getByText('secret stack')).toHaveCount(0);
    await shot(page, testInfo, 'w4-02-server-500');
    await page.unroute(searchApi);

    await page.route(searchApi, route => route.fulfill({ status: 429, contentType: 'application/json', headers: { 'Retry-After': '30' }, body: '{"detail":"Too Many Requests"}' }));
    await page.reload();
    await expect(page.getByRole('alert')).toHaveText('Too many requests right now. Please wait 30 seconds and try again.');
    await shot(page, testInfo, 'w4-03-rate-limited');
    await page.unroute(searchApi);
    await page.getByRole('button', { name: 'Try again' }).click();
    await expect(page.getByText('1 ride found.')).toBeVisible();

    // Real 409: B has the last seat open; A books it and the driver accepts first.
    await page.goto(`/passenger/rides/${rideId}`);
    await expect(page.getByRole('button', { name: 'Request a seat' })).toBeVisible();
    const aAuth = await apiAuth(request, a.email);
    const aBooking = await request.post(`/bookings/${rideId}`, { headers: aAuth });
    expect(aBooking.status()).toBe(201);
    expect((await request.patch(`/bookings/${(await aBooking.json()).id}/respond?accept=true`, { headers: driverAuth })).status()).toBe(200);
    await page.getByRole('button', { name: 'Request a seat' }).click();
    await expect(page.getByRole('alert')).toHaveText('This ride is full. There are no seats left to request.');
    await shot(page, testInfo, 'w4-04-conflict-409-full');

    // API down while a signed-in tab reloads (session restore cannot reach GET /me).
    // D-RC-2: the restore treats a network failure like a rejected token and clears the session.
    const meApi = (url: URL) => url.pathname === '/me';
    await page.route(meApi, route => route.abort('connectionrefused'));
    await page.goto('/passenger/bookings');
    await expect(page).toHaveURL(/\/login/);
    await expect(page.getByText('We couldn’t restore your session. Please sign in again.')).toBeVisible();
    await shot(page, testInfo, 'w4-05-api-down-during-restore');
    await page.unroute(meApi);
    await context.close();
  });

  test('W5 expired pending request is shown as expired to passenger and driver', async ({ browser, request }, testInfo) => {
    test.setTimeout(180_000);
    const tag = `${run}w5`;
    const d = person('Driver Five');
    const p = person('Passenger Five');
    await registerApi(request, d, 'driver');
    await registerApi(request, p, 'passenger');
    const driverAuth = await apiAuth(request, d.email);
    const rideId = await publishApi(request, driverAuth, `RC Quetta ${tag}`, `RC Sukkur ${tag}`);
    const pAuth = await apiAuth(request, p.email);
    const created = await request.post(`/bookings/${rideId}`, { headers: pAuth, data: { seats: 2 } });
    expect(created.status()).toBe(201);
    const bookingId: number = (await created.json()).id;
    // The TTL elapsing, compressed: back-date this one disposable row's expiry.
    expect(psql(`UPDATE bookings SET expires_at = now() - interval '1 minute' WHERE id = ${bookingId} AND status = 'pending' RETURNING id`)).toBe(String(bookingId));
    if (process.env.E2E_RC_SWEEP_CMD) execFileSync('bash', ['-c', process.env.E2E_RC_SWEEP_CMD], { stdio: 'ignore' });

    const pCtx = await isolatedContext(browser);
    const page = await pCtx.newPage();
    await uiLogin(page, p.email, '/passenger');
    await page.goto('/passenger/bookings');
    await expect(page.getByRole('heading', { name: 'Expired (1)' })).toBeVisible();
    await expect(page.getByRole('heading', { name: /^Pending/ })).toHaveCount(0);
    await expect(page.getByRole('button', { name: /^Cancel booking:/ })).toHaveCount(0);
    await shot(page, testInfo, 'w5-01-passenger-expired');

    const dCtx = await isolatedContext(browser);
    const driver = await dCtx.newPage();
    await uiLogin(driver, d.email, '/driver');
    await driver.goto(`/driver/rides/${rideId}`);
    await expect(driver.getByRole('heading', { name: 'Expired (1)' })).toBeVisible();
    await expect(driver.getByRole('button', { name: `Accept request from ${p.name}` })).toHaveCount(0);
    await expect(driver.getByText('3 seats left')).toBeVisible();
    await shot(driver, testInfo, 'w5-02-driver-expired');
    // Server agrees: accepting an expired request is a 409.
    const forced = await request.patch(`/bookings/${bookingId}/respond?accept=true`, { headers: driverAuth });
    expect(forced.status()).toBe(409);
    expect(await seatsLeft(request, driverAuth, rideId)).toBe(3);
    await pCtx.close();
    await dCtx.close();
  });

  test('W6 suspension: live session kicked out, login refused generically, bookings cancelled and seats restored', async ({ browser, request }, testInfo) => {
    test.setTimeout(180_000);
    // Suspension needs the admin API (#19/#21). On a stack without it (e.g. #24 alone) /admin/* is 404: skip, don't fail.
    test.skip((await request.get('/admin/audit-log')).status() === 404, 'Admin API (#19/#21) not mounted on this backend.');
    const tag = `${run}w6`;
    const d = person('Driver Six');
    const s = person('Passenger Six');
    const admin = person('Admin Six');
    await registerApi(request, d, 'driver');
    const suspendedId = await registerApi(request, s, 'passenger');
    await registerApi(request, admin, 'passenger');
    // Test-only admin: promoted by direct SQL in the disposable database (no API exists for this, on purpose).
    expect(psql(`UPDATE users SET role = 'admin' WHERE email = '${admin.email}' RETURNING role`)).toBe('admin');
    const driverAuth = await apiAuth(request, d.email);
    const rideId = await publishApi(request, driverAuth, `RC Peshawar ${tag}`, `RC Mardan ${tag}`);

    const context = await isolatedContext(browser);
    const page = await context.newPage();
    await uiLogin(page, s.email, '/passenger');
    await page.goto(`/passenger/rides/${rideId}`);
    await page.getByRole('combobox', { name: 'Seats' }).selectOption('2');
    await page.getByRole('button', { name: 'Request 2 seats' }).click();
    await expect(page.getByText('2 seats requested.')).toBeVisible();
    const pending = (await (await request.get(`/rides/${rideId}/bookings?status=pending`, { headers: driverAuth })).json())[0];
    expect((await request.patch(`/bookings/${pending.id}/respond?accept=true`, { headers: driverAuth })).status()).toBe(200);
    expect(await seatsLeft(request, driverAuth, rideId)).toBe(1);
    await page.goto('/passenger/bookings');
    await expect(page.getByRole('heading', { name: 'Accepted (1)' })).toBeVisible();

    const adminAuth = await apiAuth(request, admin.email);
    const suspended = await request.post(`/admin/users/${suspendedId}/suspend`, { headers: adminAuth, data: { reason: 'RC acceptance synthetic suspension' } });
    expect(suspended.status(), await suspended.text()).toBe(200);

    // The live session is rejected on its next request and lands on login with a message.
    await page.getByRole('button', { name: 'Refresh' }).first().click().catch(() => page.reload());
    await expect(page).toHaveURL(/\/login/, { timeout: 15_000 });
    await expect(page.getByText('Your session has ended. Please sign in again.')).toBeVisible();
    await shot(page, testInfo, 'w6-01-session-kicked-out');

    // Login refused with the same generic copy as a wrong password.
    await fillLogin(page, s.email, password);
    const suspendedCopy = await page.getByRole('alert').innerText();
    await shot(page, testInfo, 'w6-02-login-refused');
    await fillLogin(page, s.email, `${password} wrong`);
    await expect(page.getByRole('alert')).toHaveText(suspendedCopy);
    expect(suspendedCopy).not.toMatch(/suspend/i);
    await expect(page).toHaveURL(/\/login/);

    // Their bookings were cancelled and the seats restored.
    expect(await seatsLeft(request, driverAuth, rideId)).toBe(3);
    const statuses = (await (await request.get(`/rides/${rideId}/bookings`, { headers: driverAuth })).json()).map((b: { status: string }) => b.status);
    expect(statuses).toEqual(['cancelled']);
    const dCtx = await isolatedContext(browser);
    const driver = await dCtx.newPage();
    await uiLogin(driver, d.email, '/driver');
    await driver.goto(`/driver/rides/${rideId}`);
    await expect(driver.getByText('3 seats left')).toBeVisible();
    await expect(driver.getByRole('heading', { name: 'Cancelled (1)' })).toBeVisible();
    await shot(driver, testInfo, 'w6-03-driver-seats-restored');
    await context.close();
    await dCtx.close();
  });

  test('W7 empty search results state', async ({ browser, request }, testInfo) => {
    test.setTimeout(90_000);
    const p = person('Passenger Seven');
    await registerApi(request, p, 'passenger');
    const context = await isolatedContext(browser);
    const page = await context.newPage();
    await uiLogin(page, p.email, '/passenger');
    await page.goto('/passenger/search');
    await page.getByLabel('From', { exact: true }).fill(`Nowhere ${run}`);
    await page.getByLabel('To', { exact: true }).fill(`Neverland ${run}`);
    await page.getByRole('button', { name: 'Search rides' }).click();
    await expect(page.getByText('No rides found.')).toBeVisible();
    await expect(page.getByRole('heading', { name: 'No rides match yet.' })).toBeVisible();
    await axe(page, testInfo, 'search-empty');
    await shot(page, testInfo, 'w7-01-empty-search');
    await context.close();
  });
});
