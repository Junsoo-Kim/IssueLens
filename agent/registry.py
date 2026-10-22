"""패키지 이름 -> GitHub 저장소 (기획서 17.5절).

1. 큐레이션 목록(configs/agent_corpus.yaml)의 매핑을 먼저 본다
2. 없으면 PyPI `GET https://pypi.org/pypi/{name}/json`의 project_urls에서 github.com 주소를 찾는다
3. 그래도 없으면 저장소를 모른다고 표시한다 (이후 단계에서 GitHub 전체 검색으로 대체)
PyPI 조회 결과는 캐시 파일에 저장해 같은 패키지를 다시 묻지 않는다.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from agent.signals import Signals, normalize_package
from src.config import load_yaml

PYPI_URL = "https://pypi.org/pypi/{name}/json"
GITHUB_REPO = re.compile(r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", re.I)
# project_urls에서 소스 저장소일 가능성이 높은 키 순서
URL_KEYS = ("source", "source code", "repository", "code", "github", "homepage", "home", "issues", "bug tracker",
            "documentation")
CACHE_FILE = "agent_registry.json"

Fetch = Callable[[str], dict | None]  # URL -> JSON (없으면 None)


@dataclass
class Library:
    repo: str
    category: str
    packages: list[str]
    discussions: bool = True  # Discussions Q&A 채택 답변을 수집할지


@dataclass
class Resolution:
    package: str
    repo: str | None
    source: str  # curated, pypi, unknown
    curated: bool = False


@dataclass
class Corpus:
    libraries: list[Library]
    defaults: dict = field(default_factory=dict)

    def by_package(self) -> dict[str, Library]:
        return {normalize_package(p): lib for lib in self.libraries for p in lib.packages}

    def by_repo(self) -> dict[str, Library]:
        return {lib.repo.lower(): lib for lib in self.libraries}


def load_corpus(path: Path) -> Corpus:
    config = load_yaml(path)
    defaults = config.get("defaults", {})
    return Corpus([Library(item["repo"], item.get("category", ""), [normalize_package(p) for p in item.get("packages", [])],
                           item.get("discussions", defaults.get("discussions", True)))
                   for item in config.get("libraries", [])], defaults)


def github_repo_from_urls(urls: dict[str, str] | None, home_page: str | None = None) -> str | None:
    """PyPI info의 project_urls와 home_page에서 owner/repo. 소스 저장소일 가능성이 높은 키부터 본다."""
    items = {(k or "").strip().lower(): v for k, v in (urls or {}).items() if v}
    ordered = [items[k] for k in URL_KEYS if k in items] + [v for k, v in items.items() if k not in URL_KEYS]
    if home_page:
        ordered.append(home_page)
    for url in ordered:
        match = GITHUB_REPO.search(url)
        if match and match.group(1).lower() not in {"sponsors", "orgs", "topics"}:
            name = re.sub(r"\.git$", "", match.group(2))
            return f"{match.group(1)}/{name}"
    return None


def default_fetch(url: str) -> dict | None:
    import requests

    try:
        response = requests.get(url, timeout=10, headers={"Accept": "application/json"})
    except requests.RequestException:
        return None
    return response.json() if response.status_code == 200 else None


class Registry:
    def __init__(self, corpus: Corpus, cache_dir: Path | None = None, fetch: Fetch | None = None):
        self.corpus = corpus
        self.curated = corpus.by_package()
        self.fetch = fetch or default_fetch
        self.cache_path = Path(cache_dir) / CACHE_FILE if cache_dir else None
        self.cache: dict[str, str | None] = {}
        if self.cache_path and self.cache_path.exists():
            self.cache = json.loads(self.cache_path.read_text(encoding="utf-8"))

    def _save(self) -> None:
        if self.cache_path:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self.cache, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    def resolve(self, package: str) -> Resolution:
        name = normalize_package(package)
        if name in self.curated:
            return Resolution(name, self.curated[name].repo, "curated", True)
        if name not in self.cache:
            data = self.fetch(PYPI_URL.format(name=name)) or {}
            info = data.get("info") or {}
            self.cache[name] = github_repo_from_urls(info.get("project_urls"), info.get("home_page"))
            self._save()
        repo = self.cache[name]
        curated = bool(repo and repo.lower() in self.corpus.by_repo())  # 다른 이름으로 같은 저장소를 쓰는 패키지
        return Resolution(name, repo, "pypi" if repo else "unknown", curated)

    def resolve_signals(self, signals: Signals) -> list[Resolution]:
        return [self.resolve(p) for p in signals.packages()]

    def as_dict(self, resolutions: list[Resolution]) -> list[dict]:
        return [asdict(r) for r in resolutions]


def format_resolutions(resolutions: list[Resolution]) -> str:
    labels = {"curated": "큐레이션", "pypi": "PyPI", "unknown": "저장소 모름"}
    lines = [f"== 라이브러리 저장소 ({sum(r.curated for r in resolutions)}/{len(resolutions)}개 큐레이션 코퍼스)"]
    lines += [f"  {r.package} -> {r.repo or '-'} ({labels[r.source]}{', 코퍼스 있음' if r.curated and r.source != 'curated' else ''})"
              for r in resolutions]
    return "\n".join(lines)
