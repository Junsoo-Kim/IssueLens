"""PR 선제 리뷰 에이전트 단계 A: 변경 파일 읽기, 제외 규칙, 상한, 마스킹, 진입점."""
from __future__ import annotations

import json

from agent.diff_reader import parse_patch, parse_unified_diff
from agent.main import load_settings, main, pr_from_event
from tests.conftest import FakeResponse

FAKE_KEY = "sk-" + "a1B2" * 8  # 마스킹 확인용 가짜 키 (문자열로 이어 붙여 파일에 키 모양이 남지 않게 함)

DIFF = f"""diff --git a/requirements.txt b/requirements.txt
index 1111111..2222222 100644
--- a/requirements.txt
+++ b/requirements.txt
@@ -1,3 +1,3 @@
 langchain==0.2.16
-openai==0.28.1
+openai==1.51.0
 pydantic==2.9.2
diff --git a/app/llm.py b/app/llm.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/app/llm.py
@@ -0,0 +1,5 @@
+import openai
+
+OPENAI_API_KEY = "{FAKE_KEY}"
+def ask(prompt):
+    return openai.ChatCompletion.create(model="gpt-4o", messages=[{{"role": "user", "content": prompt}}])
diff --git a/poetry.lock b/poetry.lock
index 4444444..5555555 100644
--- a/poetry.lock
+++ b/poetry.lock
@@ -10,2 +10,2 @@
-version = "0.28.1"
+version = "1.51.0"
diff --git a/assets/logo.png b/assets/logo.png
index 6666666..7777777 100644
Binary files a/assets/logo.png and b/assets/logo.png differ
"""

SETTINGS = {"max_files": 30, "max_changed_lines": 1500, "exclude_paths": []}


def test_parse_patch_tracks_line_numbers():
    hunks = parse_patch("@@ -10,3 +10,4 @@ def f():\n a = 1\n-b = 2\n+b = 3\n+c = 4\n d = 5")
    assert len(hunks) == 1 and hunks[0].header == "def f():"
    kinds = [(ln.kind, ln.old_no, ln.new_no) for ln in hunks[0].lines]
    assert kinds == [(" ", 10, 10), ("-", 11, None), ("+", None, 11), ("+", None, 12), (" ", 12, 13)]


def test_unified_diff_skips_lock_and_binary_and_masks_secrets():
    bundle = parse_unified_diff(DIFF, "local.patch", SETTINGS)
    reviewed = {f.path: f for f in bundle.reviewed}
    assert set(reviewed) == {"requirements.txt", "app/llm.py"}
    assert {f.path: f.skipped for f in bundle.skipped} == {"poetry.lock": "lock", "assets/logo.png": "binary"}

    requirements = reviewed["requirements.txt"]
    assert requirements.status == "modified" and (requirements.additions, requirements.deletions) == (1, 1)
    assert requirements.added_lines() == [(2, "openai==1.51.0")]
    assert requirements.removed_lines() == [(2, "openai==0.28.1")]

    llm = reviewed["app/llm.py"]
    assert llm.status == "added" and llm.additions == 5
    assert FAKE_KEY not in llm.patch and all(FAKE_KEY not in text for _, text in llm.added_lines())
    assert bundle.redactions.get("openai_key", 0) + bundle.redactions.get("key_value", 0) >= 1
    assert llm.added_lines()[-1][0] == 5 and "ChatCompletion" in llm.added_lines()[-1][1]


def test_limits_and_excluded_paths():
    limited = parse_unified_diff(DIFF, "local.patch", {**SETTINGS, "max_files": 1})
    assert [f.path for f in limited.reviewed] == ["requirements.txt"]
    assert limited.truncated and {f.path: f.skipped for f in limited.skipped}["app/llm.py"] == "limit"

    excluded = parse_unified_diff(DIFF, "local.patch", {**SETTINGS, "exclude_paths": ["app/**"]})
    assert {f.path: f.skipped for f in excluded.skipped}["app/llm.py"] == "excluded"

    generated = parse_unified_diff(DIFF.replace("app/llm.py", "build/llm.py"), "local.patch", SETTINGS)
    assert {f.path: f.skipped for f in generated.skipped}["build/llm.py"] == "generated"


def test_target_repo_config_overrides_defaults(tmp_path):
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github/issuelens.yml").write_text("max_files: 3\nmax_rag_findings: 2\nunknown: 1\n", encoding="utf-8")
    settings = load_settings(tmp_path)
    assert settings["diff"]["max_files"] == 3 and settings["review"]["max_rag_findings"] == 2
    assert "unknown" not in settings["diff"] and "unknown" not in settings["review"]


def test_main_reads_pr_from_action_event(tmp_path, make_client, monkeypatch, capsys):
    repo = "octo/app"
    routes = {
        f"/repos/{repo}/pulls/7": [FakeResponse(json_data={"number": 7, "head": {"sha": "abc1234def"}})],
        f"/repos/{repo}/pulls/7/files": [FakeResponse(json_data=[
            {"filename": "requirements.txt", "status": "modified", "additions": 1, "deletions": 1,
             "patch": "@@ -1,2 +1,2 @@\n langchain==0.2.16\n-openai==0.28.1\n+openai==1.51.0"},
            {"filename": "uv.lock", "status": "modified", "additions": 3, "deletions": 3, "patch": "@@ -1 +1 @@\n-a\n+b"},
        ])],
    }
    client, _ = make_client(routes)
    monkeypatch.setattr("agent.main.github_client", lambda env: client)
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 7}}), encoding="utf-8")
    env = {"GITHUB_EVENT_PATH": str(event), "GITHUB_REPOSITORY": repo}
    assert pr_from_event(env) == (repo, 7)

    assert main(["--dry-run", "--target-root", str(tmp_path)], env=env) == 0
    printed = capsys.readouterr().out
    assert "octo/app#7, abc1234" in printed and "리뷰 1개 / 건너뜀 1개" in printed
    assert "uv.lock (lock)" in printed


def test_main_reads_local_diff(tmp_path, capsys):
    patch = tmp_path / "changes.patch"
    patch.write_text(DIFF, encoding="utf-8")
    assert main(["--diff", str(patch), "--dry-run", "--json", "--target-root", str(tmp_path)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [f["path"] for f in data["diff"]["files"] if not f["skipped"]] == ["requirements.txt", "app/llm.py"]
    assert data["signals"]["python_project"]
    assert FAKE_KEY not in json.dumps(data)


# ---------------------------------------------------------------- 단계 B: 대상 판별과 변경 신호

from agent.signals import extract_signals, normalize_package, package_of_import  # noqa: E402

SIGNAL_DIFF = '''diff --git a/requirements.txt b/requirements.txt
--- a/requirements.txt
+++ b/requirements.txt
@@ -1,5 +1,6 @@
 # app deps
-openai==0.28.1
+openai==1.51.0
-langchain>=0.1.20,<0.2
+langchain>=0.3.0
 pydantic==2.9.2
-requests==2.32.3
+requests==2.31.0
+langchain_openai==0.2.1
-tiktoken==0.7.0
diff --git a/pyproject.toml b/pyproject.toml
--- a/pyproject.toml
+++ b/pyproject.toml
@@ -1,8 +1,9 @@
 [project]
 dependencies = [
-  "chromadb>=0.4.24",
+  "chromadb>=0.5.5",
   "fastapi",
 ]
 [tool.poetry.dependencies]
 python = "^3.11"
-transformers = "^4.40"
+transformers = { version = "^4.44", extras = ["torch"] }
diff --git a/app/chat.py b/app/chat.py
--- a/app/chat.py
+++ b/app/chat.py
@@ -1,6 +1,11 @@
 import os
 import openai
+import json
+from langchain.chat_models import ChatOpenAI
+from pydantic import BaseModel
+from .utils import helper
 
 def run(prompt):
-    return openai.Completion.create(prompt=prompt)
+    resp = openai.ChatCompletion.create(model=os.getenv("MODEL"), messages=[])  # 이전 방식
+    llm = ChatOpenAI(temperature=0)
+    return json.dumps(resp), helper(llm)
diff --git a/.env.example b/.env.example
--- a/.env.example
+++ b/.env.example
@@ -1,3 +1,3 @@
-OPENAI_MODEL=gpt-3.5-turbo
+OPENAI_MODEL=gpt-4o-mini
 OPENAI_BASE_URL=
-REQUEST_TIMEOUT=30
+REQUEST_TIMEOUT=5
'''


def test_dependency_changes_from_requirements_and_pyproject():
    signals = extract_signals(parse_unified_diff(SIGNAL_DIFF, "t", SETTINGS))
    deps = {d.package: d for d in signals.dependencies}
    assert (deps["openai"].old, deps["openai"].new, deps["openai"].kind, deps["openai"].major_change) == \
        ("==0.28.1", "==1.51.0", "upgraded", True)
    assert deps["openai"].file == "requirements.txt" and deps["openai"].line == 2
    assert deps["langchain"].kind == "upgraded" and deps["langchain"].major_change  # 0.1 -> 0.3 (0.x의 마이너는 경계)
    assert deps["requests"].kind == "downgraded" and not deps["requests"].major_change
    assert deps["langchain-openai"].kind == "added"  # 밑줄 이름도 PEP 503으로 정규화
    assert deps["tiktoken"].kind == "removed" and deps["tiktoken"].line is None
    assert "pydantic" not in deps  # 바뀌지 않은 줄
    assert deps["chromadb"].kind == "upgraded" and deps["chromadb"].file == "pyproject.toml"  # PEP 621 배열
    assert deps["transformers"].kind == "upgraded" and deps["transformers"].new == "^4.44"  # Poetry 표
    assert "python" not in deps


def test_python_imports_and_api_calls():
    signals = extract_signals(parse_unified_diff(SIGNAL_DIFF, "t", SETTINGS))
    imports = {i.module: i for i in signals.imports}
    assert set(imports) == {"langchain.chat_models", "pydantic"}  # 표준 라이브러리(json)와 상대 import 제외
    assert imports["langchain.chat_models"].names == ["ChatOpenAI"] and imports["langchain.chat_models"].package == "langchain"
    calls = {c.call: c for c in signals.api_calls}
    assert "openai.ChatCompletion.create" in calls and calls["openai.ChatCompletion.create"].line == 9  # 기존 import 별칭도 인식
    assert "langchain.chat_models.ChatOpenAI" in calls
    assert not any(c.startswith(("os.", "json.", "helper")) for c in calls)
    assert signals.packages()[:2] == ["openai", "langchain"]


def test_config_changes():
    signals = extract_signals(parse_unified_diff(SIGNAL_DIFF, "t", SETTINGS))
    configs = {(c.file, c.key): c for c in signals.configs}
    assert (configs[(".env.example", "OPENAI_MODEL")].old, configs[(".env.example", "OPENAI_MODEL")].new) == \
        ("gpt-3.5-turbo", "gpt-4o-mini")
    assert configs[(".env.example", "REQUEST_TIMEOUT")].new == "5"
    assert (".env.example", "OPENAI_BASE_URL") not in configs  # 바뀌지 않은 키


def test_non_python_repo_is_skipped(tmp_path):
    diff = "diff --git a/src/app.ts b/src/app.ts\n--- a/src/app.ts\n+++ b/src/app.ts\n@@ -1 +1 @@\n-a\n+b\n"
    signals = extract_signals(parse_unified_diff(diff, "t", SETTINGS), tmp_path)
    assert not signals.python_project and not signals.dependencies
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    assert extract_signals(parse_unified_diff(diff, "t", SETTINGS), tmp_path).python_project  # 루트에 의존성 파일


def test_package_names():
    assert normalize_package("LangChain_OpenAI") == "langchain-openai"
    assert package_of_import("yaml") == "pyyaml" and package_of_import("google.generativeai.types") == "google-generativeai"
    assert package_of_import("langchain_core.messages") == "langchain-core"
