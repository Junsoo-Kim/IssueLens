"""정제(마스킹, Markdown)와 RAG 1/RAG 2 문서 생성 테스트."""
from __future__ import annotations

import pytest

from src.preprocessing.document_builder import (
    MAX_COMMENTS_PER_ISSUE,
    build_comment_documents,
    build_issue_body,
    build_pull_request_documents,
)
from src.preprocessing.metadata_builder import build_issue_item, symptom_summary
from src.preprocessing.normalizer import normalize_markdown
from src.preprocessing.secret_redactor import redact

REPO = "keycloak/keycloak"


# ---------------------------------------------------------------- 마스킹


@pytest.mark.parametrize("text,expected,kind", [
    ("Authorization: Bearer abc.def-123", "Authorization: Bearer <REDACTED>", "auth_header"),
    ("token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U", "token <REDACTED_JWT>", "jwt"),
    ("GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789", "GITHUB_TOKEN=<REDACTED_TOKEN>", "github_token"),
    ("password: hunter2secret", "password: <REDACTED>", "key_value"),
    ("contact me at jane.doe@example.com", "contact me at <EMAIL>", "email"),
    ("server 203.120.45.67 refused", "server <IP> refused", "ip"),
])
def test_redact_masks_secrets(text, expected, kind):
    redacted, counts = redact(text)
    assert redacted == expected
    assert counts[kind] == 1


@pytest.mark.parametrize("text", [
    "password: ${DB_PASSWORD}",
    "password: <REDACTED>",
    "connect to 192.168.1.20 and 127.0.0.1",
    "upgrade to TBE 4.2.1.2 fixed it",
    "version 3.10.4.1",
    "elapsed 0.40.12.001 and 10.020.30.40",
])
def test_redact_keeps_non_secrets(text):
    assert redact(text) == (text, {})


def test_redact_and_normalize_never_raise_on_random_input():
    import random

    rng = random.Random(0)
    alphabet = "0123456789....:=/@-_ abcxyzABC\n`#<>!"
    for _ in range(3000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 80)))
        normalize_markdown(redact(text)[0])


def test_redact_private_key_block():
    text = "key:\n-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----\nend"
    assert redact(text)[0] == "key:\n<REDACTED_PRIVATE_KEY>\nend"


# ---------------------------------------------------------------- Markdown 정제


def test_normalize_removes_html_outside_code_only():
    text = "<!-- template hint -->\n<details><summary>Logs</summary>\n\n```xml\n<!-- keep -->\n<dependency/>\n```\n</details>"
    normalized, truncated = normalize_markdown(text)
    assert "template hint" not in normalized and "<details>" not in normalized
    assert "<!-- keep -->" in normalized and "<dependency/>" in normalized
    assert truncated is False


def test_normalize_generalizes_user_paths_and_drops_empty_sections():
    text = ("### Logs\nat /home/jane/app/run.py and C:\\Users\\Jane\\app\n\n"
            "### Anything else?\n_No response_\n\n### Version\n26.0.1")
    normalized, _ = normalize_markdown(text)
    assert "/home/<USER>/app" in normalized and "C:\\Users\\<USER>" in normalized
    assert "Anything else" not in normalized and "### Version\n26.0.1" in normalized


def test_normalize_truncates_long_documents():
    normalized, truncated = normalize_markdown("x" * 50, max_chars=20)
    assert truncated and normalized.endswith("[... truncated]")


# ---------------------------------------------------------------- RAG 1 항목


ISSUE = {
    "number": 7, "title": "Token validation fails for jane@example.com", "state": "closed",
    "state_reason": "completed", "labels": [{"name": "kind/bug"}], "html_url": f"https://github.com/{REPO}/issues/7",
    "created_at": "2026-01-01T00:00:00Z", "closed_at": "2026-01-05T00:00:00Z", "updated_at": "2026-01-05T00:00:00Z",
    "author_association": "NONE", "user": {"login": "reporter"},
    "body": "### Describe the bug\nLogin fails after upgrade.\n\n```\nTokenVerificationException\n```\n\n### Version\n26.0.1",
}
TAG = {"issue_type": "bug", "component": "area/oidc", "components": ["area/oidc"], "category": "auth",
       "categories": ["auth"], "error_types": ["TokenVerificationException"], "versions": ["26.0.1"],
       "languages": ["java"], "dependencies": ["postgresql"]}
SELECTION = {"issue_id": f"{REPO}#7", "repo": REPO, "number": 7, "final_split": "index", "evidence_level": "strong",
             "merged_pr": True, "merged_prs": [42], "maintainer_detail": True}


def test_symptom_summary_prefers_description_and_skips_code():
    assert symptom_summary(ISSUE["body"]) == "Login fails after upgrade."


def test_build_issue_item_index_text_and_redaction():
    item = build_issue_item(ISSUE, TAG, SELECTION, {"reference_urls": {"official_docs": ["https://www.keycloak.org/x"]}})
    text = item["index_text"]
    assert "저장소: keycloak/keycloak" in text and "오류 유형: TokenVerificationException" in text
    assert "해결 근거: 병합 PR #42, 유지보수자 상세 댓글 (증거 수준 strong)" in text
    assert "jane@example.com" not in text and item["redaction_counts"] == {"email": 1}
    assert item["official_doc_urls"] == ["https://www.keycloak.org/x"]
    assert item["content_hash"] == build_issue_item(ISSUE, TAG, SELECTION, {})["content_hash"]


# ---------------------------------------------------------------- RAG 2 문서


def item():
    return build_issue_item(ISSUE, TAG, SELECTION, {})


def comment(cid, body, login="other", association="NONE", user_type="User", created="2026-01-02T00:00:00Z"):
    return {"id": cid, "body": body, "user": {"login": login, "type": user_type}, "author_association": association,
            "created_at": created, "html_url": f"https://github.com/{REPO}/issues/7#issuecomment-{cid}"}


def test_issue_body_document_is_required_and_cleaned():
    document = build_issue_body(ISSUE, item())
    assert document["document_id"] == f"{REPO}#7:issue_body" and document["document_type"] == "issue_body"
    assert "TokenVerificationException" in document["body"]


def test_comment_documents_filter_bots_and_noise_and_infer_roles():
    comments = [
        comment(1, "+1"),
        comment(2, "Closing due to inactivity, this is a long enough bot message", "stale[bot]", user_type="Bot"),
        comment(3, "Fixed in 26.0.2 by changing the token validator, please upgrade.", "core-dev", "CONTRIBUTOR"),
        comment(4, "I can reproduce this on postgres as well with the same config", "reporter",
                created="2026-01-01T12:00:00Z"),
    ]
    documents, rejected = build_comment_documents(comments, item(), "reporter", {"core-dev"})
    assert [d["document_id"].rsplit(":", 1)[1] for d in documents] == ["4", "3"]  # 작성 시각 순
    roles = {d["document_id"].rsplit(":", 1)[1]: d["author_role"] for d in documents}
    assert roles == {"3": "maintainer", "4": "reporter"}
    assert {r["reject_reason"] for r in rejected} == {"noise_or_too_short", "bot_comment"}


def test_comment_documents_keep_top_scored_within_limit():
    comments = [comment(i, f"plain remark number {i} with enough length here") for i in range(MAX_COMMENTS_PER_ISSUE + 3)]
    comments.append(comment(999, "Fixed in 26.0.2, workaround below", "core-dev", "MEMBER"))
    documents, rejected = build_comment_documents(comments, item(), "reporter", set())
    assert len(documents) == MAX_COMMENTS_PER_ISSUE
    assert any(d["document_id"].endswith(":999") for d in documents)
    assert sum(r["reject_reason"].startswith("comment_over_limit") for r in rejected) == 4


def test_pull_request_documents():
    pr = {"number": 42, "title": "Fix validator", "body": "Fixes #7", "merged_at": "2026-01-04T00:00:00Z",
          "html_url": f"https://github.com/{REPO}/pull/42"}
    files = [
        {"filename": "services/src/TokenValidator.java", "status": "modified", "additions": 3, "deletions": 1,
         "patch": "@@ -1 +1 @@\n-old\n+new"},
        {"filename": "js/package-lock.json", "status": "modified", "patch": "@@ lock"},
        {"filename": "big/Generated.java", "status": "added"},
    ]
    documents, rejected = build_pull_request_documents(pr, files, item())
    assert [d["document_type"] for d in documents] == ["pull_request", "pull_patch"]
    assert "services/src/TokenValidator.java (modified, +3/-1)" in documents[0]["body"]
    assert documents[1]["source_url"].endswith("/pull/42/files")
    assert {r["reject_reason"] for r in rejected} == {"generated_or_binary_file", "patch_unavailable"}

    unmerged, rejected = build_pull_request_documents({**pr, "merged_at": None}, files, item())
    assert unmerged == [] and rejected[0]["reject_reason"] == "pr_not_merged"
