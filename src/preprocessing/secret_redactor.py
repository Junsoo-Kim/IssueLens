"""API 키, JWT, 인증 헤더, 개인 키, 이메일, 공인 IP 후보 마스킹 (기획서 5.12절)."""
from __future__ import annotations

import ipaddress
import re
from collections import Counter

# (이름, 패턴, 치환). 치환이 함수이면 match를 받아 문자열을 돌려준다
_KEY_VALUE = re.compile(
    r"(?i)\b(api[_-]?key|secret[_-]?key|client[_-]?secret|access[_-]?key|auth[_-]?token|access[_-]?token"
    r"|password|passwd|pwd|secret|token)(\s*[:=]\s*[\"']?)([^\s\"',;}{]{6,})"
)
_PATTERNS: list[tuple[str, re.Pattern, object]] = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
     "<REDACTED_PRIVATE_KEY>"),
    ("auth_header", re.compile(r"(?i)\b((?:proxy-)?authorization\s*[:=]\s*)(?:(bearer|basic|token)\s+)?[^\s\"']+"),
     lambda m: f"{m.group(1)}{(m.group(2) + ' ') if m.group(2) else ''}<REDACTED>"),
    ("jwt", re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}"), "<REDACTED_JWT>"),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"), "<REDACTED_TOKEN>"),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}"), "<REDACTED_TOKEN>"),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "<REDACTED_TOKEN>"),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "<REDACTED_TOKEN>"),
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "<REDACTED_TOKEN>"),
    ("key_value", _KEY_VALUE, lambda m: f"{m.group(1)}{m.group(2)}<REDACTED>"),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "<EMAIL>"),
]
_IPV4 = re.compile(r"(?<![\w.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\w.])")
# 값이 이미 마스킹 표시이거나 환경 변수 참조이면 다시 치환하지 않는다
_PLACEHOLDER = re.compile(r"^(<REDACTED|<EMAIL>|\$\{?|\*+$|x+$|changeme$|<[^>]+>$)", re.I)


def _mask_ip(match: re.Match, counts: Counter) -> str:
    octets = [int(g) for g in match.groups()]
    if any(o > 255 for o in octets):
        return match.group(0)
    address = ipaddress.ip_address(match.group(0))
    if address.is_private or address.is_loopback or address.is_link_local or address.is_unspecified:
        return match.group(0)
    # "4.2.1.2" 같은 4자리 버전과 구분하기 위해 두 자리 이상 큰 값이 2개 이상일 때만 공인 IP로 본다
    if sum(o >= 50 for o in octets) < 2:
        return match.group(0)
    counts["ip"] += 1
    return "<IP>"


def redact(text: str) -> tuple[str, dict[str, int]]:
    """마스킹한 텍스트와 항목별 마스킹 수를 반환한다."""
    if not text:
        return text, {}
    counts: Counter = Counter()
    for name, pattern, replacement in _PATTERNS:
        def substitute(match: re.Match, name=name, replacement=replacement) -> str:
            if name == "key_value" and _PLACEHOLDER.match(match.group(3)):
                return match.group(0)
            counts[name] += 1
            return replacement(match) if callable(replacement) else replacement
        text = pattern.sub(substitute, text)
    text = _IPV4.sub(lambda m: _mask_ip(m, counts), text)
    return text, dict(counts)
