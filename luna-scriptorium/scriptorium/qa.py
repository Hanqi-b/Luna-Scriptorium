"""Deterministic candidate validation and review flags."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
import re
from .common import ProjectError, _safe_relative, _normalise_text

def _read_translation_file(path: str, project_root: Path | None = None) -> str:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() and project_root is not None:
        candidate = _safe_relative(project_root,str(candidate))
    try:
        text = candidate.read_bytes().decode("utf-8")
    except FileNotFoundError as exc:
        raise ProjectError(f"file not found: {path}") from exc
    except UnicodeDecodeError as exc:
        raise ProjectError("candidate must be UTF-8") from exc
    if not text.strip() or "\x00" in text:
        raise ProjectError("candidate is empty or contains a NUL byte")
    return _normalise_text(text).rstrip()


def _numeric_tokens(text: str) -> list[str]:
    return [re.sub(r"\D","",match) for match in re.findall(r"(?<![0-9])[0-9][0-9,./:%-]*[0-9](?![0-9])",text)]


def _mechanical_check(source: str, translation: str) -> list[str]:
    source_heading = re.match(r"^\s*(#{1,6})(?=\s)",source)
    if source_heading:
        target_heading = re.match(r"^\s*(#{1,6})(?=\s)",translation)
        if not target_heading or target_heading.group(1) != source_heading.group(1):
            raise ProjectError("mechanical QA failed: Markdown heading marker must be preserved on the first line")
    original, translated = Counter(_numeric_tokens(source)), Counter(_numeric_tokens(translation))
    return sorted(token for token,count in original.items() if translated[token] < count)
