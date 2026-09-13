import type { TranscriptEntry, TranscriptWindow } from './api-types';

export function mergeEntries(
  current: readonly TranscriptEntry[],
  incoming: readonly TranscriptEntry[],
): TranscriptEntry[] {
  const entries = new Map(current.map((entry) => [entry.entry_id, entry]));
  for (const entry of incoming) {
    const previous = entries.get(entry.entry_id);
    if (
      !previous ||
      (entry.revision_seq !== previous.revision_seq && entry.observed_at >= previous.observed_at)
    )
      entries.set(entry.entry_id, entry);
  }
  return [...entries.values()].sort((a, b) => (BigInt(a.first_seq) < BigInt(b.first_seq) ? -1 : 1));
}

export function countUpdates(
  current: readonly TranscriptEntry[],
  incoming: readonly TranscriptEntry[],
): string[] {
  const previous = new Map(current.map((entry) => [entry.entry_id, entry]));
  return incoming
    .filter((entry) => {
      const old = previous.get(entry.entry_id);
      return (
        !old || (entry.revision_seq !== old.revision_seq && entry.observed_at >= old.observed_at)
      );
    })
    .map((entry) => entry.entry_id);
}

export interface ScrollAnchor {
  entryId: string;
  offset: number;
}
export function readAnchor(container: HTMLElement): ScrollAnchor | null {
  const top = container.getBoundingClientRect().top;
  const entry = [...container.querySelectorAll<HTMLElement>('[data-entry-id]')].find(
    (node) => node.getBoundingClientRect().bottom > top,
  );
  return entry
    ? { entryId: entry.dataset.entryId!, offset: entry.getBoundingClientRect().top - top }
    : null;
}
export function restoreAnchor(container: HTMLElement, anchor: ScrollAnchor): void {
  const entry = [...container.querySelectorAll<HTMLElement>('[data-entry-id]')].find(
    (node) => node.dataset.entryId === anchor.entryId,
  );
  if (entry)
    container.scrollTop +=
      entry.getBoundingClientRect().top - container.getBoundingClientRect().top - anchor.offset;
}

export type WindowBounds = Pick<
  TranscriptWindow,
  'generation' | 'before_cursor' | 'after_window_cursor' | 'has_older' | 'has_newer'
>;
export interface TranscriptState {
  entries: TranscriptEntry[];
  unread: Set<string>;
  bounds: WindowBounds | null;
}

export function applyWindow(
  state: TranscriptState,
  page: TranscriptWindow,
  mode: 'replace' | 'older' | 'newer',
): TranscriptState {
  const { generation, before_cursor, after_window_cursor, has_older, has_newer } = page;
  const bounds = { generation, before_cursor, after_window_cursor, has_older, has_newer };
  if (mode === 'replace' || !state.bounds) {
    return {
      entries: mergeEntries(
        page.entries,
        state.entries.filter((entry) =>
          page.entries.some((item) => item.entry_id === entry.entry_id),
        ),
      ),
      unread: state.unread,
      bounds,
    };
  }
  return {
    ...state,
    entries: mergeEntries(state.entries, page.entries),
    bounds:
      mode === 'older'
        ? { ...state.bounds, before_cursor, has_older }
        : { ...state.bounds, after_window_cursor, has_newer },
  };
}

export function applyChanges(
  state: TranscriptState,
  incoming: readonly TranscriptEntry[],
  follow: boolean,
): TranscriptState {
  const updates = countUpdates(state.entries, incoming);
  if (updates.length === 0) return state;
  const visible = follow
    ? incoming
    : incoming.filter((entry) =>
        state.entries.some((current) => current.entry_id === entry.entry_id),
      );
  return {
    ...state,
    entries: mergeEntries(state.entries, visible),
    unread: follow ? state.unread : new Set([...state.unread, ...updates]),
  };
}
