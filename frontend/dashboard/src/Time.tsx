const fullTime = new Intl.DateTimeFormat(undefined, {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
});
const detailTime = new Intl.DateTimeFormat(undefined, {
  dateStyle: 'full',
  timeStyle: 'long',
  hour12: false,
});

export function Time({
  value,
  label,
}: {
  value: string | number | null | undefined;
  label?: string;
}) {
  if (value == null) return <span className="time unavailable">Time unavailable</span>;
  const date = new Date(typeof value === 'number' ? value * 1000 : value);
  return (
    <time
      className="time"
      dateTime={date.toISOString()}
      title={`${label ? label + ': ' : ''}${detailTime.format(date)}`}
    >
      {fullTime.format(date)}
    </time>
  );
}

export function duration(milliseconds: number): string {
  if (milliseconds < 1000) return `${Math.round(milliseconds)} ms`;
  if (milliseconds < 60_000) return `${(milliseconds / 1000).toFixed(1)} s`;
  return `${Math.floor(milliseconds / 60_000)} min ${Math.round((milliseconds % 60_000) / 1000)} s`;
}
