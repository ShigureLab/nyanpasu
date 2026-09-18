import { useCallback, useEffect, useRef, useState } from 'react';
import { useApi, type Page, type Session } from './api';

const PAGE_SIZE = 50;

export function useSessions(path: string, live: boolean, refresh: number) {
  const { get } = useApi();
  const [range, setRange] = useState({ path, pages: 1 });
  const pages = range.path === path ? range.pages : 1;
  const [state, setState] = useState<{
    path: string;
    pages: number;
    data?: Page<Session>;
    error?: string;
    loading: boolean;
  }>({ path, pages, loading: true });
  const previous = useRef({ path: '', pages: 0, refresh: -1 });

  useEffect(() => {
    setRange((current) => (current.path === path ? current : { path, pages: 1 }));
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();
    const shouldLoad =
      live ||
      state.loading ||
      path !== previous.current.path ||
      pages !== previous.current.pages ||
      refresh !== previous.current.refresh;
    previous.current = { path, pages, refresh };
    async function load() {
      setState((old) => ({
        path,
        pages,
        data: old.path === path ? old.data : undefined,
        loading: true,
      }));
      try {
        const items = new Map<string, Session>();
        let page: Page<Session>;
        // Refresh the loaded range together: live updates can move sessions across page boundaries.
        for (let index = 0; index < pages; index++) {
          page = await get<Page<Session>>(
            `${path}&offset=${index * PAGE_SIZE}&limit=${PAGE_SIZE}`,
            controller.signal,
          );
          for (const item of page.items) items.set(item.session_id, item);
          if (!page.has_more) break;
        }
        if (!stopped)
          setState({ path, pages, data: { ...page!, items: [...items.values()] }, loading: false });
      } catch (error) {
        if (!stopped) setState((old) => ({ ...old, error: String(error), loading: false }));
      } finally {
        if (!stopped && live) timer = setTimeout(() => void load(), document.hidden ? 25000 : 5000);
      }
    }
    if (shouldLoad) void load();
    return () => {
      stopped = true;
      controller.abort();
      clearTimeout(timer);
    };
  }, [get, path, pages, live, refresh]);

  const data = state.path === path ? state.data : undefined;
  const loading = state.path !== path || state.pages !== pages || state.loading;
  const error = state.path === path ? state.error : undefined;
  const loadMore = useCallback(() => {
    if (!loading && !error && data?.has_more) setRange({ path, pages: pages + 1 });
  }, [path, pages, data?.has_more, loading, error]);
  return { data, loading, error, loadMore };
}
