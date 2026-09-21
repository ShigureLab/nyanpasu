import { useEffect, useMemo, useState } from 'react';
import { ApiContext } from './api';
import { createApi } from './api-client';
import { App } from './App';

const TOKEN_KEY = 'nyanpasu.dashboard.token';

export function Auth() {
  const [token, setToken] = useState(() => localStorage.getItem(TOKEN_KEY) ?? '');
  const [attempt, setAttempt] = useState(0);
  const [locked, setLocked] = useState(false);
  const [error, setError] = useState('');
  const [draft, setDraft] = useState('');
  const api = useMemo(
    () =>
      createApi(token, () => {
        localStorage.removeItem(TOKEN_KEY);
        setLocked(true);
        setError('Enter a valid access token to continue.');
      }),
    [token, attempt],
  );

  useEffect(() => () => api.dispose(), [api]);

  useEffect(() => {
    const sync = (event: StorageEvent) => {
      if (event.key === TOKEN_KEY || event.key === null) {
        const nextToken = localStorage.getItem(TOKEN_KEY) ?? '';
        if (nextToken === token && !locked) return;
        api.dispose();
        setLocked(!nextToken);
        setError('');
        setToken(nextToken);
        setAttempt((value) => value + 1);
      }
    };
    window.addEventListener('storage', sync);
    return () => window.removeEventListener('storage', sync);
  }, [api, token, locked]);

  if (!locked)
    return (
      <ApiContext value={api}>
        <App
          key={attempt}
          onSignOut={
            token
              ? () => {
                  api.dispose();
                  localStorage.removeItem(TOKEN_KEY);
                  setLocked(true);
                  setError('');
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
            const nextToken = draft.trim();
            if (nextToken) localStorage.setItem(TOKEN_KEY, nextToken);
            else localStorage.removeItem(TOKEN_KEY);
            setToken(nextToken);
            setAttempt((value) => value + 1);
            setDraft('');
            setError('');
            setLocked(false);
          }}
        >
          <label htmlFor="access-token">Access token</label>
          <input
            id="access-token"
            type="password"
            autoComplete="current-password"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
          />
          <button type="submit">Open dashboard</button>
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
