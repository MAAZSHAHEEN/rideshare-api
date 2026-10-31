import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach, vi } from 'vitest';

afterEach(() => {
  cleanup(); vi.unstubAllGlobals(); vi.useRealTimers();
  // jsdom keeps one window per file: a session saved by one test must not leak into the next.
  if (typeof window !== 'undefined') { window.sessionStorage.clear(); window.localStorage.clear(); }
});
if (typeof window !== 'undefined') {
  window.scrollTo = () => {};
  Element.prototype.scrollIntoView = () => {};
}
