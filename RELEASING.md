# Community Release Procedure

Lummenna Community releases use PyPI Trusted Publishing and versioned direct
downloads from lummenna.com. Reusable PyPI API tokens are not part of this
process.

Any token disclosed in chat, logs, or command history must be revoked before
release work continues. Do not replace it with another reusable token; use the
OIDC configuration below.

## One-Time PyPI Configuration

Configure a pending publisher in the PyPI account that owns the `lummenna`
project with these exact values:

| Field | Value |
|---|---|
| PyPI project | `lummenna` |
| GitHub owner | `Quantumind-SSI` |
| Repository | `lummenna` |
| Workflow | `release.yml` |
| Environment | `pypi` |

Create the matching `pypi` environment in the repository settings. No PyPI
secret is required because `.github/workflows/release.yml` exchanges its GitHub
OIDC identity for a short-lived publishing credential.

## Release Gate

Run every command in this section from the approved clean host:

```bash
python -m pip install build twine
python scripts/build_community_dist.py
python -m twine check \
  website/downloads/lummenna-*.whl \
  website/downloads/lummenna-*.tar.gz
python -m pytest -q
```

The builder refuses to replace an existing same-version artifact or checksum
manifest. A byte change requires a version increment. The
`--replace-unpublished` option is permitted only before that version has reached
PyPI or the production website.

## Publish

After the reviewed release changes are committed on the clean host, create and
push a tag that exactly matches the package version:

```bash
git tag v1.0.1rc2
git push origin v1.0.1rc2
```

The Release workflow builds the sanitized source archive and wheel, checks their
metadata, creates the prerelease entry, and publishes only those two artifacts
to PyPI through OIDC.

Deploy the frontend only after the PyPI job succeeds. From the repository root:

```bash
wrangler pages deploy --branch main
```

## Verify

Use a new virtual environment and an empty temporary directory:

```bash
python -m pip download \
  --dest /tmp/lummenna-verify \
  --no-deps \
  --pre \
  lummenna==1.0.1rc2

curl --fail --show-error --silent --remote-name \
  https://lummenna.com/downloads/lummenna-1.0.1rc2-py3-none-any.whl
curl --fail --show-error --silent --remote-name \
  https://lummenna.com/downloads/SHA256SUMS
sha256sum --check --ignore-missing SHA256SUMS
```

The PyPI download and direct download must have the same SHA-256 digest before
the release is announced.
