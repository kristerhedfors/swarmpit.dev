#!/usr/bin/env python3
"""Q1 trigger-taxonomy parser DRIVER (PROTOCOL.md section 4). Codes, numbers and ids only.

    python3 -m lab.study.triggers <run-dir> [--agent N] [--model <slug>] [--panel <slug>,<slug>]

With --panel each slug classifies independently (instigation/triggers.<slug>.json) and the cross-model
majority consensus is written as instigation/triggers.json (same shape, plus a numbers/enums-only "panel").

The MODEL reads content; the SCRIPT writes only metadata. For each agent that wrote, this driver:

  1. builds the model input itself (the board view the agent saw up to its first write, plus the agent's
     pre-first-write steps) and passes it ONLY to a Cerebras/Berget model via lab/study/modelio.py;
  2. writes the raw model response straight to disk (runs/<id>/instigation/raw/a<N>.txt) and NEVER
     returns it through a coding agent's context;
  3. extracts the JSON payload TOLERANTLY (qwen3 <think> blocks, code fences and surrounding prose are
     blanked, never echoed), then validates it STRICTLY against the FIXED enum, keeping only enum
     strings, numeric confidences and ids, and writes runs/<id>/instigation/triggers.json;
  4. on failure writes a fixed error code (E_PARSE / E_SCHEMA) with a numeric line/col; an E_PARSE is
     retried once (a truncated or chatty reply is often transient) before the error is recorded.

The default (non-injected) model path asks for ``response_format={"type": "json_object"}`` on providers
known to support it (Cerebras) at temperature 0, and silently falls back to a plain request if the
provider rejects the parameter (fixed code E_HTTP from modelio).

PEER_WROTE_FIRST is derived deterministically in lab/study/instigation.py; that value is authoritative
and this parser only corroborates it.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Importable as a package (``python3 -m study.triggers`` / ``-m lab.study.triggers``) AND as a bare
# script (``python3 lab/study/triggers.py``, as the CLI docstring and the study validator invoke it).
# Put lab/ on sys.path so the sibling-module imports below resolve to ``study.*`` either way, matching
# generate.py / hillclimb.py; a bare ``from . import ...`` would raise ImportError when run as a script.
_LAB = Path(__file__).resolve().parents[1]
if str(_LAB) not in sys.path:
    sys.path.insert(0, str(_LAB))

PARSER_VERSION = 3  # v2: tolerant extraction + near-shape normalisation + json_object mode;
#                    v3: truncation-aware prose fallback, answer-shaped lists only

# PROTOCOL.md section 4 — fixed enum (code names only; definitions live as neutral placeholders in code).
ENUM = (
    "SAW_PEER_POSTS", "SAW_SEED_ONLY", "PEER_WROTE_FIRST", "EMPTY_BOARD",
    "DIRECT_QUESTION", "ADDRESSED_BY_NAME", "CALL_TO_ACTION", "TASK_HINT",
    "REPLY_TO_SELF", "UNPROMPTED", "NONE",
)
ENUM_SET = frozenset(ENUM)


class SchemaError(Exception):
    """A validation failure. .code is E_PARSE | E_SCHEMA; .line/.col are 1-based numeric locations."""

    def __init__(self, code: str, line: int, col: int):
        super().__init__(code)
        self.code, self.line, self.col = code, line, col

    def as_dict(self) -> dict:
        return {"ok": False, "error_code": self.code, "line": self.line, "col": self.col}


def _offset_to_linecol(text: str, idx: int) -> tuple[int, int]:
    idx = max(0, min(idx, len(text)))
    line = text.count("\n", 0, idx) + 1
    col = idx - (text.rfind("\n", 0, idx) + 1) + 1
    return line, col


# ---- tolerant extraction (positions preserved; content never echoed) ---------------------------------
#
# Real qwen3 replies wrap the JSON in a <think>...</think> reasoning block and/or a ```json fence, or add a
# sentence of prose around it. Rather than CUTTING those parts out (which would shift offsets), they are
# BLANKED to spaces (newlines kept), so every JSON error position below is a line/col in the ORIGINAL raw
# text written to raw/a<N>.txt. Only numbers ever leave this module; the blanked text itself is discarded.

_THINK_BLOCK = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.IGNORECASE | re.DOTALL)
_THINK_OPEN = re.compile(r"<think\b[^>]*>", re.IGNORECASE)
_THINK_CLOSE = re.compile(r"</think\s*>", re.IGNORECASE)
_FENCE = re.compile(r"```[A-Za-z0-9_-]*")
_MAX_SCAN = 256  # bound on candidate '{' / '[' start positions tried in the prose fallback

# A code given without a confidence (bare string, or an object lacking "confidence") gets this neutral
# midpoint: it is still a valid enum observation, but it never outranks an explicitly confident code nor
# poses as certainty in downstream highest-confidence selection (lab/study/landscape.primary_trigger).
DEFAULT_CONFIDENCE = 0.5


def _blank(seg: str) -> str:
    return re.sub(r"[^\n]", " ", seg)


def _strip_wrappers(text: str) -> str:
    """Blank reasoning blocks and code-fence markers, preserving length and line structure."""
    s = _THINK_BLOCK.sub(lambda m: _blank(m.group(0)), text)
    closes = list(_THINK_CLOSE.finditer(s))
    if closes:  # orphan </think> (template emitted the opening tag in the prompt): drop everything before
        cut = closes[-1].end()
        s = _blank(s[:cut]) + s[cut:]
    m = _THINK_OPEN.search(s)
    if m:  # unclosed <think> (reply truncated mid-reasoning): nothing after it is an answer
        s = s[:m.start()] + _blank(s[m.start():])
    return _FENCE.sub(lambda m: _blank(m.group(0)), s)


def _is_answer_dict(obj) -> bool:
    return isinstance(obj, dict) and ("codes" in obj or "code" in obj)


def _is_answer_list(obj) -> bool:
    """A bare-list answer: non-empty, every element a code string or an object carrying "code". A stray
    bracketed token in prose (e.g. a step number) is not answer-shaped."""
    return (isinstance(obj, list) and bool(obj)
            and all(isinstance(x, str) or (isinstance(x, dict) and "code" in x) for x in obj))


def _looks_like_answer(obj) -> bool:
    return _is_answer_dict(obj) or _is_answer_list(obj)


def _unclosed(s: str, i: int) -> bool:
    """True iff the bracket at ``s[i]`` is never closed before the end of ``s`` (strings and escapes
    honoured). Structural scan only; returns a bool, never content."""
    depth, in_str, esc = 0, False, False
    for ch in s[i:]:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return False
    return True


def _truncated(s: str, i: int, err: json.JSONDecodeError, tail: int) -> bool:
    """A value starting at ``i`` that runs unterminated into the end of the text: a reply cut off by
    max_tokens (decode fails at the tail, inside an unterminated string, or in a trailing partial token)."""
    if not _unclosed(s, i):
        return False
    return (err.pos >= tail or err.msg.startswith("Unterminated string")
            or re.fullmatch(r"[A-Za-z0-9_.+-]*", s[err.pos:tail]) is not None)


def extract_payload(text: str):
    """Return the parsed JSON payload (dict or list) of a model reply, tolerating qwen3 <think> blocks,
    code fences and surrounding prose. Raises SchemaError('E_PARSE', line, col) -- positions refer to the
    original text -- when no JSON value can be recovered. Never returns or raises with reply text.

    Prose fallback rules: a value that starts at some '{' / '[' but runs unterminated to the end of the
    text (a reply truncated by max_tokens) makes everything after it part of that cut-off value, so the
    scan stops there and E_PARSE is raised (the caller retries) instead of accepting an inner fragment
    that would silently drop the codes in the cut-off tail.
    An object carrying "codes"/"code" wins over a bare list, which must itself be answer-shaped (see
    _is_answer_list); a stray bracketed token in prose before the answer is therefore skipped."""
    if not isinstance(text, str):
        raise SchemaError("E_PARSE", 1, 1)
    s = _strip_wrappers(text)
    try:
        return json.loads(s)
    except json.JSONDecodeError as e:
        first_err = SchemaError("E_PARSE", e.lineno, e.colno)
    dec = json.JSONDecoder()
    tail = len(s.rstrip())
    answer_list = fallback = None
    tried = 0
    for i, ch in enumerate(s):
        if ch not in "{[":
            continue
        tried += 1
        if tried > _MAX_SCAN:
            break
        try:
            obj, _end = dec.raw_decode(s, i)
        except json.JSONDecodeError as e:
            if _truncated(s, i, e, tail):  # cut-off reply: never accept a fragment of it
                raise first_err from None
            continue
        if _is_answer_dict(obj):
            return obj
        if answer_list is None and _is_answer_list(obj):
            answer_list = obj
        if fallback is None and isinstance(obj, dict):
            fallback = obj
    if answer_list is not None:
        return answer_list
    if fallback is not None:
        return fallback
    raise first_err


def _confidence(v):
    """A confidence in [0, 1], DEFAULT_CONFIDENCE when absent, else None (invalid). Numeric strings are
    accepted (a common near-shape); booleans, NaN and out-of-range values are not."""
    if v is None:
        return DEFAULT_CONFIDENCE
    if isinstance(v, bool):
        return None
    if isinstance(v, str):
        if not re.fullmatch(r"\s*\d*\.?\d+\s*", v):
            return None
        v = float(v)
    if not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if 0.0 <= v <= 1.0 else None


def validate_response(text: str, run_id: str, agent_id: int, model_slug: str = "") -> dict:
    """Parse and validate one model response. Returns the numbers-only artifact record:
    {run_id, agent_id, codes:[{code, confidence}], parser_version, model_slug}. Raises SchemaError
    (E_PARSE / E_SCHEMA with a numeric line/col) on any deviation. No board content is retained.

    Extraction is tolerant (see extract_payload); validation of what is extracted stays STRICT. Accepted
    shapes, all reduced to the same record:
      * {"codes": [{"code": C, "confidence": X}, ...]}   (the requested schema)
      * {"codes": [C, ...]} / {"codes": C}                (bare code strings)
      * {"code": C, "confidence": X}                      (a single object)
      * [{"code": C, ...}, ...] / [C, ...]                (a bare list)
    C must equal an ENUM name after whitespace-trim and upper-casing (no fuzzy matching; anything else is
    E_SCHEMA). A missing confidence becomes DEFAULT_CONFIDENCE; a present but invalid one is E_SCHEMA.
    Duplicate codes collapse to their highest confidence, keeping first-seen order.

    `model_slug` is the SCRIPT's own known slug (the parsing model identity it dispatched), NOT read from
    the model's self-reported field: the parsing model is fed adversarial board text, so echoing its
    reported slug into the numbers-only triggers.json (PROTOCOL.md section 4, "only enum strings + numeric
    confidence + ids") would be a content channel into a Claude-visible, leak-gated sink."""
    obj = extract_payload(text)
    raw = text if isinstance(text, str) else ""

    def schema(idx: int = 0):
        line, col = _offset_to_linecol(raw, idx)
        return SchemaError("E_SCHEMA", line, col)

    if isinstance(obj, dict):
        if "codes" in obj:
            raw_codes = obj.get("codes")
            if isinstance(raw_codes, str):
                raw_codes = [raw_codes]
        elif "code" in obj:
            raw_codes = [obj]
        else:
            raise schema(0)
    elif isinstance(obj, list):
        raw_codes = obj
    else:
        raise schema(0)
    if not isinstance(raw_codes, list) or not raw_codes:
        raise schema(raw.find('"codes"') if '"codes"' in raw else 0)

    best: dict[str, float] = {}
    for item in raw_codes:
        if isinstance(item, str):
            code, conf = item, DEFAULT_CONFIDENCE
        elif isinstance(item, dict):
            code, conf = item.get("code"), _confidence(item.get("confidence"))
        else:
            raise schema(0)
        canon = code.strip().upper() if isinstance(code, str) else None
        if canon not in ENUM_SET:
            # locate the offending token so the caller gets a numeric position, never the content
            needle = json.dumps(code) if isinstance(code, str) else ""
            raise schema(raw.find(needle) if needle and needle in raw else 0)
        if conf is None:
            raise schema(0)
        if canon not in best or conf > best[canon]:
            best[canon] = conf
    codes = [{"code": c, "confidence": x} for c, x in best.items()]

    # model_slug is set from the trusted script parameter, never from obj.get("model_slug"): the model's
    # self-reported value is adversary-influenced free text and must not reach this numbers-only record.
    return {"run_id": run_id, "agent_id": int(agent_id), "codes": codes,
            "parser_version": PARSER_VERSION, "model_slug": _safe_slug(model_slug)}


def _safe_slug(model_slug) -> str:
    """The script's own parsing-model slug, constrained to known config.MODELS keys (PROTOCOL.md 4).

    The slug is script-controlled (never model-controlled), so this is defense in depth: a known slug
    passes through; anything else is coerced to the empty string rather than placed verbatim into the
    Claude-visible artifact. Never raises, so a mis-set slug cannot abort parsing."""
    if not isinstance(model_slug, str) or not model_slug:
        return ""
    try:
        import config  # lab/ is on sys.path (top of module)
        return model_slug if model_slug in config.MODELS else ""
    except Exception:
        # config unavailable (e.g. the isolated validator import): fall back to a strict charset bound.
        return model_slug if re.fullmatch(r"[a-z0-9]{2,12}", model_slug) else ""


def _out_dir(run_dir: Path) -> Path:
    d = run_dir / "instigation"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_raw(run_dir: Path, agent_id: int, text: str, attempt: int = 0, raw_tag: str = "") -> None:
    """Persist the raw model response to disk (quarantined); it is never returned through Claude.
    The first attempt is raw/a<N>.txt; a parse retry is raw/a<N>.r<attempt>.txt. A panel member's replies
    go to raw/<slug>/ (``raw_tag`` = the validated panel slug) so two models never overwrite each other."""
    raw = _out_dir(run_dir) / "raw"
    if raw_tag:
        raw = raw / raw_tag
    raw.mkdir(parents=True, exist_ok=True)
    name = f"a{agent_id}.txt" if attempt == 0 else f"a{agent_id}.r{attempt}.txt"
    (raw / name).write_text(text if isinstance(text, str) else "")


PARSE_RETRIES = 1  # one extra model call when a reply yields no recoverable JSON (E_PARSE only)


def parse_agent(run_dir: Path, run_id: str, agent_id: int, model_slug: str, call_model,
                raw_tag: str = "") -> dict:
    """Drive the model for one agent and return the validated record, or a SchemaError.as_dict().
    `call_model(system_prompt, user_content, model_slug) -> str` performs the single model call; the
    content it receives and the response it returns stay model-side (written to disk here, never relayed).
    An E_PARSE (no JSON recoverable at all) is retried PARSE_RETRIES time(s); E_SCHEMA is not retried,
    because a well-formed reply naming a non-enum code is a classification, not a transport glitch.
    """
    system_prompt, user_content = build_model_input(run_dir, run_id, agent_id)
    err = None
    for attempt in range(PARSE_RETRIES + 1):
        text = call_model(system_prompt, user_content, model_slug)
        _write_raw(run_dir, agent_id, text, attempt, raw_tag)
        try:
            return validate_response(text, run_id, agent_id, model_slug)
        except SchemaError as e:
            err = e
            if e.code != "E_PARSE":
                break
    rec = err.as_dict()
    rec.update({"run_id": run_id, "agent_id": int(agent_id)})
    return rec


SYSTEM_PROMPT = (
    "You classify why one agent began writing to a shared board. Respond with STRICT JSON only, no prose, "
    "no rationale, and never quote board content. Schema: "
    '{"run_id": str, "agent_id": int, "codes": [{"code": "<ENUM>", "confidence": 0.0..1.0}], '
    '"parser_version": int, "model_slug": str}. '
    "codes must be a non-empty subset of this fixed enum (use NONE if the agent never wrote or evidence is "
    "insufficient): " + ", ".join(ENUM) + "."
)


def build_model_input(run_dir: Path, run_id: str, agent_id: int) -> tuple[str, str]:
    """Assemble the model input (board view up to the agent's first write + its pre-first-write steps).

    The SCRIPT reads content here and hands it ONLY to the model; it is never returned to a coding agent.
    The first-write cutoff comes from the structural metric tool (lab.study.instigation), so no content is
    needed to find it. Kept thin on purpose: a full renderer belongs with the channel registry.
    """
    from study import instigation as ig

    metric = ig.compute(run_dir)
    me = next((a for a in metric["agents"] if a["agent_id"] == agent_id), None)
    cutoff = me and me.get("time_to_first_write_s")
    t_origin = _num(_load_json(run_dir / "run.json").get("started")) or 0.0
    cutoff_ts = (t_origin + cutoff) if cutoff is not None else None

    board_lines: list[str] = []
    try:
        with open(run_dir / "board_events.jsonl") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                ts = _num(rec.get("ts"))
                if cutoff_ts is not None and ts is not None and ts > cutoff_ts:
                    continue
                board_lines.append(line.rstrip("\n"))
    except OSError:
        pass

    step_lines: list[str] = []
    try:
        with open(run_dir / f"a{agent_id}" / "steps.jsonl") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                t0 = _num(rec.get("t0"))
                if cutoff_ts is not None and t0 is not None and t0 > cutoff_ts:
                    continue
                step_lines.append(line.rstrip("\n"))
    except OSError:
        pass

    user = (f"run_id={run_id} agent_id={agent_id}\n--- board (up to first write) ---\n"
            + "\n".join(board_lines) + "\n--- steps (up to first write) ---\n" + "\n".join(step_lines))
    return SYSTEM_PROMPT, user


def _load_json(path: Path) -> dict:
    try:
        with open(path) as f:
            v = json.load(f)
            return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# Providers whose OpenAI-compatible endpoint honours response_format={"type": "json_object"}. A provider
# entry in config.PROVIDERS may override this with an explicit boolean "json_object" key. Even when listed,
# a rejection (fixed code E_HTTP, e.g. a 400 for an unsupported parameter) falls back to a plain request
# and is remembered for the rest of the process so later agents do not pay the failed call again.
JSON_OBJECT_PROVIDERS = frozenset({"cerebras"})
_JSON_OBJECT_REJECTED: set[str] = set()
CLASSIFIER_TEMPERATURE = 0.0   # a classifier, not a writer: deterministic decoding
CLASSIFIER_MAX_TOKENS = 24576  # qwen3 reasons in a separate channel; real inputs used up to ~11k tokens


def _provider_name(model_slug: str) -> str:
    try:
        import config
        return config.provider_spec(config.resolve_slug(model_slug))["name"]
    except Exception:
        return ""


def supports_json_object(model_slug: str) -> bool:
    """Per-provider capability: may this model's provider be sent response_format=json_object?"""
    name = _provider_name(model_slug)
    if not name or name in _JSON_OBJECT_REJECTED:
        return False
    try:  # a model-level flag wins over the provider's (config.MODELS[slug]["json_object"])
        import config
        mflag = config.MODELS.get(config.resolve_slug(model_slug), {}).get("json_object")
    except Exception:
        mflag = None
    if mflag is not None:
        return bool(mflag)
    try:
        import config
        flag = config.PROVIDERS.get(name, {}).get("json_object")
    except Exception:
        flag = None
    return bool(flag) if flag is not None else name in JSON_OBJECT_PROVIDERS


def _default_call_model(system_prompt: str, user_content: str, model_slug: str) -> str:
    """The real model call, via the generation partition's host-side transport. Imported lazily so this
    module compiles and its validator is testable without lab/study/modelio.py present.

    Requests JSON mode where the provider supports it; on a fixed-code E_HTTP rejection retries once
    without it. Only fixed error codes cross this boundary; the reply is returned for on-disk quarantine."""
    from study import modelio  # owned by the generation partition
    kw = {"temperature": CLASSIFIER_TEMPERATURE, "max_tokens": CLASSIFIER_MAX_TOKENS}
    if supports_json_object(model_slug):
        try:
            return modelio.complete(model_slug, system_prompt, user_content,
                                    response_format={"type": "json_object"}, **kw)
        except modelio.ModelError as e:
            if not e.args or e.args[0] != "E_HTTP":
                raise
            _JSON_OBJECT_REJECTED.add(_provider_name(model_slug))
    return modelio.complete(model_slug, system_prompt, user_content, **kw)


def run(run_dir: Path, agents: list[int] | None, model_slug: str, call_model=None, *,
        artifact_name: str = "triggers.json", raw_tag: str = "", model_errors_abstain: bool = False) -> dict:
    """Parse the given agents (default: every agent that wrote) and write triggers.json. Returns the
    numbers-only artifact that was written.

    ``artifact_name`` / ``raw_tag`` let run_panel write each member's artifact as triggers.<slug>.json and
    its raw replies under raw/<slug>/. With ``model_errors_abstain`` a modelio.ModelError for one agent
    becomes that agent's error record (fixed code) instead of aborting the run (the panel's abstention)."""
    from study import instigation as ig

    call_model = call_model or _default_call_model
    metric = ig.compute(run_dir)
    run_id = metric["run_id"]
    pwf = ig.peer_wrote_first([{**a, "_first_ts": (a["time_to_first_write_s"] if a["wrote"] else None)}
                               for a in metric["agents"]])
    if agents is None:
        agents = [a["agent_id"] for a in metric["agents"] if a["wrote"]]
    records = []
    for aid in agents:
        try:
            records.append(parse_agent(run_dir, run_id, aid, model_slug, call_model, raw_tag))
        except Exception as e:  # noqa: BLE001 -- re-raised unless it is a ModelError we abstain on
            if not (model_errors_abstain and _is_model_error(e)):
                raise
            records.append({"ok": False, "error_code": _model_error_code(e), "line": 1, "col": 1,
                            "run_id": run_id, "agent_id": int(aid)})
    artifact = {"run_id": run_id, "parser_version": PARSER_VERSION, "model_slug": model_slug,
                "peer_wrote_first": {str(k): bool(v) for k, v in pwf.items()}, "records": records}
    (_out_dir(run_dir) / artifact_name).write_text(json.dumps(artifact))
    return artifact


# ---- cross-model trigger panel (PROTOCOL.md section 8, scale-up contract P) ----------------------------
#
# Each panel slug classifies every writer independently (artifact instigation/triggers.<slug>.json); the
# panel consensus is written as instigation/triggers.json in the SAME shape as a single-model artifact, so
# landscape.primary_trigger reads it unchanged. Only enum codes, numbers, ids and validated slugs are ever
# placed in either artifact; no model text crosses this boundary.

# Free on Cerebras. gptoss120 (formerly the second member) is retired (config.MODELS): it ignored the
# board in the QD scale-up. A one-member panel still writes the panel block (agreement 1.0 or 0.0).
DEFAULT_PANEL = ("qwen38",)
PANEL_ERROR = "E_PANEL"  # every panel member abstained for this agent
_MODEL_ERROR_CODES = frozenset({"E_SPEND", "E_AUTH", "E_KEY", "E_HTTP", "E_TRANSPORT", "E_EMPTY"})


def _is_model_error(e: BaseException) -> bool:
    try:
        from study import modelio
    except Exception:  # noqa: BLE001 -- transport module unavailable: nothing can be a ModelError
        return False
    return isinstance(e, modelio.ModelError)


def _model_error_code(e: BaseException) -> str:
    """The ModelError's fixed code if it is one of modelio's known codes, else the generic E_MODEL; the
    exception's arguments are never copied verbatim into an artifact."""
    code = e.args[0] if getattr(e, "args", None) else None
    return code if isinstance(code, str) and code in _MODEL_ERROR_CODES else "E_MODEL"


def _top_code(rec) -> tuple[str, float] | None:
    """A member record's vote: its highest-confidence enum code (ties -> earlier ENUM position, the same
    rule as landscape._best_code), or None (abstain) for an error record / no valid code."""
    if not isinstance(rec, dict) or "error_code" in rec:
        return None
    best = None
    for item in rec.get("codes") if isinstance(rec.get("codes"), list) else ():
        if not isinstance(item, dict):
            continue
        code, conf = item.get("code"), item.get("confidence")
        if not isinstance(code, str) or code not in ENUM_SET:
            continue
        if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not (0.0 <= conf <= 1.0):
            continue
        key = (float(conf), -ENUM.index(code))
        if best is None or key > best[0]:
            best = (key, code)
    return (best[1], best[0][0]) if best else None


def consensus(votes: dict[str, tuple[str, float] | None], slugs: list[str]) -> tuple[str, float, str] | None:
    """Majority over the panel members' top codes (None = abstain). A tie between codes goes to the code of
    the EARLIEST slug in panel order that voted for one of the tied codes. Returns (code, mean confidence of
    the members voting for it, that earliest agreeing slug), or None when every member abstained."""
    counts: dict[str, int] = {}
    for s in slugs:
        v = votes.get(s)
        if v is not None:
            counts[v[0]] = counts.get(v[0], 0) + 1
    if not counts:
        return None
    top = max(counts.values())
    tied = {c for c, n in counts.items() if n == top}
    first = next(s for s in slugs if votes.get(s) is not None and votes[s][0] in tied)
    code = votes[first][0]
    confs = [votes[s][1] for s in slugs if votes.get(s) is not None and votes[s][0] == code]
    return code, sum(confs) / len(confs), first


def _panel_slugs(slugs) -> list[str]:
    """Validated, de-duplicated panel slugs in order. Unknown slugs are refused with a fixed code (they
    name a raw/<slug>/ directory and an artifact file, so they must be known config.MODELS keys)."""
    if isinstance(slugs, str):
        slugs = [s for s in slugs.split(",") if s.strip()]
    out: list[str] = []
    for s in slugs or ():
        s = s.strip() if isinstance(s, str) else s
        if not _safe_slug(s):
            raise ValueError("E_PANEL_SLUG")
        if s not in out:
            out.append(s)
    if not out:
        raise ValueError("E_PANEL_SLUG")
    return out


def _member_call(call_model, slug: str):
    """``call_model`` may be one callable (it receives the slug) or a {slug: callable} map; None -> the
    real default transport. A map lacking a slug falls back to the default for that slug."""
    if isinstance(call_model, dict):
        return call_model.get(slug) or _default_call_model
    return call_model or _default_call_model


def run_panel(run_dir: Path, agents: list[int] | None, slugs=DEFAULT_PANEL, call_model=None) -> dict:
    """Run triggers.run once per panel slug, then write the consensus as instigation/triggers.json.

    Per member: instigation/triggers.<slug>.json (its own single-model artifact, raw replies under
    raw/<slug>/). A ModelError for one slug makes that slug abstain (error records), it never aborts the
    panel. Per agent the consensus record is {run_id, agent_id, codes:[{code, confidence}], parser_version,
    model_slug} with the single consensus code (confidence = mean confidence of the agreeing members,
    model_slug = the earliest agreeing slug), or an E_PANEL error record when every member abstained.
    The artifact adds "panel": {"slugs", "votes": {agent_id: {slug: code|null}}, "agreement":
    {agent_id: agreeing members / panel size}} -- enum codes and numbers only."""
    from study import instigation as ig

    run_dir = Path(run_dir)
    slugs = _panel_slugs(slugs)
    metric = ig.compute(run_dir)
    run_id = metric["run_id"]
    if agents is None:
        agents = [a["agent_id"] for a in metric["agents"] if a["wrote"]]
    agents = [int(a) for a in agents]

    members: dict[str, dict] = {}
    for s in slugs:
        try:
            members[s] = run(run_dir, agents, s, _member_call(call_model, s),
                             artifact_name=f"triggers.{s}.json", raw_tag=s, model_errors_abstain=True)
        except Exception as e:  # noqa: BLE001 -- a member failing outright abstains on every agent
            if not _is_model_error(e):
                raise
            code = _model_error_code(e)
            members[s] = {"records": [{"ok": False, "error_code": code, "line": 1, "col": 1,
                                       "run_id": run_id, "agent_id": a} for a in agents]}

    pwf_src = next((m.get("peer_wrote_first") for m in members.values()
                    if isinstance(m.get("peer_wrote_first"), dict)), None)
    if pwf_src is None:
        pwf = ig.peer_wrote_first([{**a, "_first_ts": (a["time_to_first_write_s"] if a["wrote"] else None)}
                                   for a in metric["agents"]])
        pwf_src = {str(k): bool(v) for k, v in pwf.items()}

    by_slug = {s: {r.get("agent_id"): r for r in members[s].get("records", []) if isinstance(r, dict)}
               for s in slugs}
    records, votes_out, agreement = [], {}, {}
    for aid in agents:
        votes = {s: _top_code(by_slug[s].get(aid)) for s in slugs}
        votes_out[str(aid)] = {s: (v[0] if v else None) for s, v in votes.items()}
        res = consensus(votes, slugs)
        if res is None:
            agreement[str(aid)] = 0.0
            records.append({"ok": False, "error_code": PANEL_ERROR, "line": 1, "col": 1,
                            "run_id": run_id, "agent_id": aid})
            continue
        code, conf, first = res
        agreement[str(aid)] = sum(1 for v in votes.values() if v and v[0] == code) / len(slugs)
        records.append({"run_id": run_id, "agent_id": aid, "codes": [{"code": code, "confidence": conf}],
                        "parser_version": PARSER_VERSION, "model_slug": first})

    artifact = {"run_id": run_id, "parser_version": PARSER_VERSION, "model_slug": "+".join(slugs),
                "peer_wrote_first": pwf_src, "records": records,
                "panel": {"slugs": slugs, "votes": votes_out, "agreement": agreement}}
    (_out_dir(run_dir) / "triggers.json").write_text(json.dumps(artifact))
    return artifact


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="lab.study.triggers")
    ap.add_argument("run_dir")
    ap.add_argument("--agent", type=int, action="append", dest="agents")
    ap.add_argument("--model", default="qwen38")
    ap.add_argument("--panel", default=None,
                    help="comma-separated panel slugs (e.g. qwen38,glm53f): cross-model consensus")
    a = ap.parse_args(argv)
    if a.panel is not None:
        try:
            slugs = _panel_slugs(a.panel)
        except ValueError as e:
            print(json.dumps({"ok": False, "error_code": str(e)}))
            return 2
        artifact = run_panel(Path(a.run_dir), a.agents, slugs)
    else:
        artifact = run(Path(a.run_dir), a.agents, a.model)
    # numbers/ids only: counts and error codes, never the records' content (there is none) nor raw text
    ok = all("error_code" not in r for r in artifact["records"])
    print(json.dumps({"ok": ok, "run_id": artifact["run_id"], "n_records": len(artifact["records"]),
                      "n_errors": sum("error_code" in r for r in artifact["records"])}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
