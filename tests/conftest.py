"""테스트 공용 픽스처: 환경 변수 격리, 가짜 HTTP 세션과 시계. 실제 GitHub·LLM·LangSmith API는 호출하지 않는다."""
from __future__ import annotations

import json
import os

import pytest
from requests.structures import CaseInsensitiveDict

from src.collectors.cache import ResponseCache
from src.collectors.github_client import API_BASE_URL, GitHubClient, HttpSettings


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    """CLI main()이 실제 .env를 읽어 os.environ을 바꿔도 다음 테스트로 새지 않게 되돌린다.

    LangSmith 추적은 항상 끈다. load_dotenv는 이미 있는 변수를 덮어쓰지 않으므로 .env의
    LANGSMITH_TRACING=true가 테스트 중에 적용되지 않는다.
    """
    saved = dict(os.environ)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    yield
    os.environ.clear()
    os.environ.update(saved)


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, headers=None, next_url=None):
        self.status_code = status_code
        self._json = json_data
        self.headers = CaseInsensitiveDict(headers or {})
        self.links = {"next": {"url": next_url}} if next_url else {}
        self.content = b"" if json_data is None else json.dumps(json_data).encode()
        self.text = self.content.decode()

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeSession:
    """URL(경로 또는 전체 URL)별로 준비한 응답을 순서대로 돌려준다. 마지막 응답은 반복된다."""

    def __init__(self, routes: dict):
        self.routes = {self._full(url): list(responses) for url, responses in routes.items()}
        self.calls: list[dict] = []

    @staticmethod
    def _full(url: str) -> str:
        return url if url.startswith("http") else API_BASE_URL + url

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        if url not in self.routes:
            raise AssertionError(f"예상하지 않은 요청: {url}")
        queue = self.routes[url]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    def paths(self) -> list[str]:
        return [call["url"].removeprefix(API_BASE_URL) for call in self.calls]


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0):
        self.now = start
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def time(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def make_client(clock, tmp_path):
    """호출 간격 0, 재시도 3회인 테스트용 클라이언트를 만든다."""

    def factory(routes, token="test-token", cache=True, refresh=False, **settings):
        session = FakeSession(routes)
        http = HttpSettings(max_retries=3, request_interval_seconds=0, search_interval_seconds=0)
        for name, value in settings.items():
            setattr(http, name, value)
        client = GitHubClient(
            token=token,
            settings=http,
            cache=ResponseCache(tmp_path / "cache") if cache else None,
            refresh=refresh,
            session=session,
            sleep=clock.sleep,
            monotonic=clock.time,
            wall_clock=clock.time,
        )
        return client, session

    return factory
