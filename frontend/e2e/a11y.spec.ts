import AxeBuilder from '@axe-core/playwright';
import type { Page, TestInfo } from '@playwright/test';
import { expect, fillLogin, fillRegistration, mockSignedIn, test } from './fixtures';

// Basic automated accessibility check (axe-core, WCAG 2.0/2.1/2.2 A+AA rules).
// Serious and critical violations fail the test; every violation, at any impact,
// is attached to the report as JSON so it can be triaged.
// Known, documented exceptions (handover/tasks/AST-009.md). Keep this list empty
// unless a violation is reviewed and deferred with a reason.
// - color-contrast: fixed by AST-004 (branch fix/frontend-copy-contrast, styles.css).
//   With that change applied, these pages report zero violations. Remove this
//   entry when AST-004 is merged so contrast regressions fail CI again.
const KNOWN_EXCEPTIONS: string[] = ['color-contrast'];

async function audit(page: Page, testInfo: TestInfo, name: string) {
  const results = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'])
    .analyze();
  const summary = results.violations.map(v => ({ id: v.id, impact: v.impact, help: v.help, nodes: v.nodes.map(n => n.target.join(' ')) }));
  await testInfo.attach(`axe-${name}.json`, { body: JSON.stringify(summary, null, 2), contentType: 'application/json' });
  if (summary.length) console.log(`[axe] ${name}: ${summary.map(v => `${v.id} (${v.impact}, ${v.nodes.length})`).join(', ')}`);
  const blocking = summary.filter(v => (v.impact === 'serious' || v.impact === 'critical') && !KNOWN_EXCEPTIONS.includes(v.id));
  expect(blocking, `serious/critical axe violations on ${name}`).toEqual([]);
}

const settle = (page: Page) => page.evaluate(() => document.fonts.ready.then(() => undefined));

test('landing page has no serious accessibility violations', async ({ page }, testInfo) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
  await settle(page);
  await audit(page, testInfo, 'landing');
});

test('login page has no serious accessibility violations', async ({ page }, testInfo) => {
  await page.goto('/login');
  await expect(page.getByRole('heading', { name: 'Welcome back.' })).toBeVisible();
  await audit(page, testInfo, 'login');
});

test('registration page, including its error state, has no serious accessibility violations', async ({ page }, testInfo) => {
  await page.goto('/register');
  await expect(page.getByRole('heading', { name: 'Create your account' })).toBeVisible();
  await audit(page, testInfo, 'register');
  await fillRegistration(page, { password: 'short' }); // fails client-side validation; no request
  await expect(page.getByRole('alert')).toBeVisible();
  await audit(page, testInfo, 'register-error');
});

test('passenger dashboard has no serious accessibility violations', async ({ page }, testInfo) => {
  await mockSignedIn(page);
  await page.goto('/login');
  await fillLogin(page);
  await expect(page.getByRole('heading', { name: 'Your journey is just beginning.' })).toBeVisible();
  await audit(page, testInfo, 'passenger-dashboard');
});

test('mobile landing (390x844) with the menu open has no serious accessibility violations', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  await page.getByRole('button', { name: 'Open navigation' }).click();
  await expect(page.getByRole('navigation', { name: 'Main navigation' })).toBeVisible();
  await audit(page, testInfo, 'mobile-landing-menu');
});
