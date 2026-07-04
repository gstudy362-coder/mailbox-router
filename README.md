# mailbox-router

**A file-based coordination protocol for multiple Claude Code (LLM agent) live sessions.**

Different agent sessions — each working in its own repo — pass each other *letters* to delegate
tasks and report progress, with **no human manually relaying messages**. A tiny delivery layer moves
mail between mailboxes; every session reads, processes, and replies *itself*, so the whole exchange
stays auditable and a human can gate anything destructive.

> This is personal infrastructure, shared as a reference. It assumes a `~/claudeworkspace/<repo>/mailbox`
> layout and is intentionally opinionated. Paths and the alert channel are configurable via env vars.

---

## Why

When you run several long-lived LLM coding sessions in parallel (one per repo), they often need to
hand work to each other: "your data job finished, here's the schema" / "please add field X". Doing
that by copy-pasting between terminals is slow and lossy. `mailbox-router` makes it a protocol:

- **Delivery is dumb and reliable** — a launchd-resident router only moves `*.md` letters between
  `outbox/` and `inbox/`. It never reads content.
- **Processing is smart and accountable** — each session is woken when it has mail, then *itself*
  reads, decides, replies, and archives. Destructive/ambiguous requests are gated by the human.

## How it works

```
        ┌──────────────────────── each party runs its own ────────────────────────┐
   [session A]                                            [session B]
        │  inbox_poller.sh A                                    │  inbox_poller.sh B
        ▼                                                       ▼
   background poller (~60s)                               background poller (~60s)
     1. python mailbox_router.py --once   ← delivers outbox → recipient inbox
     2. any unprocessed mail in my inbox?
        └─ yes ─▶ exit ─▶ wakes THIS live session
                            └─ session reads → does the work → replies (outbox) → moves to received/
```

## Key concepts

- **STAGE lifecycle** — every letter carries a `STAGE` (`ask` / `accept` / `deliver` / `block` /
  `done` / `reject` / `fyi`). Convergence (everyone terminal) silences a thread; a progress-aware
  circuit breaker only trips on *no forward progress*, never on a healthy busy thread; mutual `block`
  raises a deadlock alert. So threads never get silently stuck.
  - *Progress is non-saturating*: progress = raising the thread's stage high-water mark **or**
    shipping/closing (a `deliver` or terminal letter). So an active stream — one party already `done`
    while another keeps shipping `deliver`s — never false-trips; only `ask`/`accept`/`block`
    ping-pong with nothing shipped accumulates toward the cap. The trip alert is latched per thread
    (one alert per stall episode; progress re-arms it) so a stuck thread doesn't re-alert every pass.
- **Dynamic session registry** — any session registers by starting its poller, which heartbeats a
  `.state/registry/<name>.json` entry. Others see who is `online / processing / offline` and can
  address them by name. Entries persist while offline so mail still waits in their inbox.
- **Capability-aware dispatch** — sessions self-declare `roles` + a one-line `description`
  (via `.mailbox-card` or env). When deciding *who* should do a task, the sending session reads the
  registry, reasons about the best recipient, **confirms with the human**, and sends one letter per
  recipient (complex tasks are decomposed into tailored letters). The router stays strictly by-name.
- **Read-only TUI** — `python3 dashboard_tui.py` shows who's online (with roles), each thread's
  current stage (who's waiting on whom), router health, and stuck mail. It never writes state.
- **Stuck-mail watcher** — a launchd-resident watcher scans every registered party's inbox and
  alerts (Telegram) when a letter sits too long. Alert-only; it never processes mail.
- **Single-instance poller guard** — `inbox_poller.sh` takes an atomic `mkdir` lock on its mailbox
  at startup, so a duplicate same-name poller refuses (`exit 2`) instead of corrupting shared
  in-flight state; a stale lock (dead holder, e.g. after `kill -9`) is safely taken over on the
  next start. So when restarting, only kill *your own* poller by name
  (`pkill -f "inbox_poller.sh <name>"`) — a blanket `pkill -f inbox_poller.sh` would also kill
  other sessions' pollers.
- **Supervisor injection wake (primary wake layer)** — the original design woke a session by having
  its own `run_in_background` poller exit and relying on the harness's task-notification. But a
  harness SIGTERMs background tasks at turn boundaries, so that poller can never persist, and a hook
  that force-relaunches it becomes an infinite loop. So wake is decoupled from the poller:
  `mailbox_supervisor.py` runs in a dedicated terminal pane (immune to the harness and to terminal
  hibernation; in the multiplexer's lineage so its socket works). Each cycle it writes a heartbeat,
  and for any party with unprocessed mail it **injects a fixed protocol message into that party's
  terminal** (its normal turn then reads/replies/archives — the injected text is the protocol
  instruction only, never letter content). It also revives dead pollers with no model tokens. Pollers
  may die freely; wake no longer depends on them.
- **Host-aware injection backends** — the supervisor injects via the party's host: a cmux-native
  session via `cmux send --surface <surface>`; a tmux-hosted agent (e.g. behind ttyd/web) via
  `tmux send-keys -t <session>`. Each poller auto-detects its host (`$TMUX` vs a cmux surface id) and
  reports `wake: {backend, target}` to its registry entry (legacy `cmux_surface` still read). tmux
  injection also survives the user killing an ephemeral viewer, so a tmux-hosted poller should be
  started from inside its persistent host (see `skills/join-mailbox/SKILL.md` for onboarding
  non-Claude agents like OpenCode / Antigravity CLI).
- **Stop hook = supervisor dead-man switch** — the Stop hook (`hooks/stop_relaunch_poller.py`) no
  longer force-relaunches the poller (that was the token-burning loop). It is scoped to participants
  (`.mailbox-card` or a registry cwd), fails open, and blocks turn-end at most once per window ONLY
  when the supervisor's heartbeat is stale — i.e. it just asks a human to restart the supervisor pane
  if the wake engine itself has died. Delivery is launchd-backed regardless.
- **Codex wake adapter (second wake path)** — a Claude Code session self-wakes via `inbox_poller.sh`
  (the poller exits → the harness resumes the live session). An OpenAI Codex CLI participant has no
  such harness hook, so it gets a second wake adapter: a launchd watcher (`WatchPaths` on the Codex
  inbox + a `StartInterval` backstop) runs `codex_wake.sh`, which wakes Codex by **resuming one fixed
  mailbox session** — `codex exec resume <session-id>` — so Codex continues the *same* live session
  (accumulating context + an on-disk transcript at `~/.codex/sessions/.../rollout-<id>.jsonl` you can
  `tail -f`) instead of cold-starting a stranger each time. The first wake cold-starts `codex exec`,
  captures the session id (`codex_wake.py:parse_session_id`), and persists it to
  `mailbox/.codex_session_id`; a failed resume self-heals to a cold start. Codex is treated as a
  trusted peer — no privilege cage, it runs under its own `~/.codex/config.toml`. Single-instance via
  the same atomic `mkdir` lock; it bails out when the inbox has no unprocessed mail. Traceability is
  two-level: the mailbox letters (the cowork audit trail) and the fixed session's rollout transcript.

## Quickstart

```bash
# clone next to your other repos, e.g. ~/claudeworkspace/mailbox-router
git clone <this-repo> ~/claudeworkspace/mailbox-router
cd ~/claudeworkspace/mailbox-router

# each participating session starts its poller from ITS repo root, in the background:
bash ./inbox_poller.sh <name>          # mailbox defaults to $PWD/mailbox

# see the live picture:
python3 dashboard_tui.py

# deliver one round manually:
python3 mailbox_router.py --once

# (optional) make poller relaunch reliable — wire the Stop hook into ~/.claude/settings.json:
python3 hooks/install_stop_hook.py
```

## Letter format (front-matter)

```
TO: <recipient name>
THREAD: <topic id, shared across a conversation>
STAGE: ask | accept | deliver | block | done | reject | fyi
```

To advertise capabilities, drop a `.mailbox-card` in the repo root:

```
roles: data-ingest, backfill
desc: owns FinMind ingestion (daily + streaming)
```

## Configuration (env)

| Var | Purpose | Default |
|-----|---------|---------|
| `MAILBOX_WORKSPACE` | root holding the sibling repos | `~/claudeworkspace` |
| `MAILBOX_ROLES` / `MAILBOX_DESC` | capability declaration (override `.mailbox-card`) | from card / empty |
| `MAILBOX_ALERT_CHAT_ID` | Telegram chat id for alerts (token read from `~/.claude/channels/telegram/.env`) | none (alerts skipped) |

## Components

| File | Role |
|------|------|
| `mailbox_router.py` | delivery-only router: scan outboxes → deliver by name; sha1 dedup; STAGE breaker/convergence/deadlock; single-flight lock |
| `registry.py` | session registry read/write + liveness; self-declared roles/description; `write-self` CLI |
| `inbox_poller.sh` | per-session wake poller + heartbeat registration (Claude sessions) |
| `codex_wake.sh` + `codex_wake.py` | Codex wake adapter: launchd-triggered `codex exec resume` of a fixed mailbox session |
| `dashboard_tui.py` | read-only ANSI dashboard |
| `mailbox_stuck_watcher.py` + `launchd/*.plist` | registry-driven stuck-mail alerting |
| `tests/` | pure-function unit tests (`pytest`) |

## Tests

```bash
python3 -m pytest -q     # 167 tests, pure stdlib
```

## License

MIT — see [LICENSE](LICENSE).
