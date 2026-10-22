"""PR 변경 파일 읽기 (기획서 17.3절 1~2단계).

GitHub API(`/pulls/{n}/files`)나 로컬 `git diff` 출력을 같은 형태(FileChange)로 바꾼다.
lock·생성·바이너리 파일과 제외 경로는 건너뛰고, 파일 수와 변경 줄 수에 상한을 둔다.
patch는 LLM이나 검색에 쓰기 전에 마스킹한다.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import asdict, dataclass, field

from src.collectors.github_client import GitHubClient
from src.preprocessing.secret_redactor import redact

HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
LOCK_FILES = {"poetry.lock", "uv.lock", "Pipfile.lock", "pdm.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml"}
GENERATED_PARTS = ("node_modules/", "dist/", "build/", "vendor/", ".venv/", "venv/", "__pycache__/", "site-packages/")
GENERATED_SUFFIXES = (".min.js", ".min.css", ".map", "_pb2.py", "_pb2_grpc.py", ".ipynb")


@dataclass
class Line:
    kind: str  # "+", "-", " "
    old_no: int | None
    new_no: int | None
    text: str


@dataclass
class Hunk:
    old_start: int
    new_start: int
    header: str
    lines: list[Line] = field(default_factory=list)


@dataclass
class FileChange:
    path: str
    status: str  # added, modified, removed, renamed
    additions: int = 0
    deletions: int = 0
    previous_path: str | None = None
    patch: str = ""  # 마스킹된 patch
    hunks: list[Hunk] = field(default_factory=list)
    skipped: str | None = None  # 건너뛴 이유 (lock, generated, binary, excluded, limit)

    def added_lines(self) -> list[tuple[int, str]]:
        """(새 파일의 줄 번호, 내용). 리뷰 후보는 이 줄만 가리킬 수 있다."""
        return [(ln.new_no, ln.text) for h in self.hunks for ln in h.lines if ln.kind == "+" and ln.new_no]

    def removed_lines(self) -> list[tuple[int, str]]:
        return [(ln.old_no, ln.text) for h in self.hunks for ln in h.lines if ln.kind == "-" and ln.old_no]


@dataclass
class DiffBundle:
    source: str  # "owner/repo#123" 또는 diff 파일 경로
    head_sha: str | None
    files: list[FileChange]
    redactions: dict[str, int] = field(default_factory=dict)
    truncated: bool = False  # 상한을 넘어 일부만 리뷰함

    @property
    def reviewed(self) -> list[FileChange]:
        return [f for f in self.files if not f.skipped]

    @property
    def skipped(self) -> list[FileChange]:
        return [f for f in self.files if f.skipped]

    def as_dict(self) -> dict:
        return asdict(self)


def parse_patch(patch: str) -> list[Hunk]:
    """GitHub patch(hunk 헤더부터 시작) 또는 파일 하나의 unified diff 본문을 hunk로 나눈다."""
    hunks: list[Hunk] = []
    old_no = new_no = 0
    for raw in patch.splitlines():
        match = HUNK_HEADER.match(raw)
        if match:
            old_no, new_no = int(match.group(1)), int(match.group(3))
            hunks.append(Hunk(old_no, new_no, match.group(5).strip()))
            continue
        if not hunks or raw.startswith("\\ No newline"):
            continue
        kind, text = (raw[0], raw[1:]) if raw[:1] in "+- " and raw else (" ", raw)
        if kind == "+":
            hunks[-1].lines.append(Line("+", None, new_no, text))
            new_no += 1
        elif kind == "-":
            hunks[-1].lines.append(Line("-", old_no, None, text))
            old_no += 1
        else:
            hunks[-1].lines.append(Line(" ", old_no, new_no, text))
            old_no += 1
            new_no += 1
    return hunks


def skip_reason(path: str, has_patch: bool, exclude_paths: list[str]) -> str | None:
    name = path.rsplit("/", 1)[-1]
    if name in LOCK_FILES:
        return "lock"
    if any(part in f"/{path}" for part in ("/" + p for p in GENERATED_PARTS)) or path.endswith(GENERATED_SUFFIXES):
        return "generated"
    if any(fnmatch.fnmatch(path, pattern) for pattern in exclude_paths):
        return "excluded"
    if not has_patch:
        return "binary"  # GitHub는 바이너리와 너무 큰 파일에 patch를 주지 않는다
    return None


def _finalize(source: str, head_sha: str | None, files: list[FileChange], settings: dict) -> DiffBundle:
    """제외 규칙, 상한, 마스킹을 적용한다."""
    exclude = list(settings.get("exclude_paths", []))
    max_files = settings.get("max_files", 30)
    max_lines = settings.get("max_changed_lines", 1500)
    redactions: dict[str, int] = {}
    reviewed = changed = 0
    truncated = False
    for change in files:
        change.skipped = skip_reason(change.path, bool(change.patch), exclude)
        if change.skipped:
            change.patch, change.hunks = "", []
            continue
        if reviewed >= max_files or changed + change.additions + change.deletions > max_lines:
            change.skipped, change.patch, change.hunks = "limit", "", []
            truncated = True
            continue
        masked, counts = redact(change.patch)
        for key, value in counts.items():
            redactions[key] = redactions.get(key, 0) + value
        change.patch, change.hunks = masked, parse_patch(masked)
        reviewed += 1
        changed += change.additions + change.deletions
    return DiffBundle(source, head_sha, files, redactions, truncated)


def from_pr_files(source: str, head_sha: str | None, items: list[dict], settings: dict) -> DiffBundle:
    """GitHub `/repos/{owner}/{repo}/pulls/{n}/files` 응답 항목으로 만든다."""
    files = [FileChange(path=item["filename"], status=item.get("status", "modified"),
                        additions=item.get("additions", 0), deletions=item.get("deletions", 0),
                        previous_path=item.get("previous_filename"), patch=item.get("patch") or "")
             for item in items]
    return _finalize(source, head_sha, files, settings)


def fetch_pull_request(client: GitHubClient, repo: str, number: int, settings: dict) -> DiffBundle:
    pull = client.get(f"/repos/{repo}/pulls/{number}", use_cache=False).data or {}
    items: list[dict] = []
    for page in client.paginate(f"/repos/{repo}/pulls/{number}/files", {"per_page": 100}, max_pages=10):
        items.extend(page.data or [])
    return from_pr_files(f"{repo}#{number}", (pull.get("head") or {}).get("sha"), items, settings)


def parse_unified_diff(text: str, source: str, settings: dict) -> DiffBundle:
    """`git diff` 출력(여러 파일)으로 만든다. 로컬 dry-run과 테스트용."""
    files: list[FileChange] = []
    current: FileChange | None = None
    body: list[str] = []

    def close() -> None:
        if current is not None:
            current.patch = "\n".join(body)
            current.additions = sum(1 for line in body if line.startswith("+") and not line.startswith("+++"))
            current.deletions = sum(1 for line in body if line.startswith("-") and not line.startswith("---"))
            files.append(current)

    for line in text.splitlines():
        if line.startswith("diff --git "):
            close()
            parts = line.split(" b/", 1)
            current, body = FileChange(path=parts[1] if len(parts) == 2 else line.split()[-1], status="modified"), []
            continue
        if current is None:
            continue
        if line.startswith("new file mode"):
            current.status = "added"
        elif line.startswith("deleted file mode"):
            current.status = "removed"
        elif line.startswith("rename from "):
            current.status, current.previous_path = "renamed", line[len("rename from "):]
        elif line.startswith("Binary files "):
            body = []
        elif line.startswith(("index ", "--- ", "+++ ", "similarity index", "rename to ", "old mode", "new mode")):
            continue
        elif line.startswith("@@") or body:
            body.append(line)
    close()
    return _finalize(source, None, files, settings)
