import { useState } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { MemoryNote, MemoryPage } from './api-types';
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
  const note = useResource<MemoryNote>(
    selected ? query(`/api/memory/${encodeURIComponent(selected)}`, { task_id: taskId }) : null,
    live,
    refresh,
  );
  return (
    <section className="full-view memory-view">
      <span className="eyebrow">SHARED KNOWLEDGE</span>
      <h1>Memory</h1>
      {taskId ? (
        <div className="toolbar memory-context">
          <span>
            Memory available to task <code>{taskId}</code>
          </span>
          <button onClick={() => navigate({ view: 'tasks', task: taskId })}>Open task</button>
          <button onClick={() => navigate({ memory_task: null })}>Public memory</button>
        </div>
      ) : (
        <p className="subtle">Public memory. Open a task to inspect its authorized memory.</p>
      )}
      {page.error && (
        <p className="notice error" role="alert">
          {page.error}
        </p>
      )}
      {page.data?.enabled === false && <p className="empty">Memory is disabled.</p>}
      {page.data?.enabled && (
        <>
          <div className="memory-domains" aria-label="Authorized memory domains">
            {page.data.domains.map((domain) => (
              <span key={domain.id}>
                <code>{domain.id}</code> · {domain.count}
              </span>
            ))}
          </div>
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
              placeholder="Search knowledge, conditions or topics…"
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
                  {item.applies_to.length > 0 && <small>{item.applies_to.join(' · ')}</small>}
                  <small>
                    {item.domain} · <Time value={item.updated_at} label="Updated" />
                  </small>
                  {item.merged_from.length > 0 && (
                    <small>Merged {item.merged_from.length} notes</small>
                  )}
                </button>
              ))}
              {page.data.items.length === 0 && <p className="empty">No matching memories.</p>}
              {page.data.has_more && (
                <p className="subtle">
                  More results are available. Refine the search or select a topic.
                </p>
              )}
            </div>
            <article className="memory-note" aria-label="Memory details">
              {!selected && (
                <p className="empty">Select a memory to read its evidence and revision.</p>
              )}
              {selected && note.loading && !note.data && <p className="empty">Loading memory…</p>}
              {note.error && (
                <p className="notice error" role="alert">
                  {note.error}
                </p>
              )}
              {note.data && (
                <>
                  <h2>{note.data.title}</h2>
                  <dl className="task-metadata">
                    <div>
                      <dt>Canonical key</dt>
                      <dd>
                        <code>{note.data.key}</code>
                      </dd>
                    </div>
                    <div>
                      <dt>Memory ID</dt>
                      <dd>
                        <code>{note.data.id}</code>
                      </dd>
                    </div>
                    <div>
                      <dt>Topics</dt>
                      <dd>{note.data.topics.join(' · ') || 'None'}</dd>
                    </div>
                    <div>
                      <dt>Applies to</dt>
                      <dd>{note.data.applies_to.join(' · ') || 'No additional conditions'}</dd>
                    </div>
                    <div>
                      <dt>Domain</dt>
                      <dd>{note.data.domain}</dd>
                    </div>
                    <div>
                      <dt>Updated</dt>
                      <dd>
                        <Time value={note.data.updated_at} />
                      </dd>
                    </div>
                    <div>
                      <dt>Revision</dt>
                      <dd>
                        <code>{note.data.revision}</code>
                      </dd>
                    </div>
                  </dl>
                  <div className="markdown">
                    <Markdown remarkPlugins={[remarkGfm]}>{note.data.body}</Markdown>
                  </div>
                  <h3>Sources</h3>
                  <ul className="memory-sources">
                    {note.data.sources.map((source) => (
                      <li key={source}>
                        {source.startsWith('task:') ? (
                          <button
                            className="quiet"
                            onClick={() => navigate({ view: 'tasks', task: source.slice(5) })}
                          >
                            {source}
                          </button>
                        ) : (
                          <span>{source}</span>
                        )}
                      </li>
                    ))}
                  </ul>
                  {(note.data.merged_from ?? []).length > 0 && (
                    <section aria-label="Merged memories">
                      <h3>Merged from</h3>
                      <p className="subtle">
                        These notes were consolidated into this revision and are no longer separate
                        search results.
                      </p>
                      <ul>
                        {(note.data.merged_from ?? []).map((id) => (
                          <li key={id}>
                            <code>{id}</code>
                          </li>
                        ))}
                      </ul>
                    </section>
                  )}
                </>
              )}
            </article>
          </div>
        </>
      )}
    </section>
  );
}
