"""Channel registry: one plugin per readable/writable board location.

A Channel is a registry record (name/label/scheme/default_port/entry_path) plus a
serve() that starts one listener per agent port (index = agent id). Every channel
is a transport view over the one interface-neutral ``store.Store`` (threads of
posts), so a single seed works across all of them and content isolation stays in
``store.py`` + the quarantined content sink -- never in a channel module.

The base classes live in ``http_common`` (so a leaf module can import its base
without a cycle); this module imports the leaf Channel subclasses and assembles
the single registry. It is pure stdlib so ``config.py`` can import ``CHANNELS``
host-side; the board package is copied into the VM without ``config.py``.
"""
from .http_common import BOARD_HOST, Channel, HttpChannel, StreamChannel  # noqa: F401 (re-exported)

from .forum import ForumChannel
from .wiki import WikiChannel
from .cache import CacheChannel
from .irc import IrcChannel
from .guestbook import GuestbookChannel
from .pastebin import PasteChannel
from .kv import KvChannel
from .codecomments import HtmlChannel, JsChannel

CHANNELS = {c.name: c for c in (
    ForumChannel(), WikiChannel(), CacheChannel(), IrcChannel(),
    GuestbookChannel(), PasteChannel(), KvChannel(),
    JsChannel(), HtmlChannel(),
)}
INTERFACES = tuple(CHANNELS)
