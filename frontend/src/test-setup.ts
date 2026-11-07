import { beforeEach, vi } from 'vitest';
import { configure } from '@testing-library/react';

// Route chunks can take more than the default one second on Windows CI.
configure({asyncUtilTimeout: 5000});

beforeEach(() => {
  // jsdom has no browser layout observer; callbacks are driven by the tests.
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal('matchMedia', (query: string) => ({matches: false, media: query, onchange: null,
    addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {}, dispatchEvent() { return true; }}));
});
