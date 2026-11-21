import * as React from "react";
import {
  Connection,
  resolveSocketUrl,
  type ConnectionStatus,
} from "./lib/connection";
import { addUserPrompt, initialState, reduce, type TranscriptState } from "./lib/transcript";
import type { ImageAttachment } from "./protocol";

export interface QuartzApi {
  state: TranscriptState;
  status: ConnectionStatus;
  prompt: (text: string, images: ImageAttachment[]) => void;
  steer: (text: string) => void;
  cancel: () => void;
  runCommand: (name: string, arg?: string) => void;
  newSession: () => void;
}

/**
 * The single point where the transport meets the reducer.
 *
 * Everything below is pure: this hook owns the one `Connection`, folds its frames
 * into transcript state, and exposes intent-level actions. Components never see a
 * socket or a protocol message.
 */
export function useQuartz(url?: string): QuartzApi {
  const [state, setState] = React.useState<TranscriptState>(initialState);
  const [status, setStatus] = React.useState<ConnectionStatus>("disconnected");

  const connectionRef = React.useRef<Connection | null>(null);

  React.useEffect(() => {
    const target = url ?? resolveSocketUrl(window.location);
    const connection = new Connection(target);
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

  return { state, status, prompt, steer, cancel, runCommand, newSession };
}
