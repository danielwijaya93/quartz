import * as React from "react";
import {
  Connection,
  resolveSocketUrl,
  type ConnectionStatus,
} from "./lib/connection";
import {
  addUserPrompt,
  clearBgFailed as clearBgFailedState,
  initialState,
  reduce,
  setRowFormState,
  type TranscriptState,
} from "./lib/transcript";
import type { ImageAttachment, PermissionDecision, ServerMessage } from "./protocol";

export interface QuartzApi {
  state: TranscriptState;
  status: ConnectionStatus;
  prompt: (text: string, images: ImageAttachment[]) => void;
  steer: (text: string) => void;
  cancel: () => void;
  runCommand: (name: string, arg?: string) => void;
  /** Start a fresh session. `cwd` is the folder the sidebar picked, if any. */
  newSession: (cwd?: string) => void;
  /** Resume a session from the sidebar. Its directory comes from its own log. */
  loadSession: (sessionId: string) => void;
  /** Persist trust for the active session's folder and reload it. */
  trustProject: () => void;
  /** Toggle OS sandbox for the active session. */
  setSandbox: (enabled: boolean) => void;
  respondPermission: (requestId: string, decision: PermissionDecision) => void;
  /** Keep alive or close an idle backgrounded session. */
  respondIdleSession: (sessionId: string, action: "keep" | "close") => void;
  /** Persist an OpenUI surface's form state onto the row that rendered it. */
  setFormState: (id: string, formState: Record<string, unknown>) => void;
  /** Clear the failed background job badge once the user opens /jobs. */
  clearBgFailed: () => void;
  /** Dismiss a non-permission item from Active Sessions. */
  dismissActiveSession: (sessionId: string) => void;
  /** Clear all read non-permission items from Active Sessions. */
  clearReadActiveSessions: () => void;
  /** Tell other connected devices that notifications were read. */
  markNotificationsRead: () => void;
}

/**
 * The single point where the transport meets the reducer.
 *
 * Everything below is pure: this hook owns the one `Connection`, folds its frames
 * into transcript state, and exposes intent-level actions. Components never see a
 * socket or a protocol message.
 */
export interface UseQuartzOptions {
  /** Bind this connection directly to a specific session on initial `hello`. */
  initialSessionId?: string | null;
  /** When set (including ""), start a fresh session in this cwd on first open instead of attaching to default. */
  createFreshCwd?: string;
}

export function useQuartz(
  url?: string,
  systemPromptAppend = "",
  onServerMessage?: (message: ServerMessage) => void,
  onAuthError?: () => void,
  options?: UseQuartzOptions,
): QuartzApi {
  const [state, setState] = React.useState<TranscriptState>(initialState);
  const [status, setStatus] = React.useState<ConnectionStatus>("disconnected");

  const connectionRef = React.useRef<Connection | null>(null);
  // Read by the connection effect so a re-created connection (url change, remount)
  // inherits the current append instead of starting from an empty one.
  const appendRef = React.useRef(systemPromptAppend);
  const onMessageRef = React.useRef(onServerMessage);
  onMessageRef.current = onServerMessage;
  const onAuthErrorRef = React.useRef(onAuthError);
  onAuthErrorRef.current = onAuthError;
  const initialSessionIdRef = React.useRef(options?.initialSessionId ?? undefined);
  const createFreshCwdRef = React.useRef(options?.createFreshCwd);

  React.useEffect(() => {
    const target = url ?? resolveSocketUrl(window.location);
    const initialSessionId = initialSessionIdRef.current || undefined;
    const connection = new Connection(target, initialSessionId ? { initialSessionId } : undefined);
    connection.setSystemPromptAppend(appendRef.current);
    connectionRef.current = connection;

    let createdFresh = false;
    const offOpen = connection.on("open", () => {
      if (!createdFresh && !initialSessionId && createFreshCwdRef.current !== undefined) {
        createdFresh = true;
        connection.sendNewSession(createFreshCwdRef.current || undefined);
      }
    });
    const offMessage = connection.on("message", (message) => {
      setState((prev) => reduce(prev, message));
      onMessageRef.current?.(message);
    });
    const offStatus = connection.on("status", setStatus);
    const offAuthError = connection.on("authError", () => {
      onAuthErrorRef.current?.();
    });

    connection.connect();

    return () => {
      offOpen();
      offMessage();
      offStatus();
      offAuthError();
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

  const newSession = React.useCallback((cwd?: string) => {
    setState((prev) => ({
      ...initialState,
      session: prev.session,
      ...(prev.activeSessions !== undefined ? { activeSessions: prev.activeSessions } : {}),
      ...(prev.idlePrompts !== undefined ? { idlePrompts: prev.idlePrompts } : {}),
    }));
    connectionRef.current?.sendNewSession(cwd);
  }, []);

  const loadSession = React.useCallback((sessionId: string) => {
    // No local reset here: the reducer retires the old transcript when
    // `session_state` arrives for a different session id, which also covers a
    // session switched from somewhere else.
    connectionRef.current?.sendLoadSession(sessionId);
  }, []);

  const trustProject = React.useCallback(() => {
    connectionRef.current?.sendTrustProject();
  }, []);

  const setSandbox = React.useCallback((enabled: boolean) => {
    connectionRef.current?.sendSetSandbox(enabled);
  }, []);

  const respondPermission = React.useCallback(
    (requestId: string, decision: PermissionDecision) => {
      connectionRef.current?.sendPermissionResponse(requestId, decision);
    },
    [],
  );

  const respondIdleSession = React.useCallback(
    (sessionId: string, action: "keep" | "close") => {
      connectionRef.current?.sendIdleSessionResponse(sessionId, action);
    },
    [],
  );

  const setFormState = React.useCallback(
    (id: string, formState: Record<string, unknown>) => {
      setState((prev) => setRowFormState(prev, id, formState));
    },
    [],
  );

  const clearBgFailed = React.useCallback(() => {
    setState((prev) => clearBgFailedState(prev));
  }, []);

  const dismissActiveSession = React.useCallback((sessionId: string) => {
    connectionRef.current?.sendActiveSessionDismiss(sessionId);
  }, []);

  const clearReadActiveSessions = React.useCallback(() => {
    connectionRef.current?.sendActiveSessionsClearRead();
  }, []);

  const markNotificationsRead = React.useCallback(() => {
    connectionRef.current?.sendNotificationsRead();
  }, []);

  return {
    state,
    status,
    prompt,
    steer,
    cancel,
    runCommand,
    newSession,
    loadSession,
    trustProject,
    setSandbox,
    respondPermission,
    respondIdleSession,
    setFormState,
    clearBgFailed,
    dismissActiveSession,
    clearReadActiveSessions,
    markNotificationsRead,
  };
}