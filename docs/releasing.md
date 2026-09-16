# Release preparation

Use Python 3.13+ and [uv](https://docs.astral.sh/uv/). The development dependencies
are locked in `uv.lock`.

```sh
uv sync --frozen
uv run --frozen python -m pytest
uv run --frozen python scripts/build_release.py
gitleaks dir . --redact --no-banner
```

The ZIP is built deterministically in ignored `dist/`, verified against the
source files, and accompanied by `SHA256SUMS`. The integration includes a copy
of the MIT `LICENSE`, which the build checks against the repository copy.
Extract it into the Home Assistant configuration directory for manual
installation. HACS custom-repository installs use the integration directory in
Git; the ZIP is an optional manual artifact.

Before publishing:

1. Review `manifest.json`, `hacs.json`, the release version and release notes.
2. Run the tests from a clean checkout and validate the exact artifact.
3. Check source and outgoing Git history for personal credentials, captures and
   app binaries. The two allowlisted Tuya constants are shared app constants;
   the allowlist does not cover personal lock keys or user credentials.
4. Include the MIT [LICENSE](../LICENSE) and preserve its copyright notice.
5. Document remaining hardware checks and experimental features accurately in
   the release notes, including for stable releases.
6. Publish only the intended branch/tag and verified integration ZIP.

The local research archive and its backups must remain outside Git. A backup on
the same computer protects against cleanup mistakes, but is not an off-device
backup. Capture-derived protocol test fixtures contain no personal access keys.
