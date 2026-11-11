"""Loading helpers for run logs. A missing file reads as empty: crashed or partial runs still analyse."""
import json
from pathlib import Path
from typing import Any


def read_jsonl(path: Path) -> list[dict]:
    """All records of a JSONL file, or [] if it does not exist."""
    if not path.exists():
        return []
    with open(path) as f:
        return [json.loads(line) for line in f]


def read_json(path: Path, default: Any = None) -> Any:
    """Parsed JSON file, or `default` if it does not exist."""
    return json.loads(path.read_text()) if path.exists() else default
