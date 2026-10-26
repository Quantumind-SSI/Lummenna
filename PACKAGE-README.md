# Lummenna Community 1.0.1rc1

Lummenna is a local-first memory and context framework for AI agents. This
Community build is a public release candidate distributed through PyPI and
directly by `lummenna.com`. Its Python API may change before a stable release.

## Install

Lummenna supports Python 3.10 through 3.12. Install the prerelease from PyPI:

```bash
python -m pip install --pre "lummenna[export]==1.0.1rc1"
```

For a package-index-independent installation, download the same wheel directly:

```bash
curl --fail --show-error --silent --remote-name \
  https://lummenna.com/downloads/lummenna-1.0.1rc1-py3-none-any.whl
python -m pip install "./lummenna-1.0.1rc1-py3-none-any.whl[export]"
```

Verify direct downloads against the published checksums:

```text
https://lummenna.com/downloads/SHA256SUMS
```

The corresponding source archive is available at:

```text
https://lummenna.com/downloads/lummenna-1.0.1rc1.tar.gz
```

## Initialize And Run

The embedding model is not bundled with the package. Provision it once before
starting the service:

```bash
lummenna init --device generic
LUMMENNA_SOVEREIGN=false lummenna model download bge-small-en-v1.5
lummenna serve --host 127.0.0.1 --port 8848
```

The dashboard is then available at `http://localhost:8848/`. Set
`LUMMENNA_API_KEY` before exposing the API beyond localhost.

## Sidecar Deployment

The complete, checksum-pinned Community sidecar procedure is published at
`https://lummenna.com/community`.

## License And Contact

The Community Edition is licensed under AGPL-3.0-or-later. Network deployment
of a modified build carries the AGPL corresponding-source obligations described
in `LICENSE`.

- Support: `support@lummenna.com`
- Engineering: `engineering@lummenna.com`
- Commercial licensing: `license@lummenna.com`
