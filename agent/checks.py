"""에이전트 검증 PR 세트 실행과 판정 (기획서 17.11절, 17.12절 단계 G).

tests/fixtures/agent_diffs/cases.yaml의 사례마다 임시 대상 레포를 만들고 main을 --dry-run --json으로 실행해
결과를 기대값과 비교한다. scripts/run_agent_checks.py(실제 LLM·코퍼스)와 tests/test_agent_checks.py(가짜)가 함께 쓴다.
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from src.config import load_yaml

ROOT = Path(__file__).resolve().parents[1]
CASES_DIR = ROOT / "tests/fixtures/agent_diffs"
FAKE_KEY = "sk-" + "a1B2" * 8  # 마스킹 확인용 가짜 키 (파일에 키 모양 문자열이 남지 않게 실행할 때 만든다)


@dataclass
class CheckResult:
    id: str
    title: str
    status: str  # PASS, WARN, FAIL, ERROR
    observed: dict = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    comment: str | None = None  # 원문 근거가 섞일 수 있어 보고서에는 넣지 않는다

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if k != "comment"}


class RecordingLLM:
    """LLM 입력에 마스킹 전 비밀정보가 들어가는지 확인하려고 프롬프트를 남긴다."""

    def __init__(self, llm):
        self.llm = llm
        self.prompts: list[str] = []

    def __call__(self, system: str, user: str) -> str:
        self.prompts.append(user)
        return self.llm(system, user)


def load_cases(path: Path = CASES_DIR / "cases.yaml") -> list[dict]:
    return load_yaml(path)["cases"]


def section_items(comment: str | None, heading: str) -> list[str]:
    """코멘트에서 제목으로 시작하는 섹션의 항목("- "로 시작하는 줄). 빈 줄에서 섹션이 끝난다."""
    lines = (comment or "").splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(heading)), None)
    if start is None:
        return []
    items = []
    for line in lines[start + 1:]:
        if not line.strip():
            break
        if line.startswith("- "):
            items.append(line)
    return items


def observe(output: dict) -> dict:
    research = output.get("research") or []
    comment = output.get("comment")
    return {
        "reviewed": "review" in output,
        "comment": comment is not None,
        "rag": sum(r["status"] == "grounded" for r in research),
        "general": len(section_items(comment, "### 일반 코드 리뷰")),
        "findings": [f["kind"] for f in (output.get("review") or {}).get("findings", [])],
        "review_source": (output.get("review") or {}).get("source"),
        "research": {r["finding_id"]: r["status"] for r in research},
    }


def grade(case: dict, output: dict, prompts: list[str]) -> CheckResult:
    expect, observed = case["expect"], observe(output)
    result = CheckResult(case["id"], case["title"], "PASS", observed, comment=output.get("comment"))
    if "reviewed" in expect and observed["reviewed"] != expect["reviewed"]:
        result.problems.append(f"리뷰 여부 {observed['reviewed']} (기대 {expect['reviewed']})")
    if expect.get("comment") in (True, False) and observed["comment"] != expect["comment"]:
        result.problems.append(f"코멘트 {'있음' if observed['comment'] else '없음'} (기대 {'있음' if expect['comment'] else '없음'})")
    for section in ("rag", "general"):
        want, count = expect.get(section, "any"), observed[section]
        if want == "required" and not count:
            result.problems.append(f"{section} 지적 없음 (필수)")
        elif want == "none" and count:
            result.problems.append(f"{section} 지적 {count}건 (없어야 함)")
        elif want == "expected" and not count:
            result.warnings.append(f"{section} 근거 기반 지적 없음 (코퍼스·검색 확인 필요)")
    if expect.get("masked"):
        leaked = [where for where, text in (("출력", json.dumps(output, ensure_ascii=False)), ("LLM 입력", "\n".join(prompts)))
                  if FAKE_KEY in text]
        if leaked:
            result.problems.append(f"가짜 키가 {', '.join(leaked)}에 남음")
    result.status = "FAIL" if result.problems else ("WARN" if result.warnings else "PASS")
    return result


def run_case(case: dict, main, extra_args: list[str], **main_kwargs) -> tuple[dict, str]:
    """임시 대상 레포에서 main을 실행해 (JSON 출력, 표준 오류)를 돌려준다."""
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp)
        if case.get("python_project"):
            (target / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
        patch = target / "changes.patch"
        patch.write_text((CASES_DIR / case["patch"]).read_text(encoding="utf-8").replace("{FAKE_KEY}", FAKE_KEY),
                         encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            main(["--diff", str(patch), "--dry-run", "--json", "--target-root", str(target), *extra_args], **main_kwargs)
        return json.loads(stdout.getvalue()), stderr.getvalue()
