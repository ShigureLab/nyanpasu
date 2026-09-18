export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(`${status}: ${message}`);
  }
}

export function createApi(token: string, onUnauthorized: () => void = () => {}) {
  const lifetime = new AbortController();
  async function request(path: string, signal?: AbortSignal) {
    const response = await fetch(path, {
      signal: signal ? AbortSignal.any([signal, lifetime.signal]) : lifetime.signal,
      cache: 'no-store',
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (response.status === 401) {
      onUnauthorized();
      lifetime.abort();
      throw new ApiError(401, 'Invalid or missing bearer token');
    }
    if (!response.ok) {
      const message = (await response.json().catch(() => ({ detail: response.statusText }))) as {
        detail?: unknown;
      };
      throw new ApiError(response.status, String(message.detail ?? response.statusText));
    }
    return response;
  }
  return {
    async get<T>(path: string, signal?: AbortSignal): Promise<T> {
      return (await request(path, signal)).json() as Promise<T>;
    },
    async download(path: string, filename: string) {
      const response = await request(path);
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement('a');
      link.href = url;
      link.download = filename;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 0);
    },
    dispose: () => lifetime.abort(),
  };
}
