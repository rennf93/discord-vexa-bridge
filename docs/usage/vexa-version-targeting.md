# Vexa version targeting

The bridge writes rows directly into Vexa's Postgres (`meetings` / `meeting_sessions` /
`transcriptions`, `platform = "discord"`). That contract is tight to a specific Vexa line.

## Which Vexa line each bridge release targets

| Bridge release | Targeted Vexa line | Mechanism |
|---|---|---|
| 0.1.0 – 0.5.x | **Vexa 0.10.x** | Direct Postgres writes + the `discord` platform enum patch (README Step 2). |

## The `discord` platform enum

Vexa validates the meeting platform against a fixed enum (`google_meet`, `zoom`, `teams`,
`browser_session`). Until `discord` is in it, Vexa's read API returns **422** for Discord
transcripts — the data is written fine, but the dashboard/MCP won't serve it.

Add `DISCORD = "discord"` to `Platform` in `services/meeting-api/meeting_api/schemas.py` and
rebuild your `meeting-api` + `api-gateway` images. A ready-made change (enum + URL handling +
concurrency exclusion + dashboard icon) is at
[the `discord` platform patch](https://github.com/rennf93/vexa/tree/add-discord-platform) —
ideally land it upstream so future Vexa releases include it.

## Vexa 0.12 and the external-ingest contract

!!! warning "Vexa 0.12 rejects `discord` as a platform"
    Vexa **0.12** tightens platform validation and **rejects `discord`** as a platform. The
    direct-Postgres-write approach this bridge uses (targeting 0.10.x) does **not** work
    against 0.12 as-is.

The external-ingest contract — a supported way to feed meetings from an outside adapter into
Vexa without direct DB writes — is **planned for Vexa 0.12.x**, tracked by
[Vexa issue #463](https://github.com/Vexa-ai/vexa/issues/463). Once that lands, the bridge will
move off direct Postgres writes onto the external-ingest API, which will restore compatibility
with 0.12.x and later.

**Until then:** pin your Vexa stack to the **0.10.x** line if you want to run this bridge, and
apply the `discord` platform enum patch from README Step 2.