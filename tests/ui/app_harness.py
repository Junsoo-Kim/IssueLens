"""AppTest용 진입점. 테스트가 정한 루트와 가짜 LLM·임베딩으로 실제 화면 코드를 띄운다."""
import os
from pathlib import Path

from src.services.assistant import build_assistant
from src.ui.main import run
from tests.test_indexing import FakeEmbedder
from tests.test_services import ExploreLLM

KC = "keycloak/keycloak"


def factory(root: Path):
    llm = None
    if os.environ.get("ISSUELENS_TEST_LLM") == "fake":
        llm = ExploreLLM({"repos": [KC], "issue_types": ["bug"], "versions": ["26"], "categories": [],
                          "symptom": "login fails token verification after upgrade"},
                         matches=[{"issue_id": f"{KC}#2", "reason": "26.0.1 업그레이드 후 토큰 검증 실패"}])
    return build_assistant(root, llm=llm, embedder=FakeEmbedder(), enable_collection=False)


run(Path(os.environ["ISSUELENS_TEST_ROOT"]), factory)
