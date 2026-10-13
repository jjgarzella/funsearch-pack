# Codex mutator (trusted local v1)

Set the following in `problem.toml` to route slots to
`<rig>/funsearch.mutator-codex`:

```toml
[mutator]
provider = "codex"
model = "gpt-6-luna"
```

The provider defaults to `claude`; an omitted model defaults to
`claude-haiku-4-5-20251001` for Claude and `gpt-6-luna` for Codex. Explicit
models are preserved. Existing manifests without `provider` retain Claude
routing. Both pools have a separate default cap of three sessions; budget
combined concurrency across both pools and all runs in the rig.

The Codex role is a one-shot native `codex exec` process, with a new context
and private `slot/` workspace inside each session work directory. The nested
project-root marker excludes Gas City projections in the parent directory. Its trusted launcher claims via
`gc hook --claim --drain-ack --json`, reads that exact bead's `opt_model`, and
starts the model with the same slot-loop instructions. The model's initial
hook resumes the assigned claim. No shared daemon or shared Codex configuration
is used. The launcher copies only `auth.json` to the private Codex home when
available, or uses normal API authentication from the process environment.
It never copies shared MCP configuration, plugins, skills or hooks. Never put
credentials in the problem or prompt. Private receipts/auth/config remain
sensitive host files and should follow the city's work-directory retention
policy.

## Containment and task policy

The native shell sandbox is `workspace-write`, with network disabled,
approval `never`, no additional writable roots, and both implicit `/tmp` and
`TMPDIR` writes disabled. A configuration-only app-server preflight checks the
installed CLI's effective settings before any model request or slot claim.
It refuses inherited MCP/plugin configuration, permission-profile overrides,
and changes to the required capability/sandbox settings. Project discovery
stops at the private slot's marker. The CLI runs with strict configuration
checking and without its shared daemon. The launcher pins CLI 0.160.0, the version used for the local
configuration check, so upgrades require policy review; live launch/hook loading must still pass the next bead's
smoke test before production use. Linux needs bubblewrap and working user
namespaces; a host sandbox refusal must be resolved before live acceptance.

The generated `PreToolUse` hook matches every local tool. It delegates to
`scripts/mutator_guard.py` for session ownership, retirement, task selection,
run/slot/task identity, path/symlink checks, and literal command grammar.
It allows only the original lifecycle and engine commands, `cat` of the
current task's TASK.md/child.c (including local staged `child.c`), and `apply_patch` adding/updating the slot-local
`child.c`. Patches cannot move/delete files or edit multiple files. Other local
tools are denied. Web search, apps, plugins, browser/computer use, image tools,
subagents, skill search and memories are disabled. Unified exec is disabled
so `write_stdin` cannot send unchecked input to an already approved process.
Host skill discovery and inherited project instructions are disabled.
Shell snapshots/login shells are disabled and the model shell receives only a
fixed PATH.

The model writes a **whole candidate** to local `child.c`. Immediately before
an approved try/submit, the trusted hook rechecks the current task and copies
that staging file to its canonical task path. A successful next-task or slot
show clears the old staging file and restores any existing candidate for a
resumed task. The model never receives permission to write the shared run DB,
Gas City control state, evaluator, canonical task directory, or settings.

Approved read/lifecycle/engine calls execute inside the trusted hook as argv
without a shell, with no stdin and a 300-second timeout. They need host access
to the engine DB and Gas City; that access is **outside the model shell
sandbox**. The hook replaces the model shell command with a trusted command
that reads a private output receipt and returns the original exit status
(including RUN_OVER=3 and rejection=4). This avoids granting broad write roots
or unsandboxed model commands to make the shared engine work. The receipt is
not another model-accessible file operation. The same native sandbox still
applies to the receipt-reading shell and local candidate patch.

## Gaps requiring overseer sign-off

This implementation must not be described as equivalent fail-closed OS
isolation. Before enabling the role, the overseer must explicitly accept all
of the following for trusted local experiments:

1. Codex native hooks can fail open when the hook process cannot start, times
   out, crashes, or returns malformed output. The Python guard converts caught
   exceptions and malformed events into an explicit deny, but it cannot handle
   its own SIGKILL or a missing interpreter. Strict startup/config checks and
   automation trust of the pack-authored hook reduce configuration mistakes;
   they do not change that runtime behavior. The wrapper uses
   `--dangerously-bypass-hook-trust` **only** for these vetted automation hooks;
   it never bypasses approvals or the shell sandbox.
2. Workspace-write limits writes and shell networking, but permits filesystem
   reads outside the slot. Task-only reads and child-only edits rely on the
   native tool hook. A skipped hook therefore weakens those task/tool
   restrictions. Hosted tools are not all covered by PreToolUse, so their
   supported switches are disabled; the hook is not a complete enforcement
   boundary for arbitrary future Codex tools or CLI versions.
3. The trusted hook performs only allowlisted engine/control operations outside
   the shell sandbox. Candidate C still compiles and runs in the existing host
   evaluator workers. It has the host user's filesystem/network access and can
   tamper with guard state or settings. The engine's lint/env/reply hygiene is
   unchanged; untrusted/adversarial candidates are unsupported. OS isolation is
   still backlog `mc-v8f3.5`.
4. Native CLI/model behavior and actual hook loading have not been validated by
   a live model in this implementation bead. The separate resource-window smoke
   test must verify claim, read, local patch, try/submit, retirement and drain,
   plus deliberate denied calls and no inherited tools. Do not infer live
   acceptance from unit tests or a config preflight.

Until that sign-off is recorded, the launcher refuses to run. After acceptance,
set `FS_CODEX_POLICY_ACK=trusted-local-v1` in the Codex agent environment (for
example the city's agent patch), preserving the generated safety settings.
This acknowledgment is an operational gate, not proof of isolation or a way to
skip the review/smoke-test gates. Changing the policy or CLI semantics requires
renewed review. Do not enable the role merely to make a failing launch pass.

References: [official hook contracts and coverage](https://learn.chatgpt.com/docs/hooks),
[official sandbox modes](https://learn.chatgpt.com/docs/sandboxing),
[official configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference),
and [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna).
