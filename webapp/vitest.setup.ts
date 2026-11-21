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
