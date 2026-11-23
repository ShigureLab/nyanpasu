import { useState, type ReactNode } from 'react';
import type { ModelUsageSnapshot, ModelUsageTotals, ModelUsageWindow } from './api-types';
import { query, useResource } from './api';
import { Time } from './Time';
import { Cost, UsageCostChart } from './UsageCostChart';

const count = new Intl.NumberFormat('en-US');
const compact = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 });
const percent = new Intl.NumberFormat('en-US', { style: 'percent', maximumFractionDigits: 1 });
const periods = [
  { key: 'last_24h', short: '24h', label: 'Last 24 hours' },
  { key: 'last_7d', short: '7d', label: 'Last 7 days' },
  { key: 'last_30d', short: '30d', label: 'Last 30 days' },
] as const;
const dimensions = [
  { key: 'by_agent', label: 'Agents', column: 'Agent' },
  { key: 'by_model', label: 'Models', column: 'Model' },
  { key: 'top_sessions', label: 'Sessions', column: 'Session' },
] as const;
type Dimension = (typeof dimensions)[number];
type UsageRow = { key: string; name: ReactNode; usage: ModelUsageTotals };

function rate(hits: number, total: number) {
  return total ? hits / total : null;
}

function rateLabel(value: number | null) {
  return value === null ? '—' : percent.format(value);
}

function Count({ value }: { value: number }) {
  return <span title={count.format(value)}>{compact.format(value)}</span>;
}

function dimensionRows(usage: ModelUsageWindow, dimension: Dimension): UsageRow[] {
  switch (dimension.key) {
    case 'by_agent':
      return usage.by_agent.map((item) => ({
        key: JSON.stringify(item.agent),
        name: item.agent ?? 'Unattributed',
        usage: item,
      }));
    case 'by_model':
      return usage.by_model.map((item) => ({
        key: JSON.stringify([item.backend, item.model]),
        name: (
          <>
            {item.model ?? 'Unknown model'} <small>{item.backend}</small>
          </>
        ),
        usage: item,
      }));
    case 'top_sessions':
      return usage.top_sessions.map((item) => ({
        key: JSON.stringify([item.backend, item.thread_id]),
        name: (
          <>
            {item.session_id ? (
              <a
                href={query('', { view: 'sessions', session: item.session_id })}
                title={item.thread_id}
              >
                {item.context_key}
              </a>
            ) : (
              <span title={item.thread_id}>{item.context_key}</span>
            )}
            <small>{item.backend}</small>
          </>
        ),
        usage: item,
      }));
  }
}

function UsageTable({
  label,
  dimension,
  rows,
}: {
  label: string;
  dimension: string;
  rows: UsageRow[];
}) {
  return (
    <div className="runtime-usage-table">
      <table aria-label={label}>
        <thead>
          <tr>
            <th scope="col">{dimension}</th>
            <th scope="col">Est. USD</th>
            <th scope="col">Total tokens</th>
            <th scope="col">Calls</th>
            <th scope="col">Input</th>
            <th scope="col">Output</th>
            <th scope="col">Token cache hit</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(({ key, name, usage }) => (
            <tr key={key}>
              <th scope="row">{name}</th>
              <td>
                <Cost usage={usage} />
              </td>
              <td>
                <Count value={usage.input_tokens + usage.output_tokens} />
              </td>
              <td>
                <Count value={usage.calls} />
              </td>
              <td>
                <Count value={usage.input_tokens} />
              </td>
              <td>
                <Count value={usage.output_tokens} />
              </td>
              <td>{rateLabel(rate(usage.cached_input_tokens, usage.input_tokens))}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ModelUsage({ live, refresh }: { live: boolean; refresh: number }) {
  const { data, error } = useResource<ModelUsageSnapshot>('/api/model-usage', live, refresh, 30000);
  const [period, setPeriod] = useState<(typeof periods)[number]>(periods[0]);
  const [dimension, setDimension] = useState<Dimension>(dimensions[0]);
  const current = data?.periods[period.key];
  const rows = current ? dimensionRows(current, dimension) : [];
  const tokenHit = current ? rate(current.cached_input_tokens, current.input_tokens) : null;
  const requestHit = current ? rate(current.cached_calls, current.calls) : null;
  const uncachedInput = current
    ? current.input_tokens - current.cached_input_tokens - current.cache_write_tokens
    : 0;
  return (
    <section className="runtime-section runtime-usage" aria-labelledby="runtime-usage-heading">
      <div className="runtime-usage-heading">
        <h2 id="runtime-usage-heading">Model usage</h2>
        <div className="runtime-usage-switch" role="group" aria-label="Usage time range">
          {periods.map((item) => (
            <button
              type="button"
              key={item.key}
              aria-pressed={period.key === item.key}
              title={item.label}
              onClick={() => setPeriod(item)}
            >
              {item.short}
            </button>
          ))}
        </div>
      </div>
      {error && <p className="notice error">Model usage is temporarily unavailable.</p>}
      {!data && !error && (
        <p className="empty" role="status">
          Reading native session usage…
        </p>
      )}
      {data && current && (
        <>
          <dl className="runtime-usage-metrics">
            <div>
              <dt>Estimated cost</dt>
              <dd className="runtime-usage-value">
                <Cost usage={current} />
              </dd>
              <dd>
                {current.priced_calls > 0 ? (
                  <>
                    USD · <Count value={current.priced_calls} /> of <Count value={current.calls} />{' '}
                    calls priced
                  </>
                ) : current.calls ? (
                  'No matching API rates'
                ) : (
                  'No recorded calls'
                )}
              </dd>
            </div>
            <div>
              <dt>Total tokens</dt>
              <dd className="runtime-usage-value">
                <Count value={current.input_tokens + current.output_tokens} />
              </dd>
              <dd>
                <Count value={current.output_tokens} /> output tokens
              </dd>
            </div>
            <div>
              <dt>Model calls</dt>
              <dd className="runtime-usage-value">
                <Count value={current.calls} />
              </dd>
              <dd>
                <Count value={current.cached_calls} /> reused cache
              </dd>
            </div>
            <div>
              <dt>Input tokens</dt>
              <dd className="runtime-usage-value">
                <Count value={current.input_tokens} />
              </dd>
              <dd>
                <Count value={uncachedInput} /> uncached
              </dd>
            </div>
            <div>
              <dt>Token cache hit</dt>
              <dd className="runtime-usage-value">{rateLabel(tokenHit)}</dd>
              <dd>
                <Count value={current.cached_input_tokens} /> cached input tokens
              </dd>
              {tokenHit !== null && (
                <dd>
                  <meter aria-label="Token cache hit rate" value={tokenHit} />
                </dd>
              )}
            </div>
          </dl>
          <UsageCostChart
            key={period.key}
            buckets={current.cost_buckets}
            hourly={period.key === 'last_24h'}
          />
          <div className="runtime-usage-toolbar">
            <div className="runtime-usage-switch" role="group" aria-label="Group usage by">
              {dimensions.map((item) => (
                <button
                  type="button"
                  key={item.key}
                  aria-pressed={dimension.key === item.key}
                  onClick={() => setDimension(item)}
                >
                  {item.label}
                </button>
              ))}
            </div>
            {rows.length > 0 && (
              <span>
                {dimension.key === 'top_sessions' ? 'Top ' : ''}
                {count.format(rows.length)}{' '}
                {rows.length === 1 ? dimension.column.toLowerCase() : dimension.label.toLowerCase()}
                {' · by total tokens'}
              </span>
            )}
          </div>
          {rows.length ? (
            <UsageTable
              label={`Usage by ${dimension.column.toLowerCase()}`}
              dimension={dimension.column}
              rows={rows}
            />
          ) : (
            <p className="empty">No recorded usage in this period.</p>
          )}
          <details className="runtime-notes runtime-usage-details">
            <summary>Token details</summary>
            <dl className="runtime-usage-tokens">
              <div>
                <dt>Uncached input</dt>
                <dd>
                  <Count value={uncachedInput} />
                </dd>
              </div>
              <div>
                <dt>Cache read</dt>
                <dd>
                  <Count value={current.cached_input_tokens} />
                </dd>
              </div>
              <div>
                <dt>Cache write</dt>
                <dd>
                  <Count value={current.cache_write_tokens} />
                </dd>
              </div>
              <div>
                <dt>Output</dt>
                <dd>
                  <Count value={current.output_tokens} />
                </dd>
              </div>
              <div>
                <dt>Request cache hit</dt>
                <dd>{rateLabel(requestHit)}</dd>
              </div>
            </dl>
            <h3>Usage by period</h3>
            <UsageTable
              label="Usage by period"
              dimension="Period"
              rows={[
                ...periods.map(({ key, label }) => ({
                  key,
                  name: label,
                  usage: data.periods[key],
                })),
                { key: 'all', name: 'All recorded time', usage: data.periods.all_time },
              ]}
            />
            <p>Agents are grouped by recorded task kind. Input includes cache reads and writes.</p>
            <p>
              USD estimates use OpenAI Standard and DeepSeek peak API token rates. * indicates
              partial coverage.
            </p>
          </details>
          <footer className="runtime-usage-footer">
            {current.calls > 0 && data.latest_at != null && (
              <span>
                Latest call <Time value={data.latest_at} />
              </span>
            )}
            {data.unavailable_sessions > 0 && (
              <span>
                Session logs unavailable for {count.format(data.unavailable_sessions)} sessions.
              </span>
            )}
          </footer>
        </>
      )}
    </section>
  );
}
