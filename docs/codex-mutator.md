# Codex mutator

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

### Authentication: shared ChatGPT login

The overseer decided (2026-10-15 14:32Z, `mc-kfbz1`) to use the city's existing
shared Codex ChatGPT login, reversing the earlier API-key decision. Plan quota
and rate limits are shared with the city's Codex agents; there are no per-token
charges for these plan-backed sessions.

Before overriding `CODEX_HOME`, the launcher resolves `auth.json` beneath the
inherited `CODEX_HOME`, or `~/.codex` when that variable is unset or empty. Before
claiming a slot, it requires a regular file containing JSON with
`auth_mode = "chatgpt"` and a non-empty `tokens` object. Otherwise it refuses
and tells the operator to run `codex login` with the shared account. It never
prints token values.

The private `<slot>/.funsearch-codex-home/auth.json` is a **symlink** to that
resolved shared file, replacing any stale private file or symlink. It is never
copied: a copy can diverge during token refresh and could sign out the main
login. The private home still supplies all configuration and hooks; shared
configuration, MCP servers, plugins and skills remain excluded. Setup and
preflight require `forced_login_method = "chatgpt"`. The launcher drops every
inherited `OPENAI_*` and `CODEX_*` variable, then sets only the private
`CODEX_HOME`; the Codex process environment no longer holds an API key.
The hook's `env -u CODEX_API_KEY -u CODEX_ACCESS_TOKEN -u OPENAI_API_KEY` and
broker credential stripping remain in place.

`~/.codex/auth.json` is already readable by any same-user code, including
candidate C under either mutator, so this adds no new credential exposure.
Never put credentials in the problem or prompt, and never print them. Private
receipts/config remain sensitive host files and should follow the city's
work-directory retention policy.

**Refresh and symlink replacement semantics remain unverified.** Static
inspection of the 0.160 binary strings found `login/src/auth/storage.rs`,
file/keyring storage messages, and the `allow_symlinked_codex_home`
configuration key. This does not establish 0.164 behavior. A one-session
0.164 smoke in a temporary private home authenticated through the symlink and
left it pointing at the shared file; the shared file's modification time did
not change, so the session did not exercise refresh. Strings and this session
do not establish whether refresh writes through the symlink in place or
replaces it (for example, with a temporary file and rename), nor the exact
scope of the home-symlink setting. The private home itself remains a real
directory; only `auth.json` is linked. Before production, the separate smoke
test must verify the link and shared login after a mutator session that
exercises refresh. Unit/config tests do not establish this. There is no
copy-based fallback.
See [official credential storage documentation](https://learn.chatgpt.com/docs/auth)
for the storage modes and automatic refresh; it does not specify symlink write
semantics.

## Native containment and task policy

The launcher pins `codex-cli 0.164.0` and selects Codex's host-native backend:
macOS Seatbelt or Linux `workspace-write`. On macOS, the private
`funsearch_native` permission profile extends `:workspace`, denies reads from
`~/.ssh`, keeps `:tmpdir` and `:slash_tmp` read-only, and disables command
networking. On Linux, the existing `sandbox_mode = "workspace-write"` policy
remains in place with network access disabled, no additional writable roots,
and writes to implicit `/tmp` and `TMPDIR` excluded.

The launcher reads the installed CLI's effective configuration before any
model request or slot claim. It refuses inherited MCP/plugin configuration,
unexpected permission profiles, and changes to the required capability or
sandbox settings. The effective config must also have ChatGPT login and no
inline hooks, `notify`, telemetry (`otel`), profile, model provider or
`openai_base_url` override. The layer list must contain only known 0.164 layer
types, exactly one user layer (the private `config.toml`, no profile), no
project layer, and only empty non-user layers. Codex loads `hooks.json` beside
each layer's `config.toml` and hook JSON is not reported by config/read, so a
`hooks.json` beside any non-user layer is refused, and the private one must
still match what setup wrote. Project discovery stops at the private slot's
marker. The CLI runs with strict configuration checking and without its shared
daemon. Before claiming, a harmless `/usr/bin/true` is run through the selected
Codex sandbox; failure stops the launch.

On 0.164, `codex sandbox --help` documents `--permission-profile` and a command
argument. `codex sandbox macos` is not a backend selector: `macos` is treated
as the executable name. Codex selects Seatbelt from the host OS. Linux still
depends on its native sandbox prerequisites, including bubblewrap and working
user namespaces. The optional `FS_CODEX_SANDBOX` setting accepts only
`workspace-write`; other values are refused. There is no container or
no-native-sandbox fallback. A sandbox failure never proceeds to a slot claim.

On the Darwin arm64 host, a 0.164 CLI probe ran a temporary Python script:

```sh
codex sandbox -C "$slot" --permission-profile funsearch_native \
  /usr/bin/python3 "$slot/seatbelt_probe.py"
```

It exited 0 and printed `INSIDE_WRITE=ALLOWED`,
`OUTSIDE_WRITE=REFUSED errno=1 Operation not permitted`,
`SSH_FILE_READ=REFUSED errno=1 Operation not permitted`, and
`NETWORK=REFUSED errno=1 Operation not permitted`. A separate real `codex exec`
probe with the same private profile produced the same four policy results.

### Task guard

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
not another model-accessible file operation. The selected native sandbox
applies to the receipt-reading shell and local candidate patch.

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
evaluated, and the effect is limited to that model's own task. CLI 0.164 has no
switch found to disable parallel tool calls, so the prompt tells the model to
make one tool call at a time and wait for the patch result before try/submit.

## Known limitations and smoke requirements

This implementation combines a host-native OS sandbox with a task/tool policy;
it does not make Codex hooks a fail-closed OS boundary. The known limitations
are:

1. Codex native hooks can fail open when the hook process cannot start, times
   out, crashes, or returns malformed output. The Python guard converts caught
   exceptions and malformed events into an explicit deny, but it cannot handle
   its own SIGKILL or a missing interpreter. Strict startup/config checks and
   automation trust of the pack-authored hook reduce configuration mistakes;
   they do not change that runtime behavior. The wrapper uses
   `--dangerously-bypass-hook-trust` **only** for these vetted automation hooks;
   it does not select or disable the sandbox. The pinned native hook integration
   has no supported switch to turn callback failure into a denial. A Python
   supervisor could catch a child guard's crash, but its own launch failure,
   SIGKILL or hook timeout would still fail open. Fully fail-closed enforcement
   therefore requires a Codex runtime change or an independent external
   boundary; this pack cannot guarantee it with a hook. Caught guard errors
   still return an explicit deny under either native backend; a broker overrun
   instead returns the exit-75 `OUTCOME UNKNOWN` receipt described above,
   because the command already started.
2. Linux `workspace-write` limits writes and shell networking, but permits
   filesystem reads outside the slot. On macOS, the Seatbelt profile confines
   writes to the workspace and explicitly denies `~/.ssh` reads. In both
   backends, task-only reads and child-only edits rely on the native tool hook;
   a skipped hook weakens those task/tool restrictions. Hosted tools are not all
   covered by PreToolUse, so their supported switches are disabled; the hook is
   not a complete enforcement boundary for arbitrary future Codex tools or CLI
   versions. The shared ChatGPT login cache is readable by same-user code,
   including candidate C under either mutator and a model shell whose hook was
   skipped. Linking that already-readable cache adds no new exposure; the
   Codex process environment no longer holds an API key. Task policy does not
   protect the shared login from same-user code. Refresh through the symlink
   and continued shared-login usability require the smoke check described above.
3. The trusted hook performs only allowlisted engine/control operations outside
   the shell sandbox. Candidate C still compiles and runs in the existing host
   evaluator workers. It has the host user's filesystem/network access and can
   tamper with guard state or settings. The engine's lint/env/reply hygiene is
   unchanged; untrusted/adversarial candidates are unsupported. Broader
   evaluator isolation remains backlog `mc-v8f3.5`.
4. Real CLI probes verified the 0.164 Seatbelt command path, but native hook
   loading and a live model session remain unverified. The separate
   resource-window smoke test must verify claim, read, local patch, try/submit,
   retirement and drain, plus deliberate denied calls and no inherited tools.
   Do not infer live acceptance from unit tests, containment probes or a config
   preflight.

No acknowledgment variable or no-native-sandbox fallback is part of this
policy. Changing the policy or CLI semantics requires review. Do not enable the
role merely to make a failing launch pass.

References: [official hook contracts and coverage](https://learn.chatgpt.com/docs/hooks),
[official sandbox modes](https://learn.chatgpt.com/docs/sandboxing),
[official configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference),
and [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna).
