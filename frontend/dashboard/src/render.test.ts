import { describe, expect, it } from 'vitest';

import { renderApp, renderSnapshot } from './render';
import type { DashboardSnapshot } from './types';

describe('dashboard rendering', () => {
  it('renders metrics and backlog rows', () => {
    document.body.innerHTML = '<div id="app"></div>';
    const root = document.getElementById('app');
    if (!root) {
      throw new Error('missing app root');
    }
    renderApp(root);
    renderSnapshot(snapshot);

    expect(document.querySelector('.metric.total .value')?.textContent).toBe('2');
    expect(document.querySelector('#plugins tr')?.textContent).toContain('github_reviewer');
    expect(document.querySelector('#backlog tr')?.textContent).toContain('Review PR');
    expect(document.querySelector('#recent tr')?.textContent).toContain('Create PR');
  });
});

const snapshot: DashboardSnapshot = {
  generated_at: 1_780_000_000,
  totals: {
    total: 2,
    queued: 1,
    running: 1,
    completed: 0,
    failed: 0,
    backlog: 2,
    contexts: 1,
    active_leases: 1,
  },
  status_counts: {
    queued: 1,
    running: 1,
    completed: 0,
    failed: 0,
  },
  action_counts: {
    run: 2,
    cleanup: 0,
    ignored: 0,
  },
  plugins: [
    {
      plugin_id: 'github_reviewer',
      total: 2,
      queued: 1,
      running: 1,
      completed: 0,
      failed: 0,
      last_updated_at: 1_780_000_000,
    },
  ],
  backlog: [
    {
      task_id: 'task-1',
      dedupe_key: 'task-1',
      plugin_id: 'github_reviewer',
      action: 'run',
      status: 'running',
      context_key: 'github:owner/repo#1',
      title: 'Review PR',
      source: 'owner/repo#1',
      thread_id: null,
      turn_id: null,
      error: null,
      created_at: 1_780_000_000,
      updated_at: 1_780_000_000,
      age_seconds: 65,
    },
  ],
  recent: [
    {
      task_id: 'task-2',
      dedupe_key: 'task-2',
      plugin_id: 'github_pr_maker',
      action: 'run',
      status: 'queued',
      context_key: 'github-pr-maker:owner/repo:task-2',
      title: 'Create PR',
      source: 'owner/repo',
      thread_id: null,
      turn_id: null,
      error: null,
      created_at: 1_780_000_000,
      updated_at: 1_780_000_000,
      age_seconds: 12,
    },
  ],
};
