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

The default native shell sandbox is `workspace-write`, with network disabled,
approval `never`, no additional writable roots, and both implicit `/tmp` and
`TMPDIR` writes disabled. A configuration-only app-server preflight checks the
installed CLI's effective settings before any model request or slot claim.
It refuses inherited MCP/plugin configuration, permission-profile overrides,
and changes to the required capability/sandbox settings. Project discovery
stops at the private slot's marker. The CLI runs with strict configuration
checking and without its shared daemon. The launcher pins CLI 0.160.0, the version used for the local
configuration check, so upgrades require policy review; live launch/hook loading must still pass the next bead's
smoke test before production use. Linux native mode needs bubblewrap and working
user namespaces. A sandbox failure never automatically selects container mode.

### Explicit container boundary

For a city already running inside an operator-managed Docker container, set
**both** variables in the Codex agent environment:

```sh
FS_CODEX_SANDBOX=container
FS_CODEX_POLICY_ACK=trusted-local-v1
```

The default when `FS_CODEX_SANDBOX` is absent is `workspace-write`; that value
can also be set explicitly. All other values (including `danger-full-access`,
empty strings, aliases and whitespace variants) are refused. Container mode
requires the exact acknowledgment above during setup and effective-config
preflight, before claiming a slot or requesting a model. The launcher sets
`sandbox_mode = "danger-full-access"` and passes `-s danger-full-access` with
approval `never`. Native mode continues to require its existing acknowledgment
at launch. Configuration cannot opt into full access by itself.

This implements the overseer's **2026-10-13 decision on `mc-kfbz1.4`, option
(1)**: the city's Docker container is the containment boundary, with no native
OS sandbox, for trusted local experiments. Docker's default seccomp policy on
this host blocks the namespaces bubblewrap needs. The overseer accepted the
listed gaps and overrode the earlier requirement to fix that host refusal
before live acceptance. Container mode neither creates a container nor verifies
Docker isolation; selecting it on a host runs with that host user's access.

**Container mode removes OS-level filesystem and network enforcement by Codex.**
The task guard remains mandatory with exactly the same task/command scope.
Web search and the other hosted capabilities remain disabled, and preflight
still refuses MCP/plugins, permission profiles and capability overrides.
The generated `sandbox_workspace_write.network_access = false` and restricted
writable-root settings are retained and checked, but are **inactive** in
`danger-full-access`. There is no separate network-off enforcement in this
mode: shell networking depends on the task policy and any external Docker or
host restrictions. Model API traffic still needs connectivity. A skipped guard
can allow filesystem and network access throughout the container, including
mounted host paths accessible to its user. Native sandboxing can be restored
by removing `FS_CODEX_SANDBOX` or setting it to `workspace-write`.

### Task guard (both modes)

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
or broad model commands to make the shared engine work. The receipt is
not another model-accessible file operation. In native mode the same sandbox
applies to the receipt-reading shell and local candidate patch; in container
mode these run without a native sandbox.

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
   it does not select the sandbox mode. Only the explicit acknowledged container
   mode removes that sandbox. The pinned native hook integration has no supported
   switch to turn callback failure into a denial. A Python supervisor could catch
   a child guard's crash, but its own launch failure, SIGKILL or hook timeout
   would still fail open. Fully fail-closed enforcement therefore requires a
   Codex runtime change or an independent external boundary; this pack cannot
   guarantee it with a hook. Caught guard errors, including broker timeouts,
   still return an explicit deny in both modes.
2. Workspace-write limits writes and shell networking, but permits filesystem
   reads outside the slot. Task-only reads and child-only edits rely on the
   native tool hook. A skipped hook therefore weakens those task/tool
   restrictions. Container mode also removes the native write/network limits,
   so a skipped hook can permit arbitrary commands and edits as the container
   user. Hosted tools are not all covered by PreToolUse, so their
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

The overseer recorded acceptance for this trusted local container policy on
`mc-kfbz1.4` as described above. The launcher still requires the acknowledgment.
Set `FS_CODEX_POLICY_ACK=trusted-local-v1` in the Codex agent environment (for
example the city's agent patch), preserving the generated safety settings.
This acknowledgment is an operational gate, not proof of isolation or a way to
skip the review/smoke-test gates. Container mode also requires its explicit
`FS_CODEX_SANDBOX=container` opt-in. Changing the policy or CLI semantics requires
renewed review. Do not enable the role merely to make a failing launch pass.

References: [official hook contracts and coverage](https://learn.chatgpt.com/docs/hooks),
[official sandbox modes](https://learn.chatgpt.com/docs/sandboxing),
[official configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference),
and [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna).
