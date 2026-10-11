// @vitest-environment node
import { describe, expect, it } from 'vitest';
import config from '../../vite.config';

describe('full-stack build API configuration', () => {
  it.each(['production', 'development'])('forces same origin for builds in %s mode', async mode => {
    if (typeof config !== 'function') throw new Error('Expected mode-aware Vite configuration');
    const resolved = await config({ command: 'build', mode, isSsrBuild: false, isPreview: false });
    expect(resolved.define?.['import.meta.env.VITE_API_BASE_URL']).toBe(JSON.stringify('/'));
  });

  it('leaves the development server API override configurable', async () => {
    if (typeof config !== 'function') throw new Error('Expected mode-aware Vite configuration');
    const resolved = await config({ command: 'serve', mode: 'development', isSsrBuild: false, isPreview: false });
    expect(resolved.define?.['import.meta.env.VITE_API_BASE_URL']).toBeUndefined();
  });
});
