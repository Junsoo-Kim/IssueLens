"""대상 판별과 변경 신호 추출 (기획서 17.4절). 규칙만 쓰며 LLM을 부르지 않는다.

- 대상 판별: Python 프로젝트(의존성 파일 또는 .py 변경)만 리뷰한다
- 의존성 변경: requirements*.txt, pyproject.toml의 추가·제거·업그레이드·다운그레이드와 메이저 버전 경계
- import: .py에 새로 추가된 import (표준 라이브러리와 상대 import 제외)
- API 호출: import한 이름으로 시작하는 호출 (예: openai.ChatCompletion.create, ChatOpenAI(...))
- 설정 변경: .env.example, yaml, toml, ini, cfg의 바뀐 키와 값
"""
from __future__ import annotations

import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from agent.diff_reader import DiffBundle, FileChange

DEPENDENCY_FILES = re.compile(r"(^|/)(requirements[\w.-]*\.txt|constraints[\w.-]*\.txt|pyproject\.toml)$")
PYTHON_MARKERS = ("requirements.txt", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile")
CONFIG_FILE = re.compile(r"(^|/)(\.env[\w.-]*|[\w.-]+\.(ya?ml|toml|ini|cfg|properties))$")
# 요구사항 한 줄: name[extras] (spec), pyproject 배열 항목 "name>=1.0", Poetry 표 name = "^1.0"
REQUIREMENT = re.compile(r"""^\s*["']?([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*((?:[<>=!~^]=?|===)\s*[^"',;#\s]+(?:\s*,\s*[<>=!~]=?\s*[^"',;#\s]+)*)?""")
POETRY = re.compile(r"""^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*=\s*(?:["']([^"']+)["']|\{[^}]*version\s*=\s*["']([^"']+)["'])""")
VERSION = re.compile(r"\d+(?:\.\d+){0,3}")
IMPORT = re.compile(r"^\s*import\s+(.+)$")
FROM_IMPORT = re.compile(r"^\s*from\s+([A-Za-z_][\w.]*)\s+import\s+(.+)$")
CALL = re.compile(r"(?<![\w.])([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*\(")
CONFIG_LINE = re.compile(r"""^\s*(?:export\s+)?["']?([A-Za-z_][\w.-]*)["']?\s*[:=]\s*(.*?)\s*$""")
STDLIB = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}
# import 이름과 PyPI 패키지 이름이 다른 흔한 경우. 나머지는 밑줄을 하이픈으로 바꿔 추정한다 (큐레이션 매핑은 단계 C)
IMPORT_TO_PACKAGE = {"yaml": "pyyaml", "dotenv": "python-dotenv", "sklearn": "scikit-learn", "cv2": "opencv-python",
                     "PIL": "pillow", "bs4": "beautifulsoup4", "google.generativeai": "google-generativeai",
                     "llama_index": "llama-index", "huggingface_hub": "huggingface-hub", "dateutil": "python-dateutil"}
NON_PYTHON_PROJECT = "Python 프로젝트가 아니어서 리뷰하지 않습니다 (의존성 파일과 .py 변경 없음)."


def normalize_package(name: str) -> str:
    """PEP 503 정규화: 소문자, 연속된 . _ -를 하이픈 하나로."""
    return re.sub(r"[-_.]+", "-", name).lower()


def package_of_import(module: str) -> str:
    parts = module.split(".")
    for size in range(len(parts), 0, -1):  # 긴 경로부터: google.generativeai.types -> google.generativeai
        prefix = ".".join(parts[:size])
        if prefix in IMPORT_TO_PACKAGE:
            return IMPORT_TO_PACKAGE[prefix]
    return normalize_package(module.split(".")[0])


def version_tuple(spec: str | None) -> tuple[int, ...] | None:
    match = VERSION.search(spec or "")
    return tuple(int(p) for p in match.group(0).split(".")) if match else None


@dataclass
class DependencyChange:
    package: str
    file: str
    line: int | None  # 새 파일의 줄 (제거면 None)
    old: str | None  # 버전 조건 (예: ==0.28.1)
    new: str | None
    kind: str  # added, removed, upgraded, downgraded, changed
    major_change: bool = False  # 메이저 버전 경계를 넘음 (0.x -> 1.x 포함)


@dataclass
class ImportUse:
    module: str
    package: str
    names: list[str]
    file: str
    line: int


@dataclass
class ApiCall:
    call: str  # 모듈 경로로 풀어 쓴 호출 (예: openai.ChatCompletion.create)
    package: str
    file: str
    line: int
    code: str


@dataclass
class ConfigChange:
    file: str
    key: str
    old: str | None
    new: str | None
    line: int | None


@dataclass
class Signals:
    python_project: bool
    reason: str
    dependencies: list[DependencyChange] = field(default_factory=list)
    imports: list[ImportUse] = field(default_factory=list)
    api_calls: list[ApiCall] = field(default_factory=list)
    configs: list[ConfigChange] = field(default_factory=list)

    def packages(self) -> list[str]:
        """신호에 등장한 패키지 (의존성 변경, 새 import, API 호출 순서)."""
        names = [d.package for d in self.dependencies] + [i.package for i in self.imports] + [c.package for c in self.api_calls]
        return list(dict.fromkeys(names))

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- 대상 판별


def detect_python_project(bundle: DiffBundle, target_root: Path | None = None) -> tuple[bool, str]:
    if target_root is not None:
        found = [m for m in PYTHON_MARKERS if (target_root / m).exists()]
        if found:
            return True, f"레포 루트에 {', '.join(found)}"
    paths = [f.path for f in bundle.files]
    markers = [p for p in paths if p.rsplit("/", 1)[-1] in PYTHON_MARKERS or DEPENDENCY_FILES.search(p)]
    if markers:
        return True, f"변경 파일에 {', '.join(markers[:3])}"
    if any(p.endswith(".py") for p in paths):
        return True, ".py 파일 변경"
    return False, NON_PYTHON_PROJECT


# ---------------------------------------------------------------- 의존성


def _requirement(text: str, toml: bool) -> tuple[str, str | None] | None:
    line = text.split("#", 1)[0].strip().rstrip(",")
    if not line or line.startswith(("-", "[")):  # -r, -e, --index-url, 섹션 헤더
        return None
    if toml:
        poetry = POETRY.match(line)
        if poetry and poetry.group(1).lower() not in {"python", "name", "version", "description", "requires-python"}:
            return normalize_package(poetry.group(1)), (poetry.group(2) or poetry.group(3) or "").strip()
        if not line.startswith(("'", '"')):  # pyproject 배열 항목은 따옴표로 시작한다
            return None
    match = REQUIREMENT.match(line)
    if not match:
        return None
    return normalize_package(match.group(1)), (match.group(3) or "").replace(" ", "") or None


def _classify(old: str | None, new: str | None) -> tuple[str, bool]:
    """양쪽에 모두 있는 패키지의 버전 조건 비교. 버전을 못 읽으면 changed."""
    before, after = version_tuple(old), version_tuple(new)
    if not before or not after or before == after:
        return "changed", False
    major = before[0] != after[0] or (before[0] == 0 and len(before) > 1 and len(after) > 1 and before[1] != after[1])
    return ("upgraded" if after > before else "downgraded"), major


def dependency_changes(change: FileChange) -> list[DependencyChange]:
    toml = change.path.endswith(".toml")
    removed: dict[str, str | None] = {}
    for _, text in change.removed_lines():
        if parsed := _requirement(text, toml):
            removed[parsed[0]] = parsed[1]
    added: dict[str, tuple[str | None, int]] = {}
    for line_no, text in change.added_lines():
        if parsed := _requirement(text, toml):
            added[parsed[0]] = (parsed[1], line_no)

    results = []
    for package in dict.fromkeys([*added, *removed]):
        old = removed.get(package)
        new, line = added.get(package, (None, None))
        if package in removed and package in added:
            if old == new:
                continue  # 줄 위치만 바뀜
            kind, major = _classify(old, new)
        else:
            kind, major = ("added" if package in added else "removed"), False
        results.append(DependencyChange(package, change.path, line, old, new, kind, major))
    return results


# ---------------------------------------------------------------- import와 API 호출


def _imports(lines: list[tuple[int, str]], file: str) -> tuple[list[ImportUse], dict[str, str]]:
    """(새 import 목록, 별칭 -> 모듈 경로). 별칭은 hunk의 기존 줄과 추가 줄 모두에서 모은다."""
    uses, aliases = [], {}
    for line_no, text in lines:
        if (m := FROM_IMPORT.match(text)) and not m.group(1).startswith("."):
            module = m.group(1)
            names = [n.strip().split(" as ") for n in m.group(2).strip("() \\").split(",") if n.strip()]
            for parts in names:
                aliases[parts[-1].strip()] = f"{module}.{parts[0].strip()}"
            uses.append(ImportUse(module, package_of_import(module), [p[0].strip() for p in names], file, line_no))
        elif m := IMPORT.match(text):
            for item in m.group(1).split(","):
                parts = item.strip().split(" as ")
                module = parts[0].strip()
                if module:
                    aliases[parts[-1].strip() if len(parts) > 1 else module.split(".")[0]] = \
                        module if len(parts) > 1 else module.split(".")[0]
                    uses.append(ImportUse(module, package_of_import(module), [], file, line_no))
    return [u for u in uses if u.module.split(".")[0] not in STDLIB], aliases


def python_signals(change: FileChange) -> tuple[list[ImportUse], list[ApiCall]]:
    added = change.added_lines()
    context = [(ln.new_no, ln.text) for h in change.hunks for ln in h.lines if ln.kind == " " and ln.new_no]
    new_imports, _ = _imports(added, change.path)
    _, aliases = _imports(sorted(added + context), change.path)
    aliases = {a: m for a, m in aliases.items() if m.split(".")[0] not in STDLIB}
    calls = []
    for line_no, text in added:
        if IMPORT.match(text) or FROM_IMPORT.match(text):
            continue
        code = text.split("#", 1)[0]
        for match in CALL.finditer(code):
            head, _, rest = match.group(1).partition(".")
            if head in aliases:
                full = aliases[head] + (f".{rest}" if rest else "")
                calls.append(ApiCall(full, package_of_import(aliases[head]), change.path, line_no, text.strip()[:200]))
    return new_imports, calls


# ---------------------------------------------------------------- 설정


def config_changes(change: FileChange) -> list[ConfigChange]:
    def parse(lines):
        values = {}
        for line_no, text in lines:
            stripped = text.strip()
            if not stripped or stripped.startswith(("#", ";", "[", "-")):
                continue
            if m := CONFIG_LINE.match(text):
                values[m.group(1)] = (m.group(2).strip().strip("\"'"), line_no)
        return values

    before, after = parse(change.removed_lines()), parse(change.added_lines())
    results = []
    for key in list(dict.fromkeys([*after, *before])):
        old = before.get(key, (None, None))[0]
        new, line = after.get(key, (None, None))
        if old != new:
            results.append(ConfigChange(change.path, key, old, new, line))
    return results


def extract_signals(bundle: DiffBundle, target_root: Path | None = None) -> Signals:
    python_project, reason = detect_python_project(bundle, target_root)
    signals = Signals(python_project, reason)
    if not python_project:
        return signals
    for change in bundle.reviewed:
        if DEPENDENCY_FILES.search(change.path):
            signals.dependencies += dependency_changes(change)
            if change.path.endswith("pyproject.toml"):
                signals.configs += [c for c in config_changes(change)
                                    if not any(d.package == normalize_package(c.key) for d in signals.dependencies)]
        elif change.path.endswith(".py"):
            imports, calls = python_signals(change)
            signals.imports += imports
            signals.api_calls += calls
        elif CONFIG_FILE.search(change.path):
            signals.configs += config_changes(change)
    return signals


def format_signals(signals: Signals) -> str:
    lines = ["== 변경 신호", f"대상: {'Python 프로젝트' if signals.python_project else '리뷰 안 함'} ({signals.reason})"]
    if not signals.python_project:
        return "\n".join(lines)
    labels = {"added": "추가", "removed": "제거", "upgraded": "업그레이드", "downgraded": "다운그레이드", "changed": "변경"}
    lines.append(f"의존성 변경 {len(signals.dependencies)}건")
    lines += [f"  {d.package}: {d.old or '-'} -> {d.new or '-'} ({labels[d.kind]}{', 메이저 경계' if d.major_change else ''}) "
              f"{d.file}{':' + str(d.line) if d.line else ''}" for d in signals.dependencies]
    lines.append(f"새 import {len(signals.imports)}건")
    lines += [f"  {i.module}{' (' + ', '.join(i.names) + ')' if i.names else ''} -> {i.package} {i.file}:{i.line}"
              for i in signals.imports]
    lines.append(f"라이브러리 API 호출 {len(signals.api_calls)}건")
    lines += [f"  {c.call} {c.file}:{c.line}" for c in signals.api_calls]
    lines.append(f"설정 변경 {len(signals.configs)}건")
    lines += [f"  {c.file} {c.key}: {c.old or '-'} -> {c.new or '-'}" for c in signals.configs]
    return "\n".join(lines)
