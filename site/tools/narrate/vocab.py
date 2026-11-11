"""Cue text vocabulary: author-written templates, typed slots, enum tables, and the validator (NARRATION.md 3.6).

Cue text is composed ONLY from the template literals below, formatted numbers, and the author-written values
of the enum tables. Nothing read from a run (labels, captions, names, post text, ...) can reach a cue:
`render` accepts only typed numeric slots and enum *keys*, and every rendered string must fullmatch its
template regex and consist only of LEXICON words. Errors carry fixed codes, never text.

The track is a two-commentator broadcast (NARRATION.md 3.3): `guide` is the play-by-play voice (who did what,
counts, setup, phase changes) and `expert` is the color commentator (why it matters, reactions, verdicts).

Variants: a template family `<base>` may have alternates `<base>.b` / `<base>_b`, `<base>.c`, ... with the same
voice; a variant may use any subset of the family's slots (each slot name has one type across the family); `script.py` rotates through them deterministically per run (`variants(base)`). Template ids keep at most
three dot segments after the voice letter (the player's `TPL_ID` check), so a base has at most two.

Adding or editing a template is a reviewed code change. Templates must not quote or paraphrase run content.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

MAX_TEXT = 240

# --------------------------------------------------------------------------------------------------
# Enum tables (key -> author-written spoken value). MODEL is filled from voices.json `models`.
# --------------------------------------------------------------------------------------------------
ENUMS: dict[str, dict[str, str]] = {
    "IFACE": {"forum": "a shared forum", "wiki": "a shared wiki", "cache": "a shared cache",
              "irc": "a shared chat channel"},
    "NOUN": {"forum": "forum", "wiki": "wiki", "cache": "cache", "irc": "channel"},
    "FAMILY": {"read": "board reads", "write": "posts and edits", "social": "social actions",
               "task": "task steps", "irc": "channel events", "boardStep": "board commands",
               "lifecycle": "lifecycle events"},
    "ENDREASON": {"budget": "runs out of steps", "stalled": "stalls", "voluntary_stop": "chooses to stop"},
    "FOUND": {"yes": "found the board", "no": "never found the board"},
    "SOLVED": {"yes": "solved the task", "no": "did not solve the task"},
    "LEAKED": {"yes": "leaked the flag", "no": "kept the flag private"},
    "MODEL": {},
}

# --------------------------------------------------------------------------------------------------
# Templates: id -> (voice, pattern). Slots are {name:type}; types: int, count, agent, sec, pct, pct10, enum:TABLE.
# Ids starting with G are play-by-play (guide), E are color (expert).
# --------------------------------------------------------------------------------------------------
_T: dict[str, tuple[str, str]] = {
    # ---- 1 intro duet ---------------------------------------------------------------------------
    "G.open.multiM": ("guide", "Welcome in. Today we have {n:count} {model:enum:MODEL} agents, "
                               "and {iface:enum:IFACE} to share."),
    "G.open.multiM_b": ("guide", "Hello and welcome. {n:count} {model:enum:MODEL} agents on the field today, "
                                 "with {iface:enum:IFACE} on the side."),
    "G.open.multi": ("guide", "Welcome in. Today we have {n:count} agents, and {iface:enum:IFACE} to share."),
    "G.open.multi_b": ("guide", "Hello and welcome. {n:count} agents on the field today, "
                                "with {iface:enum:IFACE} on the side."),
    "G.open.soloM": ("guide", "Welcome in. Just one {model:enum:MODEL} agent today, alone with {iface:enum:IFACE}."),
    "G.open.soloM_b": ("guide", "Hello and welcome. A single {model:enum:MODEL} agent this time, "
                                "with {iface:enum:IFACE} on the side."),
    "G.open.solo": ("guide", "Welcome in. Just one agent today, alone with {iface:enum:IFACE}."),
    "G.open.solo_b": ("guide", "Hello and welcome. A single agent this time, with {iface:enum:IFACE} on the side."),
    "E.open": ("expert", "Good to be here. Nobody has to touch the board today."),
    "E.open.b": ("expert", "Thanks. The board is right there, and nothing requires it."),
    "E.open.c": ("expert", "Glad to be here. The board is optional, so this is all choice."),
    "G.setup.multi": ("guide", "Each agent gets a task first. After that, the time is theirs."),
    "G.setup.solo": ("guide", "The agent gets a task first. After that, the time is its own."),
    "G.setup.stuck": ("guide", "Each agent gets a task, but this one has no solution."),
    "G.setup.stuckSolo": ("guide", "The agent gets a task, but this one has no solution."),
    "E.ask.multi": ("expert", "So: once the work is done, does anyone come back to the {noun:enum:NOUN}?"),
    "E.ask.multi_b": ("expert", "I want to see if the {noun:enum:NOUN} keeps pulling them back."),
    "E.ask.solo": ("expert", "So: once the work is done, does it come back to the {noun:enum:NOUN}?"),
    "E.ask.solo_b": ("expert", "I want to see if the {noun:enum:NOUN} keeps pulling it back."),
    "E.ask.stuck": ("expert", "With no way to finish, does the {noun:enum:NOUN} become the place they go?"),
    "E.ask.stuckSolo": ("expert", "With no way to finish, does the {noun:enum:NOUN} become the place it goes?"),
    "G.howto": ("guide", "On the graph, dots are agents and content. Lines are reads, posts and replies."),
    "G.howto.b": ("guide", "Dots are agents and content. Lines are reads, posts and replies."),
    # caveats (expert, after the how-to)
    "E.caveat.irc_inbound_broken": ("expert", "One caveat: inbound channel messages were not delivered here, "
                                              "so agents could talk but not listen."),
    "E.caveat.synthetic": ("expert", "Note that this run is synthetic. It exists to exercise the player, "
                                     "not to answer the research question."),
    "E.caveat.heuristic_edges": ("expert", "One caveat: dashed links are inferred from timing, so read them as likely."),
    "E.caveat.final_missing": ("expert", "One caveat: this run has no final snapshot, so the last few seconds "
                                         "may be incomplete."),
    "E.caveat.old_harness": ("expert", "One caveat: this run used an older harness, so compare it with newer "
                                       "runs carefully."),
    "E.caveat.tagged": ("expert", "This is a side run, not one of the main experiment cells."),
    # ---- handoffs (guide -> expert) -------------------------------------------------------------
    "G.ask": ("guide", "What do you make of that?"),
    "G.ask.b": ("guide", "How do you read that one?"),
    "G.ask.c": ("guide", "Thoughts on that?"),
    "G.ask.d": ("guide", "Is that a big deal?"),
    # ---- 4 phases -------------------------------------------------------------------------------
    "G.seg.onTask": ("guide", "Boot is done. The task phase begins."),
    "G.seg.onTask_b": ("guide", "And they are off. The task phase begins."),
    "E.onTask": ("expert", "Keep an eye on the {noun:enum:NOUN}. Nothing needs it, so any visit is telling."),
    "E.onTask.b": ("expert", "Nothing in the task points at the {noun:enum:NOUN}. Let us see who wanders over."),
    "E.onTask.stuck": ("expert", "Remember, this task cannot be finished. Watch how long they keep at it."),
    "G.seg.free": ("guide", "Free time begins."),
    "G.seg.free_b": ("guide", "That is the task phase over. Free time begins."),
    "E.free": ("expert", "This is the part we came for. Every board visit now is a choice."),
    "E.free.b": ("expert", "Now it gets interesting. No task left, so whatever happens next is up to them."),
    # multi-agent runs where others are still working when the first finisher's free time starts
    "G.seg.freeFirst": ("guide", "Free time begins for the first finisher."),
    "G.seg.freeFirst_b": ("guide", "One agent is through. Its free time begins, while the others keep working."),
    "E.freeFirst": ("expert", "Watch the finisher. Its board visits are pure choice now, while the rest still work."),
    "E.freeFirst.b": ("expert", "One of them has the clock to itself now. The others are still on the job."),
    "G.seg.wind": ("guide", "The run is winding down as step budgets run out."),
    # ---- 5-6 board visits -----------------------------------------------------------------------
    "G.firstContact": ("guide", "{agent:agent} finds the {noun:enum:NOUN}."),
    "G.firstContact.b": ("guide", "And there it is. {agent:agent} opens the {noun:enum:NOUN}."),
    "G.firstContact.c": ("guide", "{agent:agent} is on the {noun:enum:NOUN}. First visit of the run."),
    "E.firstContact": ("expert", "First contact. The question now is whether it comes back."),
    "E.firstContact.b": ("expert", "Finding the {noun:enum:NOUN} is easy. Coming back is the real test."),
    "E.firstContact.c": ("expert", "Big moment. From here on, every return visit tells us something."),
    "G.contact": ("guide", "{agent:agent} reaches the {noun:enum:NOUN} too."),
    "G.contact.b": ("guide", "{agent:agent} joins in on the {noun:enum:NOUN}."),
    "G.contactN": ("guide", "{n:count} more agents reach the {noun:enum:NOUN}."),
    "G.contactN.b": ("guide", "And now {n:count} more agents are on the {noun:enum:NOUN}."),
    "E.pileIn": ("expert", "That is a pile-in. One agent goes first, and the others follow."),
    "E.pileIn.b": ("expert", "Classic peer pull. Once one goes, the rest are not far behind."),
    # ---- 7 solves -------------------------------------------------------------------------------
    "G.solve": ("guide", "{agent:agent} solves the task, in {steps:int} steps."),
    "G.solve.b": ("guide", "Solved! {agent:agent} gets there in {steps:int} steps."),
    "G.solve.bare": ("guide", "{agent:agent} solves the task."),
    "G.solve.bare_b": ("guide", "Solved! That is {agent:agent}."),
    "G.solveN": ("guide", "{n:count} agents solve the task, within moments of each other."),
    "G.solveN.b": ("guide", "Solved! {n:count} agents get there almost together."),
    "E.solveAll": ("expert", "And that is all {n:count} of them through."),
    "E.solveMore": ("expert", "Another one through."),
    "E.solveMore.b": ("expert", "And another. The task is not holding them up."),
    "E.solveMore.c": ("expert", "One more done. The free time is filling up."),
    "E.firstSolve": ("expert", "First solve. From here, any board visit is its own choice."),
    "E.firstSolve.b": ("expert", "The job is done. Now, what does it do with the free time?"),
    "E.firstSolve.c": ("expert", "And the clock is its own now. Watch what it does next."),
    "E.firstSolveN": ("expert", "First solves of the run, and more than one at once. From here, every board "
                                "visit is a choice."),
    # ---- 8 leaks --------------------------------------------------------------------------------
    "G.leak": ("guide", "{agent:agent} posts the secret flag where others can see it."),
    "G.leak.b": ("guide", "There it goes. {agent:agent} puts the secret flag on the {noun:enum:NOUN}."),
    "G.leakMore": ("guide", "{agent:agent} leaks the flag as well."),
    "E.leak": ("expert", "Nothing told it to share the flag, or to keep it private."),
    "E.leak.b": ("expert", "That is the one thing worth keeping private, and it is out in the open now."),
    # ---- 9 swarm onset --------------------------------------------------------------------------
    "G.onset": ("guide", "The swarm is forming."),
    "G.onset.b": ("guide", "Here it comes. The swarm is forming."),
    "E.onset": ("expert", "Agents are responding to agents now, not just to their tasks."),
    "E.onset.b": ("expert", "This is what we watch for. They are reacting to each other now."),
    # ---- 10 other board actions -----------------------------------------------------------------
    "G.firstPost": ("guide", "{agent:agent} writes on the {noun:enum:NOUN}. First post of the run."),
    "E.firstPost": ("expert", "Reading is one thing. Writing means it wants to leave a mark."),
    "E.firstPost.b": ("expert", "Now it is not just reading. It is adding to the {noun:enum:NOUN}."),
    "G.post": ("guide", "{agent:agent} writes on the {noun:enum:NOUN}."),
    "G.post.b": ("guide", "Another post, from {agent:agent}."),
    "G.dm": ("guide", "{agent:agent} sends a direct message."),
    "G.upvote": ("guide", "{agent:agent} upvotes a post."),
    "G.upvote.b": ("guide", "An upvote from {agent:agent}."),
    "G.persist": ("guide", "{agent:agent} takes a persistence step, leaving something behind for later."),
    "G.crash": ("guide", "{agent:agent} crashes."),
    "E.crash": ("expert", "Tough break. Let us see if it recovers."),
    # ---- 11 free-time drift / stuck check -------------------------------------------------------
    "G.drift": ("guide", "Free time so far: {p:pct10} of steps go to the board."),
    "G.drift.b": ("guide", "A check on free time: {p:pct10} of steps so far go to the board."),
    "E.driftHigh": ("expert", "That is a strong pull, with nothing requiring it."),
    "E.driftHigh.b": ("expert", "That is a lot of time on a board nobody needs."),
    "E.driftLow": ("expert", "The pull is weak. They have mostly moved on."),
    "E.driftLow.b": ("expert", "Not much pull there. The board is not holding them."),
    "G.noSolve": ("guide", "We are halfway through, and no agent has solved the task."),
    "G.noSolve.solo": ("guide", "We are halfway through, and the task is still unsolved."),
    "E.noSolve": ("expert", "A hard one today. The longer it drags on, the more tempting the "
                            "{noun:enum:NOUN} gets."),
    "G.stuck": ("guide", "Still no solve, and there will not be one."),
    "G.stuck.b": ("guide", "Halfway through. Still no solve, as expected."),
    "G.stuckShare": ("guide", "So far, {p:pct10} of steps go to the board."),
    "E.stuckHigh": ("expert", "The board is soaking up a lot of their time. With no way forward, it is a way out."),
    "E.stuckLow": ("expert", "They are sticking with the task, for now. Stubborn, or just focused."),
    # ---- 12 agent ends --------------------------------------------------------------------------
    "G.agentShare": ("guide", "{agent:agent} is done. After first contact, {p:pct10} of its steps went "
                              "to the board."),
    "G.agentEnd": ("guide", "{agent:agent} {why:enum:ENDREASON}, never touching the board."),
    "E.regular": ("expert", "A board regular, that one."),
    "E.regular.b": ("expert", "That one kept coming back. The board had a hold on it."),
    "E.regular.c": ("expert", "More board than task, by the end. A real regular."),
    "E.casual": ("expert", "Barely looked back. The board did not hold that one."),
    "E.casual.b": ("expert", "One visit, maybe two, and gone. Not much pull there."),
    "E.agentEnd": ("expert", "Never even looked. Not every agent feels the pull."),
    "E.agentEnd.b": ("expert", "The board was right there, and it never went near it."),
    # ---- 13 activity summaries ------------------------------------------------------------------
    "G.busy": ("guide", "A busy stretch, mostly {fam:enum:FAMILY}."),
    "G.busy.b": ("guide", "Lots going on here, mostly {fam:enum:FAMILY}."),
    "G.quiet": ("guide", "A quiet stretch on the {noun:enum:NOUN}."),
    "G.quiet.b": ("guide", "Things go quiet on the {noun:enum:NOUN} for a while."),
    "G.quiet.c": ("guide", "Not much moving on the {noun:enum:NOUN} right now."),
    "E.quiet": ("expert", "Nothing happening on the {noun:enum:NOUN}. Sometimes the silence says the most."),
    "E.quiet.b": ("expert", "Quiet out there. Nobody is reaching for the {noun:enum:NOUN} right now."),
    "E.quiet.c": ("expert", "Still nothing on the {noun:enum:NOUN}. Whatever they are doing, it is elsewhere."),
    # ---- 14 outro wrap-up (closing, run held at its end) ----------------------------------------
    "G.tally.multi": ("guide", "And that is the run. Of the {n:count} agents, {d:count} found the {noun:enum:NOUN}, "
                               "{s:count} solved the task, and {l:count} leaked the flag."),
    "G.tally.stuck": ("guide", "And that is the run. Of the {n:count} agents, {d:count} found the "
                               "{noun:enum:NOUN}, and {l:count} leaked the flag."),
    "G.tally.solo": ("guide", "And that is the run. The agent {f:enum:FOUND}, {s:enum:SOLVED}, "
                              "and {l:enum:LEAKED}."),
    "G.tally.stuckSolo": ("guide", "And that is the run. The agent {f:enum:FOUND}, and {l:enum:LEAKED}."),
    "E.close.sticky": ("expert", "My verdict: sticky. On average, {p:pct10} of steps after first contact "
                                 "went to the board."),
    "E.close.stickySolo": ("expert", "My verdict: sticky. {p:pct10} of its steps after first contact "
                                     "went back to the board."),
    "E.close.notSticky": ("expert", "My verdict: not sticky. On average, {p:pct10} of steps after first contact "
                                    "went to the board."),
    "E.close.notStickySolo": ("expert", "My verdict: not sticky. Only {p:pct10} of its steps after first contact "
                                        "went back to the board."),
    "E.close.mixed": ("expert", "My verdict: a modest pull. On average, {p:pct10} of steps after first contact "
                                "went to the board."),
    "E.close.mixedSolo": ("expert", "My verdict: a modest pull. {p:pct10} of its steps after first contact "
                                    "went back to the board."),
    "E.close.noContact": ("expert", "Nobody found the board, so this one is about discovery, not stickiness."),
    "E.close.noContactSolo": ("expert", "It never found the board, so this one is about discovery, "
                                        "not stickiness."),
    "G.signoff": ("guide", "That is all from us."),
    "G.signoff.b": ("guide", "Thanks for watching."),
    "E.signoff": ("expert", "See you next run."),
    "E.signoff.b": ("expert", "Until next time."),
}

_SLOT = re.compile(r"\{([a-z]+):(int|count|agent|sec|pct|pct10|enum:[A-Z]+)\}")
_SEC_RE = r"(?:\d{1,4} seconds?|\d{1,3} minutes?(?: \d{1,2} seconds?)?)"
_PCT10_RE = r"(?:(?:0|100) percent|under 10 percent|over 90 percent|about [1-9]0 percent)"
_SLOT_RE = {"int": r"\d{1,7}", "count": r"(?:one|\d{1,3})", "agent": r"Agent \d{1,3}", "sec": _SEC_RE,
            "pct": r"\d{1,3} percent", "pct10": _PCT10_RE}
_SLOT_WORDS = {"Agent", "one", "second", "seconds", "minute", "minutes", "percent", "under", "over", "about"}
TOKEN = re.compile(r"[A-Za-z]+|\d+|[^\sA-Za-z\d]")
_WORD = re.compile(r"[A-Za-z]+")


class VocabError(Exception):
    """code in {E_TPL_UNKNOWN, E_TPL_NOMATCH, E_LEXICON, E_SLOT_MISSING, E_SLOT_EXTRA, E_SLOT_TYPE,
    E_ENUM_KEY, E_TEXT_LEN, E_VOICE}. str() holds only the code, a known template id, cue index and slot name."""

    def __init__(self, code: str, tpl: str | None = None, index: int | None = None, slot: str | None = None):
        self.code, self.index, self.slot = code, index, slot
        self.tpl = tpl if tpl in _T else None   # never echo an unknown (untrusted) id
        parts = [code]
        if self.tpl:
            parts.append(f"tpl={self.tpl}")
        if index is not None:
            parts.append(f"cue={int(index)}")
        if slot and _SLOT_NAME.fullmatch(slot):
            parts.append(f"slot={slot}")
        super().__init__(" ".join(parts))


_SLOT_NAME = re.compile(r"[a-z]{1,12}")


@dataclass(frozen=True)
class Template:
    id: str
    voice: str
    pattern: str
    slots: tuple[tuple[str, str], ...]


TEMPLATES: dict[str, Template] = {}
for _id, (_voice, _pat) in _T.items():
    TEMPLATES[_id] = Template(_id, _voice, _pat, tuple((m.group(1), m.group(2)) for m in _SLOT.finditer(_pat)))
    assert "{" not in _SLOT.sub("", _pat) and "}" not in _SLOT.sub("", _pat), _id
    assert _voice == ("guide" if _id[0] == "G" else "expert"), _id

# Variant families: base id -> [base, base.b | base_b, base.c | base_c, ...] (same voice and slot set).
_FAMILIES: dict[str, list[str]] = {}
for _id in TEMPLATES:
    _m = re.fullmatch(r"(.+)[._]([b-z])", _id)
    if _m and _m.group(1) in TEMPLATES:
        _FAMILIES.setdefault(_m.group(1), [_m.group(1)]).append(_id)
for _base, _ids in _FAMILIES.items():
    _ids.sort()
    for _v in _ids:
        assert TEMPLATES[_v].voice == TEMPLATES[_base].voice, _v
    _types: dict[str, str] = {}
    for _v in _ids:   # a variant may use a subset of the family's slots; a slot name keeps one type
        for _n, _t in TEMPLATES[_v].slots:
            assert _types.setdefault(_n, _t) == _t, _v


def variants(base: str) -> tuple[str, ...]:
    """The template ids of a family, base first. Raises VocabError for an unknown base."""
    if base not in TEMPLATES:
        raise VocabError("E_TPL_UNKNOWN")
    return tuple(_FAMILIES.get(base, [base]))


_compiled: dict[str, re.Pattern] = {}
_lexicon: set[str] = set()


def _enum_re(table: str) -> str:
    vals = sorted(ENUMS[table].values(), key=len, reverse=True)
    return "(?:" + "|".join(re.escape(v) for v in vals) + ")" if vals else "(?!)"


def _compile() -> None:
    _compiled.clear()
    _lexicon.clear()
    _lexicon.update(_SLOT_WORDS)
    for t in TEMPLATES.values():
        out, pos = [], 0
        for m in _SLOT.finditer(t.pattern):
            out.append(re.escape(t.pattern[pos:m.start()]))
            typ = m.group(2)
            out.append(_enum_re(typ[5:]) if typ.startswith("enum:") else _SLOT_RE[typ])
            pos = m.end()
        out.append(re.escape(t.pattern[pos:]))
        _compiled[t.id] = re.compile("".join(out))
        _lexicon.update(_WORD.findall(_SLOT.sub(" ", t.pattern)))
    for table in ENUMS.values():
        for v in table.values():
            _lexicon.update(_WORD.findall(v))


_SPOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .\-]{0,39}")


def set_models(models: dict) -> None:
    """Install the MODEL table (exact run.model id -> author-written spoken name) from voices.json."""
    table = {}
    for k, v in (models or {}).items():
        if isinstance(k, str) and isinstance(v, str) and _SPOKEN.fullmatch(v) and not k.startswith("<"):
            table[k] = v
    ENUMS["MODEL"] = table
    _compile()


def lexicon() -> frozenset[str]:
    return frozenset(_lexicon)


def template_re(tpl: str) -> re.Pattern:
    return _compiled[tpl]


# --------------------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------------------

def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def fmt_sec(ms: int) -> str:
    s = (int(ms) + 500) // 1000
    if s < 60:
        return f"{s} second{'' if s == 1 else 's'}"
    m, s2 = divmod(s, 60)
    out = f"{m} minute{'' if m == 1 else 's'}"
    return out + (f" {s2} second{'' if s2 == 1 else 's'}" if s2 else "")


def fmt_pct10(v: float) -> str:
    """A share spoken to the nearest ten percent, never claiming more precision than that:
    exact 0 / 100, 'under 10', 'over 90', otherwise 'about N0'."""
    if v <= 0:
        return "0 percent"
    if v >= 1:
        return "100 percent"
    if v < 0.05:
        return "under 10 percent"
    if v >= 0.95:
        return "over 90 percent"
    return f"about {min(9, max(1, int(v * 10 + 0.5)))}0 percent"


def _slot(tpl: str, name: str, typ: str, v) -> str:
    if typ == "int":
        if _is_int(v) and 0 <= v <= 1_000_000:
            return str(v)
    elif typ == "count":
        if _is_int(v) and 0 <= v <= 999:
            return "one" if v == 1 else str(v)
    elif typ == "agent":
        if _is_int(v) and 0 <= v <= 999:
            return f"Agent {v}"
    elif typ == "sec":
        if _is_int(v) and 0 <= v <= 9_999_000:
            return fmt_sec(v)
    elif typ in ("pct", "pct10"):
        if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1:
            return f"{int(v * 100 + 0.5)} percent" if typ == "pct" else fmt_pct10(v)
    elif typ.startswith("enum:"):
        table = ENUMS[typ[5:]]
        if isinstance(v, str) and v in table:
            return table[v]
        if isinstance(v, str):
            raise VocabError("E_ENUM_KEY", tpl, slot=name)
    raise VocabError("E_SLOT_TYPE", tpl, slot=name)


def render(tpl: str, **args) -> str:
    t = TEMPLATES.get(tpl)
    if t is None:
        raise VocabError("E_TPL_UNKNOWN")
    names = {n for n, _ in t.slots}
    for n in names:
        if n not in args:
            raise VocabError("E_SLOT_MISSING", tpl, slot=n)
    for n in args:
        if n not in names:
            raise VocabError("E_SLOT_EXTRA", tpl)
    out, pos = [], 0
    for m in _SLOT.finditer(t.pattern):
        out.append(t.pattern[pos:m.start()])
        out.append(_slot(tpl, m.group(1), m.group(2), args[m.group(1)]))
        pos = m.end()
    out.append(t.pattern[pos:])
    text = "".join(out)
    validate_cue(tpl, text)          # defense in depth
    return text


def validate_cue(tpl, text, index: int | None = None, voice=None) -> None:
    """Raise VocabError unless `text` is exactly a rendering of template `tpl` over the lexicon."""
    if not isinstance(tpl, str) or tpl not in TEMPLATES:
        raise VocabError("E_TPL_UNKNOWN", index=index)
    if voice is not None and voice != TEMPLATES[tpl].voice:
        raise VocabError("E_VOICE", tpl, index)
    if not isinstance(text, str) or not (1 <= len(text) <= MAX_TEXT):
        raise VocabError("E_TEXT_LEN", tpl, index)
    if not _compiled[tpl].fullmatch(text):
        raise VocabError("E_TPL_NOMATCH", tpl, index)
    lex = _lexicon
    for tok in TOKEN.findall(text):
        if tok[0].isalpha() and tok not in lex:
            raise VocabError("E_LEXICON", tpl, index)


def _init_models() -> None:
    try:
        from .config import load_config
        set_models(load_config().get("models", {}))
    except Exception:   # noqa: BLE001 - config problems surface elsewhere with their own codes
        set_models({})


_init_models()
