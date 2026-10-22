"""PR 코멘트 작성과 게시·갱신 (기획서 17.8절).

코멘트는 템플릿으로 조립한다. 해결 방법 초안은 이미 근거만으로 쓰고 인용을 검증했으므로(researcher),
여기서는 LLM을 다시 부르지 않고 근거 번호를 코멘트 전체 기준으로 다시 매기기만 한다. 같은 근거는 같은 번호를 쓴다.

- 숨은 표식 `<!-- issuelens-review -->`로 기존 코멘트를 찾아 갱신한다. PR당 코멘트는 하나만 유지된다
- 지적이 모두 사라지면 기존 코멘트를 "지적 없음"으로 갱신하고, 처음부터 지적이 없으면 코멘트를 만들지 않는다
- 파일·줄 링크는 head SHA 고정 링크다
- LLM이 만든 문장의 @멘션은 알림이 가지 않게 끊고, 게시 직전에 비밀정보를 한 번 더 가린다
"""
from __future__ import annotations

import re

from agent.diff_reader import DiffBundle
from agent.researcher import Research
from agent.reviewer import Finding, Review
from src.collectors.github_client import GitHubClient
from src.preprocessing.secret_redactor import redact
from src.prompts.answer_generator import source_label

MARKER = "<!-- issuelens-review -->"
DISCLAIMER = "> 공개 이슈와 문서를 검색해 제공하는 기술 조사 보조 결과입니다. 적용 전 재현 테스트로 확인하세요."
NO_FINDINGS = "현재 커밋 기준 지적 없음."
CITATION = re.compile(r"\[근거 (\d+)\]")
MENTION = re.compile(r"(?<![\w`])@(?=[A-Za-z0-9])")


def safe(text: str) -> str:
    """한 줄로 줄이고 @멘션을 끊는다 (코멘트로 다른 사람에게 알림이 가지 않게)."""
    return MENTION.sub("@​", " ".join((text or "").split()))


def location(finding: Finding, repo: str | None, head_sha: str | None) -> str:
    where = f"{finding.file}:{finding.line}" if finding.line else finding.file
    if not (repo and head_sha):
        return f"**{where}**"
    anchor = f"#L{finding.line}" if finding.line else ""
    return f"**[{where}](https://github.com/{repo}/blob/{head_sha}/{finding.file}{anchor})**"


class Citations:
    """해결 방법 초안마다 1부터 매긴 근거 번호를 코멘트 전체 번호로 바꾼다."""

    def __init__(self):
        self.entries: list[tuple[int, object]] = []
        self._by_id: dict[str, int] = {}

    def renumber(self, research: Research) -> str:
        mapping = {}
        for evidence in research.cited_evidence:
            if evidence.id not in self._by_id:
                self._by_id[evidence.id] = len(self.entries) + 1
                self.entries.append((self._by_id[evidence.id], evidence))
            mapping[evidence.number] = self._by_id[evidence.id]
        return CITATION.sub(lambda m: f"[근거 {mapping[int(m.group(1))]}]" if int(m.group(1)) in mapping else "",
                            research.draft)

    def details(self) -> list[str]:
        if not self.entries:
            return []
        lines = ["<details><summary>근거</summary>", ""]
        lines += [f"[근거 {n}] {e.issue_id} · {source_label(e)} · {e.source_url}  " for n, e in self.entries]
        return lines + ["", "</details>", ""]


def draft_lines(draft: str) -> list[str]:
    """초안의 본문은 들여쓴 문단으로, "확인:" 줄은 하위 항목으로 둔다."""
    body, checks = [], []
    for line in (l.strip() for l in draft.splitlines()):
        if not line:
            continue
        (checks if line.startswith("확인:") else body).append(safe(line))
    lines = [f"  {' '.join(body)}"] if body else []
    return lines + [f"  - {line}" for line in checks]


def compose(bundle: DiffBundle, review: Review, researches: dict[str, Research], repo: str | None = None,
            notes: list[str] | None = None) -> str | None:
    """코멘트 Markdown. 지적이 하나도 없으면 None."""
    grounded = [(f, researches[f.id]) for f in review.findings if f.id in researches and researches[f.id].status == "grounded"]
    grounded_ids = {f.id for f, _ in grounded}
    general = [f for f in review.findings if f.id not in grounded_ids]
    if not grounded and not general:
        return None

    sha = bundle.head_sha
    lines = [MARKER, f"## IssueLens 리뷰{f' (`{sha[:7]}` 기준)' if sha else ''}", ""]
    citations = Citations()
    if grounded:
        lines += ["### 알려진 이슈 기반 (RAG)"]
        for finding, research in grounded:
            lines.append(f"- {location(finding, repo, sha)} `{safe(finding.summary)}`")
            lines += draft_lines(citations.renumber(research))
        lines.append("")
    if general:
        lines += ["### 일반 코드 리뷰 (RAG 미사용, 근거 없음)"]
        for finding in general:
            note = " (관련 라이브러리 이슈를 찾지 못함)" if finding.needs_research else ""
            concern = f" {safe(finding.concern)}" if finding.concern else ""
            lines.append(f"- {location(finding, repo, sha)} {safe(finding.summary)}.{concern}{note}")
        lines.append("")
    lines += citations.details()
    extra = list(notes or [])
    if bundle.truncated:
        extra.append("변경이 커서 앞부분 파일만 리뷰했습니다.")
    lines += [f"> {safe(n)}" for n in extra] + ([">"] if extra else []) + [DISCLAIMER]
    # LLM 입력은 이미 마스킹했지만, 코멘트는 공개되므로 게시 직전에 한 번 더 가린다
    return redact("\n".join(lines))[0]


def cleared(head_sha: str | None) -> str:
    return "\n".join([MARKER, f"## IssueLens 리뷰{f' (`{head_sha[:7]}` 기준)' if head_sha else ''}", "", NO_FINDINGS, "",
                      DISCLAIMER])


def find_comment(client: GitHubClient, repo: str, number: int) -> dict | None:
    for page in client.paginate(f"/repos/{repo}/issues/{number}/comments", {"per_page": 100}):
        for comment in page.data or []:
            if MARKER in (comment.get("body") or ""):
                return comment
    return None


def publish(client: GitHubClient, repo: str, number: int, body: str | None, head_sha: str | None) -> str:
    """created, updated, cleared, skipped 중 하나."""
    existing = find_comment(client, repo, number)
    if body is None:
        if existing is None:
            return "skipped"
        client.send("PATCH", f"/repos/{repo}/issues/comments/{existing['id']}", {"body": cleared(head_sha)})
        return "cleared"
    if existing is not None:
        client.send("PATCH", f"/repos/{repo}/issues/comments/{existing['id']}", {"body": body})
        return "updated"
    client.send("POST", f"/repos/{repo}/issues/{number}/comments", {"body": body})
    return "created"
