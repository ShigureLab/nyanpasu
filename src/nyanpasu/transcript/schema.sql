CREATE TABLE IF NOT EXISTS transcript_sessions (
    session_id TEXT PRIMARY KEY, context_key TEXT NOT NULL,
    thread_id TEXT, backend TEXT NOT NULL, title TEXT NOT NULL,
    generation TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    closed INTEGER NOT NULL DEFAULT 0, previous_session_id TEXT,
    origin TEXT NOT NULL, coverage TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS transcript_session_context ON transcript_sessions(context_key, updated_at);
CREATE TABLE IF NOT EXISTS transcript_tasks (
    task_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, turn_id TEXT,
    cwd TEXT, revision TEXT, started_at TEXT NOT NULL, ended_at TEXT,
    title TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS transcript_task_session ON transcript_tasks(session_id, started_at);
CREATE TABLE IF NOT EXISTS transcript_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
    task_id TEXT, entry_id TEXT, direction TEXT NOT NULL, type TEXT NOT NULL,
    observed_at TEXT NOT NULL, content_ref TEXT NOT NULL, source_id TEXT
);
CREATE INDEX IF NOT EXISTS transcript_event_session ON transcript_events(session_id, seq);
CREATE INDEX IF NOT EXISTS transcript_event_entry ON transcript_events(session_id, entry_id, seq);
CREATE UNIQUE INDEX IF NOT EXISTS transcript_source_event ON transcript_events(session_id, task_id, source_id) WHERE source_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS transcript_entries (
    entry_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, task_id TEXT,
    source_key TEXT NOT NULL, first_seq INTEGER NOT NULL,
    revision_seq INTEGER NOT NULL, data TEXT NOT NULL,
    UNIQUE(session_id, task_id, source_key)
);
CREATE INDEX IF NOT EXISTS transcript_entry_window ON transcript_entries(session_id, first_seq);
CREATE TABLE IF NOT EXISTS transcript_changes (
    seq INTEGER PRIMARY KEY, session_id TEXT NOT NULL, entry_id TEXT NOT NULL, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS transcript_change_session ON transcript_changes(session_id, seq);
CREATE TABLE IF NOT EXISTS transcript_contents (
    ref TEXT PRIMARY KEY, session_id TEXT NOT NULL, stream TEXT NOT NULL, size INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS transcript_chunks (
    stream TEXT NOT NULL, offset INTEGER NOT NULL, data BLOB NOT NULL,
    PRIMARY KEY(stream, offset)
);
CREATE TABLE IF NOT EXISTS transcript_imports (task_id TEXT PRIMARY KEY);
