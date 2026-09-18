import { useEffect, useMemo, useState } from 'react';
import { ApiContext } from './api';
import { ApiError, createApi } from './api-client';
import { App } from './App';

const TOKEN_KEY = 'nyanpasu.dashboard.token';

export function Auth() {
  const [token, setToken] = useState(() => localStorage.getItem(TOKEN_KEY) ?? '');
  const [attempt, setAttempt] = useState(0);
  const [status, setStatus] = useState<'checking' | 'locked' | 'ready'>('checking');
  const [error, setError] = useState('');
  const [draft, setDraft] = useState('');
  const api = useMemo(
    () =>
      createApi(token, () => {
        localStorage.removeItem(TOKEN_KEY);
        setStatus('locked');
        setError('Enter a valid access token to continue.');
      }),
    [token, attempt],
  );

  useEffect(() => {
    let active = true;
    setStatus('checking');
    setError('');
    void api.get('/api/overview').then(
      () => {
        if (!active) return;
        if (token) localStorage.setItem(TOKEN_KEY, token);
        setDraft('');
        setStatus('ready');
      },
      (error: unknown) => {
        if (!active) return;
        setStatus('locked');
        setError(
          error instanceof ApiError && error.status === 401
            ? 'Enter a valid access token to continue.'
            : `Could not connect: ${String(error)}`,
        );
      },
    );
    return () => {
      active = false;
      api.dispose();
    };
  }, [api, token]);

  useEffect(() => {
    const sync = (event: StorageEvent) => {
      if (event.key === TOKEN_KEY || event.key === null) {
        setStatus('checking');
        setToken(localStorage.getItem(TOKEN_KEY) ?? '');
        setAttempt((value) => value + 1);
      }
    };
    window.addEventListener('storage', sync);
    return () => window.removeEventListener('storage', sync);
  }, []);

  if (status === 'ready')
    return (
      <ApiContext value={api}>
        <App
          onSignOut={
            token
              ? () => {
                  api.dispose();
                  localStorage.removeItem(TOKEN_KEY);
                  setStatus('locked');
                  setToken('');
                }
              : undefined
          }
        />
      </ApiContext>
    );

  return (
    <main className="auth-screen">
      <section className="auth-panel">
        <span className="eyebrow">NYANPASU DASHBOARD</span>
        <h1>Unlock your dashboard</h1>
        <p>Enter your access token. It is saved only in this browser.</p>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            setStatus('checking');
            setToken(draft.trim());
            setAttempt((value) => value + 1);
          }}
        >
          <label htmlFor="access-token">Access token</label>
          <input
            id="access-token"
            type="password"
            autoComplete="current-password"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            disabled={status === 'checking'}
          />
          <button type="submit" disabled={status === 'checking'}>
            {status === 'checking' ? 'Connecting…' : 'Open dashboard'}
          </button>
        </form>
        {error && (
          <p className="notice error" role="alert">
            {error}
          </p>
        )}
      </section>
    </main>
  );
}
