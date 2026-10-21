"""임베딩 클라이언트. 색인과 질의 검색이 같은 설정을 쓴다.

임베딩 주소와 키는 채팅과 다를 수 있다 (예: 게이트웨이마다 모델별 주소가 다름).
    OPENAI_EMBEDDING_BASE_URL  없으면 OPENAI_BASE_URL
    OPENAI_EMBEDDING_API_KEY   없으면 OPENAI_API_KEY
"""
from __future__ import annotations

import os


def embedding_endpoint() -> tuple[str | None, str | None]:
    """(base_url, api_key). 값이 없으면 None이고, 그때는 OpenAI 라이브러리 기본값을 쓴다."""
    base_url = os.getenv("OPENAI_EMBEDDING_BASE_URL") or os.getenv("OPENAI_BASE_URL") or None
    api_key = os.getenv("OPENAI_EMBEDDING_API_KEY") or os.getenv("OPENAI_API_KEY") or None
    return base_url, api_key


class LazyOpenAIEmbedder:
    """실제로 임베딩할 텍스트가 있을 때만 클라이언트를 만든다."""

    def __init__(self, model: str):
        self.model = model
        self._client = None

    def _get_client(self):
        if self._client is None:
            base_url, api_key = embedding_endpoint()
            if not api_key:
                raise RuntimeError("임베딩 API 키가 없습니다. .env의 OPENAI_API_KEY(또는 OPENAI_EMBEDDING_API_KEY)를 "
                                   "확인하거나 --skip-embed로 실행하세요.")
            from langchain_openai import OpenAIEmbeddings
            # 토큰 배열 대신 문자열을 보내 OpenAI 호환 게이트웨이에서도 동작하게 한다 (청크는 모두 짧다)
            self._client = OpenAIEmbeddings(model=self.model, base_url=base_url, api_key=api_key,
                                            check_embedding_ctx_length=False)
        return self._client

    def __call__(self, texts: list[str]) -> list[list[float]]:
        return self._get_client().embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._get_client().embed_query(text)
