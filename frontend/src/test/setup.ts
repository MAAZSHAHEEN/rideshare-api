import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach, vi } from 'vitest';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); });
if (typeof window !== 'undefined') {
  window.scrollTo = () => {};
  Element.prototype.scrollIntoView = () => {};
}
