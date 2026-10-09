from __future__ import annotations

import json
from html.parser import HTMLParser

import pytest
from gh_slate.codec.meta import MetaSnapshot
from gh_slate.rendering.jinja import render_jinja

from nyanpasu_github_reviewer.prompt import TEMPLATES_DIR

REVIEW_URL = "https://github.com/redai-studio/Relax/pull/425#pullrequestreview-5466052458"
INVENTORY_ID = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


class Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self.href: str | None = None
        self.label: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.href = dict(attrs).get("href")
            self.label = []

    def handle_data(self, data: str) -> None:
        if self.href is not None:
            self.label.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.href is not None:
            self.links.append((self.href, "".join(self.label)))
            self.href = None


@pytest.fixture
def report() -> dict:
    data = json.loads((TEMPLATES_DIR / "review.example.json").read_text())
    data["scope"] = {
        INVENTORY_ID: {
            "counts": {"accept": 1, "relocate": 0, "clarify": 0},
            "groups": [
                {
                    "files": ["service/config.py"],
                    "decision": "accept",
                    "reason": "实现请求的配置行为。",
                    "source": "功能需求。",
                    "alternative": "保留当前入口。",
                }
            ],
        }
    }
    return data


def render(report: dict, meta: MetaSnapshot | None = None) -> tuple[str, list[tuple[str, str]]]:
    source = (TEMPLATES_DIR / "review.md.j2").read_text()
    output = render_jinja(source, data=report, meta=meta or MetaSnapshot.local("nyanpasu-review"))
    parser = Links()
    parser.feed(output)
    return output, parser.links


@pytest.mark.parametrize(
    "path",
    [
        ("summary",),
        *(("stages", stage, "summary") for stage in ("general", "deep")),
        *(("ci", "failures", "check:12345", field) for field in ("summary", "evidence", "next_step")),
        *(("scope", INVENTORY_ID, "groups", 0, field) for field in ("reason", "source", "alternative")),
        *(("simplification", audit, "summary") for audit in ("production", "tests")),
        *(
            ("simplification", audit, "entries", 0, field)
            for audit in ("production", "tests")
            for field in ("scope", "contract", "alternative", "evidence")
        ),
    ],
)
def test_free_prose_links_preserve_review_anchor(report: dict, path: tuple) -> None:
    parent = report
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = f"第二轮增量审查完成，结论 APPROVE（{REVIEW_URL}）："

    _, links = render(report)

    assert (REVIEW_URL, REVIEW_URL) in links


def test_summary_links_preserve_queries_and_keep_code_literal(report: dict) -> None:
    url = "https://example.com/report?first=1&second=2#evidence"
    report["summary"] = f"证据（{url}）；复核 {REVIEW_URL}。代码 `{url}`；<script>alert(1)</script>"

    output, links = render(report)

    assert links.count((url, url)) == 1
    assert (REVIEW_URL, REVIEW_URL) in links
    assert "<script>" not in output


def test_target_branch_preserves_complete_destination(report: dict) -> None:
    meta = MetaSnapshot.from_json(
        {
            "host": "github.com",
            "repository": {
                "owner": "redai-studio",
                "name": "Relax",
                "full_name": "redai-studio/Relax",
                "url": "https://github.com/redai-studio/Relax",
            },
            "target": {
                "kind": "pull_request",
                "number": 425,
                "id": "PR_example",
                "url": "https://github.com/redai-studio/Relax/pull/425",
            },
            "slate": {"name": "nyanpasu-review"},
        }
    )

    _, links = render(report, meta)

    assert ("https://github.com/redai-studio/Relax/tree/main", "main") in links
