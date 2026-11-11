#!/usr/bin/env python3
"""Host-side OpenAI-compatible chat transport for the instigation study.

Mirrors the wire shaping in `lab/agent/harness.py` (merge_user_after_tool /
drop_empty_assistant / sanitize_tool_args) and reads host / path / secret_env
from `config.provider_spec`. Used by generate.py, hillclimb.py and (host-side)
the Q1 triggers parser to talk to the eval models.

ISOLATION: this module is the ONLY thing that reads a model's response body.
Its callers (generate / hillclimb) write that body straight to disk as a
candidate `*.chatter.json` or reduce it to numbers; the raw response is never
returned to a coding agent's stdout. A fake transport with the same `.chat`
shape is injected in tests, so no test touches the network.

Spend: Cerebras (qwen38, unpriced) is the default and always allowed. The three
Berget models are priced; a Transport for them refuses to run unless
`allow_paid=True` is passed explicitly, so the Berget path stays opt-in and
gated behind the spend cap (settled through lab/spend.py when driven via
orchestrate).
"""
from __future__ import annotations

import http.client
import json
import os
import random
import sys
import time
from pathlib import Path

_LAB = Path(__file__).resolve().parents[1]
if str(_LAB) not in sys.path:
    sys.path.insert(0, str(_LAB))

import config  # noqa: E402

DEFAULT_SLUG = config.DEFAULT_SLUG
_RETRYABLE = {408, 409, 425, 429, 500, 502, 503, 504}


class ModelError(RuntimeError):
    """args[0] is a fixed error code: E_SPEND / E_AUTH / E_KEY / E_HTTP / E_TRANSPORT / E_EMPTY."""


# ---- wire shaping (mirrors harness.wire_messages / _sane_call) -----------------------------------

def _sane_call(c):
    fn = c.get("function") or {}
    args = fn.get("arguments")
    try:
        if isinstance(json.loads(args) if isinstance(args, str) else args, dict):
            return c if isinstance(args, str) else {**c, "function": {**fn, "arguments": json.dumps(args)}}
    except (ValueError, TypeError):
        pass
    return {**c, "function": {**fn, "arguments": json.dumps(
        {"invalid_arguments": args if isinstance(args, str) else ""})}}


def wire_messages(messages, wire):
    """Provider-specific request shaping; the conversation content is unchanged."""
    if not wire:
        return messages
    out = []
    for m in messages:
        if (wire.get("drop_empty_assistant") and m.get("role") == "assistant"
                and not m.get("content") and not m.get("tool_calls")):
            continue
        if wire.get("sanitize_tool_args") and m.get("tool_calls"):
            m = {**m, "tool_calls": [_sane_call(c) for c in m["tool_calls"]]}
        if (wire.get("merge_user_after_tool") and m.get("role") == "user"
                and out and out[-1]["role"] == "tool"):
            out[-1] = {**out[-1], "content": f"{out[-1]['content']}\n\n{m['content']}"}
        else:
            out.append(m)
    return out


def message_content(resp) -> str:
    """Extract the assistant text from a chat-completions response dict.

    Returns the raw model text (board content when generating). Callers write it
    to disk; it must never be surfaced to a coding agent's context.
    """
    try:
        return resp["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError):
        return ""


# ---- transport -----------------------------------------------------------------------------------

class Transport:
    """One OpenAI-compatible chat endpoint over a kept-alive HTTPS connection."""

    def __init__(self, slug: str = DEFAULT_SLUG, *, allow_paid: bool = False,
                 key: str = None, timeout: float = 180.0, max_retries: int = 6,
                 opener=None):
        self.slug = config.resolve_slug(slug)
        self.spec = config.MODELS[self.slug]
        self.provider = config.provider_spec(self.slug)
        self.model = self.spec["model"]
        self.wire = self.spec.get("wire") or {}
        self.timeout = timeout
        self.max_retries = max_retries
        self._opener = opener  # test seam: callable(host, timeout) -> http.client-like connection
        self._conn = None
        priced = None not in config.price(self.slug)
        if priced and not allow_paid:
            raise ModelError("E_SPEND")  # Berget models require explicit opt-in (spend cap)
        self._allow_paid = allow_paid
        if key is not None:
            self._key = key
        else:
            env = self.provider["secret_env"]
            self._key = os.environ.get(env)
            if not self._key:
                raise ModelError("E_KEY")

    def _connect(self):
        if self._conn is None:
            if self._opener is not None:
                self._conn = self._opener(self.provider["host"], self.timeout)
            else:
                self._conn = http.client.HTTPSConnection(self.provider["host"], timeout=self.timeout)
        return self._conn

    def _drop(self):
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None

    def chat(self, messages, *, tools=None, temperature=0.7, max_tokens=2048,
             response_format=None) -> dict:
        """POST a chat completion and return the parsed response dict.

        Raises ModelError (fixed code) on auth / non-retryable HTTP / exhausted
        retries. The returned dict's message content is model-authored and may be
        board content; callers persist it, never echo it.
        """
        payload = {"model": self.model, "messages": wire_messages(messages, self.wire),
                   "temperature": temperature, "max_tokens": max_tokens}
        if tools:
            payload["tools"] = tools
        if response_format:
            payload["response_format"] = response_format
        body = json.dumps(payload).encode()
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json",
                   "User-Agent": "schizboard-study/0.1"}
        delay = 5.0
        last = "E_TRANSPORT"
        for attempt in range(self.max_retries):
            try:
                conn = self._connect()
                conn.request("POST", self.provider["path"], body, headers)
                r = conn.getresponse()
                data = r.read()
                status = r.status
                if status == 200:
                    resp = json.loads(data)
                    if isinstance(resp, dict) and resp.get("choices") and not resp.get("error"):
                        return resp
                    last = "E_EMPTY"
                elif status in (401, 402, 403):
                    self._drop()
                    raise ModelError("E_AUTH")
                elif status not in _RETRYABLE:
                    last = "E_HTTP"
                    self._drop()
                    raise ModelError("E_HTTP")
                else:
                    last = "E_HTTP"
                    wait_hdr = r.getheader("retry-after")
                    delay = float(wait_hdr) if wait_hdr and wait_hdr.isdigit() else delay
            except (http.client.HTTPException, OSError, ValueError):
                last = "E_TRANSPORT"
                self._drop()
            time.sleep(delay + random.uniform(0, 1.5))
            delay = min(delay * 2, 120)
        raise ModelError(last)

    def close(self):
        self._drop()


def make_transport(slug: str = DEFAULT_SLUG, *, allow_paid: bool = False, **kw) -> Transport:
    """Factory used by generate/hillclimb; kept separate so tests inject a fake instead."""
    return Transport(slug, allow_paid=allow_paid, **kw)


def complete(slug: str, system_prompt: str, user_content: str, *, allow_paid: bool = False,
             transport=None, temperature: float = 0.0, max_tokens: int = 2048,
             response_format=None) -> str:
    """One system+user chat turn; return the raw assistant text (the model's response body).

    The single entry point the Q1 triggers parser (lab/study/triggers.py) uses for its non-injected
    model path: build (or reuse) a transport, post one completion, and hand back the assistant content.
    ISOLATION: the returned string is model-authored and may echo board content; callers write it to
    disk and reduce it to numbers -- it is never surfaced to a coding agent's context. `transport` is a
    test seam (any object with a `.chat(messages, ...)` method), mirroring generate.py.
    """
    tp = transport or make_transport(slug, allow_paid=allow_paid)
    messages = [{"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}]
    resp = tp.chat(messages, temperature=temperature, max_tokens=max_tokens,
                   response_format=response_format)
    return message_content(resp)
