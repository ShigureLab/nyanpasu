import { useState, type ReactNode } from 'react';
import { useApi } from './api';

export function Download({
  path,
  filename,
  children,
}: {
  path: string;
  filename: string;
  children: ReactNode;
}) {
  const { download } = useApi();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  return (
    <>
      <button
        disabled={busy}
        onClick={() => {
          setBusy(true);
          setError('');
          void download(path, filename)
            .catch((error) => setError(String(error)))
            .finally(() => setBusy(false));
        }}
      >
        {children}
      </button>
      {error && (
        <span className="notice error" role="alert">
          {error}
        </span>
      )}
    </>
  );
}
