import { useState } from 'react';
import type { ModelUsageCostBucket, ModelUsageTotals } from './api-types';

const dollars = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' });
const fractionalDollars = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 4,
});
const hour = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit' });
const day = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' });
const dateTime = new Intl.DateTimeFormat(undefined, {
  month: 'short',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
});

function formatUsd(amount: number) {
  if (amount > 0 && amount < 0.0001) return '<$0.0001';
  return (amount > 0 && amount < 0.01 ? fractionalDollars : dollars).format(amount);
}

type UsageCost = Pick<ModelUsageTotals, 'cost_usd' | 'calls' | 'priced_calls'>;

function estimatedCost(usage: UsageCost) {
  return usage.calls ? usage.cost_usd : 0;
}

function coverage(usage: UsageCost) {
  if (!usage.calls) return 'No recorded calls';
  if (!usage.priced_calls) return 'No matching API rates';
  return `${usage.priced_calls.toLocaleString('en-US')} of ${usage.calls.toLocaleString('en-US')} calls priced`;
}

export function Cost({ usage }: { usage: UsageCost }) {
  const amount = estimatedCost(usage);
  return (
    <span title={coverage(usage)}>
      {amount === null ? '—' : formatUsd(amount)}
      {amount !== null && usage.priced_calls < usage.calls && (
        <sup aria-label="Partial estimate">*</sup>
      )}
    </span>
  );
}

export function UsageCostChart({
  buckets,
  hourly,
}: {
  buckets: ModelUsageCostBucket[];
  hourly: boolean;
}) {
  const [index, setIndex] = useState(buckets.length - 1);
  const bucket = buckets[index];
  const amounts = buckets.map(estimatedCost);
  const peak = Math.max(0, ...amounts.map((value) => value ?? 0));
  const x = (i: number) => `${((i + 0.5) / buckets.length) * 100}%`;
  const y = (amount: number) => `${peak ? 100 - (amount / peak) * 100 : 100}%`;
  const label = hourly ? 'Hourly estimated cost' : 'Daily estimated cost';
  const interval = dateTime.formatRange(bucket.start_at * 1000, bucket.end_at * 1000);
  const start = buckets[0].start_at;
  const end = buckets[buckets.length - 1].end_at;
  const axis = hourly ? hour : day;

  return (
    <figure className="usage-cost-chart">
      <figcaption>
        <strong>{label}</strong>
        <span>
          {interval} · <Cost usage={bucket} />
          {bucket.priced_calls < bucket.calls && ` · ${coverage(bucket)}`}
        </span>
      </figcaption>
      <div className="usage-cost-axes">
        <div className="usage-cost-scale" aria-hidden="true">
          <span>{peak > 0 && formatUsd(peak)}</span>
          <span>{peak > 0 && formatUsd(peak / 2)}</span>
          <span>{formatUsd(0)}</span>
        </div>
        <svg
          className="usage-cost-plot"
          role="slider"
          tabIndex={0}
          aria-label={label}
          aria-valuemin={1}
          aria-valuemax={buckets.length}
          aria-valuenow={index + 1}
          aria-valuetext={`${interval}, ${amounts[index] === null ? '—' : formatUsd(amounts[index])}, ${coverage(bucket)}`}
          onKeyDown={(event) => {
            const next = {
              ArrowRight: index + 1,
              ArrowUp: index + 1,
              ArrowLeft: index - 1,
              ArrowDown: index - 1,
              Home: 0,
              End: buckets.length - 1,
            }[event.key];
            if (next === undefined) return;
            event.preventDefault();
            setIndex(Math.max(0, Math.min(next, buckets.length - 1)));
          }}
        >
          {[0, 50, 100].map((tick) => (
            <line
              key={tick}
              x1="0"
              x2="100%"
              y1={`${tick}%`}
              y2={`${tick}%`}
              stroke="var(--line)"
            />
          ))}
          <line
            x1={x(index)}
            x2={x(index)}
            y1="0"
            y2="100%"
            stroke="var(--muted)"
            strokeDasharray="3 4"
          />
          {amounts.map((amount, i) => {
            const previous = amounts[i - 1];
            return (
              <g key={i}>
                {amount != null && previous != null && (
                  <line
                    x1={x(i - 1)}
                    y1={y(previous)}
                    x2={x(i)}
                    y2={y(amount)}
                    stroke="currentColor"
                    strokeWidth="2"
                  />
                )}
                {amount != null && (
                  <circle cx={x(i)} cy={y(amount)} r={i === index ? 4 : 2.5} fill="currentColor" />
                )}
              </g>
            );
          })}
          {buckets.map((_, i) => (
            <rect
              key={i}
              x={`${(100 * i) / buckets.length}%`}
              width={`${100 / buckets.length}%`}
              height="100%"
              fill="transparent"
              onPointerEnter={() => setIndex(i)}
              onPointerDown={() => setIndex(i)}
            />
          ))}
        </svg>
        <div className="usage-cost-times" aria-hidden="true">
          {[start, (start + end) / 2, end].map((time) => (
            <span key={time}>{axis.format(time * 1000)}</span>
          ))}
        </div>
      </div>
    </figure>
  );
}
