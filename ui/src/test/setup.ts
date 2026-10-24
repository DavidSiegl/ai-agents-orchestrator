import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

// Node 25 and later define a localStorage global of their own, undefined without --localstorage-file, and Vitest's
// jsdom environment copies no jsdom global over one Node already has. So the app and the tests get jsdom's storage
// here, on every Node; `jsdom` is the JSDOM instance that environment puts on the global.
const { jsdom } = globalThis as unknown as { jsdom: { window: Window } };
Object.defineProperty(globalThis, "localStorage", {
  value: jsdom.window.localStorage,
  configurable: true,
  writable: true,
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  localStorage.clear();
});
