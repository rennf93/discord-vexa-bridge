# Contributing

Thanks for considering a contribution. The full guide is in
[CONTRIBUTING.md](https://github.com/rennf93/discord-vexa-bridge/blob/master/CONTRIBUTING.md);
the highlights are below.

## Dev setup

**Python 3.11 is required** — the project depends on the stdlib `audioop` module, removed in
3.13.

```bash
uv sync --extra dev     # install runtime + dev deps into .venv
make test               # run the unit suite (pytest)
make lint               # ruff lint + format check
make fix                # auto-fix lint + format
make build              # build the Docker image
```

The voice-receive internals live in `dave_voice/` (gateway, MLS, transport, frame decrypt);
`bot.py` is the control plane (slash commands + transcription/DB pipeline). Tests are in
`tests/`. See [Repository layout](layout.md).

## Pull requests

- Fill in the PR template.
- `make lint` and `make test` must pass.
- Add/adjust tests for behavior changes.
- Update the README/docs and `CHANGELOG.md` (`## [Unreleased]`) for user-facing changes.
- **Never commit secrets** (tokens, DSNs) — see
  [SECURITY.md](https://github.com/rennf93/discord-vexa-bridge/blob/master/SECURITY.md).

## CLA

By contributing you agree to the
[Contributor License Agreement](https://github.com/rennf93/discord-vexa-bridge/blob/master/CLA.md).
Signing is automated through the **CLA Assistant** bot: open a pull request, the bot comments
with a link to the Agreement, and you confirm by posting the exact comment the bot requests.
Your GitHub username and the signing timestamp are recorded so the signature can be verified on
future contributions — it is a one-time action.

## The gates

See [Testing](testing.md) for the full lint / typecheck / security / test gate list. CI runs
`pre-commit run --all-files` (ruff, ruff-format, mypy, bandit) and the pytest suite on Python
3.11.
