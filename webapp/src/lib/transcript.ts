/**
 * Transcript module barrel — re-exports types, notices, reducer, and grouping.
 */

export type {
  AssistantRow,
  BashNoticeRow,
  CommandRow,
  CompactionRow,
  DiffStats,
  ErrorRow,
  GroupOptions,
  HarnessNotice,
  Row,
  RowKind,
  SessionMeta,
  SteeringRow,
  SystemNoticeRow,
  ThinkingRow,
  ToolRow,
  TranscriptItem,
  TranscriptState,
  Turn,
  TurnEndRow,
  TurnSegment,
  TurnStep,
  TurnWorkStep,
  UserRow,
} from "./transcript/types";

export { EMPTY_SESSION, initialState } from "./transcript/types";

export { parseHarnessNotice } from "./transcript/notices";

export {
  addUserPrompt,
  clearBgFailed,
  reduce,
  reduceAll,
  resetIdCounter,
  setRowFormState,
} from "./transcript/reducer";

export {
  approxTokens,
  failedToolCount,
  firstLine,
  groupTurns,
  isTurn,
  isUnifiedDiff,
  lastUserRowId,
  lineCount,
  parseDiffStats,
  sameConversation,
  segmentTurn,
  stepLabel,
  summarizeWorkSteps,
  toolName,
  toolRunLabel,
  trayOwnsStatus,
  turnDiffStats,
  turnSummary,
} from "./transcript/grouping";
