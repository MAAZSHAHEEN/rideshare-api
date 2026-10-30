import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

// AST-004: WCAG 2.1 AA text contrast for the stylesheet, and no unearned
// "verified" claims in user-facing copy (the backend verifies nothing yet).

// Every stylesheet the app imports is checked (passenger.css: AST-023, driver.css: M3).
const css = ['styles.css', 'passenger.css', 'driver.css'].map((name) => readFileSync(join(__dirname, name), 'utf8')).join('\n');

const vars = Object.fromEntries(
  [...css.matchAll(/--([\w-]+):\s*(#[0-9a-fA-F]{3,6})/g)].map(([, name, value]) => [name, value]),
);

const resolve = (value: string) => {
  const ref = /^var\(--([\w-]+)\)$/.exec(value);
  return (ref ? vars[ref[1]] : value)?.toLowerCase();
};

const expand = (hex: string) => (hex.length === 4 ? `#${[...hex.slice(1)].map((c) => c + c).join('')}` : hex);

const luminance = (hex: string) => {
  const h = expand(hex).slice(1);
  const [r, g, b] = [0, 2, 4].map((i) => {
    const c = parseInt(h.slice(i, i + 2), 16) / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};

const contrast = (a: string, b: string) => {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
};

// Light surfaces that text in this stylesheet is drawn on.
const LIGHT_SURFACES = ['#ffffff', '#fbfcff', '#f6f9fd', '#f4f7fb', '#f0f3f7', '#eef5ff', '#edf4ff'];

// Text colours intended for dark/brand surfaces, checked against that surface.
const ON_DARK: Record<string, string> = { '#fff': '#1254d4', '#ffffff': '#1254d4', '#d4e2ff': '#1254d4' };

// Large text (>= 18.66px bold) only needs 3:1 (.step-number, 1.4rem / 700).
const LARGE_TEXT = new Set(['#7f8da0']);

// Rules whose colour only paints icons (WCAG 1.4.11 non-text contrast, 3:1).
const NON_TEXT = new Set(['.empty-illustration']);

// Pill/notice colours sit on their own tinted background in the same rule.
const pairedRules = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
  .map(([, selector, body]) => ({
    selector: selector.trim(),
    color: /(?:^|;)color:(#[0-9a-fA-F]{3,6}|var\(--[\w-]+\))/.exec(body)?.[1],
    background: /(?:^|;)background:(#[0-9a-fA-F]{3,6}|var\(--[\w-]+\))(?:;|$)/.exec(body)?.[1],
  }))
  .filter((rule) => rule.color);

describe('stylesheet text contrast (WCAG AA)', () => {
  it('meets 4.5:1 (3:1 for large text) for every text colour on its surfaces', () => {
    const failures: string[] = [];
    for (const rule of pairedRules) {
      const fg = resolve(rule.color!)!;
      const bg = rule.background ? resolve(rule.background) : undefined;
      if (bg && bg.length >= 4 && !bg.endsWith('20')) {
        const needed = NON_TEXT.has(rule.selector) ? 3 : 4.5;
        if (contrast(fg, bg) < needed) failures.push(`${rule.selector}: ${fg} on ${bg} = ${contrast(fg, bg).toFixed(2)}`);
        continue;
      }
      if (ON_DARK[fg]) {
        if (contrast(fg, ON_DARK[fg]) < 4.5) failures.push(`${rule.selector}: ${fg} on ${ON_DARK[fg]}`);
        continue;
      }
      const minimum = LARGE_TEXT.has(fg) ? 3 : 4.5;
      for (const surface of LIGHT_SURFACES) {
        const ratio = contrast(fg, surface);
        if (ratio < minimum) failures.push(`${rule.selector}: ${fg} on ${surface} = ${ratio.toFixed(2)}`);
      }
    }
    expect(failures).toEqual([]);
  });

  it('keeps the shared muted token readable on every light surface', () => {
    for (const surface of LIGHT_SURFACES) expect(contrast(vars.muted, surface)).toBeGreaterThanOrEqual(4.5);
  });
});

describe('user-facing copy', () => {
  it('makes no verification claims the backend does not back', () => {
    for (const dir of [join(__dirname, 'pages'), join(__dirname, 'pages', 'passenger'), join(__dirname, 'pages', 'driver')]) for (const file of readdirSync(dir).filter((name) => name.endsWith('.tsx') && !name.endsWith('.test.tsx'))) {
      expect(readFileSync(join(dir, file), 'utf8'), file).not.toMatch(/verified by|verified (driver|account|user)s?/i);
    }
  });
  it('labels driver identity honestly: documents are not verified (M3)', () => {
    const dashboard = readFileSync(join(__dirname, 'pages', 'Dashboard.tsx'), 'utf8');
    expect(dashboard).toContain('Driver documents not verified');
    for (const file of readdirSync(join(__dirname, 'pages', 'driver')).filter((name) => name.endsWith('.tsx') && !name.endsWith('.test.tsx'))) {
      expect(readFileSync(join(__dirname, 'pages', 'driver', file), 'utf8'), file).not.toMatch(/verified/i);
    }
  });
});
