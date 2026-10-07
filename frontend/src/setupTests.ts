import '@testing-library/jest-dom';
import { TextDecoder, TextEncoder } from 'node:util';

// react-router v7 (and its undici-based deps) expect the WHATWG TextEncoder /
// TextDecoder globals, which the jsdom environment does not provide.
if (typeof globalThis.TextEncoder === 'undefined') {
  globalThis.TextEncoder = TextEncoder as unknown as typeof globalThis.TextEncoder;
  globalThis.TextDecoder = TextDecoder as unknown as typeof globalThis.TextDecoder;
}

// jsdom doesn't implement URL.createObjectURL/revokeObjectURL.
// SkillResources tests exercise blob-download paths that touch these.
if (typeof window.URL.createObjectURL === 'undefined') {
  window.URL.createObjectURL = jest.fn(() => 'blob:mock');
  window.URL.revokeObjectURL = jest.fn();
}
