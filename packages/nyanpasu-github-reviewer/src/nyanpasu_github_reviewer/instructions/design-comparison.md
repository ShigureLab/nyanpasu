# Selected design comparisons

Use this method for a concrete simplification candidate or a requested independent comparison. It does not add an exhaustive necessity audit to ordinary review.

For an independent reference, verify the frozen source tree, requirement digest, independence disclosure and evidence. Compare both designs at the same base and requirements; latest-target integration is a separate check applied equally to both.

Compare responsibilities, ownership, state, boundaries and failure behavior rather than raw line counts. Author-only machinery may preserve compatibility or a requirement the reference missed. Reference-only machinery may expose missing behavior or be speculative. Trace actual callers and contracts to decide; agreement can also share a wrong assumption.

For a promising smaller alternative, use concrete caller/contract evidence or a bounded deletion/replacement experiment against actual production paths in a disposable workspace. Follow the local testing policy. A sketch or standalone model cannot prove production machinery removable; state unavailable validation honestly. A full alternative PR is unnecessary.

Reconcile child evidence and earlier claims, withdrawing candidates contradicted by behavior or callers. Record examined alternatives, decisions and evidence in the existing plan. The optional dashboard `simplification` record may summarize these candidates under review-output.md; do not create records for unexamined production/test families. Test-specific questions use test-review.md. Finish when the selected material questions are resolved, with no finding quota.
