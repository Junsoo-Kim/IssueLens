"""OPENAI_BASE_URL과 키로 임베딩·채팅 API가 동작하는지 점검한다. 키는 출력하지 않는다.

    python scripts/check_openai.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from src.config import load_yaml  # noqa: E402
from src.indexing.embeddings import embedding_endpoint  # noqa: E402


def describe_base_url(base_url: str | None, name: str = "OPENAI_BASE_URL") -> list[str]:
    if not base_url:
        return [f"{name}: 설정 안 됨 (https://api.openai.com/v1 사용)"]
    parsed = urlparse(base_url)
    lines = [f"{name}: {parsed.scheme}://{parsed.netloc}{parsed.path}"]
    path = parsed.path.rstrip("/")
    if path.endswith(("/chat/completions", "/embeddings", "/responses", "/completions")):
        lines.append("  경고: 주소가 API 경로까지 포함합니다. 라이브러리가 뒤에 /embeddings 등을 붙이므로 "
                     "'.../v1'까지만 적어야 합니다.")
    elif not path.endswith("/v1"):
        lines.append("  참고: 주소가 '/v1'로 끝나지 않습니다. 게이트웨이 안내 문서의 OpenAI 호환 주소인지 확인하세요.")
    if parsed.query:
        lines.append("  경고: 주소에 쿼리 문자열이 있습니다.")
    return lines


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    load_dotenv(ROOT / ".env")
    embedding_base_url, embedding_key = embedding_endpoint()
    print("\n".join(describe_base_url(os.getenv("OPENAI_BASE_URL"))))
    print("\n".join(describe_base_url(embedding_base_url, "임베딩 주소 (OPENAI_EMBEDDING_BASE_URL 또는 OPENAI_BASE_URL)")))
    print(f"OPENAI_API_KEY: {'설정됨' if os.getenv('OPENAI_API_KEY') else '설정 안 됨'}"
          f" / 임베딩 키: {'설정됨' if embedding_key else '설정 안 됨'}")
    if not os.getenv("OPENAI_API_KEY"):
        return 2

    from openai import OpenAI

    client = OpenAI()
    embedding_client = OpenAI(base_url=embedding_base_url, api_key=embedding_key)
    embedding_model = load_yaml(ROOT / "configs/retrieval.yaml").get("embedding", {}).get("model", "text-embedding-3-small")
    chat_model = load_yaml(ROOT / "configs/app.yaml").get("llm", {}).get("model", "gpt-5-mini")
    ok = True

    print(f"\n[임베딩] 요청 주소: {embedding_client.base_url}embeddings / 모델: {embedding_model}")
    try:
        response = embedding_client.embeddings.create(model=embedding_model, input=["connection timeout after upgrade"])
        print(f"  성공: 차원 {len(response.data[0].embedding)}")
    except Exception as exc:  # 진단용이므로 어떤 오류든 그대로 보여 준다
        ok = False
        print(f"  실패: {type(exc).__name__}: {str(exc)[:500]}")

    print(f"\n[채팅] 요청 주소: {client.base_url}chat/completions / 모델: {chat_model}")
    try:
        response = client.chat.completions.create(
            model=chat_model, messages=[{"role": "user", "content": "Reply with OK."}], max_completion_tokens=200)
        print(f"  성공: {response.choices[0].message.content!r}")
    except Exception as exc:
        ok = False
        print(f"  실패: {type(exc).__name__}: {str(exc)[:500]}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
