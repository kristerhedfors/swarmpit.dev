"""Instigation study (PROTOCOL.md): structural, content-free metrics and the model-only Q1 parser driver.

Nothing in this package reads board/chatter TEXT into a coding agent's context. The metric tools read
only structural keys (timestamps, port-index agent ids, fixed-enum kinds, opaque post/thread ids, and
integer byte/char lengths) and emit numbers, fixed-enum codes and opaque ids. Message generation and any
message parsing/scoring are performed only by eval models invoked from scripts.
"""
