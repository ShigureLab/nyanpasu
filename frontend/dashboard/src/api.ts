import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { createApi } from './api-client';
import type { Coverage } from './api-types';

export interface Page<T> {
  items: T[];
  total?: number;
  has_more: boolean;
}

export function backendLabel(backend: string): string {
  return ({ codex: 'Codex', claude: 'Claude Code' } as Record<string, string>)[backend] ?? backend;
}
export interface Session {
  session_id: string;
  context_key: string;
  title: string;
  thread_id: string | null;
  state: string;
  backend: string;
  created_at: string;
  updated_at: string;
  task_count: number;
  spawned_by_task_id: string | null;
  execution_uncertain: boolean;
  coverage: Coverage;
  origin: string;
  previous_session_id: string | null;
}
export interface Turn {
  task_id: string;
  turn_id: string | null;
  title: string;
  state: string;
  cwd: string | null;
  revision: string | null;
  created_at: string;
  ended_at: string | null;
}
export interface SessionDetail extends Session {
  tasks: Turn[];
  task_count: number;
  has_more_tasks: boolean;
  runtime: {
    id: string;
    cwd: string | null;
    model: string | null;
    provider: string | null;
    reasoning_effort: string | null;
    cli_version: string | null;
    created_at: string | null;
    updated_at: string | null;
  } | null;
  history_error?: string;
}
export interface Task {
  task_id: string;
  context_key: string;
  title: string;
  status: string;
  action: string;
  session_id: string | null;
  coalesced_into: string | null;
  spawned_by_task_id: string | null;
  context_generation: number;
  error: string | null;
  plugin_id: string;
  created_at: number;
  updated_at: number;
}
export interface TaskDetail extends Omit<Task, 'title' | 'plugin_id'> {
  entry_id: string | null;
  turn_id: string | null;
  event_worktree: string | null;
  task: unknown;
  lifecycle: string;
  waiting_for: string[];
  children: Array<{ task_id: string; status: string; context_key: string }>;
  subtask_result: {
    summary: string;
    artifacts: Array<{ name: string; sha256: string; bytes: number }>;
    data: Record<string, unknown>;
  } | null;
  history_error?: string;
}
export interface Diagnostic {
  timestamp: string;
  level: string;
  target: string | null;
  message: string;
}
export interface SearchHit {
  entry_id: string;
  task_id: string;
  title: string;
  snippet: string;
  block_id: string;
  content_ref: string;
  offset: number;
}
export interface ContentPage {
  text: string;
  content_ref: string;
  offset: number;
  next_offset: number | null;
  recorded_bytes: number;
}

export const ApiContext = createContext(createApi(''));
export const useApi = () => useContext(ApiContext);

export function query(
  path: string,
  params: Record<string, string | number | boolean | null | undefined>,
): string {
  const values = new URLSearchParams();
  for (const [key, value] of Object.entries(params))
    if (value !== null && value !== undefined && value !== '') values.set(key, String(value));
  return `${path}?${values}`;
}

export function useResource<T>(
  path: string | null,
  live: boolean,
  refresh: number,
  interval = 5000,
) {
  const { get } = useApi();
  const [state, setState] = useState<{
    key: string | null;
    data?: T;
    error?: string;
    received?: number;
    loading: boolean;
  }>({ key: null, loading: true });
  const previous = useRef({ path: null as string | null, refresh: -1 });
  useEffect(() => {
    if (!path) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();
    const shouldLoad =
      live ||
      state.loading ||
      path !== previous.current.path ||
      refresh !== previous.current.refresh;
    previous.current = { path, refresh };
    async function load() {
      setState((old) =>
        old.key === path ? { ...old, loading: true } : { key: path, loading: true },
      );
      try {
        const data = await get<T>(path!, controller.signal);
        if (!stopped) setState({ key: path, data, received: Date.now(), loading: false });
      } catch (error) {
        if (!stopped) setState((old) => ({ ...old, error: String(error), loading: false }));
      } finally {
        if (!stopped && live)
          timer = setTimeout(() => void load(), document.hidden ? interval * 5 : interval);
      }
    }
    if (shouldLoad) void load();
    return () => {
      stopped = true;
      controller.abort();
      clearTimeout(timer);
    };
  }, [get, path, live, refresh, interval]);
  return state.key === path
    ? state
    : { key: path, loading: true, data: undefined, error: undefined, received: undefined };
}

export function useNavigation() {
  const [selection, setSelection] = useState(() => new URLSearchParams(location.search));
  useEffect(() => {
    const change = () => setSelection(new URLSearchParams(location.search));
    window.addEventListener('popstate', change);
    return () => window.removeEventListener('popstate', change);
  }, []);
  const navigate = useCallback((values: Record<string, string | null>, replace = false) => {
    const params = new URLSearchParams(location.search);
    for (const [key, value] of Object.entries(values)) {
      if (value === null) params.delete(key);
      else params.set(key, value);
    }
    window.history[replace ? 'replaceState' : 'pushState'](
      null,
      '',
      `${location.pathname}?${params}`,
    );
    setSelection(params);
  }, []);
  return { selection, navigate };
}
export type Navigate = ReturnType<typeof useNavigation>['navigate'];
