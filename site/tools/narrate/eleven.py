"""Minimal ElevenLabs client over urllib (NARRATION.md 5).

- The API key travels only in the `xi-api-key` request header. It is never logged, printed, put in a URL,
  in an exception message, or in cache metadata.
- Error bodies are read once to extract a short `detail.status` token, then discarded. ElevenError carries
  only (status, code).
- 429 (except quota) and 5xx / network errors are retried with exponential backoff plus jitter.
- No /v1/user* calls: the key lacks the user_read permission.
"""
from __future__ import annotations

import base64
import json
import random
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://api.elevenlabs.io"
BACKOFF_S = (1, 2, 4, 8, 16)
MAX_ATTEMPTS = 5
TIMEOUT_S = 120
FATAL_CODES = frozenset({"quota_exceeded"})
_CODE = re.compile(r"[a-z_]{1,48}")
_VOICE_ID = re.compile(r"[A-Za-z0-9]{8,40}")


class ElevenError(Exception):
    def __init__(self, status: int, code: str):
        self.status = int(status)
        self.code = code if _CODE.fullmatch(code or "") else "unknown"
        super().__init__(f"elevenlabs status={self.status} code={self.code}")

    @property
    def retryable(self) -> bool:
        return (self.status == 429 and self.code not in FATAL_CODES) or self.status >= 500 or self.status == 0


def _error_code(status: int, body: bytes) -> str:
    if status == 422:
        return "validation"
    try:
        obj = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return "unknown"
    det = obj.get("detail") if isinstance(obj, dict) else None
    st = det.get("status") if isinstance(det, dict) else None
    return st if isinstance(st, str) and _CODE.fullmatch(st) else "unknown"


class Client:
    def __init__(self, key: str, opener=None, sleep=time.sleep, log=None, base: str = BASE,
                 rng: random.Random | None = None):
        if not isinstance(key, str) or not key:
            raise ElevenError(0, "no_key")
        self._key = key
        self._opener = opener or urllib.request.build_opener()
        self._sleep = sleep
        self._log = log or (lambda msg: None)
        self._base = base.rstrip("/")
        self._rng = rng or random.Random()

    def __repr__(self) -> str:          # never expose the key via repr()
        return "<eleven.Client>"

    # ----------------------------------------------------------------------------------------------
    def _once(self, path: str, query: dict, body: dict, accept: str) -> tuple[bytes, dict]:
        url = self._base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "xi-api-key": self._key, "Content-Type": "application/json", "Accept": accept})
        try:
            resp = self._opener.open(req, timeout=TIMEOUT_S)
        except urllib.error.HTTPError as e:
            try:
                raw = e.read(16384) if e.fp is not None else b""
            except Exception:  # noqa: BLE001
                raw = b""
            finally:
                try:
                    e.close()
                except Exception:  # noqa: BLE001
                    pass
            code = _error_code(e.code, raw)
            del raw
            raise ElevenError(e.code, code) from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError):
            raise ElevenError(0, "network") from None
        try:
            payload = resp.read()
            headers = {k.lower(): v for k, v in (resp.headers.items() if resp.headers else [])}
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
        return payload, headers

    def _post(self, what: str, path: str, query: dict, body: dict, accept: str) -> tuple[bytes, dict]:
        for attempt in range(MAX_ATTEMPTS):
            try:
                return self._once(path, query, body, accept)
            except ElevenError as e:
                if not e.retryable or attempt == MAX_ATTEMPTS - 1:
                    raise
                wait = BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)]
                wait += self._rng.uniform(0, wait * 0.25)
                self._log(f"eleven {what} retry status={e.status} code={e.code} attempt={attempt + 1} "
                          f"wait_ms={int(wait * 1000)}")
                self._sleep(wait)
        raise ElevenError(0, "unreachable")

    # ----------------------------------------------------------------------------------------------
    def tts_with_timestamps(self, voice_id: str, text: str, model: str, settings: dict, seed: int | None,
                            output_format: str, endpoint: str = "/v1/text-to-speech/{voice_id}/with-timestamps"
                            ) -> dict:
        if not _VOICE_ID.fullmatch(voice_id or ""):
            raise ElevenError(0, "bad_voice_id")
        body = {"text": text, "model_id": model, "voice_settings": settings}
        if seed is not None:
            body["seed"] = int(seed)
        payload, headers = self._post("tts", endpoint.format(voice_id=voice_id), {"output_format": output_format},
                                      body, "application/json")
        try:
            obj = json.loads(payload.decode("utf-8"))
            audio = base64.b64decode(obj["audio_base64"], validate=False)
        except (ValueError, KeyError, TypeError):
            raise ElevenError(200, "bad_response") from None
        if not audio:
            raise ElevenError(200, "empty_audio")
        align = obj.get("alignment") if isinstance(obj.get("alignment"), dict) else None
        cost = headers.get("character-cost") or headers.get("x-character-count")
        rid = headers.get("request-id") or headers.get("x-request-id")
        return {"audio": audio, "alignment": align,
                "characterCost": int(cost) if isinstance(cost, str) and cost.isdigit() else None,
                "requestId": rid if isinstance(rid, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", rid) else None}

    def sound_generation(self, prompt: str, seconds: float, influence: float, model: str,
                         output_format: str) -> bytes:
        body = {"text": prompt, "duration_seconds": seconds, "prompt_influence": influence, "loop": False,
                "model_id": model}
        payload, _ = self._post("sfx", "/v1/sound-generation", {"output_format": output_format}, body, "audio/mpeg")
        if not payload:
            raise ElevenError(200, "empty_audio")
        return payload
