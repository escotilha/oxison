# Oxfaz build-worker container image (sandbox Layer 2).
#
# A worker runs `claude -p` (write tools, bypassPermissions) INSIDE this
# container, which is the security boundary: only the task's worktree is
# bind-mounted, so the host filesystem (~/.ssh, the main repo, credentials) is
# physically absent — a stronger guarantee than the Layer-1 srt allowlist.
#
# Auth is bare-mode ANTHROPIC_API_KEY (the macOS Keychain / OAuth store is not
# reachable from a Linux container), injected at run time, never baked in.
FROM node:22-slim

# git: the worker commits its work. ca-certificates: TLS to the Anthropic API +
# package registries. ripgrep: claude's search tool. tini: clean PID-1 reaping.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git ca-certificates ripgrep tini \
    && rm -rf /var/lib/apt/lists/*

# The Claude Code CLI (the worker binary).
RUN npm install -g @anthropic-ai/claude-code \
    && claude --version

# Run as an unprivileged user with a real HOME (claude writes session state).
RUN useradd --create-home --shell /bin/bash worker
USER worker
ENV HOME=/home/worker
WORKDIR /work

ENTRYPOINT ["tini", "--"]
