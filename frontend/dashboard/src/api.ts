import { useEffect, useRef, useState } from 'react';
import type { Coverage } from './api-types';

export interface Page<T> {
  items: T[];
  total?: number;
  has_more: boolean;
}
export interface Session {
  session_id: string;
  context_key: string;
  title: string;
  thread_id: string | null;
  state: string;
  backend: string;
  updated_at: string;
  entry_count: number;
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
  started_at: string;
  ended_at: string | null;
}
export interface SessionDetail extends Session {
  tasks: Turn[];
  task_count: number;
  has_more_tasks: boolean;
}
export interface Task {
  task_id: string;
  context_key: string;
  title: string;
  status: string;
  action: string;
  session_id: string | null;
  coalesced_into: string | null;
  error: string | null;
  plugin_id: string;
  updated_at: number;
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
export interface EventRecord {
  seq: string;
  type: string;
  direction: string;
  observed_at: string;
  entry_id: string;
  content_ref: string;
  preview: string;
}
export interface EventPage extends Page<EventRecord> {
  next_cursor: string;
}
export interface ContentPage {
  text: string;
  content_ref: string;
  offset: number;
  next_offset: number | null;
  recorded_bytes: number;
}

export async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { signal, cache: 'no-store' });
  if (!response.ok) {
    const message = (await response.json().catch(() => ({ detail: response.statusText }))) as {
      detail?: unknown;
    };
    throw new Error(`${response.status}: ${String(message.detail ?? response.statusText)}`);
  }
  return response.json() as Promise<T>;
}

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
      live || path !== previous.current.path || refresh !== previous.current.refresh;
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
  }, [path, live, refresh, interval]);
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
  function navigate(values: Record<string, string | null>, replace = false) {
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
  }
  return { selection, navigate };
}
export type Navigate = ReturnType<typeof useNavigation>['navigate'];
