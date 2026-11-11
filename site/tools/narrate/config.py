"""Config, paths, binaries and the API key (NARRATION.md 1.1, 5.1).

The API key is returned to the caller and never logged, printed, or put into an exception message.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

PKG = Path(__file__).resolve().parent
TOOLS = PKG.parent
SITE = TOOLS.parent
PUBLIC = SITE / "public"
VOICES_JSON = PKG / "voices.json"
SFX_DIR = PKG / "sfx"
KEY_ENV = "ELEVENLABS_API_KEY"
ROLES = ("guide", "expert")


class ConfigError(Exception):
    """Fixed-code configuration error. The message never contains secrets or file content."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def cache_dir() -> Path:
    return Path(os.environ.get("NARRATE_CACHE") or (SITE / ".narration-cache"))


_CFG: dict | None = None


def load_config(path: Path | None = None) -> dict:
    """voices.json, the single source of truth for voice ids, models, SFX prompts and mix settings."""
    global _CFG
    if path is None and _CFG is not None:
        return _CFG
    try:
        with open(path or VOICES_JSON, "rb") as fh:
            cfg = json.loads(fh.read())
    except (OSError, ValueError):
        raise ConfigError("E_CONFIG_READ") from None
    if not isinstance(cfg, dict) or cfg.get("version") != 1:
        raise ConfigError("E_CONFIG_VERSION")
    voices = cfg.get("voices")
    if not isinstance(voices, dict) or set(voices) != set(ROLES):
        raise ConfigError("E_CONFIG_VOICES")
    for v in voices.values():
        if not isinstance(v.get("voiceId"), str) or not isinstance(v.get("charsPerSec"), (int, float)):
            raise ConfigError("E_CONFIG_VOICES")
    if not isinstance(cfg.get("models", {}), dict):
        raise ConfigError("E_CONFIG_MODELS")
    lib = cfg.get("sfx", {}).get("library")
    if not isinstance(lib, dict) or not lib:
        raise ConfigError("E_CONFIG_SFX")
    if path is None:
        _CFG = cfg
    return cfg


def _which(name: str) -> str:
    env = os.environ.get(f"NARRATE_{name.upper()}")
    if env:
        return env
    p = shutil.which(name)
    if p:
        return p
    for d in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"):
        c = Path(d) / name
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    raise ConfigError(f"E_NO_{name.upper()}")


def ffmpeg() -> str:
    return _which("ffmpeg")


def ffprobe() -> str:
    return _which("ffprobe")


def has_ffmpeg() -> bool:
    try:
        ffmpeg(), ffprobe()
        return True
    except ConfigError:
        return False


def _parse_env_file(p: Path, name: str) -> str | None:
    """Minimal .env reader: KEY=value with optional surrounding quotes. Nothing else is supported."""
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    for line in lines:
        s = line.strip()
        if s.startswith("export "):
            s = s[7:].lstrip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        if k.strip() != name:
            continue
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        return v or None
    return None


def api_key(start: Path | None = None) -> str:
    """ELEVENLABS_API_KEY from the environment, else the nearest .env walking up from site/ to the git root.
    Raises ConfigError('E_NO_KEY'); the key itself never appears in any message."""
    v = os.environ.get(KEY_ENV)
    if v:
        return v.strip()
    d = (start or SITE).resolve()
    while True:
        f = d / ".env"
        if f.is_file():
            v = _parse_env_file(f, KEY_ENV)
            if v:
                return v
        if (d / ".git").exists() or d.parent == d:
            break
        d = d.parent
    raise ConfigError("E_NO_KEY")


def write_atomic(path: Path, data: bytes) -> None:
    """Write via a temp file in the cache dir (never a stray temp under public/), then move into place."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = cache_dir() / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=tmp_dir, prefix="w-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        shutil.move(tmp, str(path))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_cache_json(rel: str, obj) -> Path:
    """Cache-only JSON (e.g. runs/<id>/script.json). Never printed."""
    p = cache_dir() / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return p


def read_cache_json(rel: str):
    try:
        with open(cache_dir() / rel, "rb") as fh:
            return json.loads(fh.read())
    except (OSError, ValueError):
        return None
