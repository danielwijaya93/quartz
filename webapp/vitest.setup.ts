import "@testing-library/jest-dom/vitest";

// jsdom has no layout, so scrollIntoView is not implemented. The transcript calls
// it to follow streaming output; a no-op keeps component tests quiet.
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}

// jsdom has no ResizeObserver; the auto-growing textarea uses it if present.
if (!("ResizeObserver" in globalThis)) {
  class ResizeObserverStub {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  }
  Object.defineProperty(globalThis, "ResizeObserver", {
    writable: true,
    value: ResizeObserverStub,
  });
}

// jsdom has no `matchMedia`. A layout hook asking it a question must get an answer
// rather than a crash; the default is "no match", i.e. the wide layout.
if (typeof window !== "undefined" && typeof window.matchMedia !== "function") {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }),
  });
}
