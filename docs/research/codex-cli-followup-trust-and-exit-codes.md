# Codex CLI — follow-up: project trust, `-c` override safety, exec exit semantics

Pinned SHA `6ae8b8aa7c608730d4315d59c5c081897ae02780` (2026-10-16 ≈ stable `rust-v0.163.0`).
Blob URL prefix: `https://github.com/openai/codex/blob/6ae8b8aa7c608730d4315d59c5c081897ae02780/`

---

## 1. Trust of the cwd (the critical one)

### What "trusted" means and where it lives
`TrustLevel` is a two-value enum, and its doc ties it directly to permissions
(`codex-rs/protocol/src/config_types.rs:636-645`):
```rust
/// Represents the trust level for a project directory.
/// This determines the approval policy and sandbox mode applied.
#[derive(Debug, Serialize, Deserialize, Clone, Copy, PartialEq, Eq, Display, JsonSchema, TS)]
#[serde(rename_all = "lowercase")]
pub enum TrustLevel {
    Trusted,
    Untrusted,
}
```
It is a **persisted entry in the merged config's `projects` map**, not a
per-invocation concept by default. `ConfigToml.projects: Option<HashMap<String, ProjectConfig>>`
(`codex-rs/config/src/config_toml.rs:473`), and `ProjectConfig::is_trusted()` is
`matches!(self.trust_level, Some(TrustLevel::Trusted))`
(`config_toml.rs:615-617`). Concrete TOML (from a test that writes the user
config — `codex-rs/config/src/loader/tests.rs:259-262`):
```toml
[projects."/abs/path/to/project"]
trust_level = 'trusted'
```
The key is `project_trust_key(path)` — canonicalized path, original as fallback,
with a Windows-only case fold (`loader/mod.rs:1405-1435`). The **TUI writes this
on the user's behalf** (`codex-rs/tui/src/config_update.rs:78-87`):
```rust
fn trusted_project_edit(project_path: &Path) -> ConfigEdit {
    let project_key = project_trust_key(project_path)
        .replace('\\', "\\\\").replace('"', "\\\"");
    replace_config_value(
        format!("projects.\"{project_key}\".trust_level"),
        serde_json::json!(TrustLevel::Trusted.to_string()),
    )
}
```

### Default is UNTRUSTED → project config is disabled
`decision_for_dir` returns `trust_level: None` when no key matches
(`loader/mod.rs:1067-1111`), and `disabled_reason_for_decision` returns `Some(...)`
unless the level is exactly `Some(Trusted)` (`loader/mod.rs:1113-1131`):
```rust
fn disabled_reason_for_decision(&self, decision: &ProjectTrustDecision) -> Option<String> {
    if decision.is_trusted() { return None; }
    let gated_features = "project-local config, hooks, and exec policies";
    ...
    _ => Some(format!(
        "To load {gated_features}, add {trust_key} as a trusted project in {user_config_file}."
    )),
```
The gate is applied in `discover_project_layers`: `let decision = trust_context.decision_for_dir(&dir); let disabled_reason = trust_context.disabled_reason_for_decision(&decision);` and the layer is built via `ConfigLayerEntry::new_disabled(...)`
(`loader/mod.rs:1146-1162`, `1717-1718`).
**Disabled layers are excluded from the effective config** —
`codex-rs/config/src/state.rs:601-606`:
```rust
pub fn layers_low_to_high(&self) -> impl DoubleEndedIterator<Item = &ConfigLayerEntry> {
    self.all_layers_low_to_high()
        .filter(|layer| !layer.is_disabled())
}
```
⇒ **For `codex exec --cd <freshly cloned repo>` with no pre-existing trust entry,
the repo's `.codex/config.toml` is loaded but DISABLED — its keys do not apply.**
Default answer: **untrusted unless explicitly trusted.**

### A repo CANNOT grant itself trust (verified ordering)
`project_trust_context` reads `projects` from `merged_so_far`, which is built from
`layers` **before** project layers are appended:
- `loader/mod.rs:340-348` builds `merged_so_far` from `layers` + `cli_overrides_layer`;
- `loader/mod.rs:375-383` passes `&merged_so_far` into `project_trust_context`;
- `loader/mod.rs:419` (`layers.extend(project_layers.layers)`) appends project layers **after** that.
So a project config cannot self-declare `trust_level = "trusted"`. Confirmed by ordering.

### But `-c` CAN grant trust per-invocation
`cli_overrides_layer` **is** merged into `merged_so_far` (`loader/mod.rs:346-348`)
before the trust context is computed, and the trust map is read from that merged
value (`loader/mod.rs:1296-1307`). So a `-c` override can inject
`projects.<path>.trust_level`. Caveat: `apply_toml_override` splits the dotted
path **naively** with no quoted-segment support (`codex-rs/config/src/overrides.rs:22`,
`let mut segments_iter = path.split('.').peekable();`), so a path containing a `.`
cannot be expressed correctly, and the quotes the TUI writes are not understood
by `-c`. For a dot-free absolute path, `-c 'projects./abs/path.trust_level=trusted'`
is structurally the right shape.
**There is no `--trust` or `--ignore-project-config` CLI flag.** The only
trust-named flag anywhere is `--dangerously-bypass-hook-trust`, which is scoped to
hooks (`codex-rs/utils/cli/src/shared_options.rs:61-64`).
**UNCONFIRMED:** no test in the repo exercises trusting a project via `-c`, so
end-to-end behaviour is inferred from the code path above, not observed.

### Does `exec` ignore project config entirely? NO — this corrects my earlier brief
The **main** config load for `codex exec` keeps `ignore_project_config: false`.
`loader_overrides` is constructed with `..Default::default()` and only
`user_config_path` / `user_config_profile` / `ignore_user_config` /
`ignore_user_and_project_exec_policy_rules` are set
(`codex-rs/exec/src/lib.rs:379-385`), and the main builder uses it unmodified
(`exec/src/lib.rs:611-620`, `.loader_overrides(loader_overrides.clone())`).
The doc for the override is explicit (`codex-rs/config/src/state.rs:55-56`):
```rust
/// Skip project-root discovery and all project configuration layers.
pub ignore_project_config: bool,
```
The three sites that set it `true` (`exec/src/lib.rs:416, 446, 517`) are the
**worktree / gate / source bootstrap** loads, not the ordinary run. So for a
normal `codex exec` run the project layers **are** loaded — gated by trust as above.

### Is the malicious-repo → unsandboxed-MCP chain real?
**Yes, if and only if the directory is trusted.** Even for trusted projects, a
denylist strips security-relevant keys — but `mcp_servers` is **not** in it
(`codex-rs/config/src/loader/mod.rs:89-102`):
```rust
const PROJECT_LOCAL_CONFIG_DENYLIST: &[&str] = &[
    "openai_base_url", "chatgpt_base_url", "apps_mcp_product_sku",
    "responses_api_metadata", "model_provider", "model_providers",
    "notify", "profile", "profiles",
    "experimental_realtime_webrtc_call_base_url",
    "experimental_realtime_ws_base_url", "otel",
];
```
`sanitize_project_config` removes only those keys plus some `features.*`,
`tui.keymap.chat.*`, and `shell_environment_policy.set.*` entries
(`loader/mod.rs:1164-1268`). So for a **trusted** project:
- `[mcp_servers.evil] command = "bash", args = [...]` is **not stripped** ⇒ honored;
- `sandbox_mode`, `approval_policy`, `sandbox_workspace_write.network_access` are **not stripped** either ⇒ a trusted repo can weaken its own sandbox;
- `model_providers` / `model_provider` **are** stripped.
Combined with my earlier finding that MCP stdio servers are spawned with no
sandbox at all, a trusted-but-malicious repo therefore reaches **arbitrary
unsandboxed code execution**. The only thing standing in the way is the
default-untrusted trust gate — so **trust is the security boundary**, and the
mitigation is: never trust a cwd you have not reviewed, and/or pin a trusted set
in the user/managed config rather than trusting whatever you clone.

### Hardening keys that do exist
- `projects` trust map in `$CODEX_HOME/config.toml` — only user/managed layers can set it (project layers cannot).
- `requirements.toml` supports **enforced** MCP server requirements: `mcp_servers: Option<Sourced<BTreeMap<String, McpServerRequirement>>>` (`codex-rs/config/src/config_requirements.rs:188`), copied into the stack at `state.rs:352-356`. This is the managed, non-overridable path.
- `--ignore-user-config` skips user config (auth still uses `CODEX_HOME`) — `exec/src/cli.rs:44-46`.
- A documented "run headless with trust disabled" recommendation: **UNCONFIRMED** (official prose docs unreachable, HTTP 403).

### Doc/code mismatch worth knowing
The loader doc comment lists a `cwd ${PWD}/config.toml` layer
(`loader/mod.rs:132`), but `ConfigLayerSource` has **only**
`Project { dot_codex_folder }` (`codex-rs/config/src/config_layer_source.rs:20-21`),
and the discovery code only ever reads `<dir>/.codex/config.toml`
(`loader/mod.rs:1706, 1725`). So that doc line does not correspond to a real
layer — the project layer is `.codex/config.toml` only.

---

## 2. `-c` override safety and per-layer merge semantics

### Precedence values (lower = weaker)
`codex-rs/config/src/config_layer_source.rs:33-51`:
```
PackagedDefaults -10
Mdm               0
System           10
EnterpriseManaged 15
User             20
User(profile)    21
Project          25   <-- project config OUTRANKS user config
SessionFlags     30   <-- -c / --config
LegacyManagedConfigTomlFromFile 40
LegacyManagedConfigTomlFromMdm  50
```
Implication: **a trusted repo's project config overrides your `$CODEX_HOME/config.toml`.**
Only `-c` (SessionFlags 30) and managed legacy config (40/50) outrank it. The
TUI's own "add as trusted project" write also lands in the *user* layer, so it
loses to the project's own settings.

### Merge semantics: tables deep-merge, scalars and arrays replace
`merge_toml_values` → `merge_toml_values_at_path` (`codex-rs/config/src/merge.rs:56-151`):
```rust
for (key, value) in overlay_table {
    path.push(key.clone());
    if let Some(existing) = base_table.get_mut(&key) {
        merge_toml_values_at_path(existing, value, path);   // tables: recurse
    } else {
        base_table.insert(key, value);                      // new key: insert
    }
    path.pop();
}
```
```rust
} else {
    *base = overlay;                                        // non-table: replace wholesale
}
```
So:
- **Tables are deep-merged key-by-key** (the whole table is *not* replaced).
- **Scalars are overridden**, **arrays are replaced wholesale.**
⇒ A trusted repo can override the scalar `sandbox_mode`, `approval_policy`,
`sandbox_workspace_write.network_access = true`, and can add new
`[mcp_servers.*]` subtables (new keys are inserted). It cannot override the
denylisted `model_providers`/`model_provider`, and hooks additionally require
hook trust (`--dangerously-bypass-hook-trust` is the explicit opt-in).
Note the exception handling for "structured feature" paths
(`merge.rs:80-94`) and the shell-environment-filter representation switch
(`merge.rs:161-185`).

---

## 3. `exec` exit / detection semantics

### `turn.completed` and whether it depends on file changes
Emission is keyed purely on the turn's terminal status, not on items or diffs
(`codex-rs/exec/src/event_processor_with_jsonl_output.rs:525-537`):
```rust
match notification.turn.status {
    TurnStatus::Completed => {
        ...
        events.push(ThreadEvent::TurnCompleted(TurnCompletedEvent {
            usage: self.usage_from_last_total(),
        }));
        CodexStatus::InitiateShutdown
    }
```
⇒ On a successful turn, **`turn.completed` is emitted even when the model made no
file changes.** No "model produced no changes" check exists in `exec`. Caveat:
`InProcessServerEvent::Lagged { skipped }` exists (`exec/src/lib.rs:1387-1391`),
so under backpressure an event can be dropped — treat "stream ended with no
terminal turn event" as an error rather than assuming success.

### The exact failure detection and exit code
`error_seen` gates the exit code (`exec/src/lib.rs:1399-1401`):
```rust
event_processor.print_final_output();
if error_seen {
    std::process::exit(1);
}
Ok(())
```
It is set in exactly three places (`exec/src/lib.rs:1338-1354`, `2210-2211`):
```rust
if let ServerNotification::Error(payload) = &notification {
    if payload.thread_id == primary_thread_id_for_requests
        && payload.turn_id == task_id
        && !payload.will_retry
    { error_seen = true; }
} else if let ServerNotification::TurnCompleted(payload) = &notification
    && payload.thread_id == primary_thread_id_for_requests
    && payload.turn.id == task_id
    && matches!(payload.turn.status,
        codex_app_server_protocol::TurnStatus::Failed
            | codex_app_server_protocol::TurnStatus::Interrupted)
{ error_seen = true; }
```
plus a server-request handler error (e.g. `"permissions approval is not supported in exec mode"`, `exec/src/lib.rs:2199-2211`).

### ⚠️ `Interrupted` is the trap for JSONL consumers
On `TurnStatus::Interrupted` the JSONL processor pushes **no terminal event at
all** — no `turn.completed`, no `turn.failed`
(`event_processor_with_jsonl_output.rs:559-563`):
```rust
TurnStatus::Interrupted => {
    self.final_message = None;
    self.emit_final_message_on_shutdown = false;
    CodexStatus::InitiateShutdown
}
```
yet `error_seen` still becomes true and the process exits **1**. So
"exit code 1 + a `turn.failed` event" is **not** an exhaustive failure signal;
`Interrupted` yields exit 1 with a silently truncated event stream.

### Ways to distinguish success from error, beyond exit code
1. `turn.completed` present (success) vs `turn.failed` (error) vs **neither** (interrupted/lagged) — the last must be treated as failure.
2. Exit code: `0` on success, `1` on any `error_seen`.
3. `--output-last-message/-o FILE`: written **only** on success — `print_final_output` calls `handle_last_message` only `if self.emit_final_message_on_shutdown` (`event_processor_with_jsonl_output.rs:631-636`), and that flag is set `true` only on `Completed` and `false` on `Failed`/`Interrupted`. If there is no final agent message it writes **empty content** and warns on stderr (`exec/src/event_processor.rs:31-39`). A failure does not write or delete the file, so use "was it freshly written", not "does it exist", if the path may pre-exist.
4. No other exit codes exist — only `0` and `1` occur in `exec`/`cli` sources.

### Git repo check
`exec/src/lib.rs:1006-1014`:
```rust
// When --yolo (dangerously_bypass_approvals_and_sandbox) is set, also skip the git repo check
// since the user is explicitly running in an externally sandboxed environment.
if !skip_git_repo_check
    && !dangerously_bypass_approvals_and_sandbox
    && get_git_repo_root(&default_cwd).is_none()
{
    eprintln!("Not inside a trusted directory and --skip-git-repo-check was not specified.");
    std::process::exit(1);
}
```
Exact stderr string: **`Not inside a trusted directory and --skip-git-repo-check was not specified.`**
Exit code **1**. Note this is *not* reported as a JSONL event even with `--json`
(it is a pre-flight `eprintln!` + `exit(1)` before the client starts), so a
JSONL-only consumer sees an empty stream. It is also skipped by `--yolo`. Since
you clone, the check passes; but note the check requires a **git repo root**, and
`get_git_repo_root` will not find one if the clone directory is detached from git
metadata.

---

## Left UNCONFIRMED
1. Whether `-c 'projects.<path>.trust_level=trusted'` works end-to-end in practice (code path supports it; no test exercises it).
2. Any official prose recommendation to run headless with trust disabled (docs 403).
3. Whether the "untrusted" default is ever auto-escalated for a repo with a project-root marker (`project_root_markers`) — markers affect project-root *discovery*, not trust; no auto-trust path was found.
