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
| `inbox_poller.sh` | per-session wake poller + heartbeat registration |
| `dashboard_tui.py` | read-only ANSI dashboard |
| `mailbox_stuck_watcher.py` + `launchd/*.plist` | registry-driven stuck-mail alerting |
| `tests/` | pure-function unit tests (`pytest`) |

## Tests

```bash
python3 -m pytest -q     # 114 tests, pure stdlib
```

## License

MIT — see [LICENSE](LICENSE).
