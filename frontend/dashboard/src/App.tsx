import { useEffect, useRef, useState } from 'react';
import {
  backendLabel,
  query,
  useNavigation,
  useResource,
  type Navigate,
  type Page,
  type Task,
  type TaskDetail,
  type Diagnostic,
} from './api';
import { Copy, Status } from './Entry';
import { Download } from './Download';
import { Transcript } from './Transcript';
import { Time } from './Time';
import { useSessions } from './useSessions';

interface Overview {
  service: string;
  backend: string;
  generated_at: number;
  task_counts: Record<string, number>;
}
interface Plugin {
  plugin_id: string;
  enabled: boolean;
  event_coverage: string;
  tasks: Array<{ status: string; action: string; count: number }>;
}
interface Runtime {
  connection: string;
  backend: string;
  model: string | null;
  reasoning_effort: string | null;
  concurrency: number;
  leases: Array<{ context_key: string; task_id: string; expires_at: number }>;
  diagnostics: Diagnostic[];
  bin: string;
  backends: Record<string, { connection: string; bin: string; diagnostics: Diagnostic[] }>;
}

export function App({ onSignOut }: { onSignOut?: () => void }) {
  const { selection, navigate } = useNavigation();
  const view = selection.get('view') ?? 'sessions';
  const [live, setLive] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const [theme, setTheme] = useState(() => localStorage.getItem('nyanpasu-theme') ?? 'system');
  const [indexOpen, setIndexOpen] = useState(false);
  const overview = useResource<Overview>('/api/overview', live, refresh);
  const [sessionQuery, setSessionQuery] = useState('');
  const [sessionState, setSessionState] = useState('');
  const [showSubtasks, setShowSubtasks] = useState(false);
  const sessionList = useRef<HTMLDivElement>(null);
  const sessionEnd = useRef<HTMLDivElement>(null);
  const sessionPath = query('/api/sessions', {
    include_subtasks: showSubtasks,
    q: sessionQuery,
    state: sessionState,
    context: selection.get('context'),
  });
  const sessions = useSessions(sessionPath, live, refresh);
  const session = selection.get('session');
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem('nyanpasu-theme', theme);
  }, [theme]);
  useEffect(() => {
    if (view === 'sessions' && !session && sessions.data?.items[0])
      navigate({ session: sessions.data.items[0].session_id }, true);
  }, [view, session, sessions.data]);
  useEffect(() => {
    if (!sessionEnd.current || sessions.loading || sessions.error || !sessions.data?.has_more)
      return;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) sessions.loadMore();
      },
      { root: sessionList.current, rootMargin: '160px' },
    );
    observer.observe(sessionEnd.current);
    return () => observer.disconnect();
  }, [
    view,
    indexOpen,
    sessions.loading,
    sessions.error,
    sessions.data?.has_more,
    sessions.loadMore,
  ]);
  return (
    <div className="app-shell">
      <header className="topbar">
        <a className="brand" href="?view=sessions">
          <span aria-hidden="true">✳</span> Nyanpasu
        </a>
        <nav aria-label="Main navigation">
          {['sessions', 'tasks', 'plugins', 'runtime'].map((item) => (
            <button
              key={item}
              aria-current={view === item ? 'page' : undefined}
              onClick={() => navigate({ view: item })}
            >
              {item[0]!.toUpperCase() + item.slice(1)}
            </button>
          ))}
        </nav>
        <div className="service-state">
          <span className={overview.error ? 'dot error' : 'dot'} />
          {overview.error ? 'Read failed' : overview.data ? 'Service available' : 'Connecting…'}
        </div>
        <button
          className={live ? 'live active' : 'live'}
          aria-pressed={live}
          title="Only controls page updates"
          onClick={() => setLive(!live)}
        >
          {live ? '◉ Live' : 'Ⅱ Paused'}
        </button>
        <button onClick={() => setRefresh(refresh + 1)} disabled={overview.loading}>
          Refresh
        </button>
        <select aria-label="Theme" value={theme} onChange={(event) => setTheme(event.target.value)}>
          <option value="system">System</option>
          <option value="dark">Dark</option>
          <option value="light">Light</option>
        </select>
        {onSignOut && <button onClick={onSignOut}>Sign out</button>}
      </header>
      {overview.error && (
        <p className="global-error" role="alert">
          {overview.error} · Showing last successfully read data.
        </p>
      )}

      <main className="workspace">
        {view === 'sessions' && (
          <>
            <button className="index-toggle" onClick={() => setIndexOpen(!indexOpen)}>
              Sessions {indexOpen ? '×' : '☰'}
            </button>
            <aside className={`session-index ${indexOpen ? 'open' : ''}`}>
              <div className="index-heading">
                <h2>Sessions</h2>
                <span>{sessions.data?.total ?? '…'}</span>
              </div>
              <p className="subtle">Follow the conversation. Inspect the evidence.</p>
              <input
                aria-label="Find session"
                placeholder="Find a session or context…"
                value={sessionQuery}
                onChange={(event) => {
                  setSessionQuery(event.target.value);
                }}
              />
              <select
                aria-label="Session status"
                value={sessionState}
                onChange={(event) => {
                  setSessionState(event.target.value);
                }}
              >
                <option value="">All statuses</option>
                {['queued', 'running', 'waiting', 'completed', 'failed', 'cancelled'].map(
                  (state) => (
                    <option key={state}>{state}</option>
                  ),
                )}
              </select>
              <label className="session-subtask-toggle">
                <input
                  type="checkbox"
                  checked={showSubtasks}
                  onChange={(event) => setShowSubtasks(event.target.checked)}
                />
                Show subtask sessions
              </label>
              {selection.has('context') && (
                <button onClick={() => navigate({ context: null })}>Clear context filter</button>
              )}
              {sessions.error && <p className="notice error">{sessions.error}</p>}
              <div
                key={sessionPath}
                className="session-list"
                ref={sessionList}
                aria-label="Session list"
                aria-busy={sessions.loading}
              >
                {sessions.data?.items.map((item) => (
                  <button
                    key={item.session_id}
                    className={session === item.session_id ? 'session-row selected' : 'session-row'}
                    data-state={item.execution_uncertain ? 'unconfirmed' : item.state}
                    aria-current={session === item.session_id ? 'true' : undefined}
                    onClick={() => {
                      navigate({
                        session: item.session_id,
                        tab: null,
                        entry: null,
                        task: null,
                        block: null,
                        content: null,
                        offset: null,
                      });
                      setIndexOpen(false);
                    }}
                  >
                    <div>
                      <Status state={item.execution_uncertain ? 'unconfirmed' : item.state} />
                      <Time value={item.updated_at} label="Updated" />
                    </div>
                    <strong>{item.title}</strong>
                    <code>{item.context_key}</code>
                    <small>
                      {item.task_count} tasks · {backendLabel(item.backend)}
                      {item.spawned_by_task_id && ' · subtask'}
                    </small>
                  </button>
                ))}
                <div ref={sessionEnd} className="session-list-end" role="status">
                  {sessions.loading
                    ? 'Loading sessions…'
                    : sessions.data?.has_more
                      ? 'Scroll for more'
                      : ''}
                </div>
              </div>
              {sessions.data?.items.length === 0 && <p className="empty">No matching sessions.</p>}
            </aside>
            {session ? (
              <Transcript
                key={session}
                session={session}
                selection={selection}
                navigate={navigate}
                live={live}
                refresh={refresh}
              />
            ) : (
              <div className="welcome">
                <span className="eyebrow">OBSERVE · READ · DEBUG</span>
                <h1>
                  Your agent's work,
                  <br />
                  one conversation at a time.
                </h1>
                <p>
                  {sessions.loading
                    ? 'Loading sessions…'
                    : 'Sessions appear when a task starts executing. Select a task to inspect queued or ignored work.'}
                </p>
                <button onClick={() => navigate({ view: 'tasks' })}>View tasks →</button>
              </div>
            )}
          </>
        )}
        {view === 'tasks' && (
          <Tasks selection={selection} navigate={navigate} live={live} refresh={refresh} />
        )}
        {view === 'plugins' && <Plugins navigate={navigate} live={live} refresh={refresh} />}
        {view === 'runtime' && <RuntimeView navigate={navigate} live={live} refresh={refresh} />}
      </main>
    </div>
  );
}

function Tasks({
  selection,
  navigate,
  live,
  refresh,
}: {
  selection: URLSearchParams;
  navigate: Navigate;
  live: boolean;
  refresh: number;
}) {
  const [q, setQ] = useState('');
  const [state, setState] = useState('');
  const [offset, setOffset] = useState(0);
  const data = useResource<Page<Task>>(
    query('/api/tasks', { q, state, plugin: selection.get('plugin'), offset }),
    live,
    refresh,
  );
  const taskId = selection.get('task');
  const detail = useResource<TaskDetail>(
    taskId ? `/api/tasks/${encodeURIComponent(taskId)}` : null,
    live,
    refresh,
  );
  return (
    <section className="full-view">
      <span className="eyebrow">DISPATCH & EXECUTION</span>
      <h1>Tasks</h1>
      <p className="subtle">Submitted, coalesced, ignored and executed work.</p>
      <div className="toolbar">
        <input
          aria-label="Find task"
          placeholder="Search task input or context…"
          value={q}
          onChange={(event) => {
            setQ(event.target.value);
            setOffset(0);
          }}
        />
        <select
          aria-label="Task status"
          value={state}
          onChange={(event) => {
            setState(event.target.value);
            setOffset(0);
          }}
        >
          <option value="">All statuses</option>
          {['queued', 'running', 'waiting', 'failed', 'cancelled', 'completed'].map((value) => (
            <option key={value}>{value}</option>
          ))}
        </select>
        {selection.has('plugin') && (
          <button onClick={() => navigate({ plugin: null })}>Clear plugin filter</button>
        )}
      </div>
      {data.error && <p className="notice error">{data.error}</p>}
      <div className="task-list">
        {data.data?.items.map((task) => (
          <button
            key={task.task_id}
            className="task-row"
            onClick={() => navigate({ task: task.task_id })}
          >
            <Status state={task.status} />
            <div>
              <strong>{task.title}</strong>
              <code>{task.context_key}</code>
              {task.error && <p className="error-text">{task.error.split('\n')[0]}</p>}
            </div>
            <span>
              {task.action}
              {task.coalesced_into ? ' · coalesced' : ''}
              {task.spawned_by_task_id ? ' · subtask' : ''}
            </span>
            <span>{task.plugin_id}</span>
            <div className="task-times">
              <div>
                <span>Created</span>
                <Time value={task.created_at} />
              </div>
              <div>
                <span>Updated</span>
                <Time value={task.updated_at} />
              </div>
            </div>
            <span>→</span>
          </button>
        ))}
      </div>
      <div className="pagination">
        <span>{data.data?.total ?? '…'} matching tasks</span>
        <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>
          Previous
        </button>
        <button disabled={!data.data?.has_more} onClick={() => setOffset(offset + 50)}>
          Next
        </button>
      </div>
      {taskId && (
        <section className="task-detail">
          <div className="toolbar">
            <h2>Task details</h2>
            <button onClick={() => navigate({ task: null })}>Close</button>
          </div>
          {detail.error && <p role="alert">{detail.error}</p>}
          {detail.data && (
            <>
              {detail.data.coalesced_into && (
                <p>
                  Coalesced into{' '}
                  <button onClick={() => navigate({ task: String(detail.data!.coalesced_into) })}>
                    {String(detail.data.coalesced_into)}
                  </button>
                </p>
              )}
              <p>
                Lifecycle: {detail.data.lifecycle} · Generation {detail.data.context_generation}
              </p>
              {detail.data.spawned_by_task_id && (
                <p>
                  Parent task{' '}
                  <button onClick={() => navigate({ task: detail.data!.spawned_by_task_id })}>
                    {detail.data.spawned_by_task_id}
                  </button>
                </p>
              )}
              {detail.data.children.length > 0 && (
                <section aria-label="Subtasks">
                  <h3>Subtasks</h3>
                  {detail.data.children.map((child) => (
                    <div className="subtask-row" key={child.task_id}>
                      <Status state={child.status} />
                      <button onClick={() => navigate({ task: child.task_id })}>
                        {child.task_id}
                      </button>
                      {detail.data!.waiting_for.includes(child.task_id) && (
                        <span>Waiting for result</span>
                      )}
                    </div>
                  ))}
                </section>
              )}
              {detail.data.subtask_result && (
                <section aria-label="Subtask evidence">
                  <h3>Result and evidence</h3>
                  <p>{detail.data.subtask_result.summary}</p>
                  {detail.data.subtask_result.artifacts.map((artifact, index) => (
                    <div className="subtask-artifact" key={artifact.sha256 + artifact.name}>
                      <Download
                        path={`/api/tasks/${encodeURIComponent(taskId)}/artifacts/${index}`}
                        filename={artifact.name.split('/').at(-1) ?? 'evidence'}
                      >
                        {artifact.name}
                      </Download>
                      <span>{artifact.bytes} bytes</span>
                      <code title="SHA-256">{artifact.sha256}</code>
                    </div>
                  ))}
                </section>
              )}
              {detail.data.session_id && (
                <button
                  onClick={() =>
                    navigate({
                      view: 'sessions',
                      session: String(detail.data!.session_id),
                      tab: null,
                      entry: detail.data!.entry_id as string | null,
                    })
                  }
                >
                  Open transcript →
                </button>
              )}
              <dl className="task-metadata">
                <div>
                  <dt>Task ID</dt>
                  <dd>
                    <code>{detail.data.task_id}</code>
                  </dd>
                </div>
                <div>
                  <dt>Context key</dt>
                  <dd>
                    <code>{detail.data.context_key}</code>
                  </dd>
                </div>
                <div>
                  <dt>Created</dt>
                  <dd>
                    <Time value={detail.data.created_at} />
                  </dd>
                </div>
                <div>
                  <dt>Updated</dt>
                  <dd>
                    <Time value={detail.data.updated_at} />
                  </dd>
                </div>
              </dl>
              {detail.data.error && <p className="notice error">{detail.data.error}</p>}
              <details>
                <summary>Task data</summary>
                <Copy text={JSON.stringify(detail.data, null, 2)} label="Copy task JSON" />
                <pre>{JSON.stringify(detail.data, null, 2)}</pre>
              </details>
            </>
          )}
        </section>
      )}
    </section>
  );
}

function Plugins({
  navigate,
  live,
  refresh,
}: {
  navigate: Navigate;
  live: boolean;
  refresh: number;
}) {
  const data = useResource<Page<Plugin>>('/api/plugins', live, refresh);
  return (
    <section className="full-view">
      <span className="eyebrow">EVENT SOURCES</span>
      <h1>Plugins</h1>
      {data.error && <p className="notice error">{data.error}</p>}
      <div className="plugin-grid">
        {data.data?.items.map((plugin) => (
          <article className="plugin-card" key={plugin.plugin_id}>
            <div className="toolbar">
              <h2>{plugin.plugin_id}</h2>
              <Status state={plugin.enabled ? 'enabled' : 'disabled'} />
            </div>
            <p className="subtle">{plugin.event_coverage}</p>
            {plugin.tasks.map((group) => (
              <p key={`${group.action}:${group.status}`}>
                <Status state={group.status} /> {group.count} {group.action} tasks
              </p>
            ))}
            <button onClick={() => navigate({ view: 'tasks', plugin: plugin.plugin_id })}>
              Inspect tasks →
            </button>
          </article>
        ))}
      </div>
    </section>
  );
}

function RuntimeView({
  navigate,
  live,
  refresh,
}: {
  navigate: Navigate;
  live: boolean;
  refresh: number;
}) {
  const data = useResource<Runtime>('/api/runtime', live, refresh);
  const [level, setLevel] = useState('');
  const [diagnosticBackend, setDiagnosticBackend] = useState('');
  const selectedBackend = diagnosticBackend || data.data?.backend || '';
  const diagnostics = (data.data?.backends[selectedBackend]?.diagnostics ?? []).filter(
    (item) => !level || item.level === level,
  );
  return (
    <section className="full-view">
      <span className="eyebrow">SERVICE OBSERVATIONS</span>
      <h1>Runtime</h1>
      {data.error && <p className="notice error">{data.error}</p>}
      {data.data && (
        <>
          <div className="runtime-cards">
            <article>
              <span>Backend</span>
              <h2>{backendLabel(data.data.backend)}</h2>
              <code>{data.data.bin}</code>
              <Status state={data.data.connection} />
            </article>
            <article>
              <span>Root task concurrency</span>
              <h2>{data.data.concurrency}</h2>
              <p>Subtasks share their root’s slot, including while it waits.</p>
            </article>
            <article>
              <span>Configured model</span>
              <h2>{data.data.model ?? 'Backend default'}</h2>
              <span>Reasoning: {data.data.reasoning_effort ?? 'Backend default'}</span>
            </article>
          </div>
          <h2>Context leases</h2>
          {data.data.leases.map((lease) => (
            <div className="task-row" key={lease.context_key}>
              <code>{lease.context_key}</code>
              <Status state={lease.expires_at * 1000 > Date.now() ? 'active' : 'expired'} />
              <button onClick={() => navigate({ view: 'tasks', task: lease.task_id })}>
                Open task →
              </button>
            </div>
          ))}
          <div className="diagnostics-heading">
            <h2>Backend diagnostics</h2>
            <select
              aria-label="Diagnostic backend"
              value={selectedBackend}
              onChange={(event) => setDiagnosticBackend(event.target.value)}
            >
              {[...new Set([data.data.backend, ...Object.keys(data.data.backends)])].map((name) => (
                <option key={name} value={name}>
                  {backendLabel(name)}
                </option>
              ))}
            </select>
            <select
              aria-label="Diagnostic level"
              value={level}
              onChange={(event) => setLevel(event.target.value)}
            >
              <option value="">All levels</option>
              {['error', 'warn', 'info', 'debug', 'trace', 'stderr'].map((value) => (
                <option key={value}>{value}</option>
              ))}
            </select>
          </div>
          <p className="subtle">Recent backend log messages. Times use your local timezone.</p>
          <div className="diagnostic-list">
            {diagnostics.length === 0 && (
              <p className="empty">
                {level ? 'No matching diagnostics.' : 'No backend diagnostics.'}
              </p>
            )}
            {diagnostics.map((item, index) => (
              <article className="diagnostic" key={`${item.timestamp}:${index}`}>
                <header>
                  <Time value={item.timestamp} />
                  <span className={`diagnostic-level ${item.level}`}>{item.level}</span>
                  {item.target && <code>{item.target}</code>}
                </header>
                <div className="diagnostic-message">{item.message}</div>
              </article>
            ))}
          </div>
        </>
      )}
    </section>
  );
}
