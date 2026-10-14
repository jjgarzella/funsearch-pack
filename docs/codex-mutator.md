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
is used. It never copies shared MCP configuration, plugins, skills or hooks.
If a check fails after the claim, the launcher returns the slot with
`funsearch slot release` from the slot workspace; if that release itself fails,
the error names both failures and says the slot is still claimed.

### Authentication: API key from the environment

The overseer decided (2026-10-14) that Codex mutators authenticate only with an
API key from the environment; no `auth.json` or shared ChatGPT login is copied.
Gas City forwards every `OPENAI_*` variable from the supervisor environment to
agents, and its supported persistent source is `~/.gc/secrets.env` (dotenv,
mode 600). Put a **dedicated, spend-limited** OpenAI project key there:

```sh
OPENAI_API_KEY=...
```

`CODEX_API_KEY` is not forwarded by Gas City. The launcher refuses before
claiming a slot when neither is set. It removes any stale private `auth.json`,
drops every inherited `OPENAI_*`/`CODEX_*` variable (so `OPENAI_BASE_URL`
cannot redirect the key), and passes the key to Codex only as `CODEX_API_KEY`
with `forced_login_method = "api"`. The configuration preflight runs without
the key. The hook command starts the guard through `env -u CODEX_API_KEY -u
CODEX_ACCESS_TOKEN -u OPENAI_API_KEY` (Codex may run hooks via a login shell,
whose profile output would also corrupt the hook's JSON reply), and brokered engine/lifecycle commands also run without
`OPENAI_*`, so the guard (alive while try/submit compile and run candidate C)
does not hold the key. **This does not make the key private:** the Codex
process itself holds it, and same-user code can normally read
`/proc/<codex pid>/environ`. That includes candidate C in the host evaluator
and, in either mode, a model shell whose hook was skipped (workspace-write
does not restrict reads). See gap 2. Never put credentials in the problem or
prompt, and never print the key. Private receipts/config remain sensitive host files and should follow the
city's work-directory retention policy.

## Containment and task policy

The default native shell sandbox is `workspace-write`, with network disabled,
approval `never`, no additional writable roots, and both implicit `/tmp` and
`TMPDIR` writes disabled. A configuration-only app-server preflight checks the
installed CLI's effective settings before any model request or slot claim.
It refuses inherited MCP/plugin configuration, permission-profile overrides,
and changes to the required capability/sandbox settings. The effective config
must also have API-key login and no inline hooks, `notify`, telemetry (`otel`),
profile, model provider or `openai_base_url` override. The layer list must
contain only known 0.160 layer types, exactly one user layer (the private
`config.toml`, no profile), no project layer, and only empty non-user layers.
Codex loads `hooks.json` beside each layer's `config.toml` and hook JSON is not
reported by config/read, so a `hooks.json` beside any non-user layer is
refused, and the private one must still match what setup wrote. Project discovery
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
`child.c`. A patch must be exactly `*** Begin Patch`, one `Add File`/`Update
File` header for `child.c`, then only `+` lines (Add) or `@@`, space, `-`, `+`
lines with an optional final `*** End of File` (Update), then `*** End Patch`.
Any other `***` line, in any case or indentation, and any other line form are
refused, so patches cannot move/delete files or edit multiple files. Other local
tools are denied.

A permitted patch returns an empty hook result (no `permissionDecision`). This
is an **inference** from the 0.160 binary strings, which report a bare `allow`
without `updatedInput` as unsupported; it is not documented behavior. The smoke
test must confirm that patches apply and that the Codex session log shows no
"unsupported" hook warning. Denials use `deny` with a non-empty reason. Web search, apps, plugins, browser/computer use, image tools,
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
without a shell, with no stdin, in a new process group, and with a 280-second
limit. On overrun the guard kills the group and returns exit 75 with `OUTCOME
UNKNOWN`: the evaluation may still complete, so the model must run `funsearch
slot show <bead>` before retrying. Engine children that start their own
session (compile and evaluation workers) are outside that group and can
outlive the kill. The hook timeout is 310 seconds (5 s lock
wait + 280 s + margin). They need host access
to the engine DB and Gas City; that access is **outside the model shell
sandbox**. The hook replaces the model shell command with a trusted command
that reads a private output receipt and returns the original exit status
(including RUN_OVER=3 and rejection=4) and then deletes the receipt; setup
removes any receipts left by an interrupted session. This avoids granting broad write roots
or broad model commands to make the shared engine work. The receipt is
not another model-accessible file operation. In native mode the same sandbox
applies to the receipt-reading shell and local candidate patch; in container
mode these run without a native sandbox.

### Per-slot hook lock

Each hook call holds an exclusive `flock` for its slot (5 s wait, then a
"slot busy; retry" denial) across authorization, the brokered command and the
receipt write; the kernel drops it if the guard dies. A brokered command
does not inherit the lock, so if the guard is killed (for example by the
hook timeout) a command already running continues without it. Locks live in
`FS_CODEX_LOCK_DIR` (default `/var/tmp/funsearch-codex-locks`), which must be a
real 0700 directory owned by the agent user. Setup probes that a second process
cannot take a held lock and refuses otherwise, because the virtiofs host share
accepts `flock` without enforcing it.

The lock covers concurrent next-task calls (the second one re-checks the open
task and is denied), concurrent try/submit staging copies and slot-show
restores. It does **not** cover Codex's own `apply_patch` write, which happens
after the hook returns: a try/submit issued in parallel with a patch may stage
an older or partly written `child.c`. The engine records exactly what it
evaluated, and the effect is limited to that model's own task. CLI 0.160 has no
switch found to disable parallel tool calls, so the prompt tells the model to
make one tool call at a time and wait for the patch result before try/submit.

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
   guarantee it with a hook. Caught guard errors still return an explicit deny
   in both modes; a broker overrun instead returns the exit-75 `OUTCOME
   UNKNOWN` receipt described above, because the command already started.
2. Workspace-write limits writes and shell networking, but permits filesystem
   reads outside the slot. Task-only reads and child-only edits rely on the
   native tool hook. A skipped hook therefore weakens those task/tool
   restrictions. Container mode also removes the native write/network limits,
   so a skipped hook can permit arbitrary commands and edits as the container
   user. Hosted tools are not all covered by PreToolUse, so their
   supported switches are disabled; the hook is not a complete enforcement
   boundary for arbitrary future Codex tools or CLI versions. The API key is in
   the Codex process environment. Unless Codex marks itself non-dumpable
   (unverified; the smoke test checks it), any same-user process can read
   `/proc/<codex pid>/environ`: candidate C running in the evaluator, and in
   either mode a model shell whose hook was skipped. This is the same exposure
   as the previously copied `auth.json`. Use a dedicated, spend-capped key and
   revoke it if it may have leaked.
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
