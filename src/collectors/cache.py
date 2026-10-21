"""ETag/Last-Modified 기반 요청 캐시, 원본 저장소, 수집 manifest 관리."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def repo_slug(repo: str) -> str:
    """`owner/repo`를 파일 경로에 쓸 수 있는 `owner__repo`로 바꾼다."""
    return repo.replace("/", "__")


def write_json_atomic(path: Path, data: Any) -> None:
    """임시 파일에 쓴 뒤 교체해, 중단되더라도 반쯤 쓰인 파일이 남지 않게 한다."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_jsonl_atomic(path: Path, records: Iterable[dict]) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    count = 0
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                count += 1
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return count


def read_jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


class ResponseCache:
    """URL과 쿼리 파라미터 조합별로 ETag, Last-Modified, 응답 본문을 저장한다.

    재호출 시 조건부 요청을 보내고, 304이면 저장된 본문을 재사용한다.
    """

    def __init__(self, cache_dir: Path):
        self.dir = Path(cache_dir) / "http"

    @staticmethod
    def request_key(method: str, url: str, params: dict | None = None) -> str:
        sorted_params = json.dumps(sorted((params or {}).items()), ensure_ascii=False)
        return hashlib.sha256(f"{method.upper()} {url} {sorted_params}".encode()).hexdigest()

    def _path(self, key: str) -> Path:
        return self.dir / key[:2] / f"{key}.json"

    def get(self, key: str) -> dict | None:
        path = self._path(key)
        if not path.exists():
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def put(self, key: str, entry: dict) -> None:
        write_json_atomic(self._path(key), {"request_key": key, **entry})


class RawStore:
    """API 원본 응답을 `{root}/{owner}__{repo}/{kind}/{name}.json`에 보존한다."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def path(self, repo: str, kind: str, name: str) -> Path:
        return self.root / repo_slug(repo) / kind / f"{name}.json"

    def save(self, repo: str, kind: str, name: str, data: Any) -> Path:
        path = self.path(repo, kind, name)
        write_json_atomic(path, data)
        return path


class Manifest:
    """키별 수집 상태를 JSONL에 추가 기록한다. 같은 키는 마지막 기록이 현재 상태다."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.records: dict[str, dict] = {}
        if self.path.exists():
            for record in read_jsonl(self.path):
                self.records[record["key"]] = record

    def get(self, key: str) -> dict | None:
        return self.records.get(key)

    def is_done(self, key: str) -> bool:
        record = self.records.get(key)
        return record is not None and record["status"] == "done"

    def mark(self, key: str, status: str, **fields: Any) -> dict:
        record = {"key": key, "status": status, "updated_at": utc_now(), **fields}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.records[key] = record
        return record
