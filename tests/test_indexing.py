"""색인 테스트: ID, 토크나이저, BM25, 청킹, Chroma 증분 동기화, build_indexes 전체 흐름과 멱등성."""
from __future__ import annotations

import hashlib
import json

import pytest

from src.collectors.cache import RawStore, write_jsonl_atomic
from src.indexing.bm25_indexer import BM25Index, build_bm25_indexes, load_bm25_ids, tokenize
from src.indexing.chroma_store import get_collection, open_client, scalar_metadata, sync_collection
from src.indexing.document_indexer import document_records
from src.indexing.ids import chunk_id, issue_record_id

REPO = "keycloak/keycloak"


class FakeEmbedder:
    """텍스트 해시로 만든 결정적 8차원 벡터. 호출 횟수를 센다."""

    def __init__(self):
        self.calls = 0
        self.texts = 0

    def __call__(self, texts):
        self.calls += 1
        self.texts += len(texts)
        return [[b / 255 for b in hashlib.sha256(t.encode()).digest()[:8]] for t in texts]


# ---------------------------------------------------------------- ID, 토크나이저, BM25


def test_ids_are_deterministic():
    assert issue_record_id("keycloak/keycloak#123") == "issue:keycloak:keycloak:123"
    assert chunk_id("keycloak/keycloak#123:comment:9", 2) == "doc:keycloak/keycloak#123:comment:9:2"


def test_tokenize_preserves_technical_tokens():
    tokens = tokenize("TokenVerificationException in org.keycloak.protocol.oidc after 26.0.1 with KC_DB_POOL_MAX_SIZE 401")
    for expected in ["tokenverificationexception", "TokenVerificationException", "token", "verification", "exception",
                     "org.keycloak.protocol.oidc", "keycloak", "oidc", "26.0.1", "kc_db_pool_max_size",
                     "KC_DB_POOL_MAX_SIZE", "pool", "401"]:
        assert expected in tokens, expected
    assert "in" not in tokens and "with" not in tokens and "after" not in tokens


def test_bm25_finds_exact_exception_and_version(tmp_path):
    records = [
        {"id": "a", "text": "Login fails with TokenVerificationException on 26.0.1", "metadata": {"repo": REPO}},
        {"id": "b", "text": "Dashboard renders slowly with many widgets", "metadata": {"repo": REPO}},
        {"id": "b2", "text": "Realm export misses client scopes", "metadata": {"repo": REPO}},
        {"id": "b3", "text": "Admin console theme cache not invalidated", "metadata": {"repo": REPO}},
        {"id": "c", "text": "Sync job fails with NullPointerException", "metadata": {"repo": "airbytehq/airbyte"}},
    ]
    # 문서가 2건뿐이면 1건에만 있는 단어의 IDF가 0이 되므로 현실적인 크기로 둔다
    counts = build_bm25_indexes(records, tmp_path / "idx")
    assert counts == {REPO: 4, "airbytehq/airbyte": 1}
    assert load_bm25_ids(tmp_path / "idx") == {"a", "b", "b2", "b3", "c"}
    index = BM25Index.load(tmp_path / "idx" / "keycloak__keycloak.json")
    assert index.search("TokenVerificationException")[0][0] == "a"
    assert index.search("26.0.1")[0][0] == "a"

    build_bm25_indexes(records[:1], tmp_path / "idx")  # 사라진 저장소 파일은 지운다
    assert load_bm25_ids(tmp_path / "idx") == {"a"}


# ---------------------------------------------------------------- 청킹


def doc(document_id, document_type, body):
    return {"document_id": f"{REPO}#1:{document_id}", "issue_id": f"{REPO}#1", "repo": REPO, "issue_title": "Login fails",
            "component": "area/oidc", "document_type": document_type, "title": "t", "body": body,
            "author_role": "maintainer", "source_url": "u", "source_type": "github_comment"}


def test_document_records_chunk_by_type_with_header():
    long_body = "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(6))
    documents = [doc("issue_body", "issue_body", long_body), doc("comment:1", "comment", "short comment")]
    small = document_records(documents, {"issue_body": {"chunk_size": 400, "chunk_overlap": 50}})
    large = document_records(documents, {"issue_body": {"chunk_size": 2000, "chunk_overlap": 0}})
    assert len(small) > len(large)  # 청크 설정에 따라 청크 수가 달라진다 (F2005)
    ids = [r["id"] for r in small]
    assert len(ids) == len(set(ids))
    assert ids == [r["id"] for r in document_records(documents, {"issue_body": {"chunk_size": 400, "chunk_overlap": 50}})]
    first = small[0]
    assert first["text"].startswith(f"[{REPO}#1] Login fails\n컴포넌트: area/oidc | 문서: issue_body")
    assert first["metadata"]["chunk_count"] == sum(1 for r in small if r["metadata"]["document_type"] == "issue_body")


def test_scalar_metadata():
    assert scalar_metadata({"a": ["x", "y"], "b": None, "c": 1, "d": {"k": 1}}) == {"a": "x,y", "b": "", "c": 1, "d": "{'k': 1}"}


# ---------------------------------------------------------------- Chroma 동기화


def records(n, suffix=""):
    return [{"id": f"r{i}", "text": f"text {i}{suffix if i == 0 else ''}", "metadata": {"repo": REPO, "tags": ["a"]}}
            for i in range(n)]


def test_sync_collection_is_incremental(tmp_path):
    collection = get_collection(open_client(tmp_path / "chroma"), "unit_test_col")
    embed = FakeEmbedder()

    first = sync_collection(collection, records(5), embed)
    assert (first.added, first.updated, first.unchanged, first.deleted, first.dimensions) == (5, 0, 0, 0, 8)
    again = sync_collection(collection, records(5), embed)
    assert (again.added, again.updated, again.unchanged, again.deleted) == (0, 0, 5, 0)
    assert embed.texts == 5  # 두 번째 실행은 임베딩하지 않는다

    changed = sync_collection(collection, records(4, suffix=" changed"), embed)
    assert (changed.added, changed.updated, changed.unchanged, changed.deleted) == (0, 1, 3, 1)
    assert collection.count() == 4

    with pytest.raises(RuntimeError):
        sync_collection(collection, records(6), None)
    with pytest.raises(ValueError):
        sync_collection(collection, records(2) + records(1), embed)


# ---------------------------------------------------------------- build_indexes 전체 흐름


def write_fixture(tmp_path):
    raw = RawStore(tmp_path / "raw")
    selection, tagged, summaries, pool = [], [], [], []
    for number, split in ((1, "index"), (2, "index"), (3, "evaluation")):
        issue_id = f"{REPO}#{number}"
        selection.append({"issue_id": issue_id, "repo": REPO, "number": number, "final_split": split,
                          "evidence_level": "strong", "merged_pr": number == 1, "merged_prs": [50] if number == 1 else []})
        pool.append({"issue_id": issue_id, "repo": REPO, "number": number})
        tagged.append({"issue_id": issue_id, "issue_type": "bug", "component": "area/oidc", "components": ["area/oidc"],
                       "category": "auth", "categories": ["auth"], "error_types": [], "versions": [],
                       "languages": [], "dependencies": []})
        summaries.append({"issue_id": issue_id, "repo": REPO,
                          "linked_pull_requests": [{"number": 50, "merged": True}] if number == 1 else []})
        raw.save(REPO, "issues", str(number), {
            "number": number, "title": f"Login fails {number}", "state": "closed", "state_reason": "completed",
            "labels": [{"name": "kind/bug"}], "html_url": f"https://github.com/{REPO}/issues/{number}",
            "body": f"### Describe the bug\nLogin fails case {number} with TokenVerificationException",
            "user": {"login": "reporter"}, "closed_by": {"login": "core-dev", "type": "User"},
        })
        raw.save(REPO, "comments", str(number), [
            {"id": number * 10, "body": "Fixed in 26.0.2, please upgrade to the latest release",
             "user": {"login": "core-dev", "type": "User"}, "author_association": "CONTRIBUTOR"}])
    raw.save(REPO, "pulls", "50", {"number": 50, "title": "Fix", "body": "Fixes #1", "merged_at": "2026-01-01T00:00:00Z",
                                   "html_url": f"https://github.com/{REPO}/pull/50"})
    raw.save(REPO, "pull_files", "50", [{"filename": "A.java", "status": "modified", "patch": "@@ -1 +1 @@\n-a\n+b"}])
    write_jsonl_atomic(tmp_path / "final.jsonl", selection)
    write_jsonl_atomic(tmp_path / "pool.jsonl", pool)
    write_jsonl_atomic(tmp_path / "tagged.jsonl", tagged)
    write_jsonl_atomic(tmp_path / "deep.jsonl", summaries)
    return ["--selection", str(tmp_path / "final.jsonl"), "--deep-selection", str(tmp_path / "pool.jsonl"),
            "--tagged", str(tmp_path / "tagged.jsonl"), "--deep-issues", str(tmp_path / "deep.jsonl"),
            "--raw-dir", str(tmp_path / "raw"), "--processed-dir", str(tmp_path / "p"),
            "--manifest-dir", str(tmp_path / "m"), "--chroma-dir", str(tmp_path / "chroma"),
            "--sparse-dir", str(tmp_path / "sparse")]


def test_build_indexes_end_to_end_and_idempotent(tmp_path, capsys):
    from scripts import build_indexes, verify_idempotency

    args = write_fixture(tmp_path)
    embed = FakeEmbedder()
    code, report = build_indexes.build(args, embedder=embed)
    assert code == 0
    counts, verification = report["counts"], report["verification"]
    assert counts["issue_items"] == 2  # 평가용 #3은 색인하지 않는다
    assert counts["documents_by_type"] == {"issue_body": 2, "comment": 2, "pull_request": 1, "pull_patch": 1}
    assert verification["issue_metadata_chroma"] == 2 and verification["issue_metadata_ids_match"] is True
    assert verification["issue_documents_ids_match"] is True and verification["every_issue_has_documents"]
    assert counts["maintainers_by_repo"] == {REPO: 1}
    manifest = json.loads((tmp_path / "m" / "index_manifest.json").read_text(encoding="utf-8"))
    assert manifest["embedding"]["dimensions"] == 8
    assert "Login fails case" not in (tmp_path / "m" / "index_manifest.json").read_text(encoding="utf-8")
    items = (tmp_path / "p" / "issue_items.jsonl").read_text(encoding="utf-8")
    assert f"{REPO}#3" not in items

    embedded = embed.texts
    verify_args = ["--chroma-dir", str(tmp_path / "chroma"), "--sparse-dir", str(tmp_path / "sparse")]
    rest = [a for a in args if a not in verify_args]
    assert verify_idempotency.main(verify_args + rest, embedder=embed) == 0
    assert embed.texts == embedded  # 재색인 시 임베딩 0건
    assert "결과: PASS" in capsys.readouterr().out


def test_build_indexes_skip_embed_and_rejects_evaluation(tmp_path):
    from scripts import build_indexes

    args = write_fixture(tmp_path)
    code, report = build_indexes.build(args + ["--skip-embed"])
    assert code == 0 and report["sync"] == {} and report["verification"]["issue_documents_bm25"] > 0
    assert not (tmp_path / "chroma").exists()
    assert build_indexes.build(args + ["--splits", "index", "evaluation"])[0] == 2
