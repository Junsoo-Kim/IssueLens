"""Markdown 정제: 코드 블록 보존, HTML 주석·스크립트·태그 제거, 사용자 경로 일반화, 빈 템플릿 항목 제거."""
from __future__ import annotations

import re

FENCE = re.compile(r"(^```.*?^```[ \t]*$|^~~~.*?^~~~[ \t]*$)", re.S | re.M)
HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
SCRIPT_STYLE = re.compile(r"<(script|style)\b.*?</\1\s*>", re.S | re.I)
HTML_TAG = re.compile(r"</?(?:details|summary|p|br|div|span|img|a|b|i|em|strong|sub|sup|kbd|hr|table|thead|tbody|tr|td|th|ul|ol|li|h[1-6]|picture|source|video)\b[^>]*>", re.I)
USER_PATHS = [
    (re.compile(r"(?i)\b([A-Z]:\\+Users\\+)[^\\/\s\"']+"), r"\1<USER>"),
    (re.compile(r"/(home|Users)/(?!<USER>)[^/\s\"']+"), r"/\1/<USER>"),
]
EMPTY_SECTION = re.compile(r"^(#{1,4}[^\n]*)\n+\s*_?No response_?\s*(?=\n#{1,4}\s|\Z)", re.M | re.I)
BLANK_LINES = re.compile(r"\n{3,}")
# 하나의 문서가 수만 자 로그로 임베딩 비용을 독점하지 않도록 둔 상한
MAX_DOCUMENT_CHARS = 20_000


def _outside_code(text: str, transform) -> str:
    """코드 블록은 그대로 두고 나머지 부분에만 transform을 적용한다."""
    parts = FENCE.split(text)
    return "".join(part if FENCE.fullmatch(part) else transform(part) for part in parts)


def _clean_prose(text: str) -> str:
    text = HTML_COMMENT.sub("", text)
    text = SCRIPT_STYLE.sub("", text)
    return HTML_TAG.sub("", text)


def normalize_markdown(text: str | None, max_chars: int = MAX_DOCUMENT_CHARS) -> tuple[str, bool]:
    """정제한 텍스트와 잘림 여부를 반환한다."""
    if not text:
        return "", False
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _outside_code(text, _clean_prose)
    for pattern, replacement in USER_PATHS:
        text = pattern.sub(replacement, text)
    text = EMPTY_SECTION.sub("", text)
    text = BLANK_LINES.sub("\n\n", text).strip()
    if len(text) > max_chars:
        return text[:max_chars].rstrip() + "\n\n[... truncated]", True
    return text, False
