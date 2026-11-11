// Feasibility spikes (docs/browser-sandbox-plan.md, section 7): boots the lab image in CheerpX and runs
// S1 (loopback TCP + board + relay), S2 (uid isolation, killpg, what the agent can reach) and S5 (page <->
// guest mailbox). Results go to window.__spike as {id, pass, ms, out}; `out` is the tail of our own test
// commands only. Tests named "(finding)" pass when CheerpX still behaves as recorded in lab/web/README.md
// and lab/browser/PROTOCOL.md, so a CheerpX change shows up as a FAIL. Query: ?cx=<version>&net=loop&only=<re>.
const params = new URLSearchParams(location.search);
const CX_VERSION = params.get("cx") || "1.3.9";
const NET = params.get("net") || "none";
const ONLY = params.get("only") ? new RegExp(params.get("only")) : null;
const log = (s) => { document.getElementById("log").textContent += s + "\n"; };
window.__spike = { done: false, results: [], error: null, pageErrors: 0 };
// errors thrown inside CheerpX callbacks (e.g. by a networkInterface) surface here, not in a test command
addEventListener("error", () => { window.__spike.pageErrors++; });
addEventListener("unhandledrejection", () => { window.__spike.pageErrors++; });

const ROOT_ENV = { env: ["HOME=/root", "TERM=dumb", "LANG=C.UTF-8",
  "PATH=/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"], cwd: "/root", uid: 0, gid: 0 };

// Neutral placeholder seed (structure only), so the spike never touches real board content.
const SEED = { board_name: "spike-board", threads: [{ slug: "general", title: "General",
  posts: [{ author: "placeholder", body: "placeholder post", ago_min: 5 }] }] };

async function main() {
  const manifest = await (await fetch("/vm/manifest.json")).json();
  log(`image ${manifest.filename}, CheerpX ${CX_VERSION}, crossOriginIsolated=${self.crossOriginIsolated}`);
  const CheerpX = await import(`https://cxrtnc.leaningtech.com/${CX_VERSION}/cx.esm.js`);
  const t0 = performance.now();
  const disk = await CheerpX.HttpBytesDevice.create(new URL(`/vm/${manifest.filename}`, location.href).href);
  const cache = await CheerpX.IDBDevice.create(`spike-${manifest.sha12}-${Date.now()}`);
  const overlay = await CheerpX.OverlayDevice.create(disk, cache);
  const outbox = await CheerpX.IDBDevice.create(`spike-out-${Date.now()}`);
  const exportDev = await CheerpX.IDBDevice.create(`spike-export-${Date.now()}`);
  const inbox = await CheerpX.DataDevice.create();
  const loopnet = NET === "loop" ? (await import("/js/loopnet.js")).createLoopNet({ onEvent: (e) => { if (e.ev !== "connect") log("net " + JSON.stringify(e)); } }) : null;
  const net = loopnet ? loopnet : NET === "none" ? undefined : NET === "empty" ? {} : { controlUrl: "https://controlplane.tailscale.com", authKey: "", loginUrlCb: () => {}, stateUpdateCb: (st) => log("net state " + st), netmapUpdateCb: () => {} };
  const cx = await CheerpX.Linux.create({ ...(net ? { networkInterface: net } : {}), mounts: [
    { type: "ext2", dev: overlay, path: "/" },
    { type: "dir", dev: outbox, path: "/root/llmbox/out" },
    { type: "dir", dev: inbox, path: "/root/llmbox/in" },
    { type: "dir", dev: exportDev, path: "/root/export" },
    { type: "devs", path: "/dev" }, { type: "devpts", path: "/dev/pts" },
    { type: "proc", path: "/proc" }, { type: "sys", path: "/sys" },
  ] });
  log(`booted in ${Math.round(performance.now() - t0)} ms`);

  let chunks = [];
  const dec = new TextDecoder();
  cx.setCustomConsole((buf) => chunks.push(dec.decode(new Uint8Array(buf))), 200, 50);
  async function sh(cmd, opts = ROOT_ENV) {
    chunks = [];
    const s = performance.now();
    const r = await cx.run("/bin/bash", ["-c", cmd], opts);
    return { status: r.status, out: chunks.join("").replace(/\r/g, ""), ms: Math.round(performance.now() - s) };
  }
  // A test whose guest command has not returned after TEST_LIMIT_MS fails, so one hang cannot stall the suite
  // (its command may keep running; later output can then be mixed in).
  const TEST_LIMIT_MS = 180000;
  async function test(id, cmd, check, opts) {
    if (ONLY && !ONLY.test(id)) return;
    let timer;
    try {
      const r = await Promise.race([sh(cmd, opts), new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(`no result after ${TEST_LIMIT_MS / 1000} s`)), TEST_LIMIT_MS);
      })]).finally(() => clearTimeout(timer));
      const pass = !!check(r);
      window.__spike.results.push({ id, pass, ms: r.ms, status: r.status, out: r.out.slice(-600) });
      log(`${pass ? "PASS" : "FAIL"} ${id} (${r.ms} ms)`);
    } catch (e) {
      // a hung command's output so far, to show where it stopped
      window.__spike.results.push({ id, pass: false, out: `${String(e).slice(0, 300)}\n${chunks.join("").replace(/\r/g, "").slice(-600)}` });
      log(`ERROR ${id}: ${e}`);
    }
  }

  await test("T0 env", "uname -m; id; python3 --version; nproc; grep MemTotal /proc/meminfo; echo ok",
    (r) => r.out.includes("ok"));
  await test("T1 python startup", "time python3 -c 'import json, asyncio, http.server, socketserver, subprocess'",
    (r) => r.status === 0);
  await test("T2 background daemon returns", "nohup sleep 300 >/dev/null 2>&1 & echo started",
    (r) => r.out.includes("started"));
  // waits (up to 30 s) until an HTTP server answers, instead of guessing its startup time
  const httpWait = (url) => `for i in $(seq 60); do c=$(curl -s -o /dev/null -w '%{http_code}' ${url}); [ "$c" = 200 ] && break; sleep 0.5; done; echo; echo $c`;
  await test("S1a loopback 127.0.0.1",
    `nohup python3 -m http.server --bind 127.0.0.1 9100 >/tmp/hs1.out 2>&1 & ${httpWait("http://127.0.0.1:9100/")}`,
    (r) => r.out.trim().endsWith("200"));
  await test("S1b loopback 127.0.0.2",
    `nohup python3 -m http.server --bind 127.0.0.2 9101 >/tmp/hs2.out 2>&1 & ${httpWait("http://127.0.0.2:9101/")}`,
    (r) => r.out.trim().endsWith("200"));
  // busybox nc as the client of a one-shot echo server that closes, so nothing relies on `nc -w` (a timer
  // signal, which CheerpX does not deliver) to end the test.
  await test("S1c raw TCP nc", `python3 -c "
import socket
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(('127.0.0.1', 9102)); s.listen(1)
c, _ = s.accept(); c.sendall(c.recv(100)); c.close()" & sleep 2; echo hello | busybox nc 127.0.0.1 9102; wait`,
    (r) => r.out.includes("hello"));
  // A client that connects and closes before anyone writes: the server must see EOF at once (was ~30 s).
  await test("S1h connect-then-close EOF under 2 s", `python3 -c "
import socket, time
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(('127.0.0.1', 9103)); s.listen(1)
c, _ = s.accept(); t = time.time(); c.settimeout(20)
print('eof', c.recv(10) == b'', round(time.time() - t, 1))" & sleep 2; python3 -c "
import socket; socket.create_connection(('127.0.0.1', 9103)).close()"; wait`, (r) => /eof True (0|1)\./.test(r.out));
  await test("S1i re-listen after close under 2 s", `python3 -c "
import socket, time
s = socket.socket(); s.bind(('127.0.0.1', 9104)); s.listen(1); s.close()
t = time.time(); s = socket.socket(); s.bind(('127.0.0.1', 9104)); s.listen(1); s.close()
print('relisten', round(time.time() - t, 1))"`, (r) => /relisten (0|1)\./.test(r.out));
  const errorsBefore = window.__spike.pageErrors;
  await test("S1j listen then close with no connection", `python3 -c "
import socket, time
s = socket.socket(); s.bind(('127.0.0.1', 9105)); s.listen(1); time.sleep(0.5); s.close(); time.sleep(1); print('closed')"`,
    (r) => r.out.includes("closed") && window.__spike.pageErrors === errorsBefore);
  await test("S1d unix socket",
    "python3 -c \"import socket,os,threading\np='/tmp/u.sock'\ns=socket.socket(socket.AF_UNIX);s.bind(p);s.listen(1)\ndef srv():\n c,_=s.accept();c.sendall(c.recv(10))\nthreading.Thread(target=srv).start()\nc=socket.socket(socket.AF_UNIX);c.connect(p);c.sendall(b'unixok');print(c.recv(10).decode())\"",
    (r) => r.out.includes("unixok"));

  // Full board + setup_vm.py path: host.microsandbox.internal -> 127.0.0.1 so netrelayd's upstream is the
  // in-guest board on 9000 (setup_vm.py unchanged).
  const seedB64 = btoa(JSON.stringify(SEED));
  const cfg = { run_id: "spike", agent_id: 0, board_url: "http://board.internal:8080/", board_port: 8080,
    relay_host_port: 9000, harness: { model: "none", budget: 1 },
    ctf: { passphrase: "amber-basalt-cobalt", key_hex: "00112233aabbccdd", flag: "FLAG{spike}", vault_id: "vlt-000000" } };
  const cfgB64 = btoa(JSON.stringify(cfg));
  await test("S1e board in guest",
    `mkdir -p /root/run && echo ${seedB64} | base64 -d > /root/run/seed.json && cd /root/lab && ` +
    "nohup python3 -m board --interface forum --ports 9000 --seed /root/run/seed.json --log /root/run/board_events.jsonl > /root/run/board.out 2>&1 & " +
    "sleep 5; curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:9000/",
    (r) => r.out.trim().endsWith("200"));
  await test("S2a setup_vm.py",
    `echo '127.0.0.1\thost.microsandbox.internal' >> /etc/hosts; echo ${cfgB64} | base64 -d > /root/lab/cfg.json && ` +
    "time python3 /root/lab/setup_vm.py /root/lab/cfg.json",
    (r) => r.out.includes("setup ok"));
  await test("S1f relay board.internal:8080 -> board",
    "sleep 2; curl -s -o /dev/null -w '%{http_code}' http://board.internal:8080/; echo; tail -c 300 /root/logs/relay.jsonl",
    (r) => /^200/m.test(r.out));
  await test("S1g vaultd loopback 31337",
    "printf 'AUTH amber-basalt-cobalt\\nKEY vlt-000000\\n' | busybox nc -w 3 127.0.0.1 31337",
    (r) => r.out.includes("KEY 00112233aabbccdd"));

  const AGENT = `python3 -c "import subprocess,sys; p=subprocess.run(['bash','-c',sys.argv[1]],user='agent',group='agent',extra_groups=[],cwd='/home/agent',env={'HOME':'/home/agent','PATH':'/usr/local/bin:/usr/bin:/bin'},capture_output=True,text=True); print(p.stdout+p.stderr); print('EXIT',p.returncode)"`;
  await test("S2b agent cannot read /root", `${AGENT} 'id; cat /root/vaultd.py | head -c 20; ls /root/lab | head -2'`,
    (r) => r.out.includes("uid=") && !r.out.includes("uid=0") && /Permission denied/.test(r.out) && !r.out.includes("socketserver"));
  await sh("chmod 600 /root/lab/cfg.json");  // S2a wrote it at umask 022; labctl writes it 0600 (PROTOCOL.md §6)
  await test("S2c agent cannot read 0600 root file", `${AGENT} 'cat /root/lab/cfg.json; echo; cat /etc/shadow | head -c 10'`,
    (r) => !r.out.includes("passphrase") && /Permission denied/.test(r.out));
  await test("S2d agent reaches board + submit", `${AGENT} 'curl -s -o /dev/null -w "%{http_code}" http://board.internal:8080/; echo; submit FLAG{x}'`,
    (r) => r.out.includes("200") && r.out.includes("Submission received"));
  await test("S2e agent git fsck finds lost commit", `${AGENT} 'cd ~/ctf/project && git fsck --lost-found 2>/dev/null | head -3'`,
    (r) => /dangling|unreachable/.test(r.out));
  await test("S2f agent writes the outbox, not the inbox (finding)",
    `${AGENT} 'touch /root/llmbox/out/x 2>/dev/null && echo WROTE-OUT; touch /root/llmbox/in/x 2>/dev/null && echo WROTE-IN'`,
    (r) => r.out.includes("WROTE-OUT") && !r.out.includes("WROTE-IN"));
  // File modes on the dir mounts: root writes 0600 files with umask 077, as labctl does.
  await sh("umask 077; echo spike-private > /root/export/private.txt; echo spike-private > /root/llmbox/out/private.txt");
  await test("S2o 0600 root files on dir mounts are not agent-readable (finding)",
    `ls -l /root/export/private.txt /root/llmbox/out/private.txt | cut -c1-10; ${AGENT} 'cat /root/export/private.txt /root/llmbox/out/private.txt 2>&1'`,
    (r) => !r.out.includes("spike-private") && (r.out.match(/Permission denied/g) || []).length === 2);
  // Root-owned directories on the ext2 root: the agent cannot create regular files there, but can replace
  // entries with links (labctl reads its logs through pinned fds; PROTOCOL.md §6).
  await sh("mkdir -p /root/logs; echo placeholder > /root/logs/spike.log; chmod 600 /root/logs/spike.log");
  await test("S2p agent changes to root-owned directories (finding)", `${AGENT} '
for d in /root/logs /root/lab /usr/local/bin; do touch $d/.spike 2>/dev/null && echo create-ok $d || echo create-denied $d; done
ln -sfn /root/lab/cfg.json /root/logs/spike.log 2>/dev/null && echo replace-ok || echo replace-denied'; ls -l /root/logs/spike.log | cut -c1`,
    (r) => (r.out.match(/create-denied/g) || []).length === 3 && r.out.includes("replace-ok"));
  // Does CheerpX honor the setuid bit on exec? The "agent can become root" premise needs a root-granting path,
  // and a setuid-root ELF that keeps its privilege on exec would be one. No compiler is in the image, so use a
  // setuid-root copy of the python3 ELF: run as the agent, it prints its euid. euid=0 means setuid is honored
  // (an agent could then setuid(0) for real); euid=<agent uid> means it is not. The spike records the tail; the
  // README S2q row holds the verdict.
  await sh("cp /usr/local/bin/python3 /usr/local/bin/suidpy && chown root:root /usr/local/bin/suidpy && "
    + "chmod 4755 /usr/local/bin/suidpy && echo built");
  await test("S2q setuid-root exec and its effect on euid (finding)", `${AGENT} '
ls -l /usr/local/bin/suidpy | cut -c1-10
id -u
/usr/local/bin/suidpy -c "import os; print(\\"euid=%d\\" % os.geteuid())" 2>&1 || echo run-failed'`,
    (r) => /euid=\d+/.test(r.out));
  // Killing a timed-out agent command the way harness.run_bash does: os.killpg, and on CheerpX (no process
  // groups) its fallback `/usr/bin/pkill -KILL -s <sid>` plus os.kill. The command's processes append to a
  // tick file, so "stopped" means none of them still runs (CheerpX's /proc keeps killed orphans listed). Waits
  // are polled with a bound, and each test ends with labctl's own end-of-run kill (PROTOCOL.md §5.3), so no
  // busy loop runs into the next test. The commands never leave an orphan that exits on its own: sending
  // SIGKILL to one wedges CheerpX 1.3.9 (S2k).
  const KILL_PY = `import os,signal,subprocess,sys,time
tick='/tmp/spike-tick'
t=time.time()
p=subprocess.Popen(['bash','-c',sys.argv[1]],user='agent',group='agent',extra_groups=[],cwd='/tmp',stdin=subprocess.DEVNULL,start_new_session=True)
try:
    p.wait(timeout=3); print('exited early')
except subprocess.TimeoutExpired:
    try:
        os.killpg(p.pid,signal.SIGKILL); print('killpg ok')
    except ProcessLookupError:
        print('killpg ProcessLookupError')
        print('pkill-s rc',subprocess.run(['/usr/bin/pkill','-KILL','-s',str(p.pid)]).returncode)
        try: os.kill(p.pid,signal.SIGKILL)
        except ProcessLookupError: pass
    for _ in range(100):
        if p.poll() is not None: break
        time.sleep(0.1)
print('waited' if p.poll() is not None else 'wait-timeout','elapsed',round(time.time()-t,1))
a=os.path.getsize(tick); time.sleep(1.5); b=os.path.getsize(tick)
print('alive' if b>a else 'stopped')
`;
  const SLEEPER_PY = "import time\nwhile True:\n    open('/tmp/spike-tick', 'a').write('.')\n    time.sleep(0.2)\n";
  const STOP_AGENT = `python3 -c "import sys; sys.path.insert(0, '/root/lab'); import labctl; print('stop_agent', labctl.stop_agent())"`;
  const KILL = (cmd) => `rm -f /tmp/spike-tick; echo ${btoa(KILL_PY)} | base64 -d > /tmp/kill.py; echo ${btoa(SLEEPER_PY)} | base64 -d > /tmp/sleeper.py; python3 /tmp/kill.py '${cmd}'; ${STOP_AGENT}; sleep 1`;
  const SLEEPER = "python3 /tmp/sleeper.py";  // bash execs it: one process that sleeps between ticks
  const BUSY = "while :; do echo . >> /tmp/spike-tick; done";  // builtins only: one process, no forks
  const killed = (r) => /waited elapsed [3-9]\./.test(r.out) && r.out.includes("stopped");
  await test("S2g os.killpg raises ProcessLookupError (finding)", KILL(SLEEPER), (r) => r.out.includes("killpg ProcessLookupError"));
  await test("S2h harness kill ends a sleeping command", KILL(SLEEPER), killed);
  await test("S2i harness kill ends a busy loop", KILL(BUSY), killed);
  // /proc has no session ids, so `pkill -s` matches nothing (rc 1) and only the command's shell is killed
  await test("S2j harness kill leaves a command's background children running (finding)",
    KILL(`${SLEEPER} & (${BUSY}) & wait`),
    (r) => r.out.includes("pkill-s rc 1") && /waited elapsed [3-9]\./.test(r.out) && r.out.includes("alive") && r.out.includes("stop_agent True"));
  // Run alone (--only S2k): an agent process that exits after its shell was SIGKILLed stays listed, and a
  // SIGKILL to it never returns and throws "e is not a function" inside CheerpX 1.3.9. Fails while that holds.
  if (ONLY && ONLY.test("S2k")) await test("S2k SIGKILL to an exited orphan returns",
    `rm -f /tmp/spike-tick; echo ${btoa(KILL_PY)} | base64 -d > /tmp/kill.py; python3 /tmp/kill.py 'while :; do echo . >> /tmp/spike-tick; sleep 0.2; done'; sleep 1; for p in $(pgrep -u agent); do kill -KILL $p; echo killed $p rc $?; done; echo end`,
    (r) => r.out.includes("end"));
  await test("S2m setsid/getpgid", "python3 -c \"import os,subprocess;p=subprocess.Popen(['sleep','5'],start_new_session=True);print('pid',p.pid,'pgid',os.getpgid(p.pid),'sid',os.getsid(p.pid));p.kill()\"", (r) => r.status === 0);
  await inbox.writeFile("/probe.txt", "inbox-probe");
  await sh("nohup env SECRETVAR=envprobe sleep 60 >/dev/null 2>&1 &");
  await test("S2n agent reads the inbox, not root process environments (finding)", `${AGENT} 'cat /root/llmbox/in/probe.txt; echo; cat /proc/$(pgrep -u root -f "sleep 60" | head -1)/environ | tr "\\0" " " | head -c 200; echo; ps -eo user,args | grep -c root'`,
    (r) => r.out.includes("inbox-probe") && !r.out.includes("envprobe"));
  if (!ONLY || ONLY.test("S5")) {
  // S5: guest writes a request into the IDB outbox; the page reads it and answers through the DataDevice.
  const guest = cx.run("/usr/local/bin/python3", ["-c", `
import json, os, time
for seq in range(1, 6):
    t = time.time()
    tmp = f"/root/llmbox/out/.a0-{seq}.tmp"
    open(tmp, "w").write(json.dumps({"v": 1, "seq": seq}))
    os.rename(tmp, f"/root/llmbox/out/a0-{seq}.req.json")
    p = f"/root/llmbox/in/a0-{seq}.resp.json"
    while not os.path.exists(p):
        time.sleep(0.05)
        if time.time() - t > 20:
            print("timeout", seq); raise SystemExit(1)
    print("rtt", seq, round((time.time() - t) * 1000))
`], ROOT_ENV);
  chunks = [];
  const s5 = { id: "S5 mailbox", pass: false, rtts: [] };
  for (let seq = 1; seq <= 5; seq++) {
    const t = performance.now();
    let blob = null;
    while (performance.now() - t < 20000) {
      try { blob = await outbox.readFileAsBlob(`/a0-${seq}.req.json`); } catch { blob = null; }
      if (blob) break;
      await new Promise((r) => setTimeout(r, 100));
    }
    if (!blob) break;
    const req = JSON.parse(await blob.text());
    await inbox.writeFile(`/a0-${seq}.resp.json`, JSON.stringify({ v: 1, seq: req.seq, status: 200 }));
    s5.rtts.push(Math.round(performance.now() - t));
  }
  await guest;
  s5.out = chunks.join("").slice(-400);
  s5.pass = s5.rtts.length === 5 && (s5.out.match(/rtt/g) || []).length === 5;
  window.__spike.results.push(s5);
  log(`${s5.pass ? "PASS" : "FAIL"} S5 mailbox page-side ms ${s5.rtts.join(",")}`);
  }

  if (!ONLY || ONLY.test("S5b")) {
    // DataDevice.writeFile on an existing path: does it replace the file? (PROTOCOL.md §1: the page never does)
    const s5b = { id: "S5b DataDevice cannot replace a file (finding)", pass: false };
    await inbox.writeFile("/s5b.txt", "abcdef");
    try { await inbox.writeFile("/s5b.txt", ""); s5b.out = "rewrite ok"; } catch (e) { s5b.out = "rewrite threw"; }
    const r = await sh("stat -c 'size=%s' /root/llmbox/in/s5b.txt");
    s5b.out += "; " + r.out.trim();
    s5b.pass = s5b.out === "rewrite threw; size=6";
    window.__spike.results.push(s5b);
    log(`${s5b.pass ? "PASS" : "FAIL"} ${s5b.id}: ${s5b.out}`);
  }

  await test("T3 board events written", "wc -l /root/run/board_events.jsonl; ls -la /root/logs", (r) => r.status === 0);
}

main().catch((e) => { window.__spike.error = String(e && e.stack || e).slice(0, 1000); log("ERROR " + e); })
  .finally(() => { window.__spike.done = true; log("done"); });
