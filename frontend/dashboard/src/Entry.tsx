import { memo, useEffect, useRef, useState } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { TranscriptBlock, TranscriptEntry } from './api-types';
import { get, query, type ContentPage, type Navigate } from './api';

export function Status({ state }: { state: string }) {
  const symbol = ['failed', 'interrupted', 'declined'].includes(state)
    ? '!'
    : state === 'running' || state === 'pending'
      ? '◌'
      : state === 'completed'
        ? '✓'
        : '·';
  return (
    <span className={`status ${state}`}>
      <span aria-hidden="true">{symbol} </span>
      {state}
    </span>
  );
}

export function Copy({ text, label = 'Copy' }: { text: string; label?: string }) {
  const [feedback, setFeedback] = useState('');
  return (
    <button
      className="quiet"
      onClick={() => {
        void navigator.clipboard.writeText(text).then(
          () => setFeedback('Copied'),
          () => setFeedback('Copy failed'),
        );
      }}
      aria-label={label}
    >
      {feedback || label}
    </button>
  );
}

const MarkdownBody = memo(function MarkdownBody({ text }: { text: string }) {
  return (
    <div className="markdown">
      <Markdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        components={{
          a: ({ children, href }) => (
            <a href={href} target="_blank" rel="noreferrer noopener">
              {children}
            </a>
          ),
          img: ({ src, alt }) => (
            <a href={src} target="_blank" rel="noreferrer noopener">
              Image: {alt || 'open attachment'}
            </a>
          ),
        }}
      >
        {text}
      </Markdown>
    </div>
  );
});

export function ContentBlock({
  block,
  session,
  streaming = false,
  focusOffset,
}: {
  block: TranscriptBlock;
  session: string;
  streaming?: boolean;
  focusOffset?: number;
}) {
  const [page, setPage] = useState<ContentPage | null>(null);
  const [error, setError] = useState('');
  const [plain, setPlain] = useState(false);
  const requestVersion = useRef(0);
  const endpoint = `/api/sessions/${session}/content/${block.content_ref}`;
  const readFullMessage =
    block.preview_truncated && ['markdown', 'actual_input'].includes(block.kind);
  useEffect(() => {
    const version = ++requestVersion.current;
    setPage(null);
    setError('');
    const controller = new AbortController();
    async function read() {
      try {
        let value = await get<ContentPage>(
          query(endpoint, { offset: focusOffset ?? 0 }),
          controller.signal,
        );
        if (focusOffset === undefined) {
          const parts = [value.text];
          while (value.next_offset !== null && version === requestVersion.current) {
            value = await get<ContentPage>(
              query(endpoint, { offset: value.next_offset }),
              controller.signal,
            );
            parts.push(value.text);
          }
          value = { ...value, offset: 0, text: parts.join('') };
        }
        if (version === requestVersion.current) setPage(value);
      } catch (error) {
        if (!controller.signal.aborted && version === requestVersion.current)
          setError(String(error));
      }
    }
    if (focusOffset !== undefined || readFullMessage) void read();
    return () => {
      controller.abort();
      requestVersion.current += 1;
    };
  }, [endpoint, focusOffset, readFullMessage]);
  async function load(offset: number | 'tail') {
    const version = ++requestVersion.current;
    try {
      const value = await get<ContentPage>(
        query(endpoint, typeof offset === 'number' ? { offset } : { tail: true }),
      );
      if (version === requestVersion.current) {
        setPage(value);
        setError('');
      }
    } catch (error) {
      if (version === requestVersion.current) setError(String(error));
    }
  }
  const text = page?.text ?? block.preview;
  const complete = page ? page.offset === 0 && page.next_offset === null : !block.preview_truncated;
  const markdown = block.kind === 'markdown' && !streaming && complete && !plain;
  const loadingMessage = readFullMessage && focusOffset === undefined && !page && !error;
  // Terminal controls are displayed as text, never sent to a terminal or interpreted as HTML.
  const visible = plain
    ? text.replaceAll('\x1b', '\\x1b')
    : text.replace(new RegExp(String.fromCharCode(27) + '\\[[0-?]*[ -/]*[@-~]', 'g'), '');
  return (
    <section className={`content-block ${block.kind}`} aria-label={block.block_id}>
      <div className="block-toolbar">
        <span>
          {block.block_id.replaceAll('_', ' ')} ·{' '}
          {(page?.recorded_bytes ?? block.recorded_bytes).toLocaleString()} bytes
        </span>
        <div>
          {block.kind === 'markdown' && (
            <button className="quiet" onClick={() => setPlain(!plain)}>
              {plain ? 'Markdown' : 'Plain text'}
            </button>
          )}
          <Copy text={text} label={complete ? 'Copy source' : 'Copy displayed text'} />
          <a href={query(endpoint, { download: true })} download>
            Download full content
          </a>
        </div>
      </div>
      {error && (
        <p role="alert" className="notice error">
          {error}
        </p>
      )}
      {markdown ? (
        <MarkdownBody text={text} />
      ) : (
        <pre tabIndex={0} className={focusOffset !== undefined ? 'search-focus' : ''}>
          {block.kind === 'diff'
            ? visible.split('\n').map((line, index) => (
                <span
                  key={index}
                  className={
                    line.startsWith('+')
                      ? 'diff-added'
                      : line.startsWith('-')
                        ? 'diff-removed'
                        : line.startsWith('@@')
                          ? 'diff-hunk'
                          : ''
                  }
                >
                  {line}
                  {'\n'}
                </span>
              ))
            : visible}
        </pre>
      )}
      {loadingMessage && <p className="subtle">Loading complete message…</p>}
      {!complete && !loadingMessage && (
        <div className="content-pages">
          <span>
            {page
              ? `Bytes ${page.offset}–${page.offset + new TextEncoder().encode(page.text).length} of ${page.recorded_bytes}`
              : 'Showing the beginning of this content'}
          </span>
          {page && <button onClick={() => void load(0)}>Start</button>}
          <button onClick={() => void load('tail')}>Tail</button>
          {page?.next_offset !== null && (
            <button onClick={() => void load(page?.next_offset ?? 0)}>
              {page ? 'Next section' : 'Expand'}
            </button>
          )}
        </div>
      )}
    </section>
  );
}

export const Entry = memo(function Entry({
  entry,
  navigate,
  selected,
  expand,
  focus,
  sessionMissingParts,
}: {
  entry: TranscriptEntry;
  navigate: Navigate;
  selected: boolean;
  expand?: boolean;
  focus?: { block: string; offset: number; ref: string };
  sessionMissingParts: readonly string[];
}) {
  const [manualOpen, setManualOpen] = useState<boolean | null>(null);
  useEffect(() => {
    if (expand) setManualOpen(true);
  }, [expand, focus?.block, focus?.offset, focus?.ref]);
  const isTool = [
    'tool',
    'file_change',
    'approval',
    'reasoning',
    'runtime',
    'unknown',
    'plan',
    'collaboration',
    'attachment',
  ].includes(entry.kind);
  const open =
    manualOpen ??
    (expand ||
      !isTool ||
      ['running', 'failed', 'interrupted', 'pending', 'declined'].includes(entry.state));
  const missingParts = entry.coverage.missing_parts.filter(
    (part) => !sessionMissingParts.includes(part),
  );
  return (
    <article
      data-entry-id={entry.entry_id}
      id={entry.entry_id}
      className={`entry ${entry.kind} ${selected ? 'selected' : ''}`}
    >
      <div className="entry-heading">
        <button className="entry-toggle" aria-expanded={open} onClick={() => setManualOpen(!open)}>
          <span className="role">
            {entry.phase === 'final_answer' ? 'Final response' : entry.kind}
          </span>
          <strong>{entry.title}</strong>
          <span aria-hidden="true">{open ? '▾' : '▸'}</span>
        </button>
        <Status state={entry.state} />
        <button
          className="quiet"
          onClick={() =>
            navigate({ entry: entry.entry_id, block: null, content: null, offset: null })
          }
        >
          Inspect
        </button>
      </div>
      {entry.command && <pre className="command">{entry.command}</pre>}
      {(entry.cwd || entry.exit_code !== null || entry.duration_ms !== null) && (
        <div className="entry-meta">
          {entry.cwd && <code>{entry.cwd}</code>}
          <span>exit {entry.exit_code ?? 'unknown'}</span>
          {entry.duration_ms !== null && <span>{entry.duration_ms} ms</span>}
        </div>
      )}
      {entry.kind === 'attachment' && (
        <p className="notice">
          Attachment metadata is recorded below. Image bytes are only available when supplied by the
          backend; local paths are not opened by this page.
        </p>
      )}
      {entry.decision && <p className="notice">Response sent: {entry.decision}</p>}
      {entry.coverage.capture_gap && missingParts.length > 0 && (
        <p className="notice">{missingParts.join(' · ')}</p>
      )}
      {entry.coverage.source_truncated && (
        <p className="notice">The backend truncated this content.</p>
      )}
      {entry.coverage.redacted && <p className="notice">Recognized credentials redacted.</p>}
      {open ? (
        entry.blocks.map((block) => {
          const focused = focus?.block === block.block_id;
          const content = (
            <ContentBlock
              block={focused ? { ...block, content_ref: focus.ref } : block}
              session={entry.session_id}
              streaming={entry.state === 'running'}
              focusOffset={focused ? focus.offset : undefined}
            />
          );
          return (
            <div key={block.block_id}>
              {focused && focus.ref !== block.content_ref && (
                <p className="notice">Showing the saved version linked by this search result.</p>
              )}
              {block.kind === 'actual_input' || block.block_id === 'context' ? (
                <details open={focus?.block === block.block_id}>
                  <summary>
                    {block.kind === 'actual_input' ? 'Actual submitted input' : 'Execution context'}
                  </summary>
                  {content}
                </details>
              ) : (
                content
              )}
            </div>
          );
        })
      ) : (
        <p className="entry-preview">
          {entry.blocks.find((block) => block.kind !== 'json')?.preview.slice(0, 240) ||
            'Expand to read saved content'}
        </p>
      )}
    </article>
  );
});
