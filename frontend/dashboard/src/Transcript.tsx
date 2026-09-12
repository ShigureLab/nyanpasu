import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { TranscriptChanges, TranscriptEntry, TranscriptWindow } from './api-types';
import {
  get,
  query,
  useResource,
  type EventPage,
  type Navigate,
  type Page,
  type SearchHit,
  type SessionDetail,
} from './api';
import { ContentBlock, Copy, Entry, Status } from './Entry';
import {
  applyChanges,
  type TranscriptState,
  mergeEntries,
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
  const [{ entries, unread, reloadWindow }, setTranscript] = useState<TranscriptState>({
    entries: [],
    unread: new Set(),
    reloadWindow: false,
  });
  const [window, setWindow] = useState<TranscriptWindow | null>(null);
  const [error, setError] = useState('');
  const [received, setReceived] = useState('');
  const [busy, setBusy] = useState(true);
  const [follow, setFollow] = useState(!selection.has('entry') && !readingPositions.has(session));
  const [kind, setKind] = useState('');
  const [search, setSearch] = useState('');
  const [searchQuery, setSearchQuery] = useState('');
  const [searchOffset, setSearchOffset] = useState(0);
  const [historyVersion, setHistoryVersion] = useState(0);
  const container = useRef<HTMLDivElement>(null);
  const anchor = useRef<ScrollAnchor | null>(readingPositions.get(session) ?? null);
  const returnAnchor = useRef<ScrollAnchor | null>(null);
  const following = useRef(follow);
  following.current = follow;
  const cursor = useRef<string | null>(null);
  const generation = window?.generation;
  const historyRequest = useRef(0);
  const active = useRef(true);
  const selected = selection.get('entry');
  const mode = selection.get('mode') ?? 'conversation';
  const searchResults = useResource<Page<SearchHit>>(
    searchQuery && mode === 'conversation'
      ? query(`${base}/search`, { q: searchQuery, offset: searchOffset, kind })
      : null,
    false,
    refresh,
  );
  const inspected = useResource<TranscriptEntry>(
    selected ? `${base}/entries/${selected}` : null,
    live,
    refresh,
    1000,
  );
  const [eventCursor, setEventCursor] = useState<string | null>(null);
  const eventPage = useResource<EventPage>(
    mode === 'events'
      ? query(`${base}/events`, {
          after: eventCursor,
          around: eventCursor ? null : selection.get('event'),
          q: searchQuery,
        })
      : null,
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
    setBusy(true);
    try {
      const value = await get<TranscriptWindow>(query(`${base}/transcript`, params));
      if (!active.current || request !== historyRequest.current) return;
      if (initial) {
        cursor.current = value.change_cursor;
      } else if (generation !== value.generation)
        throw new Error('Transcript generation changed. Reload this session.');
      if (container.current && !following.current) anchor.current = readAnchor(container.current);
      setTranscript((current) => ({
        entries:
          initial && generation !== value.generation
            ? value.entries
            : mergeEntries(
                value.entries,
                current.entries.filter((entry) =>
                  value.entries.some((loaded) => loaded.entry_id === entry.entry_id),
                ),
              ),
        unread: current.unread,
        reloadWindow: false,
      }));
      setWindow(value);
      setHistoryVersion((version) => version + 1);
      setReceived(value.generated_at);
      setError('');
      if (params.around) anchor.current = position ?? { entryId: params.around, offset: 12 };
    } catch (error) {
      if (active.current && request === historyRequest.current) setError(String(error));
    } finally {
      if (active.current && request === historyRequest.current) setBusy(false);
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

  useEffect(() => {
    if (reloadWindow) void loadWindow();
  }, [reloadWindow]);

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
    if (!container.current) return;
    if (following.current) container.current.scrollTop = container.current.scrollHeight;
    else if (anchor.current) restoreAnchor(container.current, anchor.current);
  }, [entries, historyVersion]);

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
          <p>
            <button
              className="quiet"
              onClick={() => navigate({ context: detail.data?.context_key ?? null })}
            >
              <code>{detail.data?.context_key ?? session}</code>
            </button>{' '}
            <span>· {detail.data?.backend}</span>
          </p>
        </div>
        {detail.data && (
          <Status state={detail.data.execution_uncertain ? 'unconfirmed' : detail.data.state} />
        )}
      </header>
      {detail.data?.previous_session_id && (
        <button
          className="quiet"
          onClick={() => navigate({ session: detail.data!.previous_session_id, entry: null })}
        >
          ← Previous session in this context
        </button>
      )}
      {detail.error && (
        <p className="notice error" role="alert">
          {detail.error}
        </p>
      )}
      {detail.data?.coverage.capture_gap && (
        <p className="notice">History coverage: {detail.data.coverage.missing_parts.join(' · ')}</p>
      )}
      {detail.data?.execution_uncertain && (
        <p className="notice">
          No active context lease. Execution status cannot be confirmed; unfinished tools have no
          recorded ending.
        </p>
      )}
      <div className="transcript-toolbar">
        <div className="segmented" aria-label="Reading mode">
          {['conversation', 'events'].map((value) => (
            <button
              key={value}
              aria-pressed={mode === value}
              onClick={() => navigate({ mode: value })}
            >
              {value === 'conversation' ? 'Conversation' : 'Events'}
            </button>
          ))}
        </div>
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
          setEventCursor(null);
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
              setEventCursor(null);
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
      {searchQuery && mode === 'conversation' && (
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
            if (event.deltaY < 0) setFollow(false);
          }}
          onPointerDown={() => setFollow(false)}
          onScroll={() => {
            if (container.current) anchor.current = readAnchor(container.current);
          }}
        >
          {mode === 'events' ? (
            <div className="event-list">
              {eventPage.error && <p className="notice error">{eventPage.error}</p>}
              {eventPage.data?.items.map((event) => (
                <article className="event" key={event.seq}>
                  <div>
                    <code>#{event.seq}</code> <strong>{event.type}</strong>{' '}
                    <span>{event.direction}</span>
                    <button onClick={() => navigate({ entry: event.entry_id, event: event.seq })}>
                      Inspect
                    </button>
                  </div>
                  <pre>{event.preview}</pre>
                  <a href={`${base}/content/${event.content_ref}?download=true`}>Full event ↓</a>
                </article>
              ))}
              {eventPage.data?.has_more && (
                <button onClick={() => setEventCursor(eventPage.data!.next_cursor)}>
                  Next events
                </button>
              )}
            </div>
          ) : (
            <div className="transcript-content">
              {window?.has_older && (
                <button
                  className="page-control"
                  onClick={() => {
                    setFollow(false);
                    void loadWindow({ before: window.before_cursor });
                  }}
                >
                  ↑ Earlier entries
                </button>
              )}
              {!window && busy && <p className="empty">Loading transcript…</p>}
              {window && entries.length === 0 && (
                <p className="empty">No transcript has been recorded for this session.</p>
              )}
              {visible.map((entry, index) => (
                <div key={entry.entry_id}>
                  {(index === 0 || visible[index - 1]?.task_id !== entry.task_id) && (
                    <div className="turn-divider">
                      <span>
                        {detail.data?.tasks.find((task) => task.task_id === entry.task_id)?.title ??
                          entry.task_id}
                      </span>
                      <code>{entry.turn_id ?? 'Turn ID not recorded'}</code>
                    </div>
                  )}
                  <Entry
                    entry={entry}
                    sessionMissingParts={detail.data?.coverage.missing_parts ?? []}
                    navigate={navigate}
                    selected={selected === entry.entry_id}
                    expand={selected === entry.entry_id}
                    focus={selected === entry.entry_id ? focus : undefined}
                  />
                </div>
              ))}
              {window?.has_newer && (
                <button
                  className="page-control"
                  onClick={() => void loadWindow({ after_window: window.after_window_cursor })}
                >
                  Later entries ↓
                </button>
              )}
            </div>
          )}
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
                    Observed: inspected.data.observed_at,
                    Started: inspected.data.started_at,
                    Ended: inspected.data.ended_at,
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
                <button
                  onClick={() => {
                    setEventCursor(null);
                    navigate({ mode: 'events', event: inspected.data!.first_seq });
                  }}
                >
                  Original events ({inspected.data.raw_event_count})
                </button>
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
