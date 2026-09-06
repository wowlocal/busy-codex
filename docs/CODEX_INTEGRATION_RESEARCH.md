# Codex session control and usage sources

Reviewed on 2026-09-06 against the installed Codex CLI 0.153.4 and the source
repositories below. Third-party projects were inspected, not installed or run.

## What the projects actually implement

| Project and inspected source | Useful mechanism | Boundary |
| --- | --- | --- |
| [morgadoronan/codex-agents](https://github.com/morgadoronan/codex-agents/blob/e64568f73790e42b40427ef8f2a9c9ea1db2dd66/src/native_cli.rs), [backend](https://github.com/morgadoronan/codex-agents/blob/e64568f73790e42b40427ef8f2a9c9ea1db2dd66/src/app_server/backend.rs), [registry](https://github.com/morgadoronan/codex-agents/blob/e64568f73790e42b40427ef8f2a9c9ea1db2dd66/src/thread_registry.rs) | Launches the native TUI with `--remote` against a shared local app-server; tracks manager-owned thread IDs separately; clears inherited session identity on fresh launches. | Ownership of a loaded thread is distinct from the thread visible in a terminal. It does not establish arbitrary unwrapped CLI control. |
| [manuelsh/codex-monitor](https://github.com/manuelsh/codex-monitor/blob/e9abc2293d4931ba61f58768169e2c93d72bb500/server/src/active-sessions.ts), [RPC client](https://github.com/manuelsh/codex-monitor/blob/e9abc2293d4931ba61f58768169e2c93d72bb500/server/src/codex-client.ts) | Derives recent session activity from JSONL lifecycle events; manages an app-server RPC connection separately. | Recent activity and file modification times do not identify foreground focus. |
| [Dimillian/CodexMonitor](https://github.com/Dimillian/CodexMonitor/blob/dd61b9abd37de5ded86e82b9fe8a83fd49d46fa5/src-tauri/src/shared/codex_core.rs), [task selection](https://github.com/Dimillian/CodexMonitor/blob/dd61b9abd37de5ded86e82b9fe8a83fd49d46fa5/src/features/threads/hooks/useThreadActions.ts) | Owns its UI selection and sends `threadId`, model and effort with `turn/start`. | This controls its own client; it is not evidence of changing effort in an independently launched native TUI. |
| [steipete/CodexBar](https://github.com/steipete/CodexBar/blob/main/docs/codex.md) | Separates account quota retrieval (OAuth/API or native CLI RPC) from local cost/history scanning; documents freshness and suspicious weekly-reset handling. | Quota data describes an account window, not the visible task. Its default app source is OAuth; CLI RPC is another supported source. |

## The reproduced BUSY Bar failure

The old bridge treated any `thread/read` for a different ID as proof that the
user had switched views. It permanently set `ready=false` with the error
`CLI has multiple task views`. A real fresh TUI launch issued approximately 20
such reads to populate background task metadata. No user navigation was needed
to disable the dial. Restarting just repeated this failure.

Native Codex confirms the distinction: [session lifecycle and agent
selection](https://github.com/openai/codex/blob/0cf189a2e4d1b71f3feb899b0f08c845da6aeee9/codex-rs/tui/src/app/session_lifecycle.rs)
use reads for liveness/history, while already cached view switches can happen
locally without a selection RPC. Removing the read heuristic alone would leave
the dial following the last resumed task after some cached switches.

## Implemented selection and write path

The native [terminal-title configuration](https://github.com/openai/codex/blob/0cf189a2e4d1b71f3feb899b0f08c845da6aeee9/codex-rs/tui/src/bottom_pane/title_setup.rs)
includes `thread-id`. Its [renderer](https://github.com/openai/codex/blob/0cf189a2e4d1b71f3feb899b0f08c845da6aeee9/codex-rs/tui/src/chatwidget/status_surfaces.rs)
uses the displayed widget's ID and emits an OSC window-title report. In the
installed version, the UUID is shortened to 29 characters followed by `...`.

BUSY Bar observes these bounded title reports in its own PTY, resolves the
prefix uniquely against tasks loaded by its own server, and retains only
settings/focus metadata. Missing, ambiguous and closed selections disable
writes and recover as soon as a valid title appears. Other terminal output is
forwarded unchanged. Background reads do not choose or disable the target.

Effort changes still use `thread/settings/update` on the same live app-server,
preserve collaboration mode, and require `thread/settings/updated` confirmation.
They do not start a turn or rewrite rollout files. See the [app-server
contract](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md).

Account quotas continue to come from fresh `account/rateLimits/read` responses.
The seven-day window is identified by duration, and progress uses its actual
reset timestamp. Historical JSONL events are not authoritative account quotas.

## Validation and limits

- Real installed native TUI: ordinary startup with its background history reads,
  controller rotation `xhigh → max → xhigh`, and both changes observed in the
  TUI's own title. Only temporary empty test sessions were used; no model prompts
  were submitted.
- Event regression tests: reads and background resumes cannot select a task;
  cached title-only switches recover each task's current settings; prefix
  collisions, closed tasks, missing titles and stale write requests fail closed.
- Title stream tests: fragmented OSC reports, BEL/ST terminators, oversized
  input and unrelated terminal output.
- The real empty-session `/new` check selected the new task. Returning to the
  empty previous task was unavailable in native Codex before it had persisted
  history; cached return behavior is covered by source inspection and event
  tests, not claimed as a completed native end-to-end check.

This depends on the native title field and `--remote`/settings protocol being
available. A custom title must retain `thread-id`. Terminal focus reporting is
still required to distinguish tabs and panes. Already running bridges keep
their loaded Python code and require a new CLI launch to adopt this change.
Display workers are checked every ten seconds during connected CLI sessions.
Adapter checks reject zombie processes and reused PIDs; concurrent restarts are
serialized. Adapter stderr is available at `~/.claude/busybar-codex-adapter.log`.

To repeat the native check explicitly with your installed CLI and login:

```sh
python3 tests/check_codex_cli_live.py /path/to/native/codex-or-launcher
```

## Codex Micro / Work Louder comparison

The [official Codex Micro product page](https://openai.com/supply/co-lab/work-louder/)
describes a reasoning dial and task status lights. It does not publish a CLI
attachment API. The implementations below were inspected at these commits:

| Implementation | Actual control path | Relevance to BUSY Bar |
| --- | --- | --- |
| [mpociot/codex-micro-stream-deck-emulator protocol](https://github.com/mpociot/codex-micro-stream-deck-emulator/blob/7093bd48f0bcb953f623b40c727470e545b48df3/src/protocol.js), [development notes](https://github.com/mpociot/codex-micro-stream-deck-emulator/blob/7093bd48f0bcb953f623b40c727470e545b48df3/DEVELOPMENT.md) | Emulates vendor HID hardware for Desktop. Encoder notifications use `v.oai.hid`, `ENC_CW` / `ENC_CC`, and `act: 2`; the host sends status through `v.oai.thstatus`. Desktop owns the active task and effort mapping. The software shim patches Electron's `node-hid`; the virtual HID alternative requires a restricted Apple entitlement. | A Desktop hardware integration, not an API into an independently running CLI. No shim was installed and Desktop was not relaunched. |
| [maxxspotter/codex-micro-app controller](https://github.com/maxxspotter/codex-micro-app/blob/cf323ada1e9716073d0748caea503f6e0974ba1c/apps/mac-companion/Sources/CodexMicroCore/ControllerActions.swift), [shim control](https://github.com/maxxspotter/codex-micro-app/blob/cf323ada1e9716073d0748caea503f6e0974ba1c/apps/mac-companion/Sources/CodexMicroCore/CodexMicroShimControl.swift) | StandardBridge reads the live composer context and supported efforts, updates the conversation through its owner client, then waits for matching composer settings. NativeShim instead sends an encoder step and leaves mapping to Desktop. | Confirms the ownership + live confirmation pattern used by our Desktop controller. The project explicitly targets Desktop, not CLI. |

Local native source was also inspected at
`/Users/michael/Developer/@oss/codex`, commit
`0cf189a2e4d1b71f3feb899b0f08c845da6aeee9`.
The native [reasoning shortcuts](https://github.com/openai/codex/blob/0cf189a2e4d1b71f3feb899b0f08c845da6aeee9/codex-rs/tui/src/chatwidget/reasoning_shortcuts.rs)
operate on the visible widget, respect modal and parent-owned-task restrictions,
and intentionally require the advanced picker to enter Max/Ultra. Simulated
keystrokes would therefore not be an equivalent replacement for the current
settings control contract. The [settings processor](https://github.com/openai/codex/blob/0cf189a2e4d1b71f3feb899b0f08c845da6aeee9/codex-rs/app-server/src/request_processors/turn_processor.rs)
accepts `thread/settings/update` for loaded tasks, validates direct-input
ownership and submits the native settings operation.

### Follow-up ERR investigation

At 11:47:45 local time the running bridge reported a native rejection, but its
old error handler discarded the RPC code and reason. At 11:48:09 another
request timed out. Native logs show a settings request and a settings-updated
event at 11:48:06, but do not include enough correlation or settings data to
prove that event confirmed this particular dial change.

On an uncertain timeout the client now opens a fresh control connection and
reads live settings for the same selected task. It accepts success only if
both model and requested effort match; it never repeats the write. A real
rejection or unmatched/changed selection remains an error. Rejection diagnostics
now retain a numeric RPC code and a bounded, allowlisted reason category,
without copying arbitrary native error payloads. Controller logs include the
target kind, task ID and requested effort.

The opt-in full wrapper check exercises production `main()` and `run_tui()` in
addition to the bridge. Its only bridge changes mark new tasks ephemeral and
export test assertions; selection still comes from production title handling.
Native TUI output independently confirms effort. Verified all advertised Astra
levels from Low through Ultra and restoration to Low; ordinary launch and Plan
mode are checked separately. No model prompts are submitted. These successful
checks do not establish the cause of the user's earlier native rejection.

```sh
python3 tests/check_codex_cli_wrapper_live.py /path/to/native/codex-or-launcher
python3 tests/check_codex_cli_wrapper_live.py /path/to/native/codex-or-launcher --model gpt-6-astra --all-levels
python3 tests/check_codex_cli_wrapper_live.py /path/to/native/codex-or-launcher --plan
```

The timeout reconciliation runs in the display daemon and can be deployed
without closing CLI sessions. Improved native rejection classification lives
in the CLI bridge and takes effect on subsequent CLI launches.
