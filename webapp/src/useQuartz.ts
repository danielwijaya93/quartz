import * as React from "react";
import {
  Connection,
  resolveSocketUrl,
  type ConnectionStatus,
} from "./lib/connection";
import {
  addUserPrompt,
  initialState,
  reduce,
  setRowFormState,
  type TranscriptState,
} from "./lib/transcript";
import type { ImageAttachment } from "./protocol";

export interface QuartzApi {
  state: TranscriptState;
  status: ConnectionStatus;
  prompt: (text: string, images: ImageAttachment[]) => void;
  steer: (text: string) => void;
  cancel: () => void;
  runCommand: (name: string, arg?: string) => void;
  newSession: () => void;
  /** Persist an OpenUI surface's form state onto the row that rendered it. */
  setFormState: (id: string, formState: Record<string, unknown>) => void;
}

/**
 * The single point where the transport meets the reducer.
 *
 * Everything below is pure: this hook owns the one `Connection`, folds its frames
 * into transcript state, and exposes intent-level actions. Components never see a
 * socket or a protocol message.
 */
export function useQuartz(url?: string, systemPromptAppend = ""): QuartzApi {
  const [state, setState] = React.useState<TranscriptState>(initialState);
  const [status, setStatus] = React.useState<ConnectionStatus>("disconnected");

  const connectionRef = React.useRef<Connection | null>(null);
  // Read by the connection effect so a re-created connection (url change, remount)
  // inherits the current append instead of starting from an empty one.
  const appendRef = React.useRef(systemPromptAppend);

  React.useEffect(() => {
    const target = url ?? resolveSocketUrl(window.location);
    const connection = new Connection(target);
    connection.setSystemPromptAppend(appendRef.current);
    connectionRef.current = connection;

    const offMessage = connection.on("message", (message) => {
      setState((prev) => reduce(prev, message));
    });
    const offStatus = connection.on("status", setStatus);

    connection.connect();

    return () => {
      offMessage();
      offStatus();
      connection.close();
      connectionRef.current = null;
    };
  }, [url]);

  /**
   * Hand the session's system-prompt append to the connection.
   *
   * The value is stored on the connection even when the socket is not open yet, so
   * the very first `hello` -- the one that builds the runtime -- already carries
   * it. A later change re-sends `hello`, which the server applies to the live
   * session, so toggling Rich UI takes effect on the next turn.
   */
  React.useEffect(() => {
    appendRef.current = systemPromptAppend;
    connectionRef.current?.setSystemPromptAppend(systemPromptAppend);
  }, [systemPromptAppend]);

  const prompt = React.useCallback((text: string, images: ImageAttachment[]) => {
    // Echo first: the wire has no `user_message` frame, so the transcript would
    // otherwise not show what was asked until the page reloaded.
    setState((prev) => addUserPrompt(prev, text, images));
    connectionRef.current?.sendPrompt(text, images);
  }, []);

  const steer = React.useCallback((text: string) => {
    connectionRef.current?.sendSteer(text);
  }, []);

  const cancel = React.useCallback(() => {
    connectionRef.current?.sendCancel();
  }, []);

  const runCommand = React.useCallback((name: string, arg?: string) => {
    connectionRef.current?.sendCommand(name, arg);
  }, []);

  const newSession = React.useCallback(() => {
    setState((prev) => ({ ...initialState, session: prev.session }));
    connectionRef.current?.sendNewSession();
  }, []);

  const setFormState = React.useCallback(
    (id: string, formState: Record<string, unknown>) => {
      setState((prev) => setRowFormState(prev, id, formState));
    },
    [],
  );

  return { state, status, prompt, steer, cancel, runCommand, newSession, setFormState };
}