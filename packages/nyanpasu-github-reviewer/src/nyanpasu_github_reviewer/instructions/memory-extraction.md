Maintain two complementary parts of this PR's memory:

1. Human feedback: prioritize substantive suggestions, corrections, and design discussions
   from human GitHub participants, including the body of APPROVED reviews. For each, retain
   who said what, the problem and reason, applicability, and the original comment/review URL.
   Record adoption, rejection, correction, or verification only when evidence supports it.
   The serving agent's own comments and bot status messages are not human feedback.
   Preserve new human advice about review methods, but exclude the fixed reviewer workflow.

2. PR substance: explain the problem solved and the core before/after behavior or contract,
   important modules/interfaces/callers, design choices and their reasons, relevant limits,
   and explicitly supported relationships to other PRs. Retain a useful PR technical description
   even if the review found no bugs and received no human advice. Use concrete domain terms,
   symbols, and paths as retrieval topics; cite the PR and technical evidence when available.
   Verified failure mechanisms and remedies belong here; finding IDs and review chronology do not.

Do not retain dashboard revisions, scope/inventory verification, publication steps,
subtask scheduling, routine CI refreshes, repeated approvals, labels/hold status, no-op follow-ups,
or boilerplate instructions. A substantive infrastructure limitation matters only if it affects
technical applicability. Do not fill missing technical content with workflow observations.
Preserve the PR's technical identity and human feedback through later empty follow-ups.
Update the relevant technical conclusion when code or human discussion materially changes;
do not append a new review-round recap. Revision/date qualifiers matter only for changing claims.
