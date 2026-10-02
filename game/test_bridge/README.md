> 更新时间：2026-10-02 12:25 +08:00（Asia/Shanghai）

# Opt-in GameTestBridge and isolated regression tools

This contribution adds a loopback-only HTTP/WebSocket test bridge to the existing game. It is disabled by default and is independent of the cafe scheduling contribution in PR #166. No cafe scheduling, provider, prompt, game resource or historical-save fixture changes are included.

The existing speed mutation now increments the world revision, so optimistic concurrency and state-change events observe an actual speed change. A replay/no-op does not increment it.

## Running the bridge

Start the game with user arguments `--gametest-bridge` and optionally `--gametest-port=18765`. Without `--gametest-commands`, the bridge is read-only. Adding `--gametest-commands` explicitly enables speed, movement and save/load commands; do not use write commands against a personal save when exploring the protocol.

- `GET /health`, `GET /world/state`, `GET /residents/{id}` expose public, whitelisted state, not credentials or resident memory contents.
- `POST /commands/set-speed`, `move-player`, `save`, `load` require command and idempotency IDs, expected session/generation/version and a timeout. Movement uses the existing physical input path, not teleportation. Save/load delegate to the existing session lifecycle and are asynchronous.
- `GET /commands/{commandId}` returns a receipt; HTTP acceptance does not mean completion.
- `/events` upgrades to a WebSocket semantic event stream. Queue overflow disconnects the client; there is no historical replay. Query state and receipts after reconnecting.

`protocol.schema.json` defines request/response/event contracts. The bridge bounds request size, connections, queued events and per-frame polling work. Loopback binding is not authentication: enable it only in a trusted local session.

## Deterministic regression

Use existing Python 3.11, Godot 4.7.2 and `requirements-phase2-core.txt`. Postman/Newman and Locust are optional independent interfaces; UI tests require a real Windows desktop and their separate UI dependencies. The GitHub Actions workflow runs core Bridge regressions and a separate Locust static job; it does not pretend a headless job validates the UI.

Run from the repository root (replace executable paths with your installed tools):

```powershell
# Use a NEW, nonexistent basetemp every time. pytest clears an existing basetemp.
python -B -m pytest -q -p no:cacheprovider -c game/test_bridge/pytest.ini game/test_bridge/phase2_tests --godot-bin D:/Godot/Godot_v4.7.2-stable_win64_console.exe --basetemp game/test_bridge/artifacts/unique-run-temp --junitxml game/test_bridge/artifacts/unique-run-junit.xml
godot --headless --path game --script res://tests/game_test_bridge_test.gd --log-file game/test_bridge/artifacts/unique-engine.log -- --gametest-isolated-test
```

The pytest fixture launches an isolated 15-resident World without booting normal UI/model services. Bridge save/load orchestration tests use a declared fake session host: they test receipts and lifecycle boundaries, **not real disk restoration**. The new read-only save test calls actual AgentSaveStore validation helpers using historical beta6 files and generated corrupt inputs, without invoking writes, restore transactions or recursive cleanup. It checks valid JSON/payload, truncated/non-object JSON, incorrect length/hash and unsupported version. This does **not** validate complete restoration, root configuration, rollback, world log/task/memory preservation or migration. Those remain separate validation work; do not run recursive-cleanup save suites under a no-deletion policy.

`run_phase2_ci.ps1` is a provider-neutral local/Actions entry point for preflight, core lint, pytest, Godot Bridge contracts and Newman report import. It uses a fresh per-run basetemp and retains failure artifacts. The upstream contribution deliberately excludes cafe-specific tests from this runner. Python dependencies are top-level pinned, not a full transitive lock.

## Failure evidence and ID boundaries

The same normalized record is appended to JSONL and SQLite. JUnit properties link a case to its requests, responses, transport failures, game events and failed assertions. A failed live assertion requests a bounded read-only public state snapshot; it is a **post-assertion read**, not an atomic failure-time snapshot.

| Identifier | Meaning |
|---|---|
| `testEvidenceId` / compatibility `eventId` | Generated evidence UUID, never a game event identifier |
| `sequence` | Evidence append order |
| `details.gameEventId`, `eventSequence` | Original game identity and sequence; absent values remain null |
| `details.receiveIndex`, `wirePayloadSha256` | Frame observation order and original payload hash |
| `requestId`, `commandId`, `worldGeneration`, `stateVersion` | Actual server metadata; client expected values remain in request details |

Events without command IDs only have a case observation-window association; temporal proximity is not a causal link. Duplicate frames are retained rather than deduplicated. Trace decode/disconnect/overflow errors and Schema violations fail the case. The collector handles the Bridge's current unmasked, unfragmented text frames, not every WebSocket extension/control format. Source hashes identify the exact dirty or clean code used by a run. Intentional failing child pytest cases verify reporter retention, not product defects.

## Protocol regression rationale and limitations

Two real-wire regressions are included: successful command events must include `failure: null`, and an idle connection must not resend its already-sent last frame. The sender resets both stored and local output when draining a frame. Event defaults preserve both required result/failure keys without weakening Schema assertions.

Development validation before this independent tree was assembled: genuine pre-fix failures were preserved; the repaired working tree passed 32 pytest cases and 685 existing Bridge checks. These numbers are not evidence for this separate tree or a future CI SHA. Independent validation results and outstanding work belong in the PR description. No claim of full save recovery, network disorder recovery, capacity/SLA performance, real-LLM evaluation or complete Phase 2 acceptance is made. Upstream review and merge remain the maintainer's decision.

Independent-tree validation on 2026-10-02: `pr-bridge-20261002-full3` completed all five core stages locally (preflight, static, pytest, Godot, Newman), 137.4 seconds excluding resource import. Pylint was 10/10, pytest was 33 passed with no skips, Godot was 685 checks and Newman was 5 requests / 14 assertions / 0 failures. JSONL, SQLite, JUnit, stage logs and summary were retained in the developer's isolated artifact directory; they are not committed or evidence of hosted CI. Source was based on upstream `43ef1d1` with the explicit uncommitted contribution; final-SHA CI still needs verification.

The first independent assembly omitted the existing speed-revision hook and failed two tests; the same assertions passed after including that required integration. Runner debugging also retained the initial PowerShell stderr/encoding and preflight failures. The runner now preserves native stderr as UTF-8 and checks the actual exit code, retains its 10 GiB free-space threshold using filesystem telemetry, writes Python syntax checks without bytecode files, and imports failure summaries instead of aborting before evidence collection. A real shell/child-process regression verifies stderr with exit 0 and failure with exit 3; these synthetic command probes validate the harness, not game behavior. Resource import exited 0 with sandbox certificate/editor-settings warnings, which are not suppressed.
