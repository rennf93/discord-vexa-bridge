# Security Policy

## Reporting a vulnerability

Please report security issues **privately** — do **not** open a public issue.

Email **rennf93@gmail.com** with a description, reproduction steps, and impact. You'll get an
acknowledgement as soon as possible, and a fix or mitigation will be coordinated before any public
disclosure.

## Supported versions

This project tracks the latest release. Fixes land on `master` and the next tagged release / image.

## Handling secrets — read this

This bridge runs a **Discord bot token** and connects to your **Vexa database**. Treat both as
sensitive:

- **Never commit `DISCORD_TOKEN`, `DATABASE_URL`, or any credentials.** Keep them in environment
  variables / a `.env` file that is gitignored (this repo ignores `.env`).
- A leaked Discord bot token grants full control of the bot — if one is ever exposed, **reset it
  immediately** in the Discord Developer Portal (Bot → Reset Token).
- Don't paste tokens or DSNs into issues, screenshots, or logs.
- The bot requests only the minimum it needs: the `Server Members` intent (speaker names) and the
  `View Channels` + `Connect` permissions (to join voice and receive audio). It never sends audio,
  video, or messages.
