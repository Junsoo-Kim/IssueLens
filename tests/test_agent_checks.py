"""에이전트 단계 G: 검증 PR 세트 (기획서 17.11절). 결정적인 사례는 가짜 LLM으로 확인한다.

근거 기반 지적이 실제로 나오는지(rag: expected)는 실제 코퍼스와 LLM이 필요해 scripts/run_agent_checks.py로 확인한다.
"""
from __future__ import annotations

import json

from agent.checks import FAKE_KEY, RecordingLLM, grade, load_cases, run_case, section_items
from agent.main import main as agent_main

CASES = {c["id"]: c for c in load_cases()}


class CaseLLM:
    """리뷰 후보만 정해 둔 가짜 LLM. app/storage.py에는 일반 지적 하나, 나머지는 지적 없음."""

    def __call__(self, system, user):
        if "PR을 검토하는 시니어 리뷰어" not in system:
            raise AssertionError("이 테스트는 근거 검색을 하지 않는다")
        if "app/storage.py" in user:
            return json.dumps({"findings": [{"file": "app/storage.py", "line": 7, "kind": "general", "severity": "medium",
                                             "summary": "파일을 닫지 않고 모든 예외를 삼킴", "concern": "설정 오류를 구분할 수 없음"}]})
        return json.dumps({"findings": []})


def run(case_id, tmp_path, llm=None):
    recorder = RecordingLLM(llm or CaseLLM())
    output, _ = run_case(CASES[case_id], agent_main, ["--no-pypi", "--skip-research", "--cache-dir", str(tmp_path),
                                                      "--index", str(tmp_path / "idx")],
                         env={}, llm=recorder, fast_llm=recorder)
    return grade(CASES[case_id], output, recorder.prompts), recorder


def test_cases_file_is_well_formed():
    from agent.checks import CASES_DIR
    assert len(CASES) == 9
    for case in CASES.values():
        assert (CASES_DIR / case["patch"]).exists() and case["expect"]
    assert FAKE_KEY not in (CASES_DIR / "secret_in_patch.patch").read_text(encoding="utf-8")


def test_logic_only_gives_general_review_without_rag(tmp_path):
    result, _ = run("logic_only", tmp_path)
    assert result.status == "PASS", result.problems
    assert result.observed["general"] == 1 and result.observed["rag"] == 0


def test_docs_only_and_non_python_make_no_comment(tmp_path):
    docs, _ = run("docs_only", tmp_path)
    assert docs.status == "PASS" and not docs.observed["comment"]
    non_python, recorder = run("non_python", tmp_path)
    assert non_python.status == "PASS" and not non_python.observed["reviewed"] and recorder.prompts == []


def test_secret_is_masked_in_llm_input_and_output(tmp_path):
    result, recorder = run("secret_in_patch", tmp_path)
    assert result.status == "PASS" and recorder.prompts and FAKE_KEY not in "".join(recorder.prompts)


def test_unknown_library_does_not_fail(tmp_path):
    result, _ = run("unknown_library", tmp_path, llm=lambda system, user: "형식이 깨진 응답")
    assert result.status == "PASS" and result.observed["review_source"] == "rule"


def test_grade_marks_missing_rag_as_warning_and_leak_as_failure():
    output = {"review": {"findings": [{"kind": "library_usage"}], "source": "llm"}, "research": [{"finding_id": "f1", "status": "no_evidence"}],
              "comment": "<!-- x -->\n### 일반 코드 리뷰 (RAG 미사용, 근거 없음)\n- a\n- b\n\n> c"}
    warned = grade(CASES["openai_legacy_call"], output, [])
    assert warned.status == "WARN" and warned.observed["general"] == 2
    leaked = grade(CASES["secret_in_patch"], {**output, "comment": f"key {FAKE_KEY}"}, [])
    assert leaked.status == "FAIL" and "출력" in leaked.problems[0]
    assert grade(CASES["docs_only"], output, []).status == "FAIL"  # 코멘트가 있으면 안 되는 사례
    assert section_items("### 알려진 이슈 기반 (RAG)\n- x\n", "### 일반") == []


def test_run_agent_checks_script_writes_report_without_comment_text(tmp_path, capsys):
    from scripts.run_agent_checks import main

    out = tmp_path / "agent_checks.json"
    llm = CaseLLM()
    code = main(["--case", "logic_only", "--case", "docs_only", "--index", str(tmp_path / "idx"), "--out", str(out)],
                llm=llm, fast_llm=llm)
    assert code == 0 and "PASS 2" in capsys.readouterr().out
    report = json.loads(out.read_text(encoding="utf-8"))
    assert [c["id"] for c in report["cases"]] == ["logic_only", "docs_only"] and "comment" not in report["cases"][0]
    assert (tmp_path / "idx" / "checks" / "logic_only.md").exists()
