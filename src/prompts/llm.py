"""채팅 LLM 호출과 JSON 응답 파싱. 프롬프트 모듈은 `LLM` 호출 형식만 알고 실제 클라이언트는 모른다.

    LLM = (system 프롬프트, user 프롬프트) -> 응답 텍스트

주소와 키는 .env의 OPENAI_BASE_URL, OPENAI_API_KEY를 따른다. 테스트에서는 가짜 LLM을 넘긴다.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Callable

LLM = Callable[[str, str], str]


class LLMUnavailable(RuntimeError):
    """API 키가 없거나 호출에 실패했다. 호출한 쪽에서 규칙 기반 대체 경로나 안내로 처리한다."""


class OpenAIChat:
    """처음 호출할 때 클라이언트를 만든다. GPT-5 계열은 temperature 기본값만 지원해 지정하지 않는다."""

    def __init__(self, model: str, reasoning_effort: str | None = None):
        self.model = model
        self.reasoning_effort = reasoning_effort
        self._client = None

    def _build(self):
        from langchain_openai import ChatOpenAI
        extra = {"reasoning_effort": self.reasoning_effort} if self.reasoning_effort else {}
        return ChatOpenAI(model=self.model, base_url=os.getenv("OPENAI_BASE_URL") or None, **extra)

    def __call__(self, system: str, user: str) -> str:
        if self._client is None:
            if not os.getenv("OPENAI_API_KEY"):
                raise LLMUnavailable("OPENAI_API_KEY가 설정되지 않았습니다.")
            self._client = self._build()
        messages = [("system", system), ("human", user)]
        try:
            response = self._client.invoke(messages)
        except Exception as exc:  # 네트워크·인증·한도 오류를 한 가지 예외로 바꿔 상위에서 안내한다
            if not (self.reasoning_effort and "reasoning_effort" in str(exc)):
                raise LLMUnavailable(f"LLM 호출 실패: {type(exc).__name__}") from exc
            # 주소가 reasoning_effort를 거부하면 빼고 다시 호출한다
            print(f"주의: reasoning_effort={self.reasoning_effort}를 지원하지 않아 기본값으로 호출합니다.", file=sys.stderr)
            self.reasoning_effort = None
            self._client = self._build()
            try:
                response = self._client.invoke(messages)
            except Exception as retry_exc:
                raise LLMUnavailable(f"LLM 호출 실패: {type(retry_exc).__name__}") from retry_exc
        return response.content if isinstance(response.content, str) else str(response.content)


def parse_json_object(text: str) -> dict | None:
    """응답에서 첫 JSON 객체를 찾는다. 코드 블록이나 앞뒤 설명이 섞여 있어도 된다. 없으면 None."""
    decoder = json.JSONDecoder()
    for start in (i for i, ch in enumerate(text or "") if ch == "{"):
        try:
            value, _ = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None
