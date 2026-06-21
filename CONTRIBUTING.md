# Contributing to discord-vexa-bridge

Thanks for considering a contribution! This keeps collaboration smooth.

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). By participating you agree to
uphold it. Report unacceptable behavior to rennf93@gmail.com.

## Reporting bugs

Check existing issues first. A good report includes:

- A clear title and exact steps to reproduce
- `discord-vexa-bridge` logs around the problem — **with `DISCORD_TOKEN` / `DATABASE_URL` / creds redacted**
- Whether Vexa accepts the `discord` platform (README Step 2)
- Environment: host/OS, Docker version, Vexa version, worker `MODEL_SIZE`/device

## Suggesting enhancements

Open an issue describing the use case, the proposed change, and any alternatives considered.

## Development setup

**Python 3.11 is required** — the project depends on the stdlib `audioop` module, removed in 3.13.

```bash
uv sync --extra dev     # install runtime + dev deps into .venv
make test               # run the unit suite (pytest)
make lint               # ruff lint + format check
make fix                # auto-fix lint + format
make build              # build the Docker image
```

The voice-receive internals live in `dave_voice/` (gateway, MLS, transport, frame decrypt);
`bot.py` is the control plane (slash commands + transcription/DB pipeline). Tests are in `tests/`.

## Pull requests

- Fill in the PR template.
- `make lint` and `make test` must pass.
- Add/adjust tests for behavior changes.
- Update the README/docs and `CHANGELOG.md` (`## [Unreleased]`) for user-facing changes.
- **Never commit secrets** (tokens, DSNs) — see [SECURITY.md](SECURITY.md).

## Releases

This project uses [Semantic Versioning](https://semver.org/). To cut a release: update
`CHANGELOG.md`, create a GitHub Release with a `vX.Y.Z` tag — CI then builds and pushes the
Docker image to GHCR.

Questions? Open an issue. Thanks for contributing!
