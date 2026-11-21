/** Types for the mock's scripted frames, so the reducer test can import them. */
import type { Command, Message } from "../src/protocol";

export declare const TINY_PNG: string;

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

export declare function commandResultFrame(name: string): unknown;
