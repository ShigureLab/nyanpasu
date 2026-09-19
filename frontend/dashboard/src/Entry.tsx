import { Time, duration } from './Time';
import { memo, useEffect, useLayoutEffect, useRef, useState } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { TranscriptBlock, TranscriptEntry } from './api-types';
import { useApi, query, type ContentPage, type Navigate } from './api';
import { Download } from './Download';
import { hasTextSelection } from './transcript-model';

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
  compact = false,
  eager = false,
}: {
  block: TranscriptBlock;
  session: string;
  streaming?: boolean;
  focusOffset?: number;
  compact?: boolean;
  eager?: boolean;
}) {
  const { get } = useApi();
  const [page, setPage] = useState<ContentPage | null>(null);
  const [error, setError] = useState('');
  const [plain, setPlain] = useState(false);
  const [position, setPosition] = useState<number | 'tail' | null>(null);
  const [nearby, setNearby] = useState(false);
  const section = useRef<HTMLElement>(null);
  const output = useRef<HTMLPreElement>(null);
  const outputTop = useRef(0);
  const followingOutput = useRef(false);
  const endpoint = `/api/sessions/${session}/content/${block.content_ref}`;
  const isOutput = block.kind === 'combined_output' || block.kind === 'error';
  const longMessage = block.preview_truncated && block.kind === 'markdown';
  const readFullMessage = longMessage && (nearby || eager);
  useEffect(() => {
    setPosition(null);
    followingOutput.current = false;
    outputTop.current = 0;
  }, [focusOffset]);
  useEffect(() => {
    const element = section.current;
    if (!element || !longMessage || nearby || eager) return;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setNearby(true);
          observer.disconnect();
        }
      },
      { root: element.closest('.transcript-scroll, .inspector'), rootMargin: '400px' },
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, [longMessage, nearby, eager]);

  useEffect(() => {
    if (
      streaming &&
      isOutput &&
      block.preview_truncated &&
      position === null &&
      focusOffset === undefined
    ) {
      followingOutput.current = true;
      setPosition('tail');
    }
  }, [streaming, isOutput, block.preview_truncated, position, focusOffset]);

  useEffect(() => {
    setError('');
    const offset = position ?? focusOffset ?? null;
    if (offset === null && !readFullMessage) {
      setPage(null);
      return;
    }
    const controller = new AbortController();
    async function read() {
      try {
        let value = await get<ContentPage>(
          query(endpoint, offset === 'tail' ? { tail: true } : { offset: offset ?? 0 }),
          controller.signal,
        );
        if (offset === null) {
          const parts = [value.text];
          while (value.next_offset !== null && !controller.signal.aborted) {
            value = await get<ContentPage>(
              query(endpoint, { offset: value.next_offset }),
              controller.signal,
            );
            parts.push(value.text);
          }
          value = { ...value, offset: 0, text: parts.join('') };
        }
        if (!controller.signal.aborted) setPage(value);
      } catch (error) {
        if (!controller.signal.aborted) setError(String(error));
      }
    }
    void read();
    return () => controller.abort();
  }, [get, endpoint, focusOffset, readFullMessage, position]);

  function load(offset: number | 'tail') {
    followingOutput.current = offset === 'tail';
    outputTop.current = 0;
    setPosition(offset);
    if (output.current)
      output.current.scrollTop = offset === 'tail' ? output.current.scrollHeight : 0;
  }
  const text = page?.text ?? block.preview;
  useLayoutEffect(() => {
    const element = output.current;
    if (!element) return;
    element.scrollTop =
      followingOutput.current && !hasTextSelection(element)
        ? element.scrollHeight
        : outputTop.current;
  }, [text]);
  const complete = page ? page.offset === 0 && page.next_offset === null : !block.preview_truncated;
  const markdown = block.kind === 'markdown' && !streaming && complete && !plain;
  const loadingMessage = readFullMessage && focusOffset === undefined && !page && !error;
  // Terminal controls are displayed as text, never sent to a terminal or interpreted as HTML.
  const visible = plain
    ? text.replaceAll('\x1b', '\\x1b')
    : text.replace(new RegExp(String.fromCharCode(27) + '\\[[0-?]*[ -/]*[@-~]', 'g'), '');
  return (
    <section
      ref={section}
      className={`content-block content-${block.kind} ${compact ? 'compact' : ''}`}
      aria-label={block.block_id}
    >
      <div className="block-toolbar">
        {!compact && (
          <span>
            {block.block_id.replaceAll('_', ' ')} ·{' '}
            {(page?.recorded_bytes ?? block.recorded_bytes).toLocaleString()} bytes
          </span>
        )}
        <div>
          {!compact && block.kind === 'markdown' && (
            <button className="quiet" onClick={() => setPlain(!plain)}>
              {plain ? 'Markdown' : 'Plain text'}
            </button>
          )}
          <Copy text={text} label={complete ? 'Copy source' : 'Copy displayed text'} />
          {!compact && (
            <Download
              path={query(endpoint, { download: true })}
              filename={`${block.content_ref}.txt`}
            >
              Download full content
            </Download>
          )}
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
        <pre
          ref={output}
          tabIndex={0}
          className={focusOffset !== undefined ? 'search-focus' : ''}
          onWheel={(event) => {
            if (event.currentTarget.scrollHeight > event.currentTarget.clientHeight)
              event.stopPropagation();
          }}
          onScroll={(event) => {
            const element = event.currentTarget;
            outputTop.current = element.scrollTop;
            const atBottom = element.scrollHeight - element.scrollTop - element.clientHeight < 8;
            followingOutput.current = atBottom && !hasTextSelection(element);
            if (!atBottom && position === 'tail' && page) setPosition(page.offset);
            else if (
              isOutput &&
              atBottom &&
              page?.next_offset === null &&
              focusOffset === undefined
            )
              setPosition('tail');
          }}
        >
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
          {page && <button onClick={() => load(0)}>Start</button>}
          <button onClick={() => load('tail')} aria-pressed={position === 'tail'}>
            Tail
          </button>
          {page?.next_offset !== null && (
            <button onClick={() => load(page?.next_offset ?? 0)}>
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
}: {
  entry: TranscriptEntry;
  navigate: Navigate;
  selected: boolean;
  expand?: boolean;
  focus?: { block: string; offset: number; ref: string };
}) {
  const [manualOpen, setManualOpen] = useState<boolean | null>(null);
  useEffect(() => {
    if (expand) setManualOpen(true);
    else if (entry.state === 'running') setManualOpen((current) => current ?? true);
  }, [expand, focus?.block, focus?.offset, focus?.ref, entry.state]);
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
          <strong>{entry.tool_name ?? entry.title}</strong>
          <span aria-hidden="true">{open ? '▾' : '▸'}</span>
        </button>
        <Status state={entry.state} />
        <div className="entry-timing">
          <Time
            value={entry.started_at ?? entry.completed_at ?? entry.recorded_at}
            label={entry.started_at ? 'Started' : entry.completed_at ? 'Completed' : 'Recorded'}
          />
          {(entry.duration_ms != null || (entry.started_at && entry.completed_at)) && (
            <small
              title={
                entry.completed_at
                  ? `Completed: ${new Date(entry.completed_at).toLocaleString()}`
                  : undefined
              }
            >
              {duration(
                entry.duration_ms ??
                  Date.parse(entry.completed_at!) - Date.parse(entry.started_at!),
              )}
              {entry.completed_at ? ' · completed' : ''}
            </small>
          )}
        </div>
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
          {entry.exit_code !== null && <span>exit {entry.exit_code}</span>}
        </div>
      )}
      {entry.kind === 'attachment' && (
        <p className="notice">
          Attachment metadata is recorded below. Image bytes are only available when supplied by the
          backend; local paths are not opened by this page.
        </p>
      )}
      {entry.decision && <p className="notice">Response sent: {entry.decision}</p>}
      {entry.coverage.source_truncated && (
        <p className="notice">The backend truncated this content.</p>
      )}
      {entry.coverage.redacted && <p className="notice">Recognized credentials redacted.</p>}
      {open ? (
        entry.blocks.map((block) => {
          const focused = focus?.block === block.block_id;
          const changed = focused && focus.ref !== block.content_ref;
          const content = (
            <ContentBlock
              block={block}
              session={entry.session_id}
              streaming={entry.state === 'running'}
              focusOffset={focused && !changed ? focus.offset : undefined}
              compact={['input', 'message'].includes(entry.kind) && block.kind === 'markdown'}
              eager={selected}
            />
          );
          return (
            <div key={block.block_id}>
              {changed && (
                <p className="notice">
                  Session content has changed since this search. Showing current content.
                </p>
              )}
              {content}
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
