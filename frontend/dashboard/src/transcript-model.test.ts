import { describe, expect, it } from 'vitest';
import type { TranscriptEntry } from './api-types';
import { countUpdates, mergeEntries } from './transcript-model';

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
