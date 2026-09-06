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
