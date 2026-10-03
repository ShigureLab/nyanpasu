# Shared topic memory

Codex and Claude tasks use the same Nyanpasu memory service. A note represents one reusable conclusion, with a stable key, topic labels, applicability conditions, and evidence references. Repository names and paths can appear in applicability and sources; they do not define the note's topic or require a separate copy of the same knowledge.

Memory is fallible background evidence. A task must check a note's applicability and sources before relying on it. Native Codex memory and Claude auto-memory are disabled for Nyanpasu workers, while the operator's normal CLI settings remain unchanged.

## Authority and deduplication

The authoritative note content is Markdown below `$NYANPASU_HOME/memory`. Each audience has a hashed storage directory containing immutable note revisions under `objects/`. `manifest.json` selects the active revisions and records idempotent write receipts. The manifest replacement is the atomic commit boundary. `index.md` is a generated navigation view and can be rebuilt; it is not another editable copy of the knowledge. Nyanpasu does not maintain a second memory body in SQLite or a vector database.

Online changes go through the memory service, which serializes writers across processes. Updates require the note ID and current `expected_revision`. An outdated revision is rejected instead of overwriting another writer's changes. A `request_key` can replay exactly the same write or merge; reusing it for different input is rejected.

The service deduplicates normalized body text with the same applicability inside a writable audience. A repeated conclusion retains one note and combines evidence references. A stable key that already names different content requires an explicit update. Equivalent wording still needs judgment: versions, environments, and conditions can make superficially similar notes different knowledge.

For semantic duplicates, read all candidates and use `memory.merge` with every input's current revision. The atomic merge preserves one canonical note, combines topics and evidence, and records merged IDs. Do not implement a merge as separate write/delete calls. A new timestamp alone does not resolve a contradiction; leave uncertain claims unchanged and report the conflict.

## Audience and access

Topic and applicability metadata describe knowledge. The task's `MemoryAccess` capability determines who can read and write it:

| Capability                                                   | Effect                                                        |
| ------------------------------------------------------------ | ------------------------------------------------------------- |
| `read_domains = []`, no `write_domain`                       | No memory access                                              |
| `read_domains = ["public"]`, no `write_domain`               | Read public notes; no writes                                  |
| Read public plus a shared domain; write that shared domain   | Use public knowledge and maintain knowledge for that audience |
| Read public plus a private domain; write that private domain | Keep newly learned knowledge within the private audience      |

The service assigns this capability before execution. Model requests cannot choose another domain, user identity, or parent. Reading public notes does not grant public write access. Ordinary subtasks inherit their parent's capability or disable memory; they cannot widen it. Reviewer independent-design children always disable memory so prior conclusions do not contaminate that input.

Generic `AgentTask` objects default to no memory. Bundled GitHub tasks normally read `public` plus `shared:github:<owner/repo>` and write only the latter. This is an audience default, not a claim that the repository is publicly accessible. Configure repository capabilities to match the actual readership. Different repositories can share a common audience when that access is explicitly intended.

```toml
[plugins.settings.github_reviewer.repos."owner/repo"]
local_path = "/path/to/repo"

[plugins.settings.github_reviewer.repos."owner/repo".memory]
read_domains = ["public", "shared:engineering"]
write_domain = "shared:engineering"
```

For private work, use a private audience and configure `server.token`:

```toml
[server]
token = "replace-with-a-random-token"

[plugins.settings.github_reviewer.repos."owner/private-repo"]
local_path = "/path/to/private-repo"

[plugins.settings.github_reviewer.repos."owner/private-repo".memory]
read_domains = ["public", "private:engineering"]
write_domain = "private:engineering"
```

To disable memory for a repository, supply its `.memory` table with `read_domains = []` and omit `write_domain`. To disable it service-wide, set `memory.enabled = false`. Setting `memory.consolidate = false` disables automatic maintenance tasks while retaining permitted interactive reads and writes.

Audience capabilities are enforced at the service boundary, and native sessions are separated when the audience changes. All backends also use the [Linux execution isolation](configuration.md#linux-execution-isolation) boundary. Network access remains available; administrative service credentials must not be exposed to a worker.

## Task operations

The per-turn control command supports these actions:

| Action            | Input                                                                                                                                     |
| ----------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `memory.describe` | No parameters; lists visible audiences, topics, and counts                                                                                |
| `memory.search`   | `query`, optional `topics`, optional `limit`                                                                                              |
| `memory.read`     | `note_id`                                                                                                                                 |
| `memory.write`    | `key`, `title`, `body`, optional `topics`, `applies_to`, `sources`, `request_key`; updates also require `note_id` and `expected_revision` |
| `memory.merge`    | `target_id`, `source_ids`, canonical content, and `expected_revisions` for every input                                                    |
| `memory.delete`   | `note_id`, `expected_revision`                                                                                                            |

For example, pipe this JSON into the task-control command supplied for the current turn:

```json
{
   "action": "memory.search",
   "input": {
      "query": "context lease recovery",
      "topics": ["runtime"],
      "limit": 5
   }
}
```

Search returns short previews; use `memory.read` before editing. `memory.max_notes_per_search` is both the default result count and the upper bound, with a configuration range of 1–100. The default is 10. Topic filters apply only to the notes already visible to the task. Writes and merges add the source task reference through the service; caller-supplied evidence references supplement it. The control capability expires with the turn.

## Consolidation is an ordinary task

When enabled, a completed root execution with memory write access creates a `memory_consolidation` task. Waiting work, child tasks, and consolidation tasks do not recursively create one. The completion and follow-up admission are recorded together, so a service restart can recover pending maintenance through the normal queue.

A consolidation task has its own resolved backend, model, reasoning, timeout, native session, and Dashboard record. It reads bounded evidence from the source task's native turns and receives exactly the source task's memory audience. It must search existing notes, retain sources and applicability, and write only supported reusable knowledge. Assistant suggestions or unverified success summaries are not enough. A successful consolidation may make no changes.

Route it like any other task kind:

```toml
[tasks.kinds.memory_consolidation.execution]
backend = "claude"
model = "your-memory-model"
reasoning = "medium"

[tasks.kinds.memory_consolidation.limits]
turn_timeout_seconds = 900
```

This uses the existing Claude adapter and task controls; no separate model client or memory-specific provider configuration is needed. Its configured target is frozen when admitted, just like review and implementation tasks.
