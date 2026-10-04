import { useEffect, useRef, useState, type ReactNode } from 'react';
import Markdown, { defaultUrlTransform } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { MemoryInjectionPage, MemoryPage, MemorySource, MemorySummary } from './api-types';
import { backendLabel, query, useResource, type Navigate } from './api';
import { Time } from './Time';

type MemorySection = 'summaries' | 'sources' | 'injections';
const SUMMARY_HEADING = /^#{1,6}[ \t]+([^\n]+)(?:\n|$)/;

export function MemoryView({
  taskId,
  navigate,
  live,
  refresh,
}: {
  taskId: string | null;
  navigate: Navigate;
  live: boolean;
  refresh: number;
}) {
  const [section, setSection] = useState<MemorySection>(taskId ? 'injections' : 'summaries');
  const [input, setInput] = useState('');
  const [search, setSearch] = useState('');
  const [topic, setTopic] = useState('');
  const [domain, setDomain] = useState('');
  const [selected, setSelected] = useState<string | null>(null);
  const [selectedSummary, setSelectedSummary] = useState<string | null>(null);
  const [reveal, setReveal] = useState(false);
  const reader = useRef<HTMLDivElement>(null);
  const page = useResource<MemoryPage>(
    query('/api/memory', {
      task_id: taskId,
      domain,
      q: section === 'sources' ? search : '',
      topic: section === 'sources' ? topic : '',
    }),
    live,
    refresh,
  );
  const injections = useResource<MemoryInjectionPage>(
    taskId ? query('/api/memory/injections', { task_id: taskId }) : null,
    live,
    refresh,
  );
  const source = useResource<MemorySource>(
    selected ? query('/api/memory/' + encodeURIComponent(selected), { task_id: taskId }) : null,
    live,
    refresh,
  );
  const summaries = (page.data?.summaries ?? [])
    .filter((item) =>
      (item.context_key + '\n' + item.body).toLowerCase().includes(search.toLowerCase()),
    )
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at) || a.id.localeCompare(b.id));
  const summary = summaries.find((item) => item.id === selectedSummary) ?? summaries[0];
  const sourceTitles = new Map(page.data?.items.map((item) => [item.id, item.title]));

  useEffect(() => {
    if (!reveal || !reader.current || !page.data) return;
    if (section === 'sources' && source.data?.id !== selected) return;
    const viewport = reader.current.closest<HTMLElement>('.memory-view');
    if (!viewport) return;
    viewport.scrollTo({
      top:
        reader.current.getBoundingClientRect().top -
        viewport.getBoundingClientRect().top +
        viewport.scrollTop -
        14,
      behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
    });
    setReveal(false);
  }, [reveal, section, selected, summary?.id, source.data, page.data]);

  function changeSection(value: MemorySection) {
    setReveal(false);
    setSection(value);
    setInput('');
    setSearch('');
    setTopic('');
  }
  function revealReader() {
    if (window.matchMedia('(max-width: 780px)').matches) setReveal(true);
  }
  function openSource(id: string) {
    changeSection('sources');
    setSelected(id);
    // A cited source may be outside the currently selected audience filter.
    setDomain('');
    revealReader();
  }

  return (
    <section className="full-view memory-view">
      <header className="memory-heading">
        <div>
          <h1>Memory</h1>
          <p className="subtle">
            Browse compact summaries, follow their evidence, and inspect what reached a task.
          </p>
        </div>
        <details className="memory-maintenance">
          <summary>Maintenance tasks</summary>
          <div>
            <button
              onClick={() =>
                navigate({ view: 'tasks', kind: 'memory_extraction', backend: null, task: null })
              }
            >
              Extraction tasks
            </button>
            <button
              onClick={() =>
                navigate({ view: 'tasks', kind: 'memory_consolidation', backend: null, task: null })
              }
            >
              Consolidation tasks
            </button>
          </div>
        </details>
      </header>
      {taskId && (
        <div className="memory-context">
          <span>
            Task scope <code>{taskId}</code>
          </span>
          <button onClick={() => navigate({ view: 'tasks', task: taskId })}>Open task</button>
          <button onClick={() => navigate({ memory_task: null })}>All memory</button>
        </div>
      )}
      {page.error && (
        <p className="notice error" role="alert">
          {page.error}
        </p>
      )}
      {!page.data && page.loading && <p className="empty">Loading memory…</p>}
      {page.data?.enabled === false && <p className="empty">Memory is disabled.</p>}
      {page.data?.enabled && (
        <>
          <nav className="memory-tabs" aria-label="Memory sections">
            <button
              aria-pressed={section === 'summaries'}
              onClick={() => changeSection('summaries')}
            >
              Summaries <span aria-hidden="true">{page.data.summary_count}</span>
            </button>
            <button aria-pressed={section === 'sources'} onClick={() => changeSection('sources')}>
              Sources <span aria-hidden="true">{page.data.count}</span>
            </button>
            {taskId && (
              <button
                aria-pressed={section === 'injections'}
                onClick={() => changeSection('injections')}
              >
                Injections{' '}
                <span aria-hidden="true">
                  {injections.data?.items.length ?? '…'}
                  {injections.data?.has_more ? '+' : ''}
                </span>
              </button>
            )}
          </nav>
          {section !== 'injections' && (
            <form
              className="memory-search"
              onSubmit={(event) => {
                event.preventDefault();
                setSearch(input.trim());
                setSelected(null);
              }}
            >
              <label className="memory-query">
                <input
                  aria-label="Search memory"
                  placeholder={
                    section === 'summaries'
                      ? 'Search contexts or summary text…'
                      : 'Search source summaries…'
                  }
                  maxLength={256}
                  value={input}
                  onChange={(event) => setInput(event.target.value)}
                />
              </label>
              <select
                aria-label="Memory audience"
                value={domain}
                onChange={(event) => {
                  setDomain(event.target.value);
                  setTopic('');
                  setSelected(null);
                  setSelectedSummary(null);
                }}
              >
                <option value="">All audiences</option>
                {page.data.domains.map((item) => (
                  <option key={item.id} value={item.id}>
                    {audienceLabel(item.id)} ({item.count})
                  </option>
                ))}
              </select>
              {section === 'sources' && (
                <select
                  aria-label="Memory topic"
                  value={topic}
                  onChange={(event) => {
                    setTopic(event.target.value);
                    setSelected(null);
                  }}
                >
                  <option value="">All topics</option>
                  {page.data.topics.map((item) => (
                    <option key={item.name} value={item.name}>
                      {item.name} ({item.count})
                    </option>
                  ))}
                </select>
              )}
              <button type="submit">Search</button>
              {(search || topic || domain) && (
                <button
                  type="button"
                  className="quiet"
                  onClick={() => {
                    setInput('');
                    setSearch('');
                    setTopic('');
                    setDomain('');
                    setSelected(null);
                  }}
                >
                  Clear filters
                </button>
              )}
            </form>
          )}
          {section === 'summaries' && (
            <section aria-label="Context summaries">
              <p className="memory-section-hint">
                One rolling summary per context. Select one to read its conclusions and supporting
                sources.
              </p>
              {summaries.length === 0 ? (
                <p className="empty">
                  {search || domain
                    ? 'No summaries match these filters.'
                    : 'No context summaries yet. They appear after background consolidation.'}
                </p>
              ) : (
                <div className="memory-layout">
                  <div className="memory-list" aria-label="Summary results">
                    <div className="memory-list-heading">{summaries.length} context summaries</div>
                    {summaries.map((item) => (
                      <button
                        key={item.id}
                        className={
                          'memory-summary-row' + (summary?.id === item.id ? ' selected' : '')
                        }
                        aria-pressed={summary?.id === item.id}
                        onClick={() => {
                          setSelectedSummary(item.id);
                          revealReader();
                        }}
                      >
                        <span className="memory-row-heading">
                          <strong>{summaryTitle(item)}</strong>
                          <SummaryStatus summary={item} />
                        </span>
                        <span>{audienceLabel(item.domain)}</span>
                        <small>
                          {item.sources.length} sources · <Time value={item.updated_at} />
                        </small>
                      </button>
                    ))}
                  </div>
                  <div ref={reader} className="memory-reader">
                    {summary && (
                      <SummaryReader
                        key={summary.id}
                        summary={summary}
                        sourceTitles={sourceTitles}
                        openSource={openSource}
                      />
                    )}
                  </div>
                </div>
              )}
            </section>
          )}
          {section === 'sources' && (
            <section aria-label="Source summaries">
              <p className="memory-section-hint">
                Published task evidence behind the summaries. Select a source to read the full
                account.
              </p>
              <div className="memory-layout">
                <div className="memory-list" aria-label="Memory results" aria-busy={page.loading}>
                  <div className="memory-list-heading">
                    {page.data.items.length}
                    {page.data.has_more ? '+' : ''} sources shown
                  </div>
                  {page.data.items.map((item) => (
                    <button
                      key={item.id}
                      className={'memory-row' + (selected === item.id ? ' selected' : '')}
                      aria-pressed={selected === item.id}
                      onClick={() => {
                        setSelected(item.id);
                        revealReader();
                      }}
                    >
                      <strong>{item.title}</strong>
                      <span>{item.topics.join(' · ') || audienceLabel(item.domain)}</span>
                      <small>
                        <Time value={item.updated_at} label="Updated" />
                      </small>
                    </button>
                  ))}
                  {page.data.items.length === 0 && (
                    <p className="empty">No matching source summaries.</p>
                  )}
                  {page.data.has_more && (
                    <p className="empty">
                      Refine the search or choose an audience to see more sources.
                    </p>
                  )}
                </div>
                <div ref={reader} className="memory-reader">
                  <article className="memory-document" aria-label="Source summary details">
                    {!selected && (
                      <div className="memory-placeholder">
                        <h2>Follow the evidence</h2>
                        <p>
                          Select a source from the list, or open a reference from a context summary.
                        </p>
                      </div>
                    )}
                    {selected && source.loading && !source.data && (
                      <p className="empty">Loading source…</p>
                    )}
                    {source.error && (
                      <p className="notice error" role="alert">
                        {source.error}
                      </p>
                    )}
                    {source.data && (
                      <SourceReader
                        key={source.data.id}
                        source={source.data}
                        navigate={navigate}
                        openSource={openSource}
                      />
                    )}
                  </article>
                </div>
              </div>
            </section>
          )}
          {section === 'injections' && taskId && (
            <section aria-label="Injected memory" className="memory-injections">
              <p className="memory-section-hint">
                What was supplied to each turn, across all authorized audiences. This does not prove
                the agent used or cited it.
              </p>
              {injections.error && (
                <p className="notice error" role="alert">
                  {injections.error}
                </p>
              )}
              {injections.loading && !injections.data && (
                <p className="empty">Loading injections…</p>
              )}
              {injections.data?.items.length === 0 && (
                <p className="empty">No recorded memory injections for this task.</p>
              )}
              {injections.data?.items.map((injection, index) => (
                <details
                  className="memory-injection-card"
                  key={injection.backend + ':' + injection.thread_id + ':' + injection.turn_id}
                  open={index === 0}
                >
                  <summary>
                    <span className="memory-turn-heading">
                      <strong>{backendLabel(injection.backend)}</strong>
                      <Time value={injection.started_at} />
                    </span>
                    <span className="memory-turn-count">
                      {injection.selected.length} summaries · {(injection.bytes / 1024).toFixed(1)}{' '}
                      / 8 KiB
                    </span>
                  </summary>
                  <div className="memory-injection-content">
                    <div className="memory-budget">
                      <meter
                        min={0}
                        max={8192}
                        value={injection.bytes}
                        aria-label="Injected memory budget"
                      />
                      <span>{injection.bytes.toLocaleString()} of 8,192 bytes</span>
                    </div>
                    <p className="subtle">
                      {injection.selected.length} selected · {injection.skipped.length} skipped
                    </p>
                    <details className="memory-disclosure">
                      <summary>Exact injected text</summary>
                      <pre className="memory-injection" tabIndex={0}>
                        {injection.prompt || 'No memory was injected.'}
                      </pre>
                    </details>
                    <details className="memory-disclosure">
                      <summary>Selection decisions</summary>
                      <ul className="memory-decisions">
                        {[
                          ...injection.selected.map((item) => ({ ...item, selected: true })),
                          ...injection.skipped.map((item) => ({ ...item, selected: false })),
                        ].map((item) => (
                          <li key={item.id}>
                            <div>
                              <span className={'memory-state ' + (item.selected ? 'ready' : '')}>
                                {item.selected ? 'Selected' : 'Skipped'}
                              </span>{' '}
                              {selectionReason(item.reason)}
                            </div>
                            <details>
                              <summary>
                                {page.data?.summaries.find((summary) => summary.id === item.id)
                                  ?.context_key ?? item.id.slice(0, 12)}
                              </summary>
                              <dl className="memory-record">
                                <div>
                                  <dt>Summary ID</dt>
                                  <dd>
                                    <code>{item.id}</code>
                                  </dd>
                                </div>
                                <div>
                                  <dt>Revision</dt>
                                  <dd>
                                    <code>{item.revision}</code>
                                  </dd>
                                </div>
                                <div>
                                  <dt>Reason</dt>
                                  <dd>
                                    <code>{item.reason}</code>
                                  </dd>
                                </div>
                              </dl>
                            </details>
                          </li>
                        ))}
                      </ul>
                      {!injection.selected.length && !injection.skipped.length && (
                        <p className="subtle">No summaries were considered.</p>
                      )}
                    </details>
                    <RecordDetails>
                      <div>
                        <dt>Backend</dt>
                        <dd>{backendLabel(injection.backend)}</dd>
                      </div>
                      <div>
                        <dt>Session</dt>
                        <dd>
                          <code>{injection.thread_id}</code>
                        </dd>
                      </div>
                      <div>
                        <dt>Turn</dt>
                        <dd>
                          <code>{injection.turn_id}</code>
                        </dd>
                      </div>
                    </RecordDetails>
                  </div>
                </details>
              ))}
              {injections.data?.has_more && (
                <p className="subtle">Showing the 20 most recent injections.</p>
              )}
            </section>
          )}
        </>
      )}
    </section>
  );
}

function audienceLabel(domain: string) {
  return domain === 'public' ? 'Public' : domain.replace(/^shared:github:/, '');
}

function summaryTitle(summary: MemorySummary) {
  return summary.body.match(SUMMARY_HEADING)?.[1] ?? summary.context_key.replace(/^github:/, '');
}

function SummaryStatus({ summary }: { summary: MemorySummary }) {
  return (
    <span className={'memory-state ' + (summary.stale ? 'stale' : 'ready')}>
      {summary.stale ? 'Needs refresh' : summary.body ? 'Ready' : 'Empty'}
    </span>
  );
}

function SummaryReader({
  summary,
  sourceTitles,
  openSource,
}: {
  summary: MemorySummary;
  sourceTitles: Map<string, string>;
  openSource: (id: string) => void;
}) {
  const heading = summary.body.match(SUMMARY_HEADING)?.[0];
  return (
    <article className="memory-document" aria-label="Context summary details">
      <div className="memory-document-heading">
        {heading ? (
          <MemoryMarkdown onSelect={openSource}>{heading}</MemoryMarkdown>
        ) : (
          <h2>{summaryTitle(summary)}</h2>
        )}
        <SummaryStatus summary={summary} />
      </div>
      <p className="memory-byline">
        {audienceLabel(summary.domain)} · Updated <Time value={summary.updated_at} />
      </p>
      {summary.stale && (
        <p className="notice">
          Sources changed. This summary will be used again after it is refreshed.
        </p>
      )}
      {summary.body ? (
        <MemoryMarkdown onSelect={openSource}>
          {heading ? summary.body.slice(heading.length) : summary.body}
        </MemoryMarkdown>
      ) : (
        <p className="empty">No information was retained for this context.</p>
      )}
      <details className="memory-disclosure">
        <summary>Supporting sources ({summary.sources.length})</summary>
        <ul className="memory-sources">
          {summary.sources.map((id) => (
            <li key={id}>
              <button className="quiet" onClick={() => openSource(id)}>
                {sourceTitles.get(id) ?? 'Source ' + id.slice(0, 12)}
              </button>
            </li>
          ))}
        </ul>
      </details>
      <RecordDetails>
        <div>
          <dt>Context</dt>
          <dd>
            {summary.context_key} · generation {summary.context_generation}
          </dd>
        </div>
        <div>
          <dt>Audience</dt>
          <dd>{summary.domain}</dd>
        </div>
        <div>
          <dt>Summary ID</dt>
          <dd>
            <code>{summary.id}</code>
          </dd>
        </div>
        <div>
          <dt>Revision</dt>
          <dd>
            <code>{summary.revision}</code>
          </dd>
        </div>
        <div>
          <dt>Injection size</dt>
          <dd>
            {
              new TextEncoder().encode(JSON.stringify({ id: summary.id, body: summary.body }))
                .length
            }{' '}
            / 1,024 bytes
          </dd>
        </div>
      </RecordDetails>
    </article>
  );
}

function SourceReader({
  source,
  navigate,
  openSource,
}: {
  source: MemorySource;
  navigate: Navigate;
  openSource: (id: string) => void;
}) {
  return (
    <>
      <div className="memory-document-heading">
        <h2>{source.title}</h2>
        <span className="memory-state ready">{source.complete ? 'Published' : 'Extracting'}</span>
      </div>
      <p className="memory-byline">
        {audienceLabel(source.domain)} · Updated <Time value={source.updated_at} />
      </p>
      <MemoryMarkdown onSelect={openSource}>{source.body}</MemoryMarkdown>
      <div className="memory-source-task">
        <span>Source task</span>
        <button className="quiet" onClick={() => navigate({ view: 'tasks', task: source.task_id })}>
          task:{source.task_id}
        </button>
      </div>
      <details className="memory-disclosure">
        <summary>Evidence references ({source.sources.length})</summary>
        <ul className="memory-sources">
          {source.sources.map((reference) => (
            <li key={reference}>
              {reference.startsWith('task:') ? (
                <button
                  className="quiet"
                  onClick={() => navigate({ view: 'tasks', task: reference.slice(5) })}
                >
                  {reference}
                </button>
              ) : (
                reference
              )}
            </li>
          ))}
        </ul>
      </details>
      <RecordDetails>
        <div>
          <dt>Source ID</dt>
          <dd>
            <code>{source.id}</code>
          </dd>
        </div>
        <div>
          <dt>Context</dt>
          <dd>
            {source.context_key} · generation {source.context_generation}
          </dd>
        </div>
        <div>
          <dt>Audience</dt>
          <dd>{source.domain}</dd>
        </div>
        <div>
          <dt>Topics</dt>
          <dd>{source.topics.join(' · ') || 'None'}</dd>
        </div>
        <div>
          <dt>Revision</dt>
          <dd>
            <code>{source.revision}</code>
          </dd>
        </div>
      </RecordDetails>
    </>
  );
}

function RecordDetails({ children }: { children: ReactNode }) {
  return (
    <details className="memory-disclosure">
      <summary>Record details</summary>
      <dl className="memory-record">{children}</dl>
    </details>
  );
}

function selectionReason(reason: string) {
  if (reason.startsWith('topic:')) return 'Topic match: ' + reason.slice(6).split(',').join(', ');
  return (
    (
      {
        current_context: 'Current context',
        related_context: 'Related context',
        other_generation: 'Different context generation',
        unauthorized: 'Outside this task’s access',
        empty: 'Empty summary',
        stale: 'Awaiting refresh',
        irrelevant: 'No topic match',
        budget: 'Would exceed the turn budget',
      } as Record<string, string>
    )[reason] ?? reason
  );
}

function MemoryMarkdown({
  children,
  onSelect,
}: {
  children: string;
  onSelect: (id: string) => void;
}) {
  return (
    <div className="markdown">
      <Markdown
        remarkPlugins={[remarkGfm]}
        urlTransform={(url) => (/^memory:[a-f0-9]{32}$/.test(url) ? url : defaultUrlTransform(url))}
        components={{
          a: ({ href, children }) =>
            href?.startsWith('memory:') ? (
              <button className="quiet" onClick={() => onSelect(href.slice(7))}>
                {children}
              </button>
            ) : (
              <a href={href}>{children}</a>
            ),
        }}
      >
        {children}
      </Markdown>
    </div>
  );
}
