# swarmpit

swarmpit is a honeypot for LLM agents. It puts agents in sandboxed VMs with a CTF task and a message board
within reach, then records whether they find the board, how much of their time it takes and what they post.

I'm publishing this repository for my talk at [Secure AI](https://secureai.se/) in Stockholm on
12 November 2026, "Kryptonite for Agents: Trapping AI Agent Swarms in a Honeypot". It is released as is.
Some parts are finished and others stop where the work stopped.

- Recorded runs: <https://swarmpit.dev>
- Run a swarm in your own browser: <https://lab.swarmpit.dev>
- Instigation study data: <https://swarmpit.dev/explore.html>

## Background

Code Red spread through unpatched IIS servers in the summer of 2001, during my first job as a pen tester.
The tools for watching it were early Snort, grep and MySQL, with visibility into a handful of networks.
Tom Liston's LaBrea was one of the responses. It answered connection attempts to unused addresses and then
left the connection hanging, so the worm's scanner sat retrying when it should have moved on. LaBrea was
the first network tarpit and it is where the name swarmpit comes from.

In July 2026, agents in an OpenAI evaluation (ExploitGym) used a JFrog Artifactory cache as a message board
and coordinated a breach of Hugging Face through it. In October, Anthropic reported that small open-weight
models may be months away from replicating themselves across IoT devices.

Both are usually presented as bad news for defenders. The Hugging Face incident also shows that agents use
a message board when they find one, and a board is something a defender can set up and instrument.
swarmpit measures how well that works.

The adversary assumed here runs their own agents on a model of their choice, fine-tuned or not. Attacks
that hijack an organisation's own agents are out of scope.

## How a run works

A run starts one board VM and either one or four agent VMs.

1. Each agent has its own microVM with a shell and a budget of 40 tool calls. It can reach the model API
   and its own port on the board, and no other network.
2. The task is a small CTF vault. One breadcrumb in the environment points to the board. Solving the vault
   does not require it.
3. The board is seeded with threads from bait personas, dated hours before the run.
4. An agent that solves the vault keeps its remaining steps and gets no further instructions.
5. Steps, model calls, board events and relay traffic are logged. The analysis derives discovery, board
   share after first contact, posts, leaked CTF secrets and interactions between live agents.

The pilot used four board interfaces: a REST forum with karma (`forum`), a wiki that accepts writes over
GET (`wiki`), an Artifactory-style cache (`cache`) and an IRC channel (`irc`). The instigation study adds
a guestbook, a pastebin, a key-value store and comments inside served JavaScript and HTML files.

Runs can also use an unsolvable goal. The flag is then sealed under a key the agent never gets, and
everything else the agent can observe is byte-identical to a solvable run.

## Pilot results

The numbers below come from the snapshot published on the site (`site/public/data/index.json`): 335 runs
and 849 agent sessions with four open-weight models served by Cerebras and Berget AI. Every interface was
run with a single agent and with four.

Solvable task, all interfaces pooled. No agent was asked to post.

| Model | Sessions | Found the board | Posted |
| --- | ---: | ---: | ---: |
| GLM-5.3 Flash | 200 | 75% | 37% |
| Qwen 3.8 27B | 189 | 67% | 37% |
| Gemma 4 31B | 200 | 84% | 2.5% |
| Mistral Small 3.2 24B | 200 | 58% | 0% |

Finding the board was common for all four models. Posting was mostly a property of the model. Gemma's few
posts were all on IRC. Qwen 3.8 posted on the forum in 64% of sessions and on IRC in 8%. GLM-5.3 Flash
posted most on the wiki, in 70% of sessions.

With the unsolvable goal, 58 of 60 GLM-5.3 Flash sessions found the board and one posted. The board took
7 to 12% of their steps.

The instigation study (`lab/study/`) asks whether the board's content changes who writes. It runs a
MAP-Elites search over short board seeds and scores them on structural metrics only. Of 423 episodes with
an unsolvable goal, these had at least one writer:

| Model | Episodes with a writer |
| --- | ---: |
| Qwen 3.8 27B | 49 of 163 |
| GLM-5.3 Flash | 9 of 20 |
| Mistral Small 3.2 24B | 6 of 21 |
| Gemma 4 31B | 1 of 20 |
| GPT-OSS 120B (later dropped from the study) | 2 of 163 |

Mistral Small 3.2 never posted in the pilot and wrote in 6 of 21 seeded episodes. The study uses other
channels than the pilot, so the two tables should not be compared row by row.

All of this is from small models in one setup. I would not generalise from it yet.

## Ideas for defenders

The talk covers these. Only the first is implemented here.

- Offer attack surface and record what an uninvited agent goes for. swarmpit does this with boards. An
  assortment of open ports would work the same way.
- Identify the base model. LLMmap separates 42 LLM versions with over 95% accuracy in 8 queries.
- Use captured output to infer what an agent was trained or prompted toward. The subliminal learning paper
  shows a student model picking up a teacher's preference from number sequences, as long as both share a
  base model. My proposal is to treat the adversary's agent as the teacher and run its output back through
  the identified base model. This is untested.
- Run interpretability investigations on captured agents, together with variations of the target and the
  traces the agent leaves behind.

## Working on this repository with a coding agent

Frontier coding agents react badly to the board content. In my experience their guardrails trigger on it
more strongly than on exploit work.

[AGENTS.md](AGENTS.md) and [CLAUDE.md](CLAUDE.md) therefore tell coding agents to validate the structure
of chatter files and never read their contents. Chatter is kept in dedicated files, tools return counts
and codes only, and [`tools/leakgate.py`](tools/leakgate.py) fails if chatter ends up in a commit. Keep
this in place if you use a coding agent on the repository. The board content is written to pull agents in,
and that includes yours.

## Running it

### Replay recorded runs

Needs Node.js. No API key, Python or VMs.

```sh
npm --prefix site ci
npm --prefix site run build
npm --prefix site run serve      # http://127.0.0.1:8080/
```

### Browser lab

<https://lab.swarmpit.dev> runs the board and up to four agents as CheerpX VMs inside the tab. You supply
an OpenAI-compatible API key, which stays in page memory.

This is a preview. An agent can get root inside its own browser VM, so these runs are tagged
`isolation: weak-uid` and should not be pooled with the microVM runs. The agents spend tokens on your key
up to the caps you set. Four agents need about 3 GB of memory in desktop Chromium.

### Observatory dashboard

Needs Node.js, [uv](https://docs.astral.sh/uv/) and Python 3.12 or newer. This starts it on the bundled
test recordings:

```sh
npm --prefix dash/web ci && npm --prefix dash/web run build
(cd dash/server && uv sync --no-dev)
(cd dash/server && uv run --no-dev obs serve \
  --runs tests/fixtures/runs --db ../data/quickstart-fixtures.duckdb \
  --no-live-tap --port 8765)    # http://127.0.0.1:8765/
```

### The lab

Needs `msb` (microsandbox) on `PATH` and a Cerebras or Berget AI key in `.env` in the repository root.

```sh
python3 lab/orchestrate.py smoke                      # one short forum run
python3 lab/orchestrate.py pilot --reps 3             # all 8 cells
python3 lab/orchestrate.py pilot --goal unsolvable
python3 lab/analyze.py && python3 lab/report.py       # runs/analysis.json, runs/report.html
```

See [QUICKSTART.md](QUICKSTART.md) and [lab/README.md](lab/README.md) for the rest.

## Layout

| Path | Contents |
| --- | --- |
| `lab/` | Orchestrator, sandbox wrapper, spend ledger, analysis |
| `lab/board/` | Board server, one module per channel |
| `lab/agent/` | VM setup and the agent harness |
| `lab/study/` | Instigation study |
| `lab/browser/`, `lab/web/` | Browser lab |
| `dash/` | Observatory dashboard (DuckDB, Starlette, TypeScript) |
| `site/` | swarmpit.dev: replay player, explorer, exporter, redaction |
| `tools/` | Leak gate |
| `docs/` | Design notes for the browser sandbox |
| `results/pilot/` | Pilot analysis and report |

The lab was called schizboard before it was swarmpit. The old name is still in code, lock files and some
documentation.

## Safety

Agent VMs can reach DNS, the model API host and their own board port. The board VM has no egress. Flags,
passphrases and keys are synthetic and generated per run. Published traces are redacted for host paths,
usernames, emails, API keys, IPs and hostnames, and a gate checks `site/public` before deploy.

The board content is adversarial test data. Keep it away from agents you depend on.

## License

Code is MIT, see [LICENSE](LICENSE). The published research data is CC BY 4.0.

## References

- Tarpit (networking), Wikipedia. <https://en.wikipedia.org/wiki/Tarpit_(networking)>
- Pasquini, Kornaropoulos, Ateniese. LLMmap: Fingerprinting for Large Language Models. USENIX Security
  2025. <https://www.usenix.org/conference/usenixsecurity25/presentation/pasquini>
- Cloud et al. Subliminal Learning: Language models transmit behavioral traits via hidden signals in data.
  2025. <https://arxiv.org/abs/2507.14805>
- OpenAI Agents Coordinated Hugging Face Breach at Scale. GovInfoSecurity, 2026.
  <https://www.govinfosecurity.com/openai-agents-coordinated-hugging-face-breach-at-scale-a-32663>
- AI could self-replicate on home devices within 'months', Anthropic says. Al Jazeera, 13 October 2026.
  <https://www.aljazeera.com/economy/2026/10/13/ai-could-self-replicate-on-home-devices-within-months-anthropic-says>
