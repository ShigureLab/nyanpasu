from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from nyanpasu_github.gh import gh_json

if TYPE_CHECKING:
    from collections.abc import Mapping


def fetch_review_feedback(
    repo: str, number: int, *, agent_login: str | None, cutoff: float, gh_env: Mapping[str, str] | None
) -> list[dict[str, Any]]:
    """Recover original feedback bodies absent from native history, bounded by source completion."""
    records = []
    for endpoint, category in (
        (f"repos/{repo}/pulls/{number}/reviews", "review"),
        (f"repos/{repo}/pulls/{number}/comments", "inline comment"),
        (f"repos/{repo}/issues/{number}/comments", "comment"),
    ):
        pages = gh_json(["api", "--paginate", "--slurp", endpoint + "?per_page=100"], env=gh_env)
        for page in pages:
            for item in page:
                author = item.get("user") or {}
                if author.get("type") == "Bot" or (agent_login and author.get("login") == agent_login):
                    continue
                body = item.get("body") or ""
                created = item.get("created_at") or item.get("submitted_at")
                updated = item.get("updated_at") or created
                if not body.strip() or created is None:
                    continue
                if any(datetime.fromisoformat(value).timestamp() > cutoff for value in (created, updated)):
                    continue
                records.append(
                    {
                        "kind": "github_feedback",
                        "title": f"GitHub {category} by {author.get('login') or 'unknown author'}",
                        "origin": "github",
                        "reference": item["html_url"],
                        "blocks": [
                            json.dumps(
                                {
                                    "author": author.get("login"),
                                    "account_type": author.get("type"),
                                    "created_at": created,
                                    "updated_at": updated,
                                    "url": item["html_url"],
                                    "body": body,
                                },
                                ensure_ascii=False,
                            )
                        ],
                    }
                )
    return sorted(records, key=lambda item: (json.loads(item["blocks"][0])["created_at"], item["reference"]))
