"""요구사항 정의서 4장 증빙 중 명령 출력으로 확인하는 항목을 한 번에 만든다 (기획서 15장, docs/evidence_checklist.md).

    python scripts/export_evidence.py                  # 전체
    python scripts/export_evidence.py --only 01 05 07  # 일부
    python scripts/export_evidence.py --skip-rebuild   # 02(재색인 멱등성) 생략

항목마다 data/processed/evidence/NN_*.txt를 만들고 화면에는 PASS/확인 필요만 요약한다. 결과 파일은 이슈 원문 일부를
담을 수 있어 커밋하지 않는다(.gitignore). 화면 조작이 필요한 항목(10~13)은 체크리스트의 순서대로 캡처한다.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from scripts.ask import format_state, snippet  # noqa: E402
from src.collectors.cache import read_jsonl  # noqa: E402
from src.config import load_yaml  # noqa: E402
from src.graph.workflow import graph_diagram  # noqa: E402
from src.indexing.document_indexer import document_records  # noqa: E402
from src.prompts.answer_generator import NO_EVIDENCE_MESSAGE, source_label  # noqa: E402
from src.services.assistant import build_assistant, resolve_paths  # noqa: E402
from src.services.citation_validator import CITATION  # noqa: E402

OUT_DIR = "data/processed/evidence"
GROUNDED_QUESTION = "DataHub 인덱스 복원 중 NullPointerException이 발생해"
ABSTAIN_QUESTION = "iOS 앱에서 푸시 알림이 오지 않아요"
# 색인에 없는 예외명이라 질문 시점 수집까지 간다. 처음 실행하면 GitHub를 검색하고, 두 번째는 캐시를 쓴다
CACHE_QUESTION = "Keycloak 업그레이드 후 SAML 로그인에서 InvalidSignatureException 발생"
SECRET_PATTERNS = {
    "OpenAI 키": re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    "GitHub 토큰": re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"),
    "AWS 키": re.compile(r"AKIA[0-9A-Z]{16}"),
    "키 값 대입": re.compile(r"(?:OPENAI|GITHUB|LANGSMITH)[A-Z_]*(?:KEY|TOKEN)[ \t]*=[ \t]*['\"]?[A-Za-z0-9_-]{16,}"),
}


class Context:
    def __init__(self, root: Path, overrides: dict):
        self.root = root
        self.overrides = overrides
        self._assistant = None
        self.app_config = load_yaml(root / "configs/app.yaml")
        self.paths = resolve_paths(root, self.app_config)

    @property
    def assistant(self):
        if self._assistant is None:
            self._assistant = build_assistant(self.root, **self.overrides)
        return self._assistant

    def ask(self, question: str, **kwargs) -> tuple[dict, float]:
        started = time.perf_counter()
        state = self.assistant.ask(question, **kwargs)
        return state, time.perf_counter() - started


# ---------------------------------------------------------------- 항목


def item_01(ctx: Context) -> tuple[bool, str]:
    """F2001: 로드 수와 RAG 1 항목 수, 색인 텍스트 예시."""
    selection = read_jsonl(ctx.paths["manifest_dir"] / "final_selection.jsonl")
    loaded = sum(r["final_split"] == "index" for r in selection)
    counts = ctx.assistant.retriever.counts()
    sample = ctx.assistant.retriever.issue_collection.get(limit=1, include=["documents", "metadatas"])
    passed = loaded == counts["issues"]
    lines = [f"최종 선별 색인 대상(로드 수): {loaded}", f"RAG 1 issue_metadata 항목 수: {counts['issues']}",
             f"RAG 2 issue_documents 청크 수: {counts['chunks']}", f"로드 수 = RAG 1 항목 수: {passed}", "",
             "== 색인 텍스트 예시", f"ID: {sample['ids'][0]}", sample["documents"][0]]
    return passed, "\n".join(lines)


def item_02(ctx: Context) -> tuple[bool, str]:
    """F2002: 같은 색인을 다시 만들어도 개수와 ID 집합이 그대로다."""
    from scripts import verify_idempotency

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = verify_idempotency.main([], embedder=ctx.overrides.get("embedder"))
    return code == 0, buffer.getvalue()


def item_03(ctx: Context) -> tuple[bool, str]:
    """F2003·F2004: 프롬프트 1 검색어와 프롬프트 2 선택·탈락 결과 (질문 시점 수집 기록)."""
    log = ctx.paths["processed_dir"] / "query_collection_log.jsonl"
    rows = [r for r in (read_jsonl(log) if log.exists() else []) if r.get("candidates")]
    if not rows:
        return False, "질문 시점 수집 기록이 없습니다. 04 항목을 먼저 실행하거나 근거가 부족한 질문을 해 주세요."
    row = rows[-1]
    selection = row.get("selection") or {}
    lines = [f"질문: {row.get('question')}", f"검색 저장소: {row.get('repos')}", f"검색어(프롬프트 1): {row.get('queries')}",
             f"후보 {len(row['candidates'])}건 (프롬프트 2 입력)", ""]
    reasons = selection.get("reasons") or {}
    titles = {c["issue_id"]: c.get("title", "") for c in row["candidates"]}
    lines.append(f"== 선택 ({selection.get('source')})")
    lines += [f"  + {i} {titles.get(i, '')} — {reasons.get(i, '')}" for i in selection.get("selected_ids", [])]
    lines.append("== 탈락")
    lines += [f"  - {i} {titles.get(i, '')} — {reasons.get(i, '')}" for i in selection.get("rejected_ids", [])]
    lines.append(f"\n새로 색인한 이슈: {row.get('collected')} (청크 {row.get('indexed_chunks')}개)")
    return bool(selection), "\n".join(lines)


def item_04(ctx: Context, question: str) -> tuple[bool, str]:
    """F2004·NF2002·NF2001: 같은 질문 두 번, 두 번째는 GitHub 검색 0회. 구간별 처리 시간 포함 (20번과 공유)."""
    lines, runs = [], []
    for label in ("첫 실행", "두 번째 실행"):
        state, seconds = ctx.ask(question)
        collection = state.get("collection") or {}
        runs.append(collection)
        lines += [f"== {label} ({seconds:.1f}s)",
                  f"  GitHub 검색: {[(s['repo'], s['query'], s['results']) for s in collection.get('searched', [])] or '없음'}",
                  f"  캐시 적중: {[h['query'] for h in collection.get('cache_hits', [])] or '없음'}",
                  "  처리 시간: " + " / ".join(f"{k} {v:.1f}s" for k, v in (state.get("timings") or {}).items()), ""]
    second = runs[1]
    passed = bool(second) and not second.get("searched") and bool(second.get("cache_hits"))
    lines.append(f"두 번째 실행에서 외부 검색 0회, 캐시 적중: {passed}")
    if not runs[0].get("searched"):
        lines.append("참고: 첫 실행도 캐시를 썼습니다. 이전에 같은 질문을 한 적이 있으면 --cache-question으로 새 질문을 주세요.")
    return passed, "\n".join(lines)


def item_05(ctx: Context) -> tuple[bool, str]:
    """F2005: 청크 설정을 바꾸면 청크 수가 달라진다 (색인은 바꾸지 않고 계산만 한다)."""
    path = ctx.paths["processed_dir"] / "issue_documents.jsonl"
    if not path.exists():
        return False, f"{path}가 없습니다. 먼저 build_indexes.py를 실행하세요."
    documents = read_jsonl(path)
    chunking = load_yaml(ctx.root / "configs/retrieval.yaml").get("chunking", {})
    smaller = {k: {"chunk_size": v["chunk_size"] // 2, "chunk_overlap": v["chunk_overlap"] // 2} for k, v in chunking.items()}
    base, half = len(document_records(documents, chunking)), len(document_records(documents, smaller))
    lines = [f"문서 수: {len(documents)}", "", "== 현재 설정"]
    lines += [f"  {k}: {v['chunk_size']}/{v['chunk_overlap']}" for k, v in chunking.items()]
    lines += [f"  청크 수: {base}", "", "== 청크 크기·겹침 절반"]
    lines += [f"  {k}: {v['chunk_size']}/{v['chunk_overlap']}" for k, v in smaller.items()]
    lines += [f"  청크 수: {half}", "", f"청크 수 변화: {base} -> {half}"]
    return half != base, "\n".join(lines)


def item_06(ctx: Context, question: str) -> tuple[bool, str]:
    """F2006: Top-k를 바꾸면 근거 목록이 달라진다."""
    retriever = ctx.assistant.retriever
    original = copy.deepcopy(retriever.config)
    lines, lists = [], []
    try:
        for issue_k, evidence_k in ((8, 8), (3, 4)):
            retriever.config["issue"]["rerank_top_k"] = issue_k
            retriever.config["document"]["final_evidence_limit"] = evidence_k
            result = retriever.retrieve(question)
            ids = [e.id for e in result.evidence]
            lists.append(ids)
            lines += [f"== 이슈 Top-k {issue_k} / 근거 상한 {evidence_k}: 근거 {len(ids)}건"]
            lines += [f"  [근거 {e.number}] {source_label(e)} · {e.issue_id}" for e in result.evidence]
            lines.append("")
    finally:
        retriever.config.clear()
        retriever.config.update(original)
    lines.append(f"질문: {question}")
    return lists[0] != lists[1], "\n".join(lines)


def item_07(ctx: Context) -> tuple[bool, str]:
    """F2007: LangGraph에 retrieve, generate 두 노드."""
    diagram = graph_diagram(ctx.assistant.graph)
    return "retrieve" in diagram and "generate" in diagram, diagram


def item_08(ctx: Context, question: str) -> tuple[bool, str]:
    """F2008: 답변의 근거 번호가 실제 근거 목록과 일치하고 출처 유형이 구분된다."""
    state, _ = ctx.ask(question)
    numbers = sorted({int(n) for n in CITATION.findall(state.get("answer", ""))})
    cited = state.get("cited_evidence") or []
    listed = sorted(e.number for e in cited)
    passed = bool(numbers) and numbers == listed and not (state.get("citation_check") or {}).get("invalid")
    lines = [f"질문: {question}", f"답변에 나온 근거 번호: {numbers}", f"화면에 표시할 근거 번호: {listed}",
             f"일치: {passed}", "", "== 근거 목록 (출처 유형)"]
    lines += [f"[근거 {e.number}] {source_label(e)} · {e.issue_id} · {e.source_url}\n    {snippet(e.text)}" for e in cited]
    lines += ["", "== 답변", state.get("answer", "")]
    return passed, "\n".join(lines)


def item_09(ctx: Context, question: str) -> tuple[bool, str]:
    """F2007·F2008: 색인에 없는 질문은 답하지 않는다."""
    state, _ = ctx.ask(question, allow_general_answer=False)
    passed = state.get("answer") == NO_EVIDENCE_MESSAGE and not state.get("cited_evidence")
    return passed, format_state(state, ctx.app_config.get("safety_notice", ""))


def item_21(ctx: Context) -> tuple[bool, str]:
    """NF2003: 커밋 대상 파일에 키 문자열이 없고 .env가 제외된다."""
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    hits, fixtures = [], []
    for name in files:
        path = ROOT / name
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for label, pattern in SECRET_PATTERNS.items():
            # tests/의 값은 마스킹이 동작하는지 확인하려고 넣은 가짜 키다. 따로 세고 실패로 보지 않는다
            (fixtures if name.startswith("tests/") else hits).extend(f"{name}: {label}" for _ in pattern.finditer(text))
    ignored = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=ROOT).returncode == 0
    tracked_env = ".env" in files
    lines = [f"검사 파일 수: {len(files)}", f"검사 패턴: {', '.join(SECRET_PATTERNS)}",
             f"키 문자열 발견: {len(hits)}건", *[f"  {h}" for h in hits],
             f"테스트용 가짜 값 (마스킹 테스트 입력, 제외): {len(fixtures)}건", *[f"  {h}" for h in fixtures],
             f".env가 .gitignore 대상: {ignored}", f".env가 커밋돼 있음: {tracked_env}"]
    return not hits and ignored and not tracked_env, "\n".join(lines)


def item_22(ctx: Context) -> tuple[bool, str]:
    """NF2005: 수집, 정제, 색인, 검색, 프롬프트, 그래프, 서비스, 화면 모듈 분리."""
    lines = []
    for folder in sorted((ROOT / "src").iterdir()):
        if not folder.is_dir() or folder.name.startswith("__"):
            continue
        lines.append(f"src/{folder.name}/")
        for file in sorted(folder.glob("*.py")):
            if file.name == "__init__.py":
                continue
            first = file.read_text(encoding="utf-8").split('"""')
            doc = first[1].strip().splitlines()[0] if len(first) > 2 else ""
            lines.append(f"  {file.name:28} {doc}")
    return True, "\n".join(lines)


def item_23(ctx: Context) -> tuple[bool, str]:
    """NF2006: 재시도, 호출 간격, User-Agent, Rate Limit 처리."""
    source = (ROOT / "src/collectors/github_client.py").read_text(encoding="utf-8").splitlines()
    keys = ("User-Agent", "X-GitHub-Api-Version", "Retry-After", "X-RateLimit-Remaining", "X-RateLimit-Reset",
            "request_interval_seconds", "search_interval_seconds", "max_retries", "backoff_factor", "If-None-Match", "304")
    lines = ["== src/collectors/github_client.py"]
    lines += [f"{n:4}: {line.strip()}" for n, line in enumerate(source, start=1) if any(k in line for k in keys)]
    lines += ["", "== configs/app.yaml http", json.dumps(ctx.app_config.get("http", {}), ensure_ascii=False, indent=2)]
    return True, "\n".join(lines)


def item_24(ctx: Context) -> tuple[bool, str]:
    """NF2007: README의 데이터 출처와 이용 조건, 저장소별 라이선스."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    match = re.search(r"^## 데이터 출처와 이용 조건.*?(?=^## |\Z)", readme, re.M | re.S)
    manifest = json.loads((ctx.paths["manifest_dir"] / "source_manifest.json").read_text(encoding="utf-8"))
    licenses = [f"  {repo}: {v['license']['spdx_id']} ({v['license']['url']})" for repo, v in manifest["repositories"].items()]
    body = match.group(0) if match else "README에 '## 데이터 출처와 이용 조건' 절이 없습니다."
    return bool(match), body + "\n\n== source_manifest.json 라이선스\n" + "\n".join(licenses)


ITEMS = {
    "01": ("01_f2001_count_and_text", item_01),
    "02": ("02_f2002_idempotent", item_02),
    "03": ("03_f2003_f2004_selection", item_03),
    "04": ("04_cache_hit_and_latency", None),
    "05": ("05_chunk_count_compare", item_05),
    "06": ("06_topk_compare", None),
    "07": ("07_langgraph_structure", item_07),
    "08": ("08_citation_match", None),
    "09": ("09_abstention", None),
    "21": ("21_security_check", item_21),
    "22": ("22_module_structure", item_22),
    "23": ("23_api_etiquette", item_23),
    "24": ("24_sources_and_terms", item_24),
}


def main(argv: list[str] | None = None, **overrides) -> int:
    parser = argparse.ArgumentParser(description="요구사항 4장 증빙 출력")
    parser.add_argument("--only", nargs="*", help="항목 번호 (예: 01 05)")
    parser.add_argument("--skip-rebuild", action="store_true", help="02 재색인 멱등성 생략")
    parser.add_argument("--question", default=GROUNDED_QUESTION, help="06·08에 쓸 근거 있는 질문")
    parser.add_argument("--abstain-question", default=ABSTAIN_QUESTION, help="09에 쓸 근거 없는 질문")
    parser.add_argument("--cache-question", default=CACHE_QUESTION, help="04에 쓸, 질문 시점 수집까지 가는 질문")
    parser.add_argument("--out", default=OUT_DIR)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    root = overrides.pop("root", ROOT)
    load_dotenv(root / ".env")
    ctx = Context(root, overrides)

    runners = {"04": lambda: item_04(ctx, args.cache_question), "06": lambda: item_06(ctx, args.question),
               "08": lambda: item_08(ctx, args.question), "09": lambda: item_09(ctx, args.abstain_question)}
    selected = [k for k in ITEMS if (not args.only or k in args.only) and not (k == "02" and args.skip_rebuild)]
    out = Path(args.out) if Path(args.out).is_absolute() else root / args.out
    out.mkdir(parents=True, exist_ok=True)
    summary = []
    for key in selected:
        name, func = ITEMS[key]
        print(f"({key}) {name}", file=sys.stderr, flush=True)
        try:
            passed, text = runners[key]() if func is None else func(ctx)
        except Exception as exc:  # noqa: BLE001  한 항목 실패가 나머지 증빙을 막지 않게 한다
            passed, text = False, f"실행 실패: {type(exc).__name__}: {exc}"
        (out / f"{name}.txt").write_text(text + "\n", encoding="utf-8")
        summary.append(f"{'PASS' if passed else '확인 필요'}  {name}.txt")
    print("\n".join(summary))
    print(f"\n출력 폴더: {out}\n화면 캡처 항목(10~13)과 나머지는 docs/evidence_checklist.md를 따른다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
