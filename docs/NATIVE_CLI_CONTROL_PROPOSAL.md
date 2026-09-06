# Native CLI control: upstream findings and proposed scope

Researched 2026-09-06. This is a proposal, not an implemented or published API.
The installed CLI and local Codex source checkout were not modified.

## Related upstream work

- [#15355: opt-in local ingress for interactive CLI/TUI](https://github.com/openai/codex/issues/15355)
  is open. It asks for a structured local interface into a live interactive
  session, avoiding keyboard/PTY injection. Its scope is broader than effort.
- [#33643: external control API for hardware controllers](https://github.com/openai/codex/issues/33643)
  is open and targets Desktop. Its discussion explicitly includes active-task
  discovery, get/set reasoning effort and device-independent controllers.
- [divyekant's implementation linked from #15355](https://github.com/openai/codex/issues/15355#issuecomment-4393744265)
  is a fork branch, not a merged upstream feature. Inspected commit
  `63ca2fdffe46e6e454a86151061a52525da1a3ea`:
  [protocol documentation](https://github.com/divyekant/codex/blob/63ca2fdffe46e6e454a86151061a52525da1a3ea/docs/tui-inbound-channel.md),
  [listener](https://github.com/divyekant/codex/blob/63ca2fdffe46e6e454a86151061a52525da1a3ea/codex-rs/tui/src/inbound_channel.rs).
  An opt-in Unix socket accepts JSONL user messages and queues native AppEvents.
  It exposes neither effort control nor selected-task status. `ok: true` is
  returned after enqueueing an event; it is not an applied-state acknowledgement.
  The implementation also uses unbounded line reads and removes an existing
  socket without proving it stale. It is architectural evidence, not a patch to
  import unchanged. Its reported tests were not rerun here.
- [PR #42151: model settings in thread metadata](https://github.com/openai/codex/pull/42151)
  merged September 1. Loaded tasks report current model/effort; unloaded tasks
  report their latest persisted settings, possibly null. This improves reads
  from the owning app-server but does not identify the task visible in a TUI.
- [PR #42328](https://github.com/openai/codex/pull/42328) merged September 2 and
  [PR #43110](https://github.com/openai/codex/pull/43110) merged September 5.
  They concern trusted reasoning configuration history and a disabled-by-default
  feature for recording changes. They do not expose a local TUI control endpoint.

No ready upstream PR implementing the complete selected-TUI effort interface
was found in the searches performed. Open issues are not an upstream commitment.

## Recommended change in our Codex fork

Add an opt-in, versioned, local settings endpoint owned by each TUI process.
Keep it device-independent. BUSY Bar connects as a small client; normal native
Codex startup owns the session and its backend as usual.

Proposed operations (names are illustrative):

| Operation | Result / semantics |
| --- | --- |
| `status/read` | TUI instance identity, full displayed task ID, focus, selection revision, model, effective effort, collaboration mode, supported effort choices, readiness and settings revision. No transcript. |
| `status/subscribe` | Initial snapshot followed by ordered changes; reconnect begins with a new snapshot. Includes focus loss, cached view switches, settings changes and task closure. |
| `effort/set` | Explicit effort plus expected instance, task, selection/settings revisions and a request ID. The TUI validates the current target, catalog and native ownership restrictions before using its existing settings path. |
| `request/read` | Read the result of a previously submitted request ID after a connection failure. Distinguish queued, applied and rejected states instead of guessing from a timeout. |

Process all target validation and dispatch in the TUI event loop. Never let a
socket worker race the selected widget. A task switch before dispatch rejects
the request. A switch after dispatch does not retarget it: completion names the
original task and request ID. Acknowledgement of receipt is separate from an
applied-settings event. Preserve Plan mode and all unrelated settings. Reuse
native supported levels and treat advanced-level policy explicitly; do not
silently change the keyboard shortcut policy for Max/Ultra.

The first implementation should reuse the current native next-turn settings
contract. It must not promise that an in-flight model response changes effort.
Global defaults are not rewritten by a session-scoped operation.

Use per-user private directories, same-user peer validation, bounded frames,
bounded connection/request state and instance-specific socket discovery.
Never unlink a live endpoint on startup. In-memory request results can expire
within a documented bound; process exit invalidates instance IDs and requests.

## Existing native integration points

Inspected local checkout: `/Users/michael/Developer/@oss/codex`, commit
`0cf189a2e4d1b71f3feb899b0f08c845da6aeee9`.

- `codex-rs/tui/src/app/thread_routing.rs`:
  `current_displayed_thread_id()` identifies the visible task from TUI state.
  Read it only when selection and widget configuration are settled.
- `codex-rs/tui/src/tui/event_stream.rs`: native focus gained/lost handling.
  Focus loss currently returns no App-level event; subscriptions must explicitly
  publish it instead of depending on redraws.
- `codex-rs/tui/src/app/event_dispatch.rs`: existing reasoning and Plan effort
  AppEvents already route through the active TUI session.
- `codex-rs/tui/src/app/thread_settings.rs`: existing parameter builders and
  settings synchronization preserve the native collaboration-mode semantics.
  Refactor to return a typed outcome for local control; reuse this implementation
  rather than maintaining a second settings implementation in Python.
- `codex-rs/tui/src/chatwidget/reasoning_shortcuts.rs`: native model catalog,
  effective effort and restrictions provide the behavior to align with.

The app-server already provides settings updates. The missing part for a dial
is a supported connection to the *displayed TUI*, with explicit selection and
request outcomes. Adding only another app-server listener does not solve that.

## BUSY Bar migration and acceptance

After native endpoint tests pass, migrate CLI control to the native client and
remove the Python PTY/WebSocket proxy, title parsing and interactive launch
wrapper from that path. The Desktop integration is separate. OS foreground-app
selection remains necessary to choose Desktop versus a terminal; distinguishing
terminal tabs still requires native terminal focus reporting or a terminal API.

Verify ordinary launch and resume, idle and active-turn settings semantics,
Plan mode, model-specific levels, cached task switches, split-pane focus,
restart discovery, rejected stale selections, parent-owned tasks, dropped
connections and duplicate request IDs. Assert native confirmed settings and TUI
display, not only successful socket writes. No prompt injection is needed to
control effort.
