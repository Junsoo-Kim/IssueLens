"""증빙 화면 캡처 자동화 (docs/evidence_checklist.md 10~13번). 로컬 색인과 API 키로 실제 화면을 띄워 찍는다.

    pip install playwright && python -m playwright install chromium     # 처음 한 번
    python scripts/capture_ui.py                    # 10~13 전부
    python scripts/capture_ui.py --only 10 --only 13

Streamlit을 직접 띄우고(포트 8599) 브라우저로 체크리스트의 조작을 그대로 수행한다.
- 13번의 "키 없음" 화면은 OPENAI_API_KEY를 비운 별도 실행으로, "색인 없음" 화면은 빈 임시 루트로 띄워 찍는다.
  .env나 chroma_db를 건드리지 않는다
- 결과: data/processed/evidence/*.png (답변에 이슈 원문 일부가 보여 커밋하지 않는다)
"""
from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "data/processed/evidence"
PORT = 8599
ANSWER_TIMEOUT_MS = 240_000  # 질문 시점 수집까지 가면 1~2분 걸린다
DEFAULTS = {
    "issue_a": "airbytehq/airbyte#9784",
    "question_a": "이 문제를 해결하려면 어떤 설정을 확인해야 해?",
    "issue_b": "datahub-project/datahub#13067",
    "question_b": "이 이슈의 원인은 뭐였어?",
    "explore": "Airbyte MySQL 대용량 테이블 동기화가 타임아웃으로 실패한 버그 찾아줘",
    "log_repo": "conductor-oss/conductor",
    "no_result": "사내 커피 머신의 추출 온도 설정은 어떻게 바꿔?",
}


# ---------------------------------------------------------------- Streamlit 실행


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


@contextmanager
def streamlit(root: Path | None = None, env: dict | None = None, port: int = PORT):
    """Streamlit을 띄우고 주소를 돌려준다. 끝나면 프로세스를 끝낸다."""
    if _port_open(port):
        raise SystemExit(f"포트 {port}가 이미 쓰이고 있습니다. 실행 중인 Streamlit을 끄고 다시 시도하세요.")
    run_env = {**os.environ, **(env or {}), "PYTHONUTF8": "1"}
    if root:
        run_env["ISSUELENS_ROOT"] = str(root)
    process = subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", str(ROOT / "app.py"), "--server.port", str(port),
         "--server.headless", "true", "--browser.gatherUsageStats", "false"],
        cwd=ROOT, env=run_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(120):
            if _port_open(port):
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("Streamlit이 시작되지 않았습니다")
        yield f"http://127.0.0.1:{port}"
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        for _ in range(20):
            if not _port_open(port):
                break
            time.sleep(0.5)


# ---------------------------------------------------------------- 화면 조작


def open_app(page, url: str) -> None:
    page.goto(url)
    page.get_by_text("IssueLens", exact=True).first.wait_for(timeout=120_000)
    wait_idle(page)


def wait_idle(page, timeout: int = ANSWER_TIMEOUT_MS) -> None:
    """Streamlit 스크립트 실행이 끝날 때까지 기다린다 (오른쪽 위 실행 표시가 사라짐)."""
    page.wait_for_timeout(800)
    page.wait_for_function(
        "() => !document.querySelector('[data-testid=\"stStatusWidget\"]')", timeout=timeout)
    page.wait_for_timeout(500)


def select_issue(page, issue_id: str) -> None:
    box = page.locator('[data-testid="stSidebar"] [data-testid="stSelectbox"]').nth(1)
    box.click()
    page.keyboard.type(f"#{issue_id.rsplit('#', 1)[1]} ")
    page.wait_for_timeout(500)
    page.keyboard.press("Enter")
    wait_idle(page)


def select_repo(page, repo: str) -> None:
    box = page.locator('[data-testid="stSidebar"] [data-testid="stSelectbox"]').nth(0)
    box.click()
    page.keyboard.type(repo)
    page.wait_for_timeout(500)
    page.keyboard.press("Enter")
    wait_idle(page)


def ask(page, question: str) -> None:
    page.get_by_placeholder("장애 증상, 오류 메시지, 궁금한 점을 입력하세요").fill(question)
    page.keyboard.press("Enter")
    wait_idle(page)


def open_tab(page, name: str) -> None:
    page.get_by_role("tab", name=name).click()
    page.wait_for_timeout(500)


def expand_status_boxes(page) -> None:
    """처리 단계가 적힌 상태 상자(처리 완료)를 펼친다."""
    for summary in page.locator('[data-testid="stExpander"] summary').filter(has_text="처리 완료").all():
        summary.click()
        page.wait_for_timeout(300)


def shot(page, name: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    page.screenshot(path=str(path), full_page=True)
    print(f"  저장: {path.relative_to(ROOT)}", flush=True)
    return path


# ---------------------------------------------------------------- 체크리스트 항목


def capture_10(page, url: str, o: dict) -> None:
    """F2009 이슈별 대화 분리: A에서 질문 -> B는 비어 있음 -> B에서 질문 -> A로 돌아오면 A 대화가 남아 있음."""
    open_app(page, url)
    select_issue(page, o["issue_a"])
    ask(page, o["question_a"])
    shot(page, "10_session_isolation_1_issue_a.png")
    select_issue(page, o["issue_b"])
    shot(page, "10_session_isolation_2_issue_b_empty.png")
    ask(page, o["question_b"])
    select_issue(page, o["issue_a"])
    shot(page, "10_session_isolation_3_back_to_a.png")


def capture_11(page, url: str, o: dict) -> None:
    """F2011 조건 탐색."""
    open_app(page, url)
    open_tab(page, "조건 탐색")
    page.get_by_label("찾을 이슈 조건").fill(o["explore"])
    page.get_by_role("button", name="탐색").click()
    wait_idle(page)
    shot(page, "11_condition_search.png")


def capture_12(page, url: str, o: dict) -> None:
    """F2010 로그 진단: 평가용 이슈(색인 제외)의 실제 로그를 넣는다."""
    from scripts.sample_logs import evaluation_samples
    from src.config import load_yaml
    from src.services.assistant import resolve_paths

    samples = evaluation_samples(resolve_paths(ROOT, load_yaml(ROOT / "configs/app.yaml")), o["log_repo"], 1)
    if not samples:
        print(f"  건너뜀: {o['log_repo']}의 평가용 이슈에서 로그를 찾지 못했습니다", flush=True)
        return
    print(f"  로그 출처(진단 후 비교용): {samples[0]['url']}", flush=True)
    open_app(page, url)
    open_tab(page, "로그 진단")
    page.get_by_label("오류 로그").fill(samples[0]["log"])
    page.get_by_role("button", name="진단").click()
    wait_idle(page)
    shot(page, "12_incident_diagnosis.png")


def capture_13(page, url: str, o: dict) -> None:
    """F2012 처리 단계와 검색 결과 없음 안내. 키 없음·색인 없음은 main에서 따로 띄워 찍는다."""
    open_app(page, url)
    ask(page, o["no_result"])
    expand_status_boxes(page)
    shot(page, "13_status_1_progress_and_no_result.png")


def empty_root() -> Path:
    """색인이 없는 루트: configs만 복사하고 경로를 빈 임시 폴더로 바꾼다."""
    import yaml

    root = Path(tempfile.mkdtemp(prefix="issuelens_noindex_"))
    shutil.copytree(ROOT / "configs", root / "configs")
    app_path = root / "configs/app.yaml"
    app = yaml.safe_load(app_path.read_text(encoding="utf-8"))
    app["paths"] = {k: str(root / "data" / k) for k in (app.get("paths") or {})}
    app_path.write_text(yaml.safe_dump(app, allow_unicode=True), encoding="utf-8")
    return root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="증빙 화면 캡처 (체크리스트 10~13)")
    parser.add_argument("--only", action="append", choices=["10", "11", "12", "13"])
    parser.add_argument("--headed", action="store_true", help="브라우저 창을 띄워 진행을 본다")
    parser.add_argument("--root", type=Path, help="다른 프로젝트 루트의 색인으로 띄운다 (스크립트 시험용)")
    for key, value in DEFAULTS.items():
        parser.add_argument(f"--{key.replace('_', '-')}", default=value)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright가 없습니다: pip install playwright && python -m playwright install chromium", file=sys.stderr)
        return 2
    options = {k: getattr(args, k) for k in DEFAULTS}
    items = args.only or ["10", "11", "12", "13"]

    with sync_playwright() as p:
        launch = {"headless": not args.headed}
        executable = os.environ.get("ISSUELENS_CHROMIUM")
        if executable:
            launch["executable_path"] = executable
        browser = p.chromium.launch(**launch)
        page = browser.new_page(viewport={"width": 1440, "height": 2400}, locale="ko-KR")
        page.set_default_timeout(ANSWER_TIMEOUT_MS)
        with streamlit(root=args.root) as url:
            for item in [i for i in items if i != "13"]:
                print(f"== {item}", flush=True)
                {"10": capture_10, "11": capture_11, "12": capture_12}[item](page, url, options)
            if "13" in items:
                print("== 13 (1/3 처리 단계, 검색 결과 없음)", flush=True)
                capture_13(page, url, options)
        if "13" in items:
            print("== 13 (2/3 API 키 없음)", flush=True)
            with streamlit(root=args.root, env={"OPENAI_API_KEY": ""}) as url:
                open_app(page, url)
                shot(page, "13_status_2_no_api_key.png")
            print("== 13 (3/3 색인 없음)", flush=True)
            root = empty_root()
            try:
                with streamlit(root=root) as url:
                    page.goto(url)
                    page.get_by_text("검색 색인이 없습니다").first.wait_for(timeout=120_000)
                    shot(page, "13_status_3_no_index.png")
            finally:
                shutil.rmtree(root, ignore_errors=True)
        browser.close()
    print(f"\n완료: {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
