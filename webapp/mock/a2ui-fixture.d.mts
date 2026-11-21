/** Types for the mock's A2UI fixture, imported by the renderer's component test. */

export declare const SURFACE_ID: string;
export declare const BASIC_CATALOG_ID: string;

export declare function cannedSurface(): Record<string, unknown>[];

export declare function a2uiFrame(turnId?: string): {
  type: "a2ui";
  turnId: string;
  surfaceId: string;
  messages: Record<string, unknown>[];
};
