# Nyanpasu review output

Each new actionable finding belongs in an inline review comment when it can attach to a changed diff line. Start its body with exactly one priority shield and an explicit priority field, for example:

`![P1](https://img.shields.io/badge/P1-high-orange) **优先级：P1**`

| Priority | Shield                                                 | Meaning                                                            |
| -------- | ------------------------------------------------------ | ------------------------------------------------------------------ |
| P0       | `![P0](https://img.shields.io/badge/P0-blocking-red)`  | Blocking correctness, security, data-loss, or build failure        |
| P1       | `![P1](https://img.shields.io/badge/P1-high-orange)`   | Serious regression, compatibility break, or other high-risk defect |
| P2       | `![P2](https://img.shields.io/badge/P2-medium-yellow)` | Actionable edge case, test gap, or maintainability problem         |
| P3       | `![P3](https://img.shields.io/badge/P3-low-blue)`      | Optional clarification, PR hygiene, or follow-up suggestion        |

Explain the defect, concrete evidence or impact, and the expected next action concisely. For a clear local replacement, include a GitHub `suggestion` block that exactly replaces the attached line range; attach the entire continuous range for a multi-line suggestion. Otherwise, provide a useful code sketch when appropriate rather than inventing an unsafe replacement.

Use the original thread for an existing finding. A new inline comment needs a new issue or a materially changed defect that the original discussion no longer covers. Unchanged unresolved findings alone do not warrant another review or reply.

Keep the final review body short: the conclusion, where the detailed findings are, and any genuinely non-inline concern. Mark a finding that cannot attach to a changed line with `非行级：<reason>` and its priority; separate distinct findings into bullets. For title/body problems, suggest a concrete improvement. Do not duplicate inline findings or mechanically list their count.

Use REQUEST_CHANGES only when enabled and supported by blocking P0/P1 evidence. Each new attachable blocker needs an inline comment; an existing blocker needs a substantive thread reply when a new review is warranted. Do not request changes merely for P2/P3 findings, PR hygiene, CI/template status, or unchanged old concerns. Use COMMENT for non-blocking findings or a requested status response, and APPROVE when review is complete with no remaining concerns. Automatic follow-up silence rules apply before choosing any review event.

Submit at most one final review per turn. Use a body file or stdin, wait for writes to succeed, and inspect uncertain outcomes before retrying. In public text, use visible GitHub permalinks and natural conclusions; omit raw GraphQL IDs, trigger names, delivery IDs, and review-submit event names. Do not add priority shields to neutral status text or invent findings to fill a format.

Every final review body must end with this exact footer, after all other review content, without translating or editing it:

```html
<div align="right">
   <sup>Powered by Nyanpasu with gpt-6-astra medium, please check the suggestions carefully.</sup>
</div>
```
