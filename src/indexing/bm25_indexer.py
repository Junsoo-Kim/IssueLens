"""기술 토큰을 보존하는 토크나이저와 저장소별 BM25 역색인 (기획서 6.5절).

인덱스는 pickle이 아니라 토큰 목록 JSON으로 저장하고, 불러올 때 BM25Okapi를 다시 만든다.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

from rank_bm25 import BM25Okapi

from src.collectors.cache import repo_slug, write_json_atomic

TOKENIZER_VERSION = "1"
TOKEN = re.compile(
    r"[A-Za-z_][\w$]*(?:[.:/][A-Za-z_][\w$-]*)+"  # 패키지·클래스·경로: org.keycloak.protocol, area/oidc
    r"|v?\d+(?:\.\d+)+(?:[-.][A-Za-z0-9]+)*"  # 버전: 26.0.1, v1.2.3-rc1
    r"|[A-Za-z_][\w-]*"  # 식별자·단어: TokenVerificationException, KC_DB_POOL_MAX_SIZE
    r"|\d+"  # 숫자·상태 코드: 401
    r"|[가-힣]+"
)
CAMEL_PART = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "if", "then", "else", "of", "to", "in", "on", "at", "by", "for", "with",
    "from", "as", "is", "are", "was", "were", "be", "been", "being", "it", "its", "this", "that", "these", "those",
    "i", "we", "you", "he", "she", "they", "me", "my", "our", "your", "so", "do", "does", "did", "have", "has", "had",
    "not", "no", "can", "will", "would", "should", "could", "there", "here", "when", "what", "which", "who", "how",
    "also", "just", "into", "about", "after", "before", "than", "too", "very", "any", "all", "some",
}


def tokenize(text: str) -> list[str]:
    """소문자 토큰과 원형 토큰을 함께 낸다. 점·밑줄·하이픈이 든 기술 토큰은 원형을 지우지 않고 부분도 추가한다."""
    tokens: list[str] = []
    for match in TOKEN.finditer(text or ""):
        token = match.group(0).strip(".-_:/")
        if not token:
            continue
        lower = token.lower()
        is_plain_word = lower.isalpha()
        if is_plain_word and (lower in STOPWORDS or len(lower) < 2):
            continue
        tokens.append(lower)
        if token != lower:
            tokens.append(token)
        if re.search(r"[.:/_-]", token) and not token[0].isdigit() and not re.match(r"v\d", lower):
            tokens.extend(p.lower() for p in re.split(r"[.:/_-]+", token) if len(p) > 1 and p.lower() not in STOPWORDS)
        elif not is_plain_word or token != lower:
            parts = CAMEL_PART.findall(token)
            if len(parts) > 1:
                tokens.extend(p.lower() for p in parts if len(p) > 1)
    return tokens


class BM25Index:
    def __init__(self, ids: list[str], tokens: list[list[str]]):
        self.ids = ids
        self.tokens = tokens
        self._bm25 = BM25Okapi(tokens) if tokens else None

    def search(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        if self._bm25 is None:
            return []
        scores = self._bm25.get_scores(tokenize(query))
        ranked = sorted(range(len(self.ids)), key=lambda i: (-scores[i], self.ids[i]))
        return [(self.ids[i], float(scores[i])) for i in ranked[:top_k] if scores[i] > 0]

    @classmethod
    def load(cls, path: Path) -> "BM25Index":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("tokenizer_version") != TOKENIZER_VERSION:
            raise ValueError(f"토크나이저 버전이 다릅니다: {path}")
        return cls(data["ids"], data["tokens"])


def build_bm25_indexes(records: list[dict], output_dir: Path) -> dict[str, int]:
    """records: [{"id", "text", "metadata": {"repo"}}]. 저장소별로 인덱스 파일을 만들고 저장소별 건수를 반환한다.

    저장소별로 문서 빈도를 따로 계산해 큰 저장소가 다른 저장소의 점수 분포를 왜곡하지 않게 한다.
    """
    by_repo: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        by_repo[record["metadata"]["repo"]].append(record)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for stale in output_dir.glob("*.json"):
        if stale.stem not in {repo_slug(repo) for repo in by_repo}:
            stale.unlink()
    counts = {}
    for repo, items in sorted(by_repo.items()):
        items = sorted(items, key=lambda r: r["id"])
        write_json_atomic(output_dir / f"{repo_slug(repo)}.json", {
            "tokenizer_version": TOKENIZER_VERSION,
            "repo": repo,
            "ids": [r["id"] for r in items],
            "tokens": [tokenize(r["text"]) for r in items],
        })
        counts[repo] = len(items)
    return counts


def load_bm25_ids(output_dir: Path) -> set[str]:
    ids: set[str] = set()
    for path in Path(output_dir).glob("*.json"):
        with open(path, encoding="utf-8") as f:
            ids.update(json.load(f)["ids"])
    return ids
