import { useState } from 'react';
import Markdown, { defaultUrlTransform } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { MemoryPage, MemorySource } from './api-types';
import { query, useResource, type Navigate } from './api';
import { Time } from './Time';

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
  const [input, setInput] = useState('');
  const [search, setSearch] = useState('');
  const [topic, setTopic] = useState('');
  const [selected, setSelected] = useState<string | null>(null);
  const page = useResource<MemoryPage>(
    query('/api/memory', { task_id: taskId, q: search, topic }),
    live,
    refresh,
  );
  const source = useResource<MemorySource>(
    selected ? query(`/api/memory/${encodeURIComponent(selected)}`, { task_id: taskId }) : null,
    live,
    refresh,
  );
  return (
    <section className="full-view memory-view">
      <span className="eyebrow">SOURCE SUMMARIES &amp; NAVIGATION</span>
      <h1>Memory</h1>
      {taskId ? (
        <div className="toolbar memory-context">
          <span>
            Memory available to task <code>{taskId}</code>
          </span>
          <button onClick={() => navigate({ view: 'tasks', task: taskId })}>Open task</button>
          <button onClick={() => navigate({ memory_task: null })}>All memory</button>
        </div>
      ) : (
        <p className="subtle">
          All memory audiences. Open a task to inspect the memory available to that task.
        </p>
      )}
      <div className="toolbar">
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
      {page.error && (
        <p className="notice error" role="alert">
          {page.error}
        </p>
      )}
      {page.data?.enabled === false && <p className="empty">Memory is disabled.</p>}
      {page.data?.enabled && (
        <>
          <div className="memory-domains" aria-label="Memory audiences">
            {page.data.domains.map((domain) => (
              <span key={domain.id}>
                <code>{domain.id}</code> · {domain.count}
              </span>
            ))}
          </div>
          <section aria-label="Domain navigation">
            <h2>Domain navigation</h2>
            {page.data.navigation.length === 0 && (
              <p className="empty">No navigation available for this audience.</p>
            )}
            {page.data.navigation.map((navigation) => (
              <details className="memory-note" key={navigation.id} open>
                <summary>
                  <code>{navigation.domain}</code> ·{' '}
                  {navigation.stale ? 'Awaiting refresh' : 'Current'}
                </summary>
                <p className="subtle">
                  Updated <Time value={navigation.updated_at} />
                </p>
                <MemoryMarkdown onSelect={setSelected}>{navigation.body}</MemoryMarkdown>
                <details>
                  <summary>Source summaries ({navigation.sources.length})</summary>
                  <ul className="memory-sources">
                    {navigation.sources.map((id) => (
                      <li key={id}>
                        <button className="quiet" onClick={() => setSelected(id)}>
                          {page.data?.items.find((item) => item.id === id)?.title ?? id}
                        </button>
                      </li>
                    ))}
                  </ul>
                </details>
              </details>
            ))}
          </section>
          <h2>Source summaries</h2>
          <p className="subtle">
            Published summaries from completed tasks. Extraction and navigation updates run in the
            background.
          </p>
          <form
            className="toolbar memory-search"
            onSubmit={(event) => {
              event.preventDefault();
              setSearch(input);
              setSelected(null);
            }}
          >
            <input
              aria-label="Search memory"
              placeholder="Search source summaries or topics…"
              value={input}
              onChange={(event) => setInput(event.target.value)}
            />
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
            <button type="submit">Search</button>
          </form>
          <div className="memory-layout">
            <div className="memory-list" aria-label="Memory results" aria-busy={page.loading}>
              {page.data.items.map((item) => (
                <button
                  key={item.id}
                  className={selected === item.id ? 'memory-row selected' : 'memory-row'}
                  aria-pressed={selected === item.id}
                  onClick={() => setSelected(item.id)}
                >
                  <strong>{item.title}</strong>
                  <span>{item.topics.join(' · ')}</span>
                  <small>
                    {item.domain} · <Time value={item.updated_at} label="Updated" />
                  </small>
                  <small>
                    {item.complete ? 'Published' : 'Extracting'} · {item.task_id}
                  </small>
                </button>
              ))}
              {page.data.items.length === 0 && (
                <p className="empty">No matching source summaries.</p>
              )}
              {page.data.has_more && (
                <p className="subtle">
                  More results are available. Refine the search or select a topic.
                </p>
              )}
            </div>
            <article className="memory-note" aria-label="Source summary details">
              {!selected && <p className="empty">Select a source summary to read its evidence.</p>}
              {selected && source.loading && !source.data && (
                <p className="empty">Loading summary…</p>
              )}
              {source.error && (
                <p className="notice error" role="alert">
                  {source.error}
                </p>
              )}
              {source.data && (
                <>
                  <h2>{source.data.title}</h2>
                  <dl className="task-metadata">
                    <div>
                      <dt>Source task</dt>
                      <dd>
                        <button
                          className="quiet"
                          onClick={() => navigate({ view: 'tasks', task: source.data!.task_id })}
                        >
                          task:{source.data.task_id}
                        </button>
                      </dd>
                    </div>
                    <div>
                      <dt>Source ID</dt>
                      <dd>
                        <code>{source.data.id}</code>
                      </dd>
                    </div>
                    <div>
                      <dt>Topics</dt>
                      <dd>{source.data.topics.join(' · ') || 'None'}</dd>
                    </div>
                    <div>
                      <dt>Status</dt>
                      <dd>{source.data.complete ? 'Published' : 'Extracting'}</dd>
                    </div>
                    <div>
                      <dt>Domain</dt>
                      <dd>{source.data.domain}</dd>
                    </div>
                    <div>
                      <dt>Updated</dt>
                      <dd>
                        <Time value={source.data.updated_at} />
                      </dd>
                    </div>
                    <div>
                      <dt>Revision</dt>
                      <dd>
                        <code>{source.data.revision}</code>
                      </dd>
                    </div>
                  </dl>
                  <MemoryMarkdown onSelect={setSelected}>{source.data.body}</MemoryMarkdown>
                  <h3>Evidence references</h3>
                  <ul className="memory-sources">
                    {source.data.sources.map((reference) => (
                      <li key={reference}>
                        {reference.startsWith('task:') ? (
                          <button
                            className="quiet"
                            onClick={() => navigate({ view: 'tasks', task: reference.slice(5) })}
                          >
                            {reference}
                          </button>
                        ) : (
                          <span>{reference}</span>
                        )}
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </article>
          </div>
        </>
      )}
    </section>
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
