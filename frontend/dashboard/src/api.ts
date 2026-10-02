import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { createApi } from './api-client';

export interface Page<T> {
  items: T[];
  total?: number;
  has_more: boolean;
}

export function backendLabel(backend: string): string {
  return ({ codex: 'Codex', claude: 'Claude Code' } as Record<string, string>)[backend] ?? backend;
}
export interface Diagnostic {
  timestamp: string;
  level: string;
  target: string | null;
  message: string;
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
