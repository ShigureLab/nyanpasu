import { firstErrorLine, formatAge, formatTime, text } from './format';
import type { DashboardPluginSummary, DashboardSnapshot, DashboardTaskItem } from './types';

const metricDefs: Array<[keyof DashboardSnapshot['totals'], string]> = [
  ['total', 'Total'],
  ['queued', 'Queued'],
  ['running', 'Running'],
  ['backlog', 'Backlog'],
  ['completed', 'Completed'],
  ['failed', 'Failed'],
  ['contexts', 'Contexts'],
];

export function renderApp(root: HTMLElement): void {
  root.innerHTML = `
    <header>
      <div class="bar">
        <div>
          <h1>Nyanpasu Dashboard</h1>
          <div class="subtle" id="generated">Loading...</div>
        </div>
        <div class="toolbar">
          <span class="subtle" id="health">Waiting for data</span>
          <button id="refresh" type="button" title="Refresh dashboard data">Refresh</button>
        </div>
      </div>
    </header>
    <main>
      <section>
        <div class="metrics" id="metrics"></div>
      </section>
      <section class="grid">
        <div class="plugins">
          <h2>Plugins</h2>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Plugin</th>
                  <th class="num">Total</th>
                  <th class="num">Queued</th>
                  <th class="num">Running</th>
                  <th class="num">Done</th>
                  <th class="num">Failed</th>
                </tr>
              </thead>
              <tbody id="plugins"></tbody>
            </table>
          </div>
        </div>
        <div>
          <h2>Backlog</h2>
          <div class="table-wrap" id="backlog-wrap">
            <table>
              <thead>
                <tr>
                  <th>Status</th>
                  <th>Task</th>
                  <th>Plugin</th>
                  <th>Context</th>
                  <th class="num">Age</th>
                </tr>
              </thead>
              <tbody id="backlog"></tbody>
            </table>
          </div>
        </div>
      </section>
      <section>
        <h2>Recent Tasks</h2>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Status</th>
                <th>Task</th>
                <th>Plugin</th>
                <th>Source</th>
                <th>Action</th>
                <th>Updated</th>
                <th>Error</th>
              </tr>
            </thead>
            <tbody id="recent"></tbody>
          </table>
        </div>
      </section>
    </main>
  `;
}

export function renderSnapshot(snapshot: DashboardSnapshot): void {
  renderMetrics(snapshot);
  renderPlugins(snapshot.plugins);
  renderBacklog(snapshot.backlog);
  renderRecent(snapshot.recent);
  mustElement('generated').textContent = `Generated ${formatTime(snapshot.generated_at)}`;
}

function renderMetrics(snapshot: DashboardSnapshot): void {
  const target = mustElement('metrics');
  clear(target);
  for (const [key, label] of metricDefs) {
    const box = document.createElement('div');
    box.className = `metric ${key}`;
    const name = document.createElement('div');
    name.className = 'label';
    name.textContent = label;
    const value = document.createElement('div');
    value.className = 'value';
    value.textContent = Number(snapshot.totals[key] || 0).toLocaleString();
    box.append(name, value);
    target.appendChild(box);
  }
}

function renderPlugins(plugins: DashboardPluginSummary[]): void {
  const target = mustElement('plugins');
  clear(target);
  if (!plugins.length) {
    emptyRow(target, 6, 'No plugin tasks recorded.');
    return;
  }
  for (const plugin of plugins) {
    const row = document.createElement('tr');
    row.append(
      cell(plugin.plugin_id, 'mono'),
      cell(plugin.total, 'num'),
      cell(plugin.queued, 'num'),
      cell(plugin.running, 'num'),
      cell(plugin.completed, 'num'),
      cell(plugin.failed, 'num'),
    );
    target.appendChild(row);
  }
}

function renderBacklog(tasks: DashboardTaskItem[]): void {
  const target = mustElement('backlog');
  clear(target);
  if (!tasks.length) {
    emptyRow(target, 5, 'No queued or running tasks.');
    return;
  }
  for (const task of tasks) {
    const row = document.createElement('tr');
    row.append(
      statusCell(task.status),
      cell(task.title, 'title'),
      cell(task.plugin_id, 'mono'),
      cell(task.context_key, 'mono'),
      cell(formatAge(task.age_seconds), 'num'),
    );
    target.appendChild(row);
  }
}

function renderRecent(tasks: DashboardTaskItem[]): void {
  const target = mustElement('recent');
  clear(target);
  if (!tasks.length) {
    emptyRow(target, 7, 'No tasks recorded.');
    return;
  }
  for (const task of tasks) {
    const row = document.createElement('tr');
    row.append(
      statusCell(task.status),
      cell(task.title, 'title'),
      cell(task.plugin_id, 'mono'),
      cell(task.source, 'mono'),
      cell(task.action, 'mono'),
      cell(formatTime(task.updated_at)),
      cell(firstErrorLine(task.error), 'error'),
    );
    target.appendChild(row);
  }
}

function mustElement(id: string): HTMLElement {
  const element = document.getElementById(id);
  if (element === null) {
    throw new Error(`missing dashboard element: ${id}`);
  }
  return element;
}

function clear(node: HTMLElement): void {
  while (node.firstChild) {
    node.removeChild(node.firstChild);
  }
}

function cell(value: string | number | null | undefined, className?: string): HTMLTableCellElement {
  const td = document.createElement('td');
  if (className) {
    td.className = className;
  }
  td.textContent = text(value);
  return td;
}

function statusCell(status: string): HTMLTableCellElement {
  const td = document.createElement('td');
  const badge = document.createElement('span');
  badge.className = `status ${status}`;
  badge.textContent = status;
  td.appendChild(badge);
  return td;
}

function emptyRow(target: HTMLElement, columns: number, label: string): void {
  const row = document.createElement('tr');
  const td = document.createElement('td');
  td.className = 'empty';
  td.colSpan = columns;
  td.textContent = label;
  row.appendChild(td);
  target.appendChild(row);
}
