import { describe, expect, it } from 'vitest';
import type { TranscriptEntry } from './api-types';
import { countUpdates, mergeEntries } from './transcript-model';

function entry(id: string, revision: string, first = revision): TranscriptEntry {
  return { entry_id: id, revision_seq: revision, first_seq: first } as TranscriptEntry;
}
describe('transcript versions', () => {
  it('orders 64 bit sequences numerically and never replaces newer history with stale changes', () => {
    const history = [entry('tool', '9007199254741002', '9'), entry('message', '10')];
    const changes = [entry('tool', '9007199254741001', '9'), entry('next', '11')];
    const merged = mergeEntries(history, changes);
    expect(merged.map((value) => value.entry_id)).toEqual(['tool', 'message', 'next']);
    expect(merged[0]!.revision_seq).toBe('9007199254741002');
    expect(history).toHaveLength(2);
  });
  it('replayed responses do not duplicate entries or unread counts', () => {
    const current = [entry('tool', '20', '1')];
    const changes = [entry('tool', '21', '1')];
    const merged = mergeEntries(current, changes);
    expect(countUpdates(current, changes)).toEqual(['tool']);
    expect(countUpdates(merged, changes)).toEqual([]);
    expect(mergeEntries(merged, changes)).toEqual(merged);
  });
});
