import { apiOrigin, expect, fakeToken, fakeUser, fillLogin, fillRegistration, json, mockOnly, mockSignedIn, noHorizontalOverflow, test } from './fixtures';

// AST-009 smoke coverage. Every API response is a Playwright route mock, so the
// suite is deterministic and never contacts a deployed backend.

test.describe('desktop smoke', () => {
  test('home renders and the header navigates between the public pages', async ({ page }) => {
    await page.goto('/');
    await expect(page).toHaveTitle('A better way together | RideShare');
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
    const nav = page.getByRole('navigation', { name: 'Main navigation' });
    await nav.getByRole('link', { name: 'Log in', exact: true }).click();
    await expect(page).toHaveURL('/login');
    await expect(page.getByRole('heading', { name: 'Welcome back.' })).toBeVisible();
    await nav.getByRole('link', { name: 'Get started' }).click();
    await expect(page).toHaveURL('/register');
    await expect(page.getByRole('heading', { name: 'Create your account' })).toBeVisible();
    await page.getByRole('link', { name: 'Log in', exact: true }).last().click();
    await expect(page).toHaveURL('/login');
    await nav.getByRole('link', { name: 'Home', exact: true }).click();
    await expect(page).toHaveURL('/');
  });

  test('unknown client route shows the not-found page with a way home', async ({ page }) => {
    await page.goto('/');
    // Client-side navigation: the FastAPI server answers unknown paths with JSON by design.
    await page.evaluate(() => { history.pushState({}, '', '/no-such-page'); dispatchEvent(new PopStateEvent('popstate')); });
    await expect(page.getByRole('heading', { name: 'This road ends here.' })).toBeVisible();
    await expect(page).toHaveTitle('Page not found | RideShare');
    await page.getByRole('link', { name: 'Back to home' }).click();
    await expect(page).toHaveURL('/');
  });

  test('successful login reaches the dashboard and logout returns to login', async ({ page }) => {
    await mockSignedIn(page);
    await page.goto('/login');
    await fillLogin(page);
    await expect(page).toHaveURL('/passenger');
    await expect(page.getByRole('heading', { name: 'Your journey is just beginning.' })).toBeVisible();
    await page.getByRole('navigation', { name: 'Main navigation' }).getByRole('button', { name: 'Log out' }).click();
    await expect(page).toHaveURL('/login');
  });

  test('bad credentials show the server message and stay on login', async ({ page }) => {
    await mockOnly(page, { '/auth/login': route => json(route, 401, { detail: 'Invalid email or password' }) });
    await page.goto('/login');
    await fillLogin(page, fakeUser.email, 'wrong password value');
    await expect(page.getByRole('alert')).toHaveText('Invalid email or password');
    await expect(page.getByRole('alert')).toBeFocused();
    await expect(page).toHaveURL('/login');
    // The form is usable again for a retry.
    await expect(page.getByRole('button', { name: 'Log in', exact: true })).toBeEnabled();
  });

  test('login 5xx shows a generic outage message and never echoes server internals', async ({ page }) => {
    await mockOnly(page, { '/auth/login': route => json(route, 500, { detail: 'Traceback: asyncpg pool exhausted at db.internal:5432' }) });
    await page.goto('/login');
    await fillLogin(page);
    await expect(page.getByRole('alert')).toHaveText('The service is temporarily unavailable. Please try again later.');
    await expect(page.getByText(/Traceback|asyncpg|db\.internal/)).toHaveCount(0);
    await expect(page).toHaveURL('/login');
  });

  test('network failure on login shows a connection error', async ({ page }) => {
    await mockOnly(page, { '/auth/login': route => route.abort('connectionrefused') });
    await page.goto('/login');
    await fillLogin(page);
    await expect(page.getByRole('alert')).toHaveText('We could not reach RideShare. Check your connection and try again.');
    await expect(page).toHaveURL('/login');
  });

  test('duplicate registration (409) shows a generic message that names no field or value', async ({ page }) => {
    await mockOnly(page, { '/auth/register': route => json(route, 409, { detail: 'Registration details already in use' }) });
    await page.goto('/register');
    await fillRegistration(page);
    const alert = page.getByRole('alert');
    await expect(alert).toHaveText('Registration details already in use');
    await expect(alert).not.toContainText(/email|cnic|phone|@|\d{5}/i);
    await expect(page).toHaveURL('/register');
  });

  test('registration 5xx does not claim success or failure and suggests signing in', async ({ page }) => {
    await mockOnly(page, { '/auth/register': route => json(route, 503, { detail: 'upstream timeout' }) });
    await page.goto('/register');
    await fillRegistration(page);
    await expect(page.getByRole('alert')).toHaveText('We could not confirm registration. Try signing in before submitting again.');
    await expect(page.getByText('upstream timeout')).toHaveCount(0);
    await expect(page).toHaveURL('/register');
  });

  test('server field validation (422) highlights the field without leaking the input', async ({ page }) => {
    await mockOnly(page, { '/auth/register': route => json(route, 422, { detail: [{ loc: ['body', 'phone_number'], msg: 'Enter a valid phone number.', type: 'value_error' }] }) });
    await page.goto('/register');
    await fillRegistration(page);
    await expect(page.getByRole('alert')).toHaveText('Please check the highlighted fields.');
    await expect(page.getByLabel('Phone number')).toHaveAttribute('aria-invalid', 'true');
    await expect(page.locator('#phone_number-error')).toHaveText('Enter a valid phone number.');
  });

  test('dashboard history 5xx shows an error state with retry, which recovers', async ({ page }) => {
    let fail = true;
    await mockOnly(page, {
      '/auth/login': route => json(route, 200, { access_token: fakeToken(), token_type: 'bearer' }),
      '/me': route => json(route, 200, fakeUser),
      '/rides/me': route => json(route, 200, []),
      '/bookings/me': route => fail ? json(route, 502, { detail: 'bad gateway' }) : json(route, 200, []),
    });
    await page.goto('/login');
    await fillLogin(page);
    await expect(page.getByRole('heading', { name: 'We couldn’t load your journeys.' })).toBeVisible();
    fail = false;
    await page.getByRole('button', { name: 'Try again' }).click();
    await expect(page.getByRole('heading', { name: 'Your journey is just beginning.' })).toBeVisible();
  });
});

test.describe('mobile 390x844', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });

  for (const path of ['/', '/login', '/register']) {
    test(`${path} fits the viewport without horizontal scrolling`, async ({ page }) => {
      await page.goto(path);
      await expect(page.getByRole('main')).toBeVisible();
      expect(await noHorizontalOverflow(page)).toBe(true);
      // The desktop navigation collapses behind the menu button.
      const toggle = page.getByRole('button', { name: 'Open navigation' });
      await expect(toggle).toBeVisible();
      await expect(page.getByRole('navigation', { name: 'Main navigation' }).getByRole('link', { name: 'Home', exact: true })).toBeHidden();
    });
  }

  test('menu opens, navigates and closes; forms fit the screen', async ({ page }) => {
    await page.goto('/');
    await page.getByRole('button', { name: 'Open navigation' }).click();
    await expect(page.getByRole('button', { name: 'Close navigation' })).toHaveAttribute('aria-expanded', 'true');
    await page.getByRole('navigation', { name: 'Main navigation' }).getByRole('link', { name: 'Get started' }).click();
    await expect(page).toHaveURL('/register');
    await expect(page.getByRole('button', { name: 'Open navigation' })).toHaveAttribute('aria-expanded', 'false');
    const submit = page.getByRole('button', { name: 'Create account', exact: true });
    await submit.scrollIntoViewIfNeeded();
    const box = await submit.boundingBox();
    expect(box).not.toBeNull();
    expect(box!.x).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(390);
    expect(box!.height).toBeGreaterThanOrEqual(24); // WCAG 2.2 minimum target size
  });

  test('signed-in dashboard fits the viewport', async ({ page }) => {
    await mockSignedIn(page);
    await page.goto('/login');
    await fillLogin(page);
    await expect(page).toHaveURL('/passenger');
    await expect(page.getByRole('heading', { name: 'Your journey is just beginning.' })).toBeVisible();
    expect(await noHorizontalOverflow(page)).toBe(true);
  });
});

test('API origin is never a production host', () => {
  expect(new URL(apiOrigin).hostname).toMatch(/^(localhost|127\.0\.0\.1)$/);
});
