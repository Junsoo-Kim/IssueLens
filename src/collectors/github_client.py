"""GitHub REST API 공통 클라이언트: 인증 헤더, Timeout, Rate Limit, 재시도, 페이지네이션."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterator

import requests

from src.collectors.cache import ResponseCache, utc_now

API_BASE_URL = "https://api.github.com"
DEFAULT_API_VERSION = "2026-03-10"
DEFAULT_USER_AGENT = "incident-rag-project/1.0"

RETRYABLE_SERVER_STATUS = {500, 502, 503, 504}
SECONDARY_RATE_LIMIT_MIN_WAIT_SECONDS = 60
# 사용자 객체는 login과 type만 남긴다 (avatar, 이메일, 프로필 URL 제외)
USER_KEEP_FIELDS = ("login", "type")


class GitHubAPIError(Exception):
    """재시도하지 않을 오류이거나 재시도 한도를 넘긴 오류.

    `retryable`이 True이면 일시적인 오류이므로 나중에 --resume으로 재개할 수 있다.
    """

    def __init__(self, status: int | None, url: str, reason: str, retryable: bool = False):
        super().__init__(f"{status} {url}: {reason}")
        self.status = status
        self.url = url
        self.reason = reason
        self.retryable = retryable


@dataclass
class HttpSettings:
    connect_timeout_seconds: float = 5
    read_timeout_seconds: float = 20
    max_retries: int = 5
    backoff_factor: float = 2
    request_interval_seconds: float = 0.5
    search_interval_seconds: float = 2.0

    @classmethod
    def from_dict(cls, values: dict | None) -> "HttpSettings":
        known = cls.__dataclass_fields__
        return cls(**{k: v for k, v in (values or {}).items() if k in known})


@dataclass
class ApiResponse:
    status: int
    data: Any
    url: str
    next_url: str | None = None
    from_cache: bool = False


@dataclass
class ClientStats:
    requests: int = 0
    cache_hits: int = 0
    retries: int = 0
    rate_limit_remaining: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def strip_personal_fields(value: Any) -> Any:
    """응답에서 사용자 객체를 login/type으로 줄이고 email 필드를 제거한다."""
    if isinstance(value, list):
        return [strip_personal_fields(item) for item in value]
    if isinstance(value, dict):
        if "login" in value and "avatar_url" in value:
            return {k: value[k] for k in USER_KEEP_FIELDS if k in value}
        return {k: strip_personal_fields(v) for k, v in value.items() if k != "email"}
    return value


def _error_message(response: requests.Response) -> str:
    try:
        return str(response.json().get("message", ""))
    except ValueError:
        return response.text[:200]


class GitHubClient:
    """순차 호출, 호출 간격, 조건부 요청, Rate Limit 대기를 처리하는 GET 전용 클라이언트."""

    def __init__(
        self,
        token: str | None = None,
        api_version: str = DEFAULT_API_VERSION,
        user_agent: str = DEFAULT_USER_AGENT,
        settings: HttpSettings | None = None,
        cache: ResponseCache | None = None,
        refresh: bool = False,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        base_url: str = API_BASE_URL,
    ):
        self.settings = settings or HttpSettings()
        self.cache = cache
        self.refresh = refresh
        self.session = session or requests.Session()
        self.sleep = sleep
        self.monotonic = monotonic
        self.wall_clock = wall_clock
        self.base_url = base_url.rstrip("/")
        self.stats = ClientStats()
        self._last_request_at: float | None = None
        self.headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": api_version,
            "User-Agent": user_agent,
        }
        # 토큰이 없으면 헤더를 넣지 않는다 (프록시가 인증 헤더를 주입하는 환경 포함)
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def get(self, path: str, params: dict | None = None, use_cache: bool = True) -> ApiResponse:
        url = self._url(path)
        key = ResponseCache.request_key("GET", url, params) if self.cache and use_cache else None
        cached = self.cache.get(key) if key else None

        headers = dict(self.headers)
        if cached and not self.refresh:
            if cached.get("etag"):
                headers["If-None-Match"] = cached["etag"]
            if cached.get("last_modified"):
                headers["If-Modified-Since"] = cached["last_modified"]

        max_retries = self.settings.max_retries
        for attempt in range(max_retries + 1):
            self._throttle(url)
            try:
                response = self.session.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=(self.settings.connect_timeout_seconds, self.settings.read_timeout_seconds),
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt >= max_retries:
                    raise GitHubAPIError(None, url, f"network error: {exc}", retryable=True) from exc
                self.stats.retries += 1
                self.sleep(self._backoff_seconds(attempt))
                continue

            self.stats.requests += 1
            self._record_rate_limit(response)

            if response.status_code == 304 and cached:
                self.stats.cache_hits += 1
                return ApiResponse(
                    status=cached["status_code"],
                    data=cached["body"],
                    url=url,
                    next_url=cached.get("next_url"),
                    from_cache=True,
                )

            if 200 <= response.status_code < 300:
                data = strip_personal_fields(response.json()) if response.content else None
                next_url = response.links.get("next", {}).get("url")
                if key:
                    self.cache.put(key, {
                        "url": url,
                        "params": params or {},
                        "etag": response.headers.get("ETag"),
                        "last_modified": response.headers.get("Last-Modified"),
                        "status_code": response.status_code,
                        "fetched_at": utc_now(),
                        "next_url": next_url,
                        "body": data,
                    })
                return ApiResponse(status=response.status_code, data=data, url=url, next_url=next_url)

            wait = self._retry_wait(response, attempt)
            if wait is None or attempt >= max_retries:
                raise GitHubAPIError(
                    response.status_code, url, _error_message(response), retryable=wait is not None
                )
            self.stats.retries += 1
            self.sleep(wait)

        raise AssertionError("unreachable")

    def paginate(self, path: str, params: dict | None = None, max_pages: int | None = None) -> Iterator[ApiResponse]:
        """Link 헤더의 next를 따라가며 페이지를 순서대로 반환한다."""
        page = self.get(path, params)
        yield page
        fetched = 1
        while page.next_url and (max_pages is None or fetched < max_pages):
            page = self.get(page.next_url)
            fetched += 1
            yield page

    def rate_limit(self) -> dict:
        """/rate_limit 조회. 이 호출은 Rate Limit을 소모하지 않는다."""
        return self.get("/rate_limit", use_cache=False).data

    def _url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            # Link 헤더 등 외부에서 받은 URL에 인증 헤더를 실어 보내지 않도록 API 호스트로 제한한다
            if not path.startswith(self.base_url + "/"):
                raise ValueError(f"API 호스트가 아닌 URL은 호출하지 않습니다: {path}")
            return path
        return self.base_url + path

    def _throttle(self, url: str) -> None:
        interval = self.settings.request_interval_seconds
        if url.startswith(self.base_url + "/search/"):
            interval = max(interval, self.settings.search_interval_seconds)
        if self._last_request_at is not None:
            elapsed = self.monotonic() - self._last_request_at
            if elapsed < interval:
                self.sleep(interval - elapsed)
        self._last_request_at = self.monotonic()

    def _backoff_seconds(self, attempt: int) -> float:
        return self.settings.backoff_factor ** attempt

    def _retry_wait(self, response: requests.Response, attempt: int) -> float | None:
        """재시도 전 대기 시간. None이면 재시도하지 않는다."""
        status = response.status_code
        headers = response.headers
        if status in (403, 429):
            retry_after = headers.get("Retry-After")
            if retry_after is not None:
                return float(retry_after)
            if headers.get("X-RateLimit-Remaining") == "0":
                reset_at = float(headers.get("X-RateLimit-Reset", 0))
                return max(reset_at - self.wall_clock(), 0) + 1
            if "secondary rate limit" in _error_message(response).lower():
                return SECONDARY_RATE_LIMIT_MIN_WAIT_SECONDS * self.settings.backoff_factor ** attempt
            if status == 429:
                return self._backoff_seconds(attempt)
            return None  # 권한 오류 403
        if status in RETRYABLE_SERVER_STATUS:
            return self._backoff_seconds(attempt)
        return None

    def _record_rate_limit(self, response: requests.Response) -> None:
        remaining = response.headers.get("X-RateLimit-Remaining")
        if remaining is not None:
            resource = response.headers.get("X-RateLimit-Resource", "core")
            self.stats.rate_limit_remaining[resource] = int(remaining)
