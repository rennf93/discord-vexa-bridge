# Testing

The unit suite is in `tests/` (pytest, `asyncio_mode = "auto"`). **No network or live Discord is
required** — the tests exercise the protocol/decrypt internals against fixtures and in-memory
fakes.

## Run the suite

```bash
make test     # uv run pytest -v
```

## Dev install

```bash
make install  # uv sync --extra dev  (installs runtime + dev deps into .venv)
```

**Python 3.11 is required** — the project depends on the stdlib `audioop` module, removed in
3.13. Do not bump past 3.11.

## The gates

| Gate | Command | What it checks |
|---|---|---|
| Lint | `make lint` | `ruff check .` + `ruff format --check .` |
| Format | `make fix` | `ruff check --fix .` + `ruff format .` (auto-fix) |
| Typecheck | `make typecheck` | `mypy bot.py dave_voice` (must stay clean — gated in CI) |
| Security | `make security` | `bandit -r bot.py dave_voice -ll` |
| Test | `make test` | `pytest -v` |
| All | `make check-all` | lint + typecheck + security + test |

CI (`.github/workflows/ci.yml`) runs `pre-commit run --all-files` (ruff, ruff-format, mypy,
bandit) and the pytest suite on Python 3.11. Keep all of these green.

## Conventions

- Match the surrounding style. The explanatory comments in `dave_voice/` document a tricky,
  Discord-controlled protocol — keep them accurate; don't strip them.
- `mypy bot.py dave_voice` is currently clean (0 errors) and gated in CI — keep it that way.
  Prefer narrowing/guards over `# type: ignore`.
- Never commit secrets. `DISCORD_TOKEN`, `DATABASE_URL`, and `.env` are runtime config and are
  gitignored.