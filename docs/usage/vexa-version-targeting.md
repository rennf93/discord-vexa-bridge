# Vexa version targeting

The bridge writes rows directly into Vexa's Postgres (`meetings` / `meeting_sessions` /
`transcriptions`, `platform = "discord"`). That contract is tight to a specific Vexa line.

## Which Vexa line each bridge release targets

| Bridge release | Targeted Vexa line | Mechanism |
|---|---|---|
| 0.1.0 – 0.5.x | **Vexa 0.10.x** | Direct Postgres writes + the `discord` platform enum patch (README Step 2). |
| 0.6.0+ | **Vexa 0.10.x and 0.12.x** (verified on v0.12.15) | Direct Postgres writes; no enum patch needed on 0.12 (reads don't gate platform). See the 0.12 notes below. |

## The `discord` platform enum

Vexa validates the meeting platform against a fixed enum (`google_meet`, `zoom`, `teams`,
`browser_session`). Until `discord` is in it, Vexa's read API returns **422** for Discord
transcripts — the data is written fine, but the dashboard/MCP won't serve it.

Add `DISCORD = "discord"` to `Platform` in `services/meeting-api/meeting_api/schemas.py` and
rebuild your `meeting-api` + `api-gateway` images. A ready-made change (enum + URL handling +
concurrency exclusion + dashboard icon) is at
[the `discord` platform patch](https://github.com/rennf93/vexa/tree/add-discord-platform) —
ideally land it upstream so future Vexa releases include it.

## Vexa 0.12.x (bridge 0.6.0+)

Vexa 0.12 still rejects `discord` at its **bot-operation intake** (`POST /bots` and friends), but
the bridge never calls those. What actually matters survived the 0.12 rewrite, verified on
v0.12.15:

- The three tables the bridge writes are unchanged where it touches them
  (`meetings.platform_specific_id` `String(255)`, `transcriptions.segment_id` nullable `String`,
  `meeting_sessions` identical), and the read API (`GET /meetings`,
  `GET /transcripts/{platform}/{id}`) takes the platform as a plain string — `discord` rows are
  listed and served without any enum patch.
- Bridge **0.6.0** adds the two behaviors 0.12 requires: `/join` retires stale non-terminal rows
  (0.12's `uq_meeting_active_user_platform_native` unique index allows only one active meeting per
  user/platform/channel), and every segment insert bumps `meetings.updated_at` so 0.12's reconcile
  sweep sees the meeting as live.

!!! warning "Two deployment requirements on 0.12.x"
    1. Raise the reconcile grace on **meeting-api** (e.g. `RECONCILE_ACTIVE_GRACE_S=86400`,
       `MEETING_UNTRACKED_GRACE_SEC=86400`). Bridge meetings have no bot container for the sweep
       to probe, so with the 300s default a long-silent live call gets force-completed mid-call.
    2. Keep the transcription unit **token-open** (`API_TOKEN` empty) or front it yourself — the
       bridge POSTs bare multipart with no auth header. Keep the worker unexposed (in-network
       only) if you do.

The schema remains **Vexa-internal and unsealed** — a 0.12.x point release may change it without
notice. The supported long-term path is the external-ingest contract tracked by
[Vexa issue #463](https://github.com/Vexa-ai/vexa/issues/463) (currently parked); when it lands,
the bridge moves off direct Postgres writes onto that API.
