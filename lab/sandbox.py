"""Thin wrapper over the `msb` microsandbox CLI.

The experiment's isolation contract (which VM may talk to what) is security-critical, so every
network-policy flag lives here, in create_board_vm / create_agent_vm, and nowhere else. Every
method runs exactly one `msb` invocation through a single runner; tests inject a fake runner to
capture the ordered command list without msb or any real sandbox.
"""
import shutil
import subprocess
from pathlib import Path

from config import (
    BOARD_GUEST_PORT_BASE,
    HOST_PORT_BASE,
    IMAGE,
    INFERENCE_HOST,
    SECRET_ENV,
)

# Matches only the harness itself (see HARNESS_START in orchestrate.py), not shells that mention it.
HARNESS_PATTERN = "^python3 /root/lab/harness.py"

# Resolved once; the same binary is used for every invocation.
MSB = shutil.which("msb") or str(Path.home() / ".local/bin/msb")

# Setup commands fail the run if msb hangs. Polling, collection and teardown have no timeout, so a
# hung copy blocks rather than skipping `msb rm` and the run.json write.
SETUP_TIMEOUT_S = 600


def _subprocess_run(argv, *, check, timeout):
    """Default runner: run msb for real and raise on failure when asked."""
    r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"msb {' '.join(argv[1:4])} failed: {r.stderr.strip()[-500:]}")
    return r


class Sandbox:
    """msb command surface used by the orchestrator.

    `runner` is a callable (argv, *, check, timeout) -> completed-process-like; the
    default runs subprocess. The full argv (msb binary first) is what tests assert on.
    The keyword arguments select a port lane and inference provider; the defaults are the
    Cerebras lane on HOST_PORT_BASE.
    """

    def __init__(self, msb=MSB, runner=_subprocess_run, *, host_port_base=HOST_PORT_BASE,
                 inference_host=INFERENCE_HOST, secret_env=SECRET_ENV):
        self.msb = msb
        self._runner = runner
        self.host_port_base = host_port_base
        self.inference_host = inference_host
        self.secret_env = secret_env

    def _msb(self, *args, check=True, timeout=SETUP_TIMEOUT_S):
        return self._runner([self.msb, *args], check=check, timeout=timeout)

    # ---- VM creation (the only place network policy is defined) ------------------------------

    def create_board_vm(self, name, n_agents):
        """Board VM: egress denied; agent i's guest port is published only on host loopback."""
        ports = []
        for i in range(n_agents):
            ports += ["-p", f"127.0.0.1:{self.host_port_base + i}:{BOARD_GUEST_PORT_BASE + i}"]
        self._msb("create", IMAGE, "--name", name, "--replace",
                  "--net-default-egress", "deny", *ports)

    def create_agent_vm(self, name, i):
        """Agent VM: default-deny; egress only to DNS, the inference API, and its own board port.

        The provider key is bound to the inference host as an msb secret, so the guest never holds
        the real value; a secret-policy violation blocks the connection.
        """
        self._msb("create", IMAGE, "--name", name, "--replace", "--no-net",
                  "--net-rule", "allow@dns",
                  "--net-rule", f"allow@{self.inference_host}:tcp:443",
                  "--net-rule", f"allow@host:tcp:{self.host_port_base + i}",
                  "--secret", f"{self.secret_env}@{self.inference_host}",
                  "--secret-violation-action", "block")

    # ---- exec / copy / teardown --------------------------------------------------------------

    def exec(self, name, *cmd):
        """Run a command in the VM and raise if it fails."""
        self._msb("exec", name, "--", *cmd)

    def exec_shell(self, name, shell_cmd):
        """Run a `sh -c` snippet in the VM; callers start daemons with `nohup ... &` inside it."""
        self._msb("exec", name, "--", "sh", "-c", shell_cmd)

    def cp(self, src, dst):
        """Copy a file into a VM (`name:/path` as dst) and raise if it fails."""
        self._msb("cp", str(src), str(dst))

    def is_done(self, name, marker="/root/logs/DONE"):
        """True once the agent has written its completion marker; any msb failure reads as not done."""
        r = self._msb("exec", name, "--", "test", "-f", marker, check=False, timeout=None)
        return r.returncode == 0

    def harness_dead(self, name):
        """True only when msb ran and pgrep found no harness process (exit 1); msb failures read as alive."""
        r = self._msb("exec", name, "--", "pgrep", "-f", HARNESS_PATTERN, check=False, timeout=None)
        return r.returncode == 1

    def collect(self, name, remote, local):
        """Best-effort copy of a log file out of a VM; missing files are tolerated."""
        self._msb("cp", f"{name}:{remote}", str(local), check=False, timeout=None)

    def rm(self, *names):
        """Delete VMs, ignoring any that are already gone."""
        self._msb("rm", "-f", *names, check=False, timeout=None)

    def ensure_busybox(self, dest):
        """Vendor a static busybox once by copying it out of a throwaway musl image."""
        dest = Path(dest)
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            self._msb("create", "busybox:musl", "--name", "sb-bbx", "--replace")
            self._msb("cp", "sb-bbx:/bin/busybox", str(dest))
            self._msb("rm", "-f", "sb-bbx", check=False)
        return dest
