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
# bubblewrap + socat: srt's sandbox deps — srt runs the worker under an
# in-container egress allowlist (Layer-2 network narrowing, M1), the same
# allowlist Layer-1 enforces on the host.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git ca-certificates ripgrep tini bubblewrap socat \
    && rm -rf /var/lib/apt/lists/*

# The Claude Code CLI (the worker binary) + the sandbox runtime (srt), which
# narrows the worker's network egress to the domain allowlist from inside.
#
# Versions are PINNED (SECURITY-AUDIT.md F1): the worker binary is the single
# execution surface for untrusted repo content, and a floating `npm install -g`
# would silently pull whatever is latest at build time — including a regression
# or a freshly-disclosed CVE. Bump these deliberately on a verified-safe release
# and record the bump in CHANGELOG.md. Check the current versions with:
#   npm view @anthropic-ai/claude-code version
#   npm view @anthropic-ai/sandbox-runtime version
ARG CLAUDE_CODE_VERSION=2.1.181
ARG SANDBOX_RUNTIME_VERSION=0.0.55
RUN npm install -g \
        "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
        "@anthropic-ai/sandbox-runtime@${SANDBOX_RUNTIME_VERSION}" \
    && claude --version

# Run as an unprivileged user with a real HOME (claude writes session state).
RUN useradd --create-home --shell /bin/bash worker
USER worker
ENV HOME=/home/worker
WORKDIR /work

ENTRYPOINT ["tini", "--"]
