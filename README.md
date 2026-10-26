# Codex Auto Title

A working prototype of a local Codex plugin plus a background title worker.
It is packaged for **new Codex chats on one Linux host, under one OS account**.
It has not been installed or enabled on the host used to develop it.

## Behavior

1. `SessionStart` enrolls a new chat. Resuming an older, unenrolled chat does
   not enroll it retroactively.
2. After the first completed question/response, a separate model receives the
   user message(s) from that turn and its final assistant response. The worker
   sets a short title through Codex's `thread/name/set` API.
3. After subsequent activity, the worker waits until **both** conditions hold:
   - The chat has been idle for at least **10 minutes**.
   - At least **1 hour** has elapsed since its last automatic rename.
4. It sends the **first 10 and last 20 messages**, in chronological order,
   removing any overlap. Messages mean user and assistant text, including
   visible assistant commentary. Tool calls, tool outputs, reasoning, system
   instructions, and developer instructions are excluded.

If more turns finish before the first title is generated, their content stays
pending for the subsequent idle update. Interrupting a later response also
leaves its visible conversation content eligible once the chat is idle; an
interrupted first turn alone does not satisfy the completed-exchange requirement.

Idle updates are eligible at:

```text
max(last_conversation_activity + 10 minutes, last_rename + 1 hour)
```

For example, if the first title is set at 12:00 and the last activity is at
12:20, the next update is eligible at 13:00. The worker checks every 15 seconds;
model and server latency add to that interval. A long-running turn is never
considered idle merely because it hasn't displayed a message recently.

Unchanged chats do not generate hourly model calls. Identical candidate titles
are not written again. State and cooldowns survive restarts. A lock prevents
multiple workers from processing the same state directory. The worker checks
activity and the current title again after each model call, and reconciles a
rename whose reply was lost instead of blindly repeating it.

## Architecture

```text
Codex SessionStart / UserPromptSubmit / Stop / Interrupt
                         |
                  short local hooks
                         |
             separate SQLite metadata store
                         |
                 persistent worker
                         |
        Codex Unix WebSocket: read history/status
                         |
             separate title model request
                         |
             Codex thread/name/set API
```

Hooks do not call the model, hold an idle timer, read transcript files, or inject
extra conversation context. The worker uses the running Codex app-server; it
does not resume chats, create hidden chats, or modify Codex's database files.

The title backend is an independently configured **Responses API** model. The
default endpoint is OpenAI. A local Responses-compatible endpoint is also
supported. There is no default model or embedded credential. A ChatGPT/Codex
subscription alone does not configure this API backend.

## Installation

Requires Python 3.11+, the `websockets` dependency, a running local Codex
app-server with Unix WebSocket and paginated thread APIs, and supported/trusted
lifecycle hooks. Development verified the read APIs against Codex CLI 0.164.0.

Copy this directory to a permanent location such as
`~/.local/share/codex-auto-title`. From that installed directory:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
mkdir -p ~/.config/codex-auto-title
cp -n config.example.json ~/.config/codex-auto-title/config.json
```

If your Python lacks `venv` or `pip`, provision them through your usual Python
environment manager first. The hook itself uses only the standard library;
the worker's virtual environment supplies `websockets`.

Edit `~/.config/codex-auto-title/config.json`:

- Set `enabled` to `true`.
- Set `title_model` to a model available to your chosen API account/server.
- Keep the timing/window defaults or adjust them.
- Leave `app_server_socket` empty to use
  `${CODEX_HOME:-~/.codex}/app-server-control/app-server-control.sock`.
  Set it explicitly if your daemon uses a different socket.

For the OpenAI endpoint, provide `OPENAI_API_KEY` in the **worker's** environment.
Do not put credentials in the plugin manifest or configuration JSON. For a
systemd service, a private file at
`~/.config/codex-auto-title/worker.env` can contain the environment assignment.
Use mode `0600`. A localhost endpoint can run without an API key; use a separate
`api_key_env` name if you do not want to use an inherited OpenAI key there.

Add the entry in `marketplace.example.json` to your personal marketplace at
`~/.agents/plugins/marketplace.json`. Merge it into an existing marketplace;
do not overwrite other plugin entries. The example assumes the permanent
installation path above. Restart the desktop app, install/enable the plugin
from that marketplace, and review/trust its hooks. Codex's `/hooks` command
provides hook review and trust controls. Hooks skipped for lack of trust will
not enroll chats.

Check the connection before starting the worker:

```bash
.venv/bin/python scripts/auto_title.py doctor
.venv/bin/python scripts/auto_title.py worker --once --dry-run
```

`doctor` only checks connectivity and reports whether model configuration and
an API-key environment variable are present; it does not use the key or call a
model. `--dry-run` reads eligible chat samples but neither calls the model nor
renames chats. It emits counts and IDs, not message content. A new installation
with no enrolled completed chat will produce no dry-run candidates.

To keep the idle timer running on Linux, install the included user service:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/codex-auto-title.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now codex-auto-title.service
```

Adjust `ExecStart` if you chose a different installation path. The worker and
Codex hooks must run as the same account with the same configuration and state
paths. It requires that account's app-server socket access. On hosts without a
user systemd manager, run `.venv/bin/python scripts/auto_title.py worker` under
the existing process supervisor. The worker must remain running while chats
are idle; installing the plugin alone does not create an idle scheduler.

Open a **new** chat after enabling/trusting the plugin to verify actual hook
delivery. Finish one response and inspect the title and worker logs. This last
live hook/model/rename check was not performed during development.

## Operation and configuration

```bash
.venv/bin/python scripts/auto_title.py status
journalctl --user -u codex-auto-title.service
systemctl --user stop codex-auto-title.service
```

Restart the worker after editing its configuration. Hooks reload configuration
for each invocation. Turning `enabled` off stops hook enrollment/activity
recording; also stop the running worker, which has already loaded its settings.

The default paths are:

- Configuration: `$XDG_CONFIG_HOME/codex-auto-title/config.json`, falling back to
  `~/.config/codex-auto-title/config.json`.
- State: `$XDG_STATE_HOME/codex-auto-title/`, falling back to
  `~/.local/state/codex-auto-title/`.

Override these with `CODEX_AUTO_TITLE_CONFIG` / `CODEX_AUTO_TITLE_DATA`, or the
CLI `--config` / `--data-dir` arguments before the subcommand. Custom paths must
be shared by hooks and worker; CLI overrides alone do not change hook settings.

Once the worker has established its first title, a subsequent title change
outside the worker pauses automatic updates for that chat by default. Resume it
explicitly with:

```bash
.venv/bin/python scripts/auto_title.py unpause THREAD_ID
```

This also starts a conservative one-hour cooldown from the time of unpausing.
Set `preserve_manual_titles` to `false` to allow future automatic replacement.

Model failures use exponential retry delays, capped at five attempts per
conversation revision. New conversation content permits another attempt.
Only successful title writes advance the last-rename timestamp; matching
titles mark the revision evaluated without issuing a rename.

## Boundaries and current limitations

- This version targets newly created root Codex chats on one host. It does not
  backfill existing chats, process forks/subagents/ephemeral chats, or integrate
  with ChatGPT conversations or other hosts.
- Hooks and the worker must be running to observe activity. Missing hook events
  can delay or prevent an update. There is no polling-based enrollment/backfill.
- `Stop` can run before a turn really finishes, so the worker verifies the
  latest persisted turn is completed or interrupted before generating a title.
  Initial naming additionally requires a completed question/response exchange.
- Before the first automatic title, Codex metadata does not identify whether
  an existing title came from the app or a manual edit. The first title may
  replace either. Manual-title preservation applies after that baseline.
- Codex's rename API has no compare-and-set field. The worker rechecks state
  immediately before writing, but another rename or turn can still occur in
  the small interval between the final check and the write.
- Each selected message is capped at 2,000 characters by default. Very long
  messages are visibly truncated in the model input. Images/attachments are
  not sent; a textless image-only exchange may not produce an initial title.
- The separate local state stores IDs, times, hashes, and title metadata, not
  message bodies. Selected message text is sent to the configured model
  endpoint with `store: false`; that flag does not override provider retention
  policies. The implementation makes no Honcho requests.
- This is a manually installed local plugin. Official documentation currently
  excludes plugins containing lifecycle hooks from the public plugin directory.

## Validation

```bash
.venv/bin/python -m unittest discover -s tests -v
```

The 34 tests cover sampling/pagination/overlap, completed-turn gating, idle and
cooldown boundaries, unchanged chats, manual changes, stale generation,
restart/crash recovery, bounded retries, dry runs, model request shape, and
transport handling. Regression cases also cover pending later turns during
initial naming, interrupted responses, and the initial-title unpause cooldown.
An end-to-end test uses a real local Unix WebSocket server
and HTTP title endpoint with synthetic messages. Live read-only checks also
verified initialization, listing, metadata, and turn-status reads against the
running Codex host. No real model call or real title mutation was made.

Official references:

- [Plugin packaging and bundled hooks](https://developers.openai.com/plugins/build/plugins#bundled-mcp-servers-and-lifecycle-hooks)
- [Lifecycle hooks, inputs, and trust review](https://learn.chatgpt.com/docs/hooks)
- [App-server transport, thread history, and rename API](https://learn.chatgpt.com/docs/app-server)
- [Responses API request and output format](https://developers.openai.com/api/docs/guides/migrate-to-responses)
