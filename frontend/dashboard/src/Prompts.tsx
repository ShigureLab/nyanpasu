import { useState } from 'react';
import type { TranscriptEntry } from './api-types';
import { useResource, type Navigate } from './api';
import { ContentBlock } from './Entry';
import { Time } from './Time';

function Prompt({ entry, navigate }: { entry: TranscriptEntry; navigate: Navigate }) {
  const [open, setOpen] = useState(false);
  const bytes = entry.blocks.reduce((sum, block) => sum + block.recorded_bytes, 0);
  return (
    <details className="prompt-record" onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>
        {entry.title} · {bytes.toLocaleString()} bytes
      </summary>
      <div className="block-toolbar">
        <Time value={entry.recorded_at} />
        <button onClick={() => navigate({ tab: null, entry: entry.entry_id })}>
          Inspect in conversation
        </button>
      </div>
      {entry.coverage.redacted && <p className="notice">Recognized credentials redacted.</p>}
      {open &&
        entry.blocks.map((block) => (
          <ContentBlock key={block.block_id} block={block} session={entry.session_id} eager />
        ))}
    </details>
  );
}

export function Prompts({
  session,
  live,
  refresh,
  navigate,
}: {
  session: string;
  live: boolean;
  refresh: number;
  navigate: Navigate;
}) {
  const result = useResource<TranscriptEntry[]>(`/api/sessions/${session}/prompts`, live, refresh);
  const bytes = result.data?.reduce(
    (sum, entry) => sum + entry.blocks.reduce((total, block) => total + block.recorded_bytes, 0),
    0,
  );
  return (
    <div className="prompt-list">
      <p className="subtle">
        Recorded system and developer instructions. Nyanpasu injections include only the
        instructions added by Nyanpasu, excluding the CLI's built-in system prompt.
      </p>
      {result.error && (
        <p className="notice error" role="alert">
          {result.error}
        </p>
      )}
      {!result.data && !result.error && <p>Loading prompts…</p>}
      {result.data && (
        <p>
          {result.data.length} records · {bytes?.toLocaleString()} bytes
        </p>
      )}
      {result.data?.length === 0 && (
        <p>No system or developer instructions were recorded for this session.</p>
      )}
      {result.data?.map((entry) => (
        <Prompt key={entry.entry_id} entry={entry} navigate={navigate} />
      ))}
    </div>
  );
}
