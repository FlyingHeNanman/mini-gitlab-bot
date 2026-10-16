# OpenAI Codex CLI (`codex`, Rust) — sourced research brief

**Version context (critical).** Ground truth below is the repo at commit
`6ae8b8aa7c608730d4315d59c5c081897ae02780` (2026-10-16), which is ~= the current
stable release `rust-v0.163.0` (published 2026-10-15). Several flags in the
question **no longer exist** at this version; where that matters I give the
version where the flag still worked.

**Sourcing caveat.** `developers.openai.com/codex/*` returned **HTTP 403** to
every fetch from this environment, and `raw.githubusercontent.com` was
unreachable, so official prose docs could not be read directly. Claims below are
therefore cited to (a) repo source at a pinned SHA, and (b) the one official
GitHub maintainer discussion below. `mintlify.wiki/openai/codex` was reachable
but is **unreliable — do not trust it** (see "Mirror reliability" at the end).

Repo paths are cited as
`https://github.com/openai/codex/blob/6ae8b8aa7c608730d4315d59c5c081897ae02780/<path>`.

---

## 1. `codex exec` flags, exit code, machine-readable output

`codex exec`'s own arg struct: `codex-rs/exec/src/cli.rs`.
`--sandbox/-s`, `--cd/-C`, `--model/-m`, `--add-dir`, `--approve-for-me` come
from the shared struct `SharedCliOptions` (`codex-rs/utils/cli/src/shared_options.rs`),
flattened into both the root CLI and `exec`.

Confirmed present at HEAD (source-cited):

| Flag | Status | Evidence |
|---|---|---|
| `--sandbox <mode>` / `-s` | present; values `read-only`,`workspace-write`,`danger-full-access` | `codex-rs/utils/cli/src/shared_options.rs:40-41`; values in generated schema `SandboxMode` |
| `--cd <DIR>` / `-C` | present | `shared_options.rs:67-68` |
| `--json` (alias `--experimental-json`) | present, **stdout JSONL** | `exec/src/cli.rs:64-70`; `exec/src/event_processor_with_jsonl_output.rs:104-108` uses `println!` |
| `--skip-git-repo-check` | present | `exec/src/cli.rs:36-38` |
| `--output-last-message <FILE>` / `-o` | present | `exec/src/cli.rs:72-79` |
| `-c key=value` / `--config key=value` | present, appendable, **global** | `codex-rs/utils/cli/src/config_override.rs:29-36` |
| `--model <MODEL>` / `-m` | present | `shared_options.rs:21-23` |
| `--full-auto` | **REMOVED** | absent from HEAD `exec/src/cli.rs` and `shared_options.rs` |
| `--ask-for-approval <POLICY>` / `-a` | **not a flag of `exec`**; defined only on the interactive TUI CLI | `codex-rs/tui/src/cli.rs:73-75` |

`--full-auto` version history (checked via GitHub contents API):
present at `rust-v0.50.0`, `rust-v0.100.0`, `rust-v0.120.0`, `rust-v0.140.0`,
`rust-v0.145.0`; **absent** at `rust-v0.147.0` and all later. At v0.120.0 it was
documented in-source as *"Convenience alias for low-friction sandboxed automatic
execution (--sandbox workspace-write)"* and was `conflicts_with
dangerously_bypass_approvals_and_sandbox`. Replacement at HEAD is
`--approve-for-me` (alias `--not-so-yolo`), which injects
`approvals_reviewer="auto_review"`, `approval_policy="on-request"`,
`sandbox_mode="workspace-write"` as config overrides
(`shared_options.rs:43-50`, `shared_options.rs:80-93`).
Ref: <https://github.com/openai/codex/blob/6ae8b8aa7c608730d4315d59c5c081897ae02780/codex-rs/exec/src/cli.rs>

**Does `exec` default approval policy to `never`? YES — hardcoded.**
`exec/src/lib.rs:581-586`:
```rust
let overrides = ConfigOverrides {
    ...
    // Default to never ask for approvals in headless mode. Rebuild below if
    // the fully resolved reviewer is AutoReview.
    approval_policy: Some(AskForApproval::Never),
```
Ref: <https://github.com/openai/codex/blob/6ae8b8aa7c608730d4315d59c5c081897ae02780/codex-rs/exec/src/lib.rs#L581-L586>

**`--ask-for-approval` and root flags with `exec`.** The root CLI **flattens**
the TUI CLI, so `codex --ask-for-approval never exec "..."` *parses*; but the
production dispatch for `Subcommand::Exec` inherits **only**
`interactive.shared` (i.e. `SharedCliOptions`) plus root `--config` overrides —
it does **not** forward `interactive.approval_policy`
(`codex-rs/cli/src/main.rs:1153-1166`). So `--ask-for-approval` does not reach
`exec`'s approval policy. **To change it headlessly, use a config override**,
e.g. `-c approval_policy=on-request` (this mechanism is proven by
`take_auto_review_config_overrides`, which sets approval policy exactly this way).
Whether a root-level `--ask-for-approval` is silently ignored vs. rejected for
`exec` in all builds: treat "ignored" as **UNCONFIRMED** beyond the code path above.
Ref: <https://github.com/openai/codex/blob/6ae8b8aa7c608730d4315d59c5c081897ae02780/codex-rs/cli/src/main.rs#L1153-L1166>

**Exit code / output behavior.**
- Only exit codes `0` and `1` appear in `exec`/`cli` sources (`exit(0)` x15,
  `exit(1)` x45); `cli/src/exit_status.rs` propagates a child's code, or
  `128 + signal`, else `1`. `run_main(...).await?` in `exec/src/main.rs` means an
  `Err` exits `1` via Rust's default `Termination`. **No exit code 2** for
  usage/validation was found. Ref: `codex-rs/exec/src/main.rs`,
  `codex-rs/cli/src/exit_status.rs`.
- Human mode writes progress to **stderr**, and prints the final agent message to
  **stdout** when stdout is not a TTY (piped) — `should_print_final_message_to_stdout`
  at `exec/src/event_processor_with_human_output.rs:524-529`; the processor is
  constructed with `stderr_with_ansi` (`exec/src/lib.rs:885-890`).
- Machine-readable output = `--json` (JSONL on stdout) and/or
  `--output-last-message FILE`; `--output-schema FILE` constrains the model's
  final response shape (`exec/src/cli.rs:52-54`).
- JSONL event `type` values: `thread.started`, `turn.started`, `turn.completed`,
  `turn.failed`, `item.started`, `item.updated`, `item.completed`, `error`
  (`exec/src/exec_events.rs:9-37`).
- A configured MCP server with `required = true` makes `codex exec` exit with an
  error if that server fails to initialize (`codex-rs/config/src/mcp_types.rs:238-242`).

---

## 2. Sandbox semantics: `workspace-write`

**Network: DISABLED by default.** In `codex-rs/config/src/types.rs:1234-1243`
```rust
pub struct SandboxWorkspaceWrite {
    #[serde(default)]
    pub writable_roots: Vec<AbsolutePathBuf>,
    #[serde(default)]
    pub network_access: bool,
    #[serde(default)]
    pub exclude_tmpdir_env_var: bool,
    #[serde(default)]
    pub exclude_slash_tmp: bool,
}
```
`#[serde(default)]` on a `bool` = `false`, and the generated schema confirms
`"network_access": {"default": false}`. The config key is
**`sandbox_workspace_write.network_access`** (table `[sandbox_workspace_write]`,
field `network_access`) — `ConfigToml` field declared at
`codex-rs/config/src/config_toml.rs:234`.
Refs: <https://github.com/openai/codex/blob/6ae8b8aa7c608730d4315d59c5c081897ae02780/codex-rs/config/src/types.rs#L1234-L1243>,
<https://github.com/openai/codex/blob/6ae8b8aa7c608730d4315d59c5c081897ae02780/codex-rs/core/config.schema.json>

Note the policy type also has a global default of restricted:
`NetworkSandboxPolicy { #[default] Restricted, Enabled }`
(`codex-rs/protocol/src/permissions.rs:86-90`).

Also note `sandbox_mode = "read-only"` in the (auto-generated) mirror docs is
described as allowing network "if enabled" — the authoritative schema is
`"network_access": {"default": false}` for the workspace-write table; for
read-only the network toggle is a separate `ReadOnly{network_access}` field.

**Writable vs readable. The agent CAN READ essentially the whole filesystem,
including `/root/.codex/config.toml`, `/etc`, and other repos.** The Linux
implementation explicitly allows read on `/` and only narrows writes
(`codex-rs/linux-sandbox/src/seccomp.rs:140-166`):
```rust
/// Installs Landlock file-system rules on the current thread allowing read
/// access to the entire file-system while restricting write access to
/// `/dev/null` and the provided list of `writable_roots`.
...
let access_rw = AccessFs::from_all(abi);
let access_ro = AccessFs::from_read(abi);
let mut ruleset = Ruleset::default()
    ...
    .add_rules(landlock::path_beneath_rules(&["/"], access_ro))?
    .add_rules(landlock::path_beneath_rules(&["/dev/null"], access_rw))?
...
if !writable_roots.is_empty() {
    ruleset = ruleset.add_rules(landlock::path_beneath_rules(&writable_roots, access_rw))?;
}
```
Writable set = workspace roots + `sandbox_workspace_write.writable_roots` +
`/dev/null`, plus `/tmp` and `$TMPDIR` unless disabled via `exclude_slash_tmp` /
`exclude_tmpdir_env_var` (that is precisely what those two flags are for;
`permissions.rs:1507-1511` refers to configured writable roots "including `/tmp`").
Ref: <https://github.com/openai/codex/blob/6ae8b8aa7c608730d4315d59c5c081897ae02780/codex-rs/linux-sandbox/src/seccomp.rs#L140-L166>

**A newer `permissions` block exists, but it does NOT remove
`sandbox_workspace_write` — both are live at HEAD.** `ConfigToml` has
`sandbox_mode`, `sandbox_workspace_write`, `default_permissions`, and
`permissions` side by side (`config_toml.rs:222-243`); the generated schema lists
all four as top-level keys. No `deprecat*` marker was found on
`sandbox_workspace_write` in `config_toml.rs` or `permissions_toml.rs`.
The new model (`codex-rs/config/src/permissions_toml.rs`):
`[permissions.<name>]` → `PermissionProfileToml { description, extends,
workspace_roots, filesystem, network }`; `filesystem` is a map of
path/special-path → `"read" | "write" | "deny"` (with legacy `"none"` aliasing
`deny`) plus `glob_scan_max_depth`; `network` is a rich proxy/domain config
(`enabled`, `mode = "limited" | "full"`, `domains`, `proxy_url`, `unix_sockets`,
MitM hooks…). Named profiles are selected by `default_permissions = "<name>"`,
and `:`-prefixed names are built-ins.
Refs: <https://github.com/openai/codex/blob/6ae8b8aa7c608730d4315d59c5c081897ae02780/codex-rs/config/src/permissions_toml.rs>,
<https://github.com/openai/codex/blob/6ae8b8aa7c608730d4315d59c5c081897ae02780/codex-rs/protocol/src/permissions.rs#L120-L136>

**On reported "migration issues":** I could not reach the official issue tracker
text from this environment. Concrete, verified friction points that would produce
migration pain are: (i) the two systems are separate code paths
(`derive_permission_profile` explicitly warns that named `[permissions]` profiles
"must be compiled through the permissions profile pipeline, not reconstructed
from `sandbox_mode`" — `codex-rs/core/src/config/config_toml.rs:844-847`);
(ii) on Windows `CODEX_HOME/managed_config.toml` is no longer supported and
emits a deprecation message directing to
`%ProgramData%\OpenAI\Codex\requirements.toml`
(`codex-rs/config/src/loader/layer_io.rs:86`). Specific third-party migration bug
reports: **UNCONFIRMED**.

---

## 3. `[mcp_servers.<name>]` supported fields

Authoritative accepted TOML shape = `RawMcpServerConfig`
(`codex-rs/config/src/mcp_types.rs:365-435`), with `#[schemars(deny_unknown_fields)]`
(unknown keys are rejected). Stdio/HTTP transport split at
`McpServerTransportConfig` (`mcp_types.rs:612-646`).

**stdio:** `command` (String), `args` (`Vec<String>`), `env`
(`HashMap<String,String>` — literal values), **`env_vars`**
(`Vec<McpServerEnvVar>`), `cwd`.
**streamable HTTP:** `url`, `bearer_token`, `bearer_token_env_var`,
`http_headers`, `env_http_headers`, `http_headers_helper`.
**shared:** `environment_id`, `auth` (`oauth`|`chatgpt`|`ema_auth`),
`startup_timeout_sec` (f64), `startup_timeout_ms` (u64), `tool_timeout_sec`,
`enabled` (default **true**), `required`, `startup_readiness`,
`supports_parallel_tool_calls`, `tool_input_schema_max_bytes`,
`omit_tools_from`, `default_tools_approval_mode`, `enabled_tools`,
`disabled_tools`, `scopes`, `oauth`, `oauth_resource`, `name` (legacy
display-name, accepted for back-compat), `tools`.

Per-tool sub-table `[mcp_servers.<name>.tools.<tool>]` =
`McpServerToolConfig { approval_mode: Option<AppToolApproval>,
output_token_limit: Option<NonZeroUsize> }` (`mcp_types.rs:77-95`).

Ref: <https://github.com/openai/codex/blob/6ae8b8aa7c608730d4315d59c5c081897ae02780/codex-rs/config/src/mcp_types.rs#L365-L435>

**`env_vars` EXISTS — confirmed, and it is exactly the "pass through host env
vars instead of hardcoding secrets" mechanism.** Type (`mcp_types.rs:97-135`):
```rust
#[derive(Serialize, Deserialize, Debug, Clone, PartialEq, Eq, JsonSchema)]
#[serde(untagged, deny_unknown_fields)]
pub enum McpServerEnvVar {
    Name(String),
    Config {
        name: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        source: Option<String>,
    },
}
```
So `env_vars = ["GITLAB_TOKEN"]` (bare name) or
`env_vars = [{ name = "GITLAB_TOKEN", source = "local" }]`; `source` may be
`"local"` or `"remote"` (any other value is a validation error:
`"unsupported env_vars source ...; expected `local` or `remote`"`).

**Exact semantics (resolution code):** for local stdio,
`create_env_for_mcp_server` builds the child env by reading each named variable
**from the Codex process's own environment** and passing it to the child
(`codex-rs/rmcp-client/src/utils.rs:16-59`):
```rust
let additional_env_vars = local_stdio_env_var_names(env_vars)?;
let mut env: HashMap<OsString, OsString> = DEFAULT_ENV_VARS
    .iter().copied().chain(additional_env_vars)
    .filter_map(|var| env::var_os(var).map(|value| (OsString::from(var), value)))
    .collect();
...
for (name, value) in extra_env.unwrap_or_default() { ... env.insert(name, value); }
```
`DEFAULT_ENV_VARS` = `HOME, LOGNAME, PATH, SHELL, USER, __CF_USER_TEXT_ENCODING,
LANG, LC_ALL, TERM, TMPDIR, TZ` — always inherited. Note: `env_vars` supplies
**names only**; the *value* is never written in config, and there is no
`source = "literal"`. `env` (map) overrides and is applied after. For local
stdio, `source = "remote"` is rejected outright
(`utils.rs:90-96`: *"env_vars entry `X` uses source `remote`, which requires
remote MCP stdio"*). Non-inheritable env var names are filtered out
(`utils.rs:54-57`). Note this also means `env_vars` is a **read-through of
Codex's ambient environment** — so the secret must be exported into the
environment Codex itself runs in.

Refs: <https://github.com/openai/codex/blob/6ae8b8aa7c608730d4315d59c5c081897ae02780/codex-rs/rmcp-client/src/utils.rs#L16-L101>,
<https://github.com/openai/codex/blob/6ae8b8aa7c608730d4315d59c5c081897ae02780/codex-rs/config/src/mcp_types.rs#L97-L135>

`bearer_token_env_var` doc (verbatim, for HTTP transport): *"Name of the
environment variable to read for an HTTP bearer token. When set, requests will
include the token via `Authorization: Bearer <token>`. The actual secret value
must be provided via the environment."* (`mcp_types.rs:630-632`).
`env_http_headers`: *"HTTP headers where the value is sourced from an environment
variable."* (`mcp_types.rs:638-640`).

---

## 4. ⚠️ Do MCP server child processes run inside or outside the Codex shell sandbox?

**ANSWER: OUTSIDE. MCP stdio servers are ordinary child processes of the Codex
orchestrator; the shell sandbox (Landlock/seccomp or Seatbelt/bubblewrap) is
applied only to model-initiated shell commands.** This is stated in-code and
confirmed by the absence of any sandbox in the MCP spawn path. Therefore **yes:
a stdio MCP server process can make arbitrary network calls (e.g. HTTPS to a
GitLab server) even when the shell sandbox blocks network.**

Design-by-design statement, `codex-rs/rmcp-client/src/stdio_server_launcher.rs:1-13`:
```
//! This module owns the "where does the server process run?" decision:
//!
//! - [`LocalStdioServerLauncher`] starts the configured command as a child of
//!   the orchestrator process.
//! - [`ExecutorStdioServerLauncher`] starts the configured command through the
//!   executor process API.
```

The actual spawn — no sandbox wrapper, no Landlock/seccomp, direct `Command`:
`stdio_server_launcher.rs:262-292`
```rust
let envs = create_env_for_mcp_server(env, &env_vars).map_err(io::Error::other)?;
let cwd = cwd.map(PathBuf::from).unwrap_or(fallback_cwd);
let resolved_program = program_resolver::resolve(program, &envs, &cwd).map_err(io::Error::other)?;

let build_command = || {
    let mut command = Command::new(&resolved_program);
    command.current_dir(&cwd).envs(&envs).args(&args);
    command.process_mode(ProcessMode::NewGroup);
    #[cfg(unix)]
    command.descriptor_policy(DescriptorPolicy::Explicit);
    command
};
```
Also, the only occurrence of the word "sandbox" anywhere in `codex-rs/rmcp-client/src/`
is `sandbox: None` — in the *executor* path's process-start params
(`stdio_server_launcher.rs:619-635`):
```rust
.start(ExecParams {
    ...
    sandbox: None,
    enforce_managed_network: false,
    managed_network: None,
    network_proxy: None,
})
```
So even the non-local (executor/remote) MCP path explicitly requests **no
sandbox and no managed network**.

Contrast — the sandbox is applied to the **shell command** path only, in a
separate helper process. `codex-rs/linux-sandbox/src/seccomp.rs:36-37`:
```
/// Apply sandbox policies inside this thread so only the child inherits
/// them, not the entire CLI process.
```
and the network filter is installed as a seccomp filter on *that* thread
(`seccomp.rs:78-80`, `install_network_seccomp_filter_on_current_thread`), denying
`connect`, `accept`, `bind`, `listen`, `sendto`, `sendmmsg`, etc. and allowing
only `AF_UNIX` sockets in `NetworkSeccompMode::Restricted`
(`seccomp.rs:203-234`). Its only callers are in the standalone sandbox launcher
binary `linux_run_main.rs:205,251,319`; the shell-command path reaches it via
`codex_linux_sandbox_exe` (`codex-rs/core/src/exec.rs:403-420`), and
`codex-exec`'s `main.rs` documents that the same binary doubles as
`codex-linux-sandbox` via arg0 dispatch (`codex-rs/exec/src/main.rs:1-15`).
Refs:
<https://github.com/openai/codex/blob/6ae8b8aa7c608730d4315d59c5c081897ae02780/codex-rs/rmcp-client/src/stdio_server_launcher.rs#L1-L13>,
<https://github.com/openai/codex/blob/6ae8b8aa7c608730d4315d59c5c081897ae02780/codex-rs/rmcp-client/src/stdio_server_launcher.rs#L262-L292>,
<https://github.com/openai/codex/blob/6ae8b8aa7c608730d4315d59c5c081897ae02780/codex-rs/rmcp-client/src/stdio_server_launcher.rs#L619-L635>,
<https://github.com/openai/codex/blob/6ae8b8aa7c608730d4315d59c5c081897ae02780/codex-rs/linux-sandbox/src/seccomp.rs#L140-L166>

**Corroborating, and the nuance you should know for your architecture:** Codex
does not enforce the sandbox on MCP servers — it *advertises the sandbox policy
to them* and lets the server sandbox its own work. Verbatim
(`codex-rs/codex-mcp/src/sandbox_metadata.rs:14-21, 43-44`):
```rust
/// Servers advertise this experimental capability to receive opaque sandbox state
/// in tool-call `_meta`, along with a local launcher when one is available.
pub const MCP_SANDBOX_STATE_CAPABILITY: &str = "codex/sandbox-state";
/// Deprecated capability and metadata spelling, retained as an identical alias.
pub const MCP_SANDBOX_STATE_META_CAPABILITY: &str = "codex/sandbox-state-meta";
/// Tool-call metadata describing a sandbox launcher on the MCP server's host.
pub const MCP_SANDBOX_LAUNCHER_META_KEY: &str = "codex/sandbox-launcher";
...
/// The known launcher envelope is distinct from the opaque policy. Absence never
/// authorizes searching PATH or falling back to an unsandboxed process.
```
The state object forwarded is
`SandboxState { permission_profile, codex_linux_sandbox_exe, sandbox_cwd,
use_legacy_landlock, use_mxc }`, and the launcher is a path to the Codex CLI so
the server can call `codex sandbox --sandbox-state-json`. Note the comment at
`mcp_types.rs:238-242` that `codex exec` errors on a `required = true` server
failing to init, and the effective env-var name `codex/sandbox-launcher`.
Refs: <https://github.com/openai/codex/blob/6ae8b8aa7c608730d4315d59c5c081897ae02780/codex-rs/codex-mcp/src/sandbox_metadata.rs>,
<https://github.com/openai/codex/blob/6ae8b8aa7c608730d4315d59c5c081897ae02780/codex-rs/core/src/mcp_tool_call.rs#L875-L905>

**Practical consequence for a "MCP server that talks to GitLab" design:** the
MCP server is *not* constrained by `sandbox_workspace_write.network_access = false`.
Its network reachability is whatever the OS grants the Codex process — i.e. it
is a **sandbox escape hatch by design**. If you need the MCP server's network
calls to be mediated, you must either (a) run the server under a separate
externally-applied sandbox/network policy, (b) implement the `codex/sandbox-state`
capability and honor the forwarded `SandboxState` yourself, or (c) move the
network call into the sandboxed shell path. Note also (architecture-relevant)
that `descriptor_policy(DescriptorPolicy::Explicit)` is Unix-only and Windows MCP
processes "can still inherit unrelated handles"
(`stdio_server_launcher.rs:285-290`).

---

## 5. `[model_providers.<id>]` fields

Source of truth: `ModelProviderInfo` (`codex-rs/model-provider-info/src/lib.rs:139-207`),
`#[schemars(deny_unknown_fields)]`. Full field list at HEAD:

`name` (String, default `""`), `base_url`, `model_catalog_url`, **`env_key`**,
`env_key_instructions`, `experimental_bearer_token`, `auth`
(`ModelProviderAuthInfo`: command-backed bearer token), `gateway_oauth`, `aws`
(SigV4), **`wire_api`**, `capabilities`, **`query_params`**
(`HashMap<String, RedactedString>`), **`http_headers`**
(`HashMap<String, RedactedString>`), **`env_http_headers`**
(`HashMap<String,String>` = header → env var name), `request_max_retries`,
`stream_max_retries`, `stream_idle_timeout_ms`, `websocket_connect_timeout_ms`,
**`requires_openai_auth`** (bool, default `false`), `supports_websockets`,
`supports_standalone_web_search`, `include_internal_metadata` (`#[serde(skip)]`,
runtime-only).

**`requires_openai_auth` and `http_headers` both EXIST.** `requires_openai_auth`
doc, verbatim: *"Does this provider require an OpenAI API key or ChatGPT login
token? If true, the user is presented with a login screen on first run, and
credentials are stored using the backend selected by `cli_auth_credentials_store`.
If false (the default), the login screen is skipped, and the API key (if needed)
comes from the environment variable specified by `env_key`."*
`http_headers` doc: *"Additional HTTP headers to include in requests to this
provider where the (key, value) pairs are the header name and value."*
`env_http_headers`: *"...the (key, value) pairs are the header name and
_environment variable_ whose value should be used. If the environment variable is
not set, or the value is empty, the header will not be included in the request."*
`query_params`: *"Optional query parameters to append to the base URL."*
Ref: <https://github.com/openai/codex/blob/6ae8b8aa7c608730d4315d59c5c081897ae02780/codex-rs/model-provider-info/src/lib.rs#L139-L207>

**`wire_api` — ONLY `responses` IS ACCEPTED at HEAD. `chat` is a hard error.**
```rust
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, JsonSchema)]
#[serde(rename_all = "lowercase")]
pub enum WireApi {
    /// The Responses API exposed by OpenAI at `/v1/responses`.
    #[default]
    Responses,
}
...
match value.as_str() {
    "responses" => Ok(Self::Responses),
    "chat" => Err(serde::de::Error::custom(CHAT_WIRE_API_REMOVED_ERROR)),
    _ => Err(serde::de::Error::unknown_variant(&value, &["responses"])),
}
```
with the exact, quotable error string:
```
`wire_api = "chat"` is no longer supported.
How to fix: set `wire_api = "responses"` in your provider config.
More info: https://github.com/openai/codex/discussions/7782
```
So at HEAD the answer to "`chat` vs `responses`" is: **`responses` only, and it is
the default**; specifying `chat` fails config validation with that message. The
built-in OSS providers are also Responses-only:
`create_oss_provider(DEFAULT_OLLAMA_PORT, WireApi::Responses)` and the same for
`lmstudio`, and the legacy provider id `ollama-chat` is removed with its own
error string (`model-provider-info/src/lib.rs:104-107, 656-692`).
Refs: <https://github.com/openai/codex/blob/6ae8b8aa7c608730d4315d59c5c081897ae02780/codex-rs/model-provider-info/src/lib.rs#L107-L136>

**Official deprecation notice (maintainer `etraut-openai`, Dec 9 2025):**
*"the Codex team is deprecating support for the chat/completions API. Full
removal is slated for early February 2026."* … *"If you have configured a custom
model provider with `wire_api = "chat"` or have not specified a `wire_api`, you
will need to update your model provider definition."* … *"Local models / OSS
usage — For local models — including OpenAI's gpt_oss, you can use LM Studio,
which supports the responses API."* … *"Maintainers of Ollama are planning to add
support for the responses API in the near future."* … *"If your organization uses
an LLM proxy or gateway, please coordinate with your IT or platform team to
ensure your proxy supports the responses API before the end of the deprecation
period."* Title: **"Deprecating `chat/completions` support in Codex"**.
Ref: <https://github.com/openai/codex/discussions/7782>

**vLLM / Ollama / local-endpoint caveats (this is the big one).** Because
`wire_api = "chat"` is gone, an internal endpoint must speak the OpenAI
**Responses** API (`/v1/responses`), not `/v1/chat/completions`. vLLM's
OpenAI-compatible server historically exposes chat/completions; whether any given
vLLM/Ollama/proxy build implements Responses — and whether it implements the
Responses **tool-calling / streaming tool-call** parts Codex needs — could not be
verified from this environment: **UNCONFIRMED**. Source-level facts that bear on
it: `ModelProviderCapabilities { external_web_access, remote_compaction }` exists
for "custom Responses-compatible provider" overrides
(`model-provider-info/src/lib.rs:177-180`), and provider `validate()` rejects
various auth-field combinations (e.g. `env_key` + `auth` + `requires_openai_auth`
together is an error). The maintainer notice explicitly frames gateways as the
coordination risk. Tool-calling failures on such endpoints: **UNCONFIRMED**
(the specific failure modes are not documented in the sources I could reach).
Ref: <https://github.com/openai/codex/discussions/7782>

**Built-in providers at HEAD:** `openai`, `amazon-bedrock`,
`amazon-bedrock-runtime`, `ollama`, `lmstudio`
(`model-provider-info/src/lib.rs:656-692`); configured
`[model_providers.*]` entries **extend** the set and are *not* generally
overridable, except the two Bedrock ids (`merge_configured_model_providers`,
`lib.rs:694-734`). OSS defaults are
`http://localhost:<port>/v1` with port from `CODEX_OSS_PORT`/`CODEX_OSS_BASE_URL`
(`lib.rs:736-753`). Refs:
<https://github.com/openai/codex/blob/6ae8b8aa7c608730d4315d59c5c081897ae02780/codex-rs/model-provider-info/src/lib.rs#L656-L753>

---

## 6. Config file location and precedence

`~/.codex` default and `$CODEX_HOME` override (`codex-rs/utils/home-dir/src/lib.rs:5-63`),
verbatim doc:
```
/// Returns the path to the Codex configuration directory, which can be
/// specified by the `CODEX_HOME` environment variable. If not set, defaults to
/// `~/.codex`.
///
/// - If `CODEX_HOME` is set, the value must exist and be a directory. The
///   value will be canonicalized and this function will Err otherwise.
/// - If `CODEX_HOME` is not set, this function does not verify that the
///   directory exists.
```
So: `$CODEX_HOME/config.toml`, and a **hard error** if `CODEX_HOME` is set to a
non-existent path or a non-directory. Ref:
<https://github.com/openai/codex/blob/6ae8b8aa7c608730d4315d59c5c081897ae02780/codex-rs/utils/home-dir/src/lib.rs#L5-L63>

**Layer precedence, ascending (later wins)** — verbatim from
`codex-rs/config/src/loader/mod.rs:123-137`:
```
/// - package:  optional default configuration supplied with the Codex package
/// - admin:    managed preferences (*)
/// - system    `/etc/codex/config.toml` (Unix) or
///   `%ProgramData%\OpenAI\Codex\config.toml` (Windows)
/// - cloud     enterprise-managed cloud config bundle fragments
/// - user      `${CODEX_HOME}/config.toml`
/// - profile   `${CODEX_HOME}/<name>.config.toml`, when selected
/// - cwd       `${PWD}/config.toml` (loaded but disabled when the directory is untrusted)
/// - tree      parent directories up to root looking for `./.codex/config.toml` (loaded but disabled when untrusted)
/// - repo      `$(git rev-parse --show-toplevel)/.codex/config.toml` (loaded but disabled when untrusted)
/// - runtime   e.g., --config flags, model selector in UI
///
/// (*) Only available on macOS via managed device profiles.
```
There is therefore **no single "project-level config"** — three separate
project-ish layers (`cwd`, `tree`, `repo`), all **disabled when the directory is
untrusted**. Requirements/enforcement layers (system, cloud, legacy
`/etc/codex/managed_config.toml` reinterpreted as requirements, macOS managed
prefs) are listed at `loader/mod.rs:108-121`.
Refs: <https://github.com/openai/codex/blob/6ae8b8aa7c608730d4315d59c5c081897ae02780/codex-rs/config/src/loader/mod.rs#L108-L139>

**`-c` / `--config` overrides** sit at the `runtime` (highest) layer.
Verbatim doc (`codex-rs/utils/cli/src/config_override.rs:20-28`):
```
/// Override a configuration value that would otherwise be loaded from
/// `~/.codex/config.toml`. Use a dotted path (`foo.bar.baz`) to override
/// nested values. The `value` portion is parsed as TOML. If it fails to
/// parse as TOML, the raw string is used as a literal.
///
/// Examples:
///   - `-c model="o3"`
///   - `-c 'sandbox_permissions=["disk-full-read-access"]'`
///   - `-c shell_environment_policy.inherit=all`
```
Parsing details: split on the **first** `=` only; value parsed as TOML by
wrapping as `_x_ = <value>` (so scalars, bools, arrays, and inline tables all
work); on parse failure the raw string is used with surrounding quotes stripped
(`config_override.rs:49-102`). One alias is canonicalized:
`use_legacy_landlock` → `features.use_legacy_landlock` (`:87-93`).
**Root vs subcommand ordering:** root-level `--config` flags are *prepended*
(`prepend_root_overrides`, `:42-45`), so **subcommand-level `-c` wins over
root-level `-c`**, and both win over every file layer. Also
`--ignore-user-config` skips `$CODEX_HOME/config.toml` while auth still uses
`CODEX_HOME` (`exec/src/cli.rs:44-46`).
Refs: <https://github.com/openai/codex/blob/6ae8b8aa7c608730d4315d59c5c081897ae02780/codex-rs/utils/cli/src/config_override.rs>,
<https://github.com/openai/codex/blob/6ae8b8aa7c608730d4315d59c5c081897ae02780/codex-rs/exec/src/cli.rs#L44-L46>

---

## 7. Auth for headless `codex exec` with a custom provider

**No interactive login is required for a custom provider when
`requires_openai_auth` is left at its default `false`.** Mechanism
(`codex-rs/model-provider-info/src/lib.rs:477-496`), verbatim:
```rust
/// If `env_key` is Some, returns the API key for this provider if present
/// (and non-empty) in the environment. If `env_key` is required but
/// cannot be found, returns an error.
pub fn api_key(&self) -> CodexResult<Option<String>> {
    match &self.env_key {
        Some(env_key) => {
            let api_key = std::env::var(env_key)
                .ok()
                .filter(|v| !v.trim().is_empty())
                .ok_or_else(|| CodexErr::EnvVar(EnvVarError {
                    var: env_key.clone(),
                    instructions: self.env_key_instructions.clone(),
                }))?;
            Ok(Some(api_key))
        }
        None => Ok(None),
    }
}
```
So: set `env_key = "MY_API_KEY"` in the provider, export `MY_API_KEY`, and the
key is read straight from the environment; if unset/empty/whitespace it is a
hard error carrying `env_key_instructions`. No `codex login`, no
`auth.json`. `experimental_bearer_token` is the discouraged inline alternative
(doc: *"Use of this config is discouraged in favor of `env_key` for security
reasons, but this may be necessary when using this programmatically"*), and
`auth` (command-backed token) is a third option — but `validate()` makes these
mutually exclusive: `env_key` + `experimental_bearer_token` + `auth` +
`requires_openai_auth` cannot be combined (`lib.rs:299-395`). Additional
non-interactive escape hatches: `--dangerously-bypass-approvals-and-sandbox`
(alias `--yolo`), `--oss`/`--local-provider`, `--ignore-user-config`.
Refs: <https://github.com/openai/codex/blob/6ae8b8aa7c608730d4315d59c5c081897ae02780/codex-rs/model-provider-info/src/lib.rs#L477-L496>,
<https://github.com/openai/codex/blob/6ae8b8aa7c608730d4315d59c5c081897ae02780/codex-rs/model-provider-info/src/lib.rs#L299-L395>

`requires_openai_auth = true` is what flips on the login screen / ChatGPT
credential storage — and it also feeds `uses_codex_backend()`
(`core/src/client.rs:1203-1212`), which requires `is_openai()`,
`requires_openai_auth`, and **no** `env_key`/bearer/auth/aws set. For an internal
OpenAI-compatible endpoint you want `requires_openai_auth = false` (default) or
omit it.
Ref: <https://github.com/openai/codex/blob/6ae8b8aa7c608730d4315d59c5c081897ae02780/codex-rs/core/src/client.rs#L1203-L1212>

---

## Mirror reliability warning (important for citation hygiene)

`mintlify.wiki/openai/codex` is **not** a trustworthy mirror of
`developers.openai.com`. Evidence it contains fabricated/auto-generated content:
- It documents a flag **`--progress-cursor`** ("Force cursor-based progress
  updates in exec mode") that **does not exist anywhere in the repo source**
  (no `progress-cursor`/`progress_cursor` match in `codex-rs/`).
- Its "Command-Line Options" table for `codex exec` **omits** real flags
  (`--sandbox`, `--cd`, `-c`, `--model`, `--skip-git-repo-check`) while listing
  `--full-auto` elsewhere.
- It carries a "Generated … by Mintlify / Claim this site" banner.
Its claims that *do* match source (e.g. exit code 0 = success, 1 = failure) can
be treated as corroboration only. Page:
<https://mintlify.wiki/openai/codex/advanced/exec-mode>

## Summary of items left UNCONFIRMED

1. Whether a root-level `codex --ask-for-approval X exec …` is silently ignored
   vs. rejected in all builds (code path implies "not forwarded").
2. Specific third-party reports of `permissions` ↔ `sandbox_workspace_write`
   migration bugs (official tracker unreachable: `developers.openai.com` 403).
3. Whether any specific vLLM/Ollama/proxy build implements the OpenAI
   **Responses** API well enough (streaming + tool calls) for Codex —
   the load-bearing question for a local/self-hosted provider today.
4. Exact `read-only` sandbox network default from official prose (source-level
   `ReadOnly { network_access }` exists; the workspace-write default `false` is
   confirmed).
