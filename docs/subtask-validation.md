# Subtask validation

The tests use real SQLite, local Git repositories, the control CLI in a separate Python process, and real HTTP/browser requests. The model output boundary is controlled with fake execution backends. This validates scheduling, persistence, isolation of supplied inputs and the UI; it does not prove model judgment or sandbox isolation.

Validated behavior includes root capacity with two running children, waiting and continuation, both sides of the child-finish/wait race, restart with preserved workspaces, ownership restrictions, idempotent creation, closing/creation races, stale-generation fencing, cleanup ordering and retry (including creation still running in a Git worker and backend startup failure), expiring controls, frozen evidence and authenticated retrieval after cleanup. Codex cancellation tests check both cancellation during `turn/start` and during execution: an interrupt acknowledgement alone must not permit cleanup.

The reference integration fixture creates divergent head and target branches. It verifies a fresh child at their common base, no PR head object or remote in the exported repository, a usable independent patch baseline unaffected by release export attributes, recorded source identities, immutable target selection under concurrent fetches and changed requirement digests. Template keyword tests are not used as evidence of review quality.

## Directed experiments

Run `uv run python scripts/check-subtask-mutations.py --output /tmp/nyanpasu-subtask-validation` after committing the candidate. The script exports HEAD to a temporary directory, establishes a clean baseline, runs each edit separately and retains full logs. It restores each file and never edits the working repository. The checked substitutions make the experiment reproducible; they are not product assertions or implementation-hash tests.

| Deliberate change                                       | Observed result                                      | Protected behavior                                                                    |
| ------------------------------------------------------- | ---------------------------------------------------- | ------------------------------------------------------------------------------------- |
| Make a child acquire the root semaphore                 | Concurrency test fails its bounded child-start check | With one root slot held, children must still start; the unrelated root remains queued |
| Do not consume the persisted wait set                   | Both early/late child completion cases fail          | A completed wait is consumed once and cannot repeatedly resume its parent             |
| Start the reference from head instead of merge-base     | Reference integration test rejects head-only code    | Independent input cannot silently contain the author's implementation                 |
| Rename the private snapshot-export helper and its calls | Reference and version tests pass                     | Internal naming can change without changing the observable contract                   |

The scheduling failure is a deterministic injected deadlock with event-gated tasks, not a timed-out external environment. The head-contamination fixture observes the wrong file before the parent test's bounded wait expires. Do not interpret arbitrary timeouts, collection errors or equivalent mutants as detected defects.

## Recorded local result

On 2026-09-24, Python 3.14.7: **265 pytest tests passed**, Ruff and `ty` passed; frontend checks, type generation and build passed; **6 frontend tests and 15 browser tests passed**. The directed experiments produced the outcomes above. Native model quality was not evaluated.

The 2026-09-25 asynchronous review update reran all **265 pytest tests**, Ruff, `ty`, Markdown formatting, frontend checks and the production build successfully. The 11 offline dashboard cases below also passed. No new model-adherence result is claimed.

The subsequent session-tree update passed **267 pytest tests, 6 frontend tests, and 17 browser tests**, plus Ruff, `ty`, frontend checks and the production build. API cases cover Codex and Claude ownership, nested waits, retained evidence, older task-group pagination, filtering before pagination, and relationships available without reading native history. Browser cases cover subtask navigation, evidence downloads, retained filters and collapsed groups during refresh/failure, and restoration of the exact parent reading position. The navigation case exposed and verified a fix for a lost scroll offset when reopening a session.

The session-tabs update passed **267 pytest tests, 6 frontend tests, and 18 browser tests**. Additional browser coverage checks keyboard tab navigation, bookmarked tabs, empty subtask views, distinct and consistent task-type colors, and preserved conversation DOM, reading position, search input, and collapsed groups across tab switches and background updates. Light, dark, and mobile layouts were inspected separately.

## Commands and limits

For the asynchronous review update, offline `gh-slate render` checks covered 11 dashboard states: initial, preliminary, integrated, deep failure preserving completed general review, skipped deep review, an early blocker, legacy data without stages, and four invalid combinations. The schema rejects preliminary results without completed general review and final approval/comment while deep work remains unfinished. The preview uses the bundled `review.example.json`; no GitHub write is needed. These checks validate the rendered publication contract, not whether a model follows the checkpoint instructions.

- `uv run pytest -q` for backend and plugin behavior.
- `uv run ruff check .`, `uv run ruff format --check .`, and the repository's `ty check --error-on-warning` command.
- `pnpm run types`, `pnpm run check`, `pnpm run test`, `pnpm run build` and `pnpm exec playwright test`.
- Browser fixtures display persisted states without recovering them into actual model runs. This host requires loopback proxy bypass and local Chromium dependencies; those host adaptations are outside the source change.

Not validated by these checks: live Codex/Claude model adherence to the prompts, cross-host execution/failover, model quality/cost on historical PRs, or an OS/network blind sandbox. The capability and base-tree snapshot are not protection from a malicious process with the same OS account. `evals/reviewer/` contains provisional cases and a maintainer grading protocol; no blind evaluation results are claimed. The 2026-09-24 deployment separately verified health, migration, authenticated Dashboard pages and reviewer polling; service readiness is not model-quality evidence.
