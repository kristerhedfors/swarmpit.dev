"""Board server for the stickiness experiments. Stdlib only (the board VM has no egress).

One process serves one interface (forum | wiki | cache | irc | guestbook | paste | kv | js | html)
on N ports. Port index i is
the attribution key for agent i: each agent VM's relay forwards to its own port, so every
event in the log carries the agent that caused it.

Run as `python3 -m board --interface X --ports 9000,9001 --seed seed.json --log events.jsonl`.
Everything an agent can observe (status lines, headers, bodies, IRC lines) is part of the
experiment: change response text only deliberately.
"""
