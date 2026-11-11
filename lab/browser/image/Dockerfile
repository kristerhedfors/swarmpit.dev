# i386 guest image for the in-browser lab (CheerpX runs 32-bit x86 only).
# Built by lab/browser/image/build.sh; the build context is a staging dir with an allowlist of files.
FROM --platform=linux/386 python:3.12-slim-bookworm

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      busybox-static ca-certificates curl git less procps wget xxd \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/* /tmp/* /var/tmp/* \
              /usr/share/doc /usr/share/man /usr/share/info /usr/share/locale/*

# CheerpX expects a regular user named "user"; the lab's own `agent` user is created per run by setup_vm.py.
RUN useradd -m user

COPY lab/ /root/lab/
# Mount points for the page's devices; their modes at runtime are decided by those devices, not by these dirs
# (lab/browser/PROTOCOL.md §1, §6).
RUN cp /bin/busybox /root/lab/busybox \
    && mkdir -p /root/llmbox/in /root/llmbox/out /root/export \
    && chmod 700 /root
# best effort: bytecode only speeds up the first imports in the VM
RUN python3 -m compileall -q /root/lab /usr/local/lib/python3.12 >/dev/null || true
# CheerpX does not enforce directory modes, so the lab's files themselves must be root-only
RUN chmod -R go-rwx /root/lab

CMD ["/bin/bash"]
