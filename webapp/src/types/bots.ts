export type WatcherType = "cron" | "github_poll" | "webhook";

export interface GitHubPollConfig {
  repo: string;
  events: string[];
  interval_seconds: number;
}

export interface CronConfig {
  expression: string;
  timezone: string;
}

export interface WebhookConfig {
  secret: string;
  event_filter: string;
}

export interface BotPermissionPolicy {
  auto_allow: string[];
  require_approval: string[];
  grace_seconds: number;
}

export interface BotConfig {
  id: string;
  name: string;
  watcher_type: WatcherType;
  instruction: string;
  cwd?: string | null;
  enabled: boolean;
  use_worktree: boolean;
  max_queue_depth: number;
  github?: GitHubPollConfig | null;
  cron?: CronConfig | null;
  webhook?: WebhookConfig | null;
  permissions: BotPermissionPolicy;
  created_at?: number;
  updated_at?: number;
}

export type BotFormValues = Omit<BotConfig, "id" | "created_at" | "updated_at"> & {
  id?: string;
};

export type BotRunStatus = "running" | "ok" | "waiting_permission" | "error" | "cancelled";

export interface BotRunRecord {
  run_id: string;
  bot_id: string;
  bot_name: string;
  trigger_id: string;
  event_key: string;
  source: string;
  status: BotRunStatus | string;
  started_at: number;
  finished_at: number | null;
  duration_ms: number | null;
  session_id: string | null;
  error: string | null;
  summary?: string | null;
}

export interface DeletedBotRecord {
  bot: BotConfig;
  deleted_at: number;
}

export interface BotsHistoryResponse {
  runs: BotRunRecord[];
  deletedBots: DeletedBotRecord[];
}
