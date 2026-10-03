import type { ExecutionTarget } from './api-types';
import { backendLabel } from './api';

export function ExecutionSummary({
  kind,
  backend,
  execution,
}: {
  kind: string;
  backend: string;
  execution: ExecutionTarget | null;
}) {
  return (
    <small className="execution-summary">
      {kind} · {backendLabel(execution?.backend ?? backend)} ·{' '}
      {execution
        ? `${execution.model ?? 'Backend default'} · ${execution.reasoning ?? 'Default reasoning'}`
        : 'Target not recorded'}
    </small>
  );
}

export function ExecutionDetails({
  kind,
  execution,
}: {
  kind: string;
  execution: ExecutionTarget | null;
}) {
  return (
    <section className="execution-details" aria-label="Configured execution target">
      <h3>Configured execution target</h3>
      <p className="subtle">Task kind: {kind}</p>
      {!execution ? (
        <p className="empty">This task has no recorded execution target.</p>
      ) : (
        <dl className="task-metadata">
          {[
            ['backend', 'Backend', execution.backend],
            ['model', 'Model', execution.model ?? 'Backend default'],
            ['reasoning', 'Reasoning', execution.reasoning ?? 'Backend default'],
            ['turn_timeout_seconds', 'Turn timeout', `${execution.turn_timeout_seconds}s`],
          ].map(([field, label, value]) => (
            <div key={field}>
              <dt>{label}</dt>
              <dd>{value}</dd>
              <small className="subtle">
                Source: {execution.sources?.[field] ?? 'Not recorded'}
              </small>
            </div>
          ))}
          <div>
            <dt>Driver</dt>
            <dd>{backendLabel(execution.driver)}</dd>
          </div>
        </dl>
      )}
    </section>
  );
}
