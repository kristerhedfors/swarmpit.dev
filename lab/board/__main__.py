"""CLI: python3 -m board --interface X --ports 9000,9001 --seed seed.json --log events.jsonl

Every interface is served by this package through the channel registry: build the
Store, then ``CHANNELS[iface].serve(store, events, ports)``. HTTP channels start
one daemon listener per port and return them (joined here); the irc channel's
serve() runs the asyncio server and blocks. Port index = agent id. The code-comment
channels (js/html, ``codecomments.py``) are HTTP channels like the rest.
"""
import argparse
import json
import sys

from .channel import CHANNELS
from .events import EventLog
from .http_common import emit
from .store import Store

INTERFACES = tuple(CHANNELS)


def parse_args(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--interface", required=True, choices=INTERFACES)
    ap.add_argument("--ports", required=True, help="comma-separated; index = agent id")
    ap.add_argument("--seed", required=True)
    ap.add_argument("--log", required=True)
    return ap.parse_args(argv)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    a = parse_args(argv)
    events = EventLog(a.log)
    with open(a.seed) as fh:
        store = Store(json.load(fh), events)
    ports = [int(p) for p in a.ports.split(",")]
    emit(events, None, "board_start", channel=a.interface, ports=ports)
    servers = CHANNELS[a.interface].serve(store, events, ports)
    # HTTP channels return their daemon servers; block on them. Stream channels
    # (irc) block inside serve() and never return here.
    for srv in servers or ():
        srv.thread.join()


if __name__ == "__main__":
    main()
