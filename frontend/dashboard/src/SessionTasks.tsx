import { useState } from 'react';
import type { SessionTaskTree, TaskLink, TaskTreeNode } from './api-types';
import { type Navigate, type useResource } from './api';
import { Status } from './Entry';
import { Time } from './Time';
import { Download } from './Download';

const kinds = new Map<string, { label: string; tone: string; symbol: string }>([
  ['independent-design', { label: 'Independent design', tone: 'violet', symbol: '◇' }],
  ['module-review', { label: 'Module review', tone: 'blue', symbol: '▦' }],
  ['test-audit', { label: 'Test audit', tone: 'amber', symbol: '◈' }],
]);
const tones = ['violet', 'blue', 'amber', 'teal', 'rose'];

function taskKind(purpose: string | null) {
  const label = purpose ?? 'Subtask';
  return (
    kinds.get(label) ?? {
      label,
      tone: tones[
        Array.from(label).reduce((hash, letter) => hash + letter.codePointAt(0)!, 0) % tones.length
      ]!,
      symbol: '↳',
    }
  );
}

function descendants(nodes: TaskTreeNode[]): TaskTreeNode[] {
  return nodes.flatMap((node) => [node, ...descendants(node.children)]);
}

function progress(nodes: TaskTreeNode[]) {
  const counts = new Map<string, number>();
  for (const node of descendants(nodes))
    counts.set(node.status, (counts.get(node.status) ?? 0) + 1);
  return [...counts].map(([status, count]) => `${count} ${status}`).join(' · ');
}

function TaskChildren({ nodes, navigate }: { nodes: TaskTreeNode[]; navigate: Navigate }) {
  return (
    <ul className="session-task-children">
      {nodes.map((node) => {
        const kind = taskKind(node.purpose);
        return (
          <li key={node.task_id} className="task-card" data-tone={kind.tone}>
            <div className="session-task-heading">
              <span className="task-kind-symbol" aria-hidden="true">
                {kind.symbol}
              </span>
              <button className="task-title" onClick={() => openTask(node, navigate)}>
                {node.title === node.purpose ? kind.label : node.title}
              </button>
              <Status state={node.status} />
            </div>
            {node.purpose && node.title !== node.purpose && (
              <small className="task-kind-label">{kind.label}</small>
            )}
            {node.waiting && <p className="task-waiting">Awaited by ancestor</p>}
            {node.summary && <p className="task-result-summary">{node.summary}</p>}
            {node.error && <p className="error-text">{node.error.split('\n')[0]}</p>}
            <div className="task-card-actions">
              <button className="quiet" onClick={() => openTask(node, navigate)}>
                {node.session_id ? 'Open conversation ↗' : 'View task →'}
              </button>
              {node.session_id && (
                <button
                  className="quiet"
                  onClick={() => navigate({ view: 'tasks', task: node.task_id })}
                >
                  Task details
                </button>
              )}
            </div>
            {node.artifacts.length > 0 && (
              <details className="session-task-evidence">
                <summary>Evidence ({node.artifacts.length})</summary>
                {node.artifacts.map((artifact, index) => (
                  <Download
                    key={artifact.sha256 + artifact.name}
                    path={`/api/tasks/${encodeURIComponent(node.task_id)}/artifacts/${index}`}
                    filename={artifact.name.split('/').at(-1) ?? 'evidence'}
                  >
                    {artifact.name}
                  </Download>
                ))}
              </details>
            )}
            {node.children.length > 0 && <TaskChildren nodes={node.children} navigate={navigate} />}
          </li>
        );
      })}
    </ul>
  );
}

export function openTask(task: TaskLink, navigate: Navigate) {
  if (task.session_id)
    navigate({
      view: 'sessions',
      session: task.session_id,
      tab: null,
      task: null,
      entry: null,
      block: null,
      content: null,
      offset: null,
    });
  else navigate({ view: 'tasks', task: task.task_id });
}

function TaskGroup({
  task,
  initialOpen,
  navigate,
}: {
  task: TaskTreeNode;
  initialOpen: boolean;
  navigate: Navigate;
}) {
  const [open, setOpen] = useState(initialOpen);
  return (
    <details
      className="session-task-group"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <Status state={task.status} /> <span>{task.title}</span>{' '}
        <Time value={task.created_at} label="Created" />
        <small>{progress(task.children)}</small>
      </summary>
      <TaskChildren nodes={task.children} navigate={navigate} />
    </details>
  );
}

export function SessionTasks({
  tree,
  offset,
  onPage,
  navigate,
}: {
  tree: ReturnType<typeof useResource<SessionTaskTree>>;
  offset: number;
  onPage: (offset: number) => void;
  navigate: Navigate;
}) {
  const data = tree.data;
  const nodes = data?.groups.flatMap((group) => descendants(group.children)) ?? [];
  const purposes = [...new Set(nodes.map((node) => node.purpose))];
  return (
    <section className="session-task-tree" aria-label="Related tasks">
      <header className="subtasks-heading">
        <div>
          <h2>Sub tasks</h2>
          <p className="subtle">Results and evidence from this session’s child tasks.</p>
        </div>
        {data && nodes.length > 0 && (
          <span className="subtasks-count">
            {nodes.length} tasks{(offset > 0 || data.has_more) && ' on this page'}
          </span>
        )}
      </header>
      {tree.error && <p className="notice error">{tree.error}</p>}
      {!data && !tree.error && <p className="empty">Loading sub tasks…</p>}
      {data && nodes.length === 0 && offset === 0 && (
        <p className="empty">No sub tasks have been created for this session.</p>
      )}
      {data && (data.groups.length > 0 || offset > 0) && (
        <>
          <div className="task-kind-legend" aria-label="Task types">
            {purposes.map((purpose) => {
              const kind = taskKind(purpose);
              return (
                <span key={purpose ?? ''} data-tone={kind.tone}>
                  <span aria-hidden="true">{kind.symbol}</span> {kind.label}
                </span>
              );
            })}
          </div>
          <p className="subtasks-progress">
            {progress(data.groups.flatMap((group) => group.children))}
          </p>
          <div className="session-task-groups" aria-busy={tree.loading}>
            {data.groups.map((group, index) => (
              <TaskGroup
                key={group.task_id}
                task={group}
                initialOpen={index === 0 || ['queued', 'running', 'waiting'].includes(group.status)}
                navigate={navigate}
              />
            ))}
            {(offset > 0 || data.has_more) && (
              <div className="pagination">
                <span>
                  Task groups {offset + 1}–{offset + data.groups.length}
                </span>
                <button disabled={offset === 0} onClick={() => onPage(Math.max(0, offset - 10))}>
                  Newer tasks
                </button>
                <button disabled={!data.has_more} onClick={() => onPage(offset + 10)}>
                  Older tasks
                </button>
              </div>
            )}
          </div>
        </>
      )}
    </section>
  );
}
