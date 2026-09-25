import { useState } from 'react';
import type { SessionTaskTree, TaskLink, TaskTreeNode } from './api-types';
import { type Navigate, query, useResource } from './api';
import { Status } from './Entry';
import { Time } from './Time';
import { Download } from './Download';

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
      {nodes.map((node) => (
        <li key={node.task_id}>
          <div className="session-task-heading">
            <Status state={node.status} />
            <button className="task-title" onClick={() => openTask(node, navigate)}>
              {node.title}
            </button>
            {node.waiting && <span className="subtle">Parent waiting</span>}
            <button
              className="quiet"
              onClick={() => navigate({ view: 'tasks', task: node.task_id })}
            >
              Task details
            </button>
          </div>
          {node.summary && <p className="task-result-summary">{node.summary}</p>}
          {node.error && <p className="error-text">{node.error.split('\n')[0]}</p>}
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
      ))}
    </ul>
  );
}

function openTask(task: TaskLink, navigate: Navigate) {
  if (task.session_id)
    navigate({
      view: 'sessions',
      session: task.session_id,
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
  session,
  navigate,
  live,
  refresh,
}: {
  session: string;
  navigate: Navigate;
  live: boolean;
  refresh: number;
}) {
  const [offset, setOffset] = useState(0);
  const [open, setOpen] = useState(true);
  const tree = useResource<SessionTaskTree>(
    query(`/api/sessions/${session}/task-tree`, { offset, limit: 10 }),
    live,
    refresh,
  );
  const data = tree.data;
  if (!data && !tree.error) return null;
  return (
    <section className="session-task-tree" aria-label="Related tasks">
      {data?.parent && (
        <div className="parent-session-link">
          <button onClick={() => openTask(data.parent!, navigate)}>
            ← Back to parent {data.parent.session_id ? 'session' : 'task'}
          </button>
          <span className="subtle">{data.parent.title}</span>
        </div>
      )}
      {tree.error && <p className="notice error">{tree.error}</p>}
      {data && (data.groups.length > 0 || offset > 0) && (
        <details
          className="session-subtasks"
          open={open}
          onToggle={(event) => setOpen(event.currentTarget.open)}
        >
          <summary>
            Subtasks{' '}
            <span className="subtle">
              {progress(data.groups.flatMap((group) => group.children))}
              {(offset > 0 || data.has_more) && ' · this page'}
            </span>
          </summary>
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
                <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 10))}>
                  Newer tasks
                </button>
                <button disabled={!data.has_more} onClick={() => setOffset(offset + 10)}>
                  Older tasks
                </button>
              </div>
            )}
          </div>
        </details>
      )}
    </section>
  );
}
