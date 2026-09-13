import { describe, expect, it } from 'vitest';
import type { TranscriptEntry, TranscriptWindow } from './api-types';
import { countUpdates, mergeEntries, applyChanges, applyWindow } from './transcript-model';

function entry(
  id: string,
  revision: string,
  observed = '2026-09-14T00:00:00Z',
  first = '1',
): TranscriptEntry {
  return {
    entry_id: id,
    revision_seq: revision,
    observed_at: observed,
    first_seq: first,
  } as TranscriptEntry;
}
describe('Codex item snapshots', () => {
  it('does not replace a newer source read with a stale response', () => {
    const history = [
      entry('tool', 'new', '2026-09-14T00:00:02Z'),
      entry('message', 'm', undefined, '10'),
    ];
    const changes = [entry('tool', 'old'), entry('next', 'n', undefined, '11')];
    const merged = mergeEntries(history, changes);
    expect(merged.map((value) => value.entry_id)).toEqual(['tool', 'message', 'next']);
    expect(merged[0]!.revision_seq).toBe('new');
    expect(countUpdates(history, changes)).toEqual(['next']);
    expect(history).toHaveLength(2);
  });
  it('replayed responses do not duplicate entries or unread counts', () => {
    const current = [entry('tool', 'before')];
    const changes = [entry('tool', 'after', '2026-09-14T00:00:01Z')];
    const merged = mergeEntries(current, changes);
    expect(countUpdates(current, changes)).toEqual(['tool']);
    expect(countUpdates(merged, changes)).toEqual([]);
    expect(mergeEntries(merged, changes)).toEqual(merged);
  });
});

it('loads adjacent pages without replacing the reading window or its opposite cursor', () => {
  const page = (ids: number[], before: string | null, after: string | null) =>
    ({
      entries: ids.map((id) => entry(String(id), 'v1', undefined, String(id))),
      generation: 'thread',
      before_cursor: before,
      after_window_cursor: after,
      has_older: before !== null,
      has_newer: after !== null,
    }) as TranscriptWindow;
  const middle = applyWindow(
    { entries: [], unread: new Set(), bounds: null },
    page([4, 5], 'before4', 'after5'),
    'replace',
  );
  const older = applyWindow(middle, page([2, 3], 'before2', 'after3'), 'older');
  expect(older.entries.map((item) => item.entry_id)).toEqual(['2', '3', '4', '5']);
  expect(older.bounds?.after_window_cursor).toBe('after5');
  const newer = applyWindow(older, page([5, 6, 7], 'before5', null), 'newer');
  expect(newer.entries.map((item) => item.entry_id)).toEqual(['2', '3', '4', '5', '6', '7']);
  expect(newer.bounds?.before_cursor).toBe('before2');
  expect(newer.bounds?.has_newer).toBe(false);
});

it('polling preserves all loaded history while paused and keeps unread changes separate', () => {
  const entries = Array.from({ length: 350 }, (_, id) =>
    entry(String(id), 'v1', undefined, String(id)),
  );
  const current = { entries, unread: new Set<string>(), bounds: null };
  const updated = applyChanges(current, [entry('349', 'v2'), entry('350', 'v1')], false);
  expect(updated.entries).toHaveLength(350);
  expect(updated.entries[0]?.entry_id).toBe('0');
  expect(updated.unread.size).toBe(2);
});
