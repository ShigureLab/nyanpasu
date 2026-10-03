# Background memory

Nyanpasu keeps source-oriented Markdown summaries and derives a bounded navigation document for each audience. Normal tasks can search and read memory. They do not write it: after eligible work completes, the service runs extraction and consolidation tasks, validates their structured output, and publishes it.

Session transcripts and tool results remain the evidence for what happened. A summary is a fallible account of that evidence, not proof of current repository, service, or PR state.

## Production and publication

A successful root `run` with a configured contribution audience starts this pipeline:

1. `memory_extraction` reads only the native turns bound to the completed source task. It processes evidence in source order, in bounded chunks, supplying the previous account with each chunk. The model returns a structured source summary; it does not call a memory write tool.
2. The service validates each result and saves a checkpoint. The source task has a stable identity within its audience. Retrying resumes the committed input digest and cursor instead of adding another source. A partial replacement does not displace the last published account; an empty completed account records a no-content result without creating a searchable entry.
3. After extraction completes, `memory_consolidation` reads source accounts from that contribution audience and updates its navigation. Large inputs are folded in bounded batches. The published document remains unchanged until all batches pass validation.
4. The service validates the final navigation's size, source identities, and source revisions, then publishes it atomically. A model's statement that it saved something is never the commit boundary.

Both stages are ordinary tasks with frozen backend/model/reasoning settings, native history, status, and errors in the Dashboard. Completion and follow-up admission use the existing durable task queue. A failed navigation task retains the successfully extracted source. Interrupted extraction resumes its checkpoint; a failed task is visible for operator retry rather than silently marked successful. Waiting tasks, children, and memory tasks do not recursively initiate another extraction pipeline.

Background models receive service-selected evidence and no task-control capability. The service validates and publishes their structured proposals. Structured output checks protect the format and references; they do not establish that every model interpretation is correct. Extraction instructions require confirmed decisions or actual tool evidence, retain applicability and uncertainty, and reject unsupported assistant claims.

## Storage and retrieval

Each audience contains published source Markdown, an optional navigation Markdown document, and a manifest referencing current objects and extraction checkpoints. Markdown holds the content; the manifest does not maintain a second copy of it. In-progress extraction accounts are drafts and are excluded from normal reads and searches.

Different source accounts can describe the same event or repeated experience. They retain independent provenance. Consolidation groups related experience in navigation instead of creating a separate registry of canonical facts with model-generated keys. The navigation is derived and bounded to 8,000 characters.

Normal tasks receive a bounded navigation excerpt for authorized audiences and use progressive reads for details:

| Action            | Input                                                       |
| ----------------- | ----------------------------------------------------------- |
| `memory.describe` | No parameters; visible audiences, topics, and source counts |
| `memory.search`   | `query`, optional `topics`, optional `limit`                |
| `memory.read`     | `source_id`                                                 |

```json
{
   "action": "memory.search",
   "input": { "query": "context lease recovery", "topics": ["runtime"], "limit": 5 }
}
```

Search returns previews from published sources; read the relevant source before relying on its account. `memory.max_results_per_search` sets both the default and upper result count, from 1 to 100. Retrieval currently uses weighted lexical matching, without embeddings or query expansion. Topics organize sources across projects; they are not access permissions.

There are no model-facing `memory.write`, `memory.merge`, or `memory.delete` actions.

## Audience and access

The service assigns each task a `MemoryAccess` capability. `read_domains` selects readable audiences. `write_domain` identifies the audience to which the service may contribute a background account; it grants no model write permission.

| Capability                                                     | Effect                                                                 |
| -------------------------------------------------------------- | ---------------------------------------------------------------------- |
| No read domains or contribution audience                       | Memory disabled for the task                                           |
| Read domains, no `write_domain`                                | Read existing memory without contributing                              |
| Read public plus a private audience; `write_domain` is private | Read both; contribute and consolidate only inside the private audience |

A consolidator never combines the task's entire readable set into its contribution audience. It receives only sources from that one audience. Model requests cannot select another audience, impersonate a user, or widen a child's capability. Topics and backend/model changes do not change access. Reviewer independent-design children disable memory.

Generic tasks default to no memory. Bundled GitHub tasks normally read `public` plus `shared:github:<owner/repo>` and contribute to the latter. This is an audience convention, not a claim that the repository is public. Administrators can assign a common audience to repositories whose readership is actually shared.

```toml
[server]
token = "replace-with-a-random-token"

[plugins.settings.github_reviewer.repos."owner/private-repo".memory]
read_domains = ["public", "private:engineering"]
write_domain = "private:engineering"
```

Private audiences require authenticated service endpoints. Audience permissions apply to service APIs and task-control requests. Native session homes keep histories separate; backend processes retain the service user's filesystem access, subject to their native CLI permissions. Administrative service credentials must not be passed to a worker.

## Execution configuration

Both stages use normal task-kind routing. A separate backend name lets the background model use its own defaults and model credentials without reviewer GitHub credentials:

```toml
[backends.memory]
driver = "codex"

[backends.memory.process]
command = ["codex", "-c", "features.multi_agent=false"]

[backends.memory.defaults]
model = "gpt-6-luna"
reasoning = "medium"

[backends.memory.options]
sandbox = "read-only"
approval_policy = "never"

[tasks.kinds.memory_extraction.execution]
backend = "memory"

[tasks.kinds.memory_consolidation.execution]
backend = "memory"
```

Configure the backend's native home template and credentials as for other workers. No separate provider SDK or memory-specific model configuration is used.

`memory.enabled = false` disables memory use and production. `memory.consolidate = false` disables background production while retaining authorized reads. The per-kind timeout and inherited execution defaults follow the [standard configuration rules](configuration.md#per-field-resolution).

## Upgrading existing memory configuration

This is an explicit configuration and storage-format upgrade; runtime loading does not translate older settings or note stores. Drain active work, stop the service, and back up the configuration, SQLite state, and memory directory before upgrading.

1. Rename `memory.max_notes_per_search` to `memory.max_results_per_search`. If supplied through the environment, rename `NYANPASU__MEMORY__MAX_NOTES_PER_SEARCH` to `NYANPASU__MEMORY__MAX_RESULTS_PER_SEARCH` too. The old name is rejected.
2. Configure both `tasks.kinds.memory_extraction` and `tasks.kinds.memory_consolidation`. Give each stage its intended backend, model, reasoning, and timeout; the example routes both to the same maintenance backend. The stages resolve independently and never inherit one another's policy. While background production is enabled, defining only one stage is rejected. Defining neither leaves both on the normal task defaults.
3. Check both resolved targets before restarting:

   ```bash
   uv run nyanpasu explain-target --kind memory_extraction
   uv run nyanpasu explain-target --kind memory_consolidation
   ```

Old knowledge-note manifests cannot be used as source stores. Keep the original memory directory and backups intact until an explicit offline conversion and deployment have been verified; do not start this version against the old-format directory.
