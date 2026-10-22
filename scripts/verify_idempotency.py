"""재색인 전후 컬렉션 개수와 ID 집합 비교 (기획서 체크리스트 2번, F2002).

    python scripts/verify_idempotency.py

build_indexes.py를 한 번 더 실행해 신규·갱신·삭제가 0건이고 개수와 ID 집합이 그대로인지 확인한다.
먼저 build_indexes.py로 색인을 만들어 두어야 한다.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import build_indexes  # noqa: E402
from src.config import load_yaml  # noqa: E402
from src.indexing import document_indexer, metadata_indexer  # noqa: E402
from src.indexing.bm25_indexer import load_bm25_ids  # noqa: E402
from src.indexing.chroma_store import collection_ids, get_collection, open_client  # noqa: E402


def snapshot(chroma_dir: Path, sparse_dir: Path, names: list[str]) -> dict:
    client = open_client(chroma_dir)
    state = {}
    for name in names:
        ids = collection_ids(get_collection(client, name))
        state[name] = {"chroma": ids, "bm25": load_bm25_ids(sparse_dir / name)}
    return state


def main(argv: list[str] | None = None, embedder=None) -> int:
    parser = argparse.ArgumentParser(description="재색인 멱등성 검증")
    parser.add_argument("--chroma-dir", type=Path, default=ROOT / "chroma_db")
    parser.add_argument("--sparse-dir", type=Path, default=ROOT / "sparse_index")
    parser.add_argument("--retrieval-config", type=Path, default=ROOT / "configs/retrieval.yaml")
    args, build_args = parser.parse_known_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    collections = load_yaml(args.retrieval_config).get("collections", {})
    names = [collections.get("issue_metadata", metadata_indexer.COLLECTION),
             collections.get("issue_documents", document_indexer.COLLECTION)]
    common = ["--chroma-dir", str(args.chroma_dir), "--sparse-dir", str(args.sparse_dir),
              "--retrieval-config", str(args.retrieval_config), *build_args]

    before = snapshot(args.chroma_dir, args.sparse_dir, names)
    if not before[names[0]]["chroma"]:
        print("색인이 비어 있습니다. 먼저 scripts/build_indexes.py를 실행하세요.", file=sys.stderr)
        return 2
    print("== 재색인 실행")
    code, report = build_indexes.build(common, embedder=embedder)
    after = snapshot(args.chroma_dir, args.sparse_dir, names)

    print("\n== 멱등성 검증")
    passed = code == 0 and report is not None and not report["skip_embed"]
    for name, sync in (report or {}).get("sync", {}).items():
        changed = sync["added"] + sync["updated"] + sync["deleted"]
        passed &= changed == 0
        print(f"{name} 재임베딩: 신규 {sync['added']} / 갱신 {sync['updated']} / 삭제 {sync['deleted']}")
    for name in names:
        for store in ("chroma", "bm25"):
            same = before[name][store] == after[name][store]
            passed &= same
            print(f"{name} {store}: 전 {len(before[name][store])} / 후 {len(after[name][store])} / ID 집합 동일 {same}")
    print("결과:", "PASS" if passed else "FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
