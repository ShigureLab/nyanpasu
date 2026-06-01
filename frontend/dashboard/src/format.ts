export function text(value: string | number | null | undefined): string {
  return value === null || value === undefined || value === '' ? '-' : String(value);
}

export function formatTime(seconds: number | null | undefined): string {
  if (!seconds) {
    return '-';
  }
  return new Date(seconds * 1000).toLocaleString();
}

export function formatAge(seconds: number | null | undefined): string {
  const value = Math.max(0, Number(seconds || 0));
  if (value < 60) {
    return `${Math.floor(value)}s`;
  }
  if (value < 3600) {
    return `${Math.floor(value / 60)}m`;
  }
  if (value < 86400) {
    return `${Math.floor(value / 3600)}h ${Math.floor((value % 3600) / 60)}m`;
  }
  return `${Math.floor(value / 86400)}d ${Math.floor((value % 86400) / 3600)}h`;
}

export function firstErrorLine(error: string | null): string {
  return error ? (error.split('\n')[0] ?? '') : '';
}
