export interface DashboardTotals {
  total: number;
  queued: number;
  running: number;
  completed: number;
  failed: number;
  backlog: number;
  contexts: number;
  active_leases: number;
}

export interface DashboardPluginSummary {
  plugin_id: string;
  total: number;
  queued: number;
  running: number;
  completed: number;
  failed: number;
  last_updated_at: number | null;
}

export interface DashboardTaskItem {
  task_id: string;
  dedupe_key: string | null;
  plugin_id: string;
  action: string;
  status: string;
  context_key: string;
  title: string;
  source: string | null;
  thread_id: string | null;
  turn_id: string | null;
  error: string | null;
  created_at: number;
  updated_at: number;
  age_seconds: number;
}

export interface DashboardSnapshot {
  generated_at: number;
  totals: DashboardTotals;
  status_counts: Record<string, number>;
  action_counts: Record<string, number>;
  plugins: DashboardPluginSummary[];
  backlog: DashboardTaskItem[];
  recent: DashboardTaskItem[];
}
