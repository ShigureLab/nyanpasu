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
export function hasTextSelection(container: HTMLElement): boolean {
  const selection = document.getSelection();
  return Boolean(selection?.toString() && selection.getRangeAt(0).intersectsNode(container));
}
export function readAnchor(container: HTMLElement): ScrollAnchor | null {
  const top = container.getBoundingClientRect().top;
  const entries = container.querySelectorAll<HTMLElement>('[data-entry-id]');
  let left = 0;
  let right = entries.length;
  while (left < right) {
    const middle = (left + right) >>> 1;
    if (entries[middle].getBoundingClientRect().bottom <= top) left = middle + 1;
    else right = middle;
  }
  const entry = entries[left];
  return entry
    ? { entryId: entry.dataset.entryId!, offset: entry.getBoundingClientRect().top - top }
    : null;
}
export function restoreAnchor(container: HTMLElement, anchor: ScrollAnchor): void {
  const entry = container.querySelector<HTMLElement>(
    `[data-entry-id="${CSS.escape(anchor.entryId)}"]`,
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
  const loaded = new Set(state.entries.map((entry) => entry.entry_id));
  const last = BigInt(state.entries.at(-1)?.first_seq ?? '0');
  // Changes replay entire turns, including entries before the loaded window.
  const relevant = incoming.filter(
    (entry) => loaded.has(entry.entry_id) || BigInt(entry.first_seq) > last,
  );
  const updates = countUpdates(state.entries, relevant);
  if (updates.length === 0) return state;
  // A historical window must stay contiguous. At the live end, retain every
  // new entry even while scrolling is paused or text is selected.
  const visible = state.bounds?.has_newer
    ? relevant.filter((entry) => loaded.has(entry.entry_id))
    : relevant;
  return {
    ...state,
    entries: mergeEntries(state.entries, visible),
    unread: follow ? state.unread : new Set([...state.unread, ...updates]),
  };
}
