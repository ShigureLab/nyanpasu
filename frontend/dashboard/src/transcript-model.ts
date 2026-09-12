import type { TranscriptEntry } from './api-types';

export function mergeEntries(
  current: readonly TranscriptEntry[],
  incoming: readonly TranscriptEntry[],
): TranscriptEntry[] {
  const entries = new Map(current.map((entry) => [entry.entry_id, entry]));
  for (const entry of incoming) {
    const previous = entries.get(entry.entry_id);
    if (!previous || BigInt(entry.revision_seq) > BigInt(previous.revision_seq))
      entries.set(entry.entry_id, entry);
  }
  return [...entries.values()].sort((a, b) => (BigInt(a.first_seq) < BigInt(b.first_seq) ? -1 : 1));
}

export function countUpdates(
  current: readonly TranscriptEntry[],
  incoming: readonly TranscriptEntry[],
): string[] {
  const revisions = new Map(current.map((entry) => [entry.entry_id, entry.revision_seq]));
  return incoming
    .filter(
      (entry) =>
        !revisions.has(entry.entry_id) ||
        BigInt(entry.revision_seq) > BigInt(revisions.get(entry.entry_id)!),
    )
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

export interface TranscriptState {
  entries: TranscriptEntry[];
  unread: Set<string>;
  reloadWindow: boolean;
}

export function applyChanges(
  state: TranscriptState,
  incoming: readonly TranscriptEntry[],
  follow: boolean,
): TranscriptState {
  const updates = countUpdates(state.entries, incoming);
  const visible = follow
    ? incoming
    : incoming.filter((entry) =>
        state.entries.some((current) => current.entry_id === entry.entry_id),
      );
  const merged = mergeEntries(state.entries, visible);
  return {
    entries: merged.slice(-300),
    unread: follow ? state.unread : new Set([...state.unread, ...updates]),
    reloadWindow: follow && merged.length > 300,
  };
}
