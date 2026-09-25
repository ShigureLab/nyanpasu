import { backendLabel, type Navigate, type SessionDetail } from './api';
import { Copy } from './Entry';
import { Time } from './Time';

export function SessionDetails({
  detail,
  session,
  navigate,
}: {
  detail?: SessionDetail;
  session: string;
  navigate: Navigate;
}) {
  return (
    <section className="session-details-card">
      <h2>Session details</h2>
      <dl className="session-metadata">
        <div>
          <dt>Native session ID</dt>
          <dd>
            <code>{detail?.thread_id ?? session}</code>
            <Copy text={detail?.thread_id ?? session} label="Copy session ID" />
          </dd>
        </div>
        <div>
          <dt>Backend</dt>
          <dd>{detail ? backendLabel(detail.backend) : 'Loading…'}</dd>
        </div>
        <div>
          <dt>Context key</dt>
          <dd>
            <button
              className="quiet"
              onClick={() => navigate({ context: detail?.context_key ?? null })}
            >
              <code>{detail?.context_key ?? 'Loading…'}</code>
            </button>
          </dd>
        </div>
        {detail?.runtime && (
          <>
            <div>
              <dt>Model</dt>
              <dd>
                {detail.runtime.model ?? 'Unavailable'}{' '}
                <span className="subtle">{detail.runtime.reasoning_effort}</span>
              </dd>
            </div>
            <div>
              <dt>Workspace</dt>
              <dd>
                <code>{detail.runtime.cwd ?? 'Unavailable'}</code>
              </dd>
            </div>
            <div>
              <dt>Created</dt>
              <dd>
                <Time value={detail.runtime.created_at} />
              </dd>
            </div>
            <div>
              <dt>Updated</dt>
              <dd>
                <Time value={detail.runtime.updated_at} />
              </dd>
            </div>
          </>
        )}
      </dl>
    </section>
  );
}
