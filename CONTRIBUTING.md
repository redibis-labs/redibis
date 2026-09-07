# Contributing

Thanks for helping improve redibis.

## Development setup

Create a virtual environment and install using the pre-resolved dependency
lockfile:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements/requirements-all.txt
pip install -e . --no-deps
```

## Running tests

```bash
pytest tests/ -q
pytest tests/ -m "not slow and not integration"   # fast subset
```

CI runs the full suite on Python 3.10–3.13 (see `.github/workflows/ci.yml`).

## Architecture rules

Read `CLAUDE.md` before larger changes. In short:

- `ContractStore.upsert()` is the only writer to the contracts bucket.
- Domain packages must not import `redibis.cli`.
- PII demotion goes through the decision overlay, not hand-edited tags.

## Developer Certificate of Origin (DCO)

All contributions to `redibis` must include a `Signed-off-by` header in commit messages certifying compliance with the Developer Certificate of Origin (DCO v1.1):

```text
Signed-off-by: Random J Developer <random@example.com>
```

You can sign your commit automatically using `git commit -s`.

## Pull requests

- Keep diffs focused; match existing naming (`*Scan`, `*Result`, `*Store`).
- Add or update tests for behavior changes.
- Update `README.md` and release notes when user-facing behavior changes.

## Security

Do not commit credentials. LLM/S3 keys are read from environment variables or
config files listed in `.gitignore`. Follow `SECURITY.md` for private
vulnerability reporting.
