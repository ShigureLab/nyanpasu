import './style.css';

import { renderApp, renderSnapshot } from './render';
import type { DashboardSnapshot } from './types';

const root = document.getElementById('app');

if (root === null) {
  throw new Error('missing #app element');
}

renderApp(root);

const refreshButton = document.getElementById('refresh');
if (refreshButton !== null) {
  refreshButton.addEventListener('click', () => {
    void refresh();
  });
}

void refresh();
window.setInterval(() => {
  void refresh();
}, 15_000);

async function refresh(): Promise<void> {
  const health = document.getElementById('health');
  setText(health, 'Refreshing');
  try {
    const response = await fetch('/api/dashboard', { cache: 'no-store' });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const snapshot = (await response.json()) as DashboardSnapshot;
    renderSnapshot(snapshot);
    setText(health, 'Live');
  } catch (error) {
    setText(health, `Error: ${error instanceof Error ? error.message : String(error)}`);
  }
}

function setText(element: HTMLElement | null, value: string): void {
  if (element !== null) {
    element.textContent = value;
  }
}
