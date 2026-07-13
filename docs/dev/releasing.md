# Releasing

This project uses [Semantic Versioning](https://semver.org/). It ships as a Docker image
(ghcr.io + Docker Hub) published by a GitHub Release, not as a PyPI package.

## Cut a release

1. Bump the version + scaffold the changelog entry with the helper:

   ```bash
   python .github/scripts/bump_version.py X.Y.Z
   ```

   This updates the `version` field in `pyproject.toml` and inserts a `## [X.Y.Z] - DATE`
   section (Keep a Changelog format) after `## [Unreleased]` in `CHANGELOG.md`. The Unreleased
   section is left in place (empty) so future changes have a home. See
   [`scripts/bump_version.py`](https://github.com/rennf93/discord-vexa-bridge/blob/master/.github/scripts/bump_version.py).

2. Fill in the changelog section (Added / Changed / Fixed) and commit.

3. Create a **GitHub Release** with a `vX.Y.Z` tag. The
   [`release.yml`](https://github.com/rennf93/discord-vexa-bridge/blob/master/.github/workflows/release.yml)
   workflow then builds and pushes the image.

## The release workflow

On a published release, `release.yml` builds the image once and tags it for **both** registries:

- **GHCR:** `ghcr.io/rennf93/discord-vexa-bridge:VERSION` and `:latest`
- **Docker Hub:** `docker.io/renzof93/discord-vexa-bridge:VERSION` and `:latest`

The version tag is derived from the release tag (`v0.1.0` → `0.1.0`). Pushes happen only after
the build succeeds, so a failure never half-publishes. The image is labelled with
`org.opencontainers.image.source` and `org.opencontainers.image.licenses=AGPL-3.0-or-later`.

## Cross-building for an amd64 NAS

If you deploy to an amd64 NAS from an Apple Silicon host, build for the target platform and push
manually (the release workflow builds on `ubuntu-latest`, which is amd64, so it produces the
right image already — this is only needed for manual/local builds):

```bash
docker buildx build --platform linux/amd64 \
  -t ghcr.io/rennf93/discord-vexa-bridge:latest --push .
```

See [Troubleshooting](../usage/troubleshooting.md#cross-building-for-an-amd64-nas).