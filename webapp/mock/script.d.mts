/** Types for the mock's scripted frames, so the reducer test can import them. */
import type { Command, Message } from "../src/protocol";

export declare const PLOT_PNG: string;

export declare const COMMANDS: Command[];

export declare function sessionState(): {
  type: "session_state";
  sessionId: string;
  cwd: string;
  model: string;
  effort: string;
  turnRunning: boolean;
  commands: Command[];
  messages: Message[];
};

export declare function scriptedTurn(): unknown[];

/** A canned openui-lang reply, replayed when the client asked for Rich UI. */
export declare const OPENUI_TURN_LANG: string;

export declare function scriptedOpenUITurn(): unknown[];

export declare function commandResultFrame(name: string): unknown;