import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { TranscriptChanges, TranscriptEntry, TranscriptWindow } from './api-types';
import {
  get,
  query,
  useResource,
  type Navigate,
  type Page,
  type SearchHit,
  type SessionDetail,
} from './api';
import { ContentBlock, Copy, Entry, Status } from './Entry';
import { Time } from './Time';
import {
  applyChanges,
  type TranscriptState,
  applyWindow,
  readAnchor,
  restoreAnchor,
  type ScrollAnchor,
} from './transcript-model';

const readingPositions = new Map<string, ScrollAnchor>();

export function Transcript({
  session,
  selection,
  navigate,
  live,
  refresh,
}: {
  session: string;
  selection: URLSearchParams;
  navigate: Navigate;
  live: boolean;
  refresh: number;
}) {
  const base = `/api/sessions/${session}`;
  const detail = useResource<SessionDetail>(base, live, refresh);
  const [{ entries, unread, bounds: window }, setTranscript] = useState<TranscriptState>({
    entries: [],
    unread: new Set(),
    bounds: null,
  });
  const [error, setError] = useState('');
  const [received, setReceived] = useState('');
  const [busy, setBusy] = useState(true);
  const [follow, setFollow] = useState(!selection.has('entry') && !readingPositions.has(session));
  const [kind, setKind] = useState('');
  const [search, setSearch] = useState('');
  const [searchQuery, setSearchQuery] = useState('');
  const [searchOffset, setSearchOffset] = useState(0);
  const container = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const loadingHistory = useRef(false);
  const anchor = useRef<ScrollAnchor | null>(readingPositions.get(session) ?? null);
  const returnAnchor = useRef<ScrollAnchor | null>(null);
  const following = useRef(follow);
  following.current = follow;
  const cursor = useRef<string | null>(null);
  const generation = window?.generation;
  const historyRequest = useRef(0);
  const active = useRef(true);
  const selected = selection.get('entry');
  const searchResults = useResource<Page<SearchHit>>(
    searchQuery ? query(`${base}/search`, { q: searchQuery, offset: searchOffset, kind }) : null,
    false,
    refresh,
  );
  const inspected = useResource<TranscriptEntry>(
    selected ? `${base}/entries/${selected}` : null,
    live,
    refresh,
    1000,
  );
  async function loadWindow(
    params: Record<string, string | null> = {},
    initial = false,
    position?: ScrollAnchor,
  ) {
    const request = ++historyRequest.current;
    loadingHistory.current = true;
    setBusy(true);
    try {
      const value = await get<TranscriptWindow>(query(`${base}/transcript`, params));
      if (!active.current || request !== historyRequest.current) return;
      if (initial) {
        cursor.current = value.change_cursor;
      } else if (generation !== value.generation)
        throw new Error('Transcript generation changed. Reload this session.');
      if (container.current && !following.current) anchor.current = readAnchor(container.current);
      setTranscript((current) =>
        applyWindow(
          current,
          value,
          params.before ? 'older' : params.after_window ? 'newer' : 'replace',
        ),
      );
      setReceived(value.generated_at);
      setError('');
      if (params.around) anchor.current = position ?? { entryId: params.around, offset: 12 };
    } catch (error) {
      if (active.current && request === historyRequest.current) setError(String(error));
    } finally {
      if (active.current && request === historyRequest.current) {
        loadingHistory.current = false;
        setBusy(false);
      }
    }
  }

  useEffect(() => {
    active.current = true;
    const target = selected ?? readingPositions.get(session)?.entryId ?? null;
    void loadWindow({ around: target }, true);
    return () => {
      active.current = false;
      historyRequest.current += 1;
      if (anchor.current) readingPositions.set(session, anchor.current);
      if (readingPositions.size > 30)
        readingPositions.delete(readingPositions.keys().next().value!);
    };
  }, [session]);

  function loadMore(direction: 'older' | 'newer') {
    if (loadingHistory.current || !window) return;
    const value = direction === 'older' ? window.before_cursor : window.after_window_cursor;
    if (!value) return;
    following.current = false;
    setFollow(false);
    void loadWindow(direction === 'older' ? { before: value } : { after_window: value });
  }

  const previousRefresh = useRef(refresh);
  useEffect(() => {
    if (!cursor.current) return;
    let stopped = false;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const explicit = previousRefresh.current !== refresh;
    previousRefresh.current = refresh;
    async function poll() {
      try {
        let more: boolean;
        do {
          const value = await get<TranscriptChanges>(
            query(`${base}/transcript`, { after: cursor.current }),
            controller.signal,
          );
          if (stopped) return;
          if (value.generation !== generation)
            throw new Error('Transcript generation changed. Reload this session.');
          const upserts = value.changes.flatMap((change) => change.upserts);
          if (container.current && !following.current)
            anchor.current = readAnchor(container.current);
          const shouldFollow = following.current && !document.getSelection()?.toString();
          setTranscript((current) => applyChanges(current, upserts, shouldFollow));
          cursor.current = value.next_cursor;
          setReceived(value.generated_at);
          setError('');
          more = value.has_more;
        } while (more && !stopped);
      } catch (error) {
        if (!stopped) setError(String(error));
      } finally {
        if (!stopped && live) timer = setTimeout(() => void poll(), document.hidden ? 5000 : 1000);
      }
    }
    if (live || explicit) void poll();
    return () => {
      stopped = true;
      controller.abort();
      clearTimeout(timer);
    };
  }, [session, live, refresh, generation]);

  useEffect(() => {
    if (!window) return;
    if (!selected) {
      const position = returnAnchor.current;
      returnAnchor.current = null;
      if (position) {
        anchor.current = position;
        if (entries.some((entry) => entry.entry_id === position.entryId)) {
          if (container.current) restoreAnchor(container.current, position);
        } else void loadWindow({ around: position.entryId }, false, position);
      }
      return;
    }
    if (!returnAnchor.current && container.current)
      returnAnchor.current = readAnchor(container.current);
    setFollow(false);
    setKind('');
    if (!entries.some((entry) => entry.entry_id === selected))
      void loadWindow({ around: selected });
    else {
      anchor.current = { entryId: selected, offset: 12 };
      if (container.current) restoreAnchor(container.current, anchor.current);
    }
  }, [selected, window !== null]);

  useLayoutEffect(() => {
    const list = container.current;
    const body = content.current;
    if (!list || !body) return;
    const restore = () => {
      if (following.current && !document.getSelection()?.toString())
        list.scrollTop = list.scrollHeight;
      else if (anchor.current) restoreAnchor(list, anchor.current);
    };
    restore();
    const observer = new ResizeObserver(restore);
    observer.observe(body);
    observer.observe(list);
    return () => observer.disconnect();
  }, [entries]);

  useEffect(() => {
    if (!selected) return;
    const previous = document.activeElement as HTMLElement | null;
    const close = document.getElementById('close-inspector');
    close?.focus({ preventScroll: true });
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape')
        navigate({ entry: null, block: null, content: null, offset: null });
    };
    document.addEventListener('keydown', key);
    return () => {
      document.removeEventListener('keydown', key);
      previous?.focus({ preventScroll: true });
    };
  }, [selected]);

  function latest() {
    returnAnchor.current = null;
    setFollow(true);
    following.current = true;
    setTranscript((current) => ({ ...current, unread: new Set() }));
    navigate({ entry: null, task: null, block: null, content: null, offset: null });
    void loadWindow();
  }
  const visible = entries.filter((entry) => !kind || entry.kind === kind);
  const focus =
    selection.has('block') && selection.has('content')
      ? {
          block: selection.get('block')!,
          ref: selection.get('content')!,
          offset: Number(selection.get('offset') ?? '0'),
        }
      : undefined;
  return (
    <section className="session-workspace">
      <header className="session-heading">
        <div>
          <span className="eyebrow">SESSION TRANSCRIPT</span>
          <h1>{detail.data?.title ?? 'Loading session…'}</h1>
        </div>
        {detail.data && (
          <Status state={detail.data.execution_uncertain ? 'unconfirmed' : detail.data.state} />
        )}
      </header>
      <dl className="session-metadata">
        <div>
          <dt>Codex session ID</dt>
          <dd>
            <code>{session}</code>
            <Copy text={session} label="Copy session ID" />
          </dd>
        </div>
        <div>
          <dt>Context key</dt>
          <dd>
            <button
              className="quiet"
              onClick={() => navigate({ context: detail.data?.context_key ?? null })}
            >
              <code>{detail.data?.context_key ?? 'Loading…'}</code>
            </button>
          </dd>
        </div>
        {detail.data?.codex && (
          <>
            <div>
              <dt>Model</dt>
              <dd>
                {detail.data.codex.model ?? 'Unavailable'}{' '}
                <span className="subtle">{detail.data.codex.reasoning_effort}</span>
              </dd>
            </div>
            <div>
              <dt>Workspace</dt>
              <dd>
                <code>{detail.data.codex.cwd ?? 'Unavailable'}</code>
              </dd>
            </div>
            <div>
              <dt>Created</dt>
              <dd>
                <Time value={detail.data.codex.created_at} />
              </dd>
            </div>
            <div>
              <dt>Updated</dt>
              <dd>
                <Time value={detail.data.codex.updated_at} />
              </dd>
            </div>
          </>
        )}
      </dl>
      {detail.error && (
        <p className="notice error" role="alert">
          {detail.error}
        </p>
      )}

      {detail.data?.execution_uncertain && (
        <p className="notice">
          No active context lease. Execution status cannot be confirmed; unfinished tools have no
          recorded ending.
        </p>
      )}
      <div className="transcript-toolbar">
        <select
          aria-label="Go to task or turn"
          value={selection.get('task') ?? ''}
          onChange={(event) => {
            const task = event.target.value;
            const entry = entries.find((item) => item.task_id === task);
            if (entry) navigate({ task, entry: entry.entry_id });
            else
              void get<{ entry_id: string | null }>(`/api/tasks/${encodeURIComponent(task)}`)
                .then((target) => navigate({ task, entry: target.entry_id }))
                .catch((error) => setError(String(error)));
          }}
        >
          <option value="">Task / turn…</option>
          {detail.data?.tasks.map((task) => (
            <option key={task.task_id} value={task.task_id}>
              {task.title}
            </option>
          ))}
        </select>
        <select
          aria-label="Filter content"
          value={kind}
          onChange={(event) => setKind(event.target.value)}
        >
          <option value="">All content</option>
          {[
            'input',
            'message',
            'tool',
            'file_change',
            'reasoning',
            'approval',
            'runtime',
            'unknown',
          ].map((value) => (
            <option key={value}>{value}</option>
          ))}
        </select>
        <button aria-pressed={follow} onClick={() => (follow ? setFollow(false) : latest())}>
          {follow ? 'Following latest' : 'Jump to latest ↓'}
        </button>
        <button
          onClick={() =>
            void get<Page<SearchHit>>(query(`${base}/search`, { errors: true, limit: 1 }))
              .then((result) => {
                const hit = result.items[0];
                if (hit) {
                  setKind('');
                  navigate({
                    entry: hit.entry_id,
                    block: hit.block_id,
                    content: hit.content_ref,
                    offset: String(hit.offset),
                  });
                } else setError('No recorded errors in this session.');
              })
              .catch((error) => setError(String(error)))
          }
        >
          Recent error
        </button>
        <a href={`${base}/export?format=markdown`} download>
          Markdown ↓
        </a>
        <a href={`${base}/export?format=jsonl`} download>
          JSONL ↓
        </a>
      </div>
      <form
        className="transcript-search"
        onSubmit={(event) => {
          event.preventDefault();
          setSearchOffset(0);
          setSearchQuery(search);
          setFollow(false);
        }}
      >
        <input
          aria-label="Search complete session"
          placeholder="Search all saved input, messages and tool output…"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <button type="submit">Search</button>
        {searchQuery && (
          <button
            type="button"
            onClick={() => {
              setSearchQuery('');
              setSearch('');
            }}
          >
            Clear
          </button>
        )}
        <span className="subtle">
          {received ? `Read ${new Date(received).toLocaleTimeString()}` : 'Loading…'}
        </span>
      </form>
      {searchQuery && (
        <div className="search-results">
          {searchResults.error && <p role="alert">{searchResults.error}</p>}
          {searchResults.data?.items.map((hit) => (
            <button
              key={`${hit.entry_id}:${hit.block_id}`}
              onClick={() =>
                navigate({
                  entry: hit.entry_id,
                  block: hit.block_id,
                  content: hit.content_ref,
                  offset: String(hit.offset),
                })
              }
            >
              <strong>{hit.title}</strong>
              <span>{hit.snippet}</span>
            </button>
          ))}
          {searchResults.data?.items.length === 0 && <p>No matches in saved content.</p>}
          {searchResults.data?.has_more && (
            <button onClick={() => setSearchOffset(searchOffset + 50)}>Next results</button>
          )}
        </div>
      )}
      {error && (
        <p className="notice error" role="alert">
          {error}{' '}
          <button onClick={() => void loadWindow({ around: selected }, true)}>
            Reload session
          </button>
        </p>
      )}
      <div className={`reading-layout ${selected ? 'with-inspector' : ''}`}>
        <div
          className="transcript-scroll"
          ref={container}
          tabIndex={0}
          aria-label="Session transcript"
          onWheel={(event) => {
            if (event.deltaY < 0) {
              following.current = false;
              setFollow(false);
            }
          }}
          onPointerDown={() => setFollow(false)}
          onScroll={(event) => {
            const list = event.currentTarget;
            anchor.current = readAnchor(list);
            if (following.current || loadingHistory.current || kind) return;
            if (list.scrollTop < 80 && window?.has_older) loadMore('older');
            else if (
              list.scrollHeight - list.scrollTop - list.clientHeight < 80 &&
              window?.has_newer
            )
              loadMore('newer');
          }}
        >
          <div className="transcript-content" ref={content}>
            {window?.has_older && (
              <button className="page-control" disabled={busy} onClick={() => loadMore('older')}>
                {busy ? 'Loading…' : '↑ Load earlier entries'}
              </button>
            )}
            {!window && busy && <p className="empty">Loading transcript…</p>}
            {window && entries.length === 0 && (
              <p className="empty">Codex has no conversation items for this session.</p>
            )}
            {visible.map((entry, index) => (
              <div key={entry.entry_id}>
                {(index === 0 || visible[index - 1]?.turn_id !== entry.turn_id) && (
                  <div className="turn-divider">
                    <span>
                      {detail.data?.tasks.find((task) => task.task_id === entry.task_id)?.title ??
                        entry.task_id ??
                        'Codex turn'}
                    </span>
                    <code>{entry.turn_id ?? 'Turn ID not recorded'}</code>
                  </div>
                )}
                <Entry
                  entry={entry}
                  navigate={navigate}
                  selected={selected === entry.entry_id}
                  expand={selected === entry.entry_id}
                  focus={selected === entry.entry_id ? focus : undefined}
                />
              </div>
            ))}
            {window?.has_newer && (
              <button className="page-control" disabled={busy} onClick={() => loadMore('newer')}>
                {busy ? 'Loading…' : 'Load later entries ↓'}
              </button>
            )}
          </div>
        </div>
        {selected && (
          <aside className="inspector" role="dialog" aria-label="Entry details">
            <div className="inspector-heading">
              <h2>Entry details</h2>
              <button
                id="close-inspector"
                onClick={() => navigate({ entry: null, block: null, content: null, offset: null })}
              >
                Close ×
              </button>
            </div>
            {inspected.error && <p className="notice error">{inspected.error}</p>}
            {inspected.data && (
              <>
                <dl>
                  {Object.entries({
                    Entry: selected,
                    Task: inspected.data.task_id,
                    Thread: inspected.data.thread_id,
                    Turn: inspected.data.turn_id,
                    Source: inspected.data.source.origin,
                    Started: inspected.data.started_at,
                    Completed: inspected.data.completed_at,
                    Recorded: inspected.data.recorded_at,
                    Revision: inspected.data.revision_seq,
                  }).map(([key, value]) => (
                    <div key={key}>
                      <dt>{key}</dt>
                      <dd>
                        <code>{value ?? 'Not recorded'}</code>
                      </dd>
                    </div>
                  ))}
                </dl>
                <Copy text={location.href} label="Copy link" />{' '}
                <Copy text={JSON.stringify(inspected.data, null, 2)} label="Copy entry JSON" />
                {inspected.data.blocks.map((block) => (
                  <details key={block.block_id}>
                    <summary>{block.block_id}</summary>
                    <ContentBlock block={block} session={session} />
                  </details>
                ))}
              </>
            )}
          </aside>
        )}
      </div>
      {unread.size > 0 && !follow && (
        <button className="new-content" onClick={latest} aria-live="polite">
          {unread.size} updated entries · Jump to latest ↓
        </button>
      )}
    </section>
  );
}
