"""configs/*.yaml 로드."""
from __future__ import annotations

from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_repositories(path: Path, only: list[str] | None = None) -> list[dict]:
    """저장소 목록에 defaults를 합쳐 반환한다. `only`가 있으면 해당 저장소만 남긴다."""
    config = load_yaml(path)
    defaults = config.get("defaults", {})
    repositories = [{**defaults, **entry} for entry in config.get("repositories", [])]
    if only:
        wanted = {name.lower() for name in only}
        known = {entry["repo"].lower() for entry in repositories}
        unknown = sorted(wanted - known)
        if unknown:
            raise ValueError(f"repositories.yaml에 없는 저장소: {', '.join(unknown)}")
        repositories = [entry for entry in repositories if entry["repo"].lower() in wanted]
    return repositories
