# Contributing to wgea-mcp

Thanks for your interest. Bug reports and small improvements are welcome.

## Development setup

```bash
git clone https://github.com/Bigred97/wgea-mcp.git
cd wgea-mcp
uv venv
uv pip install -e ".[dev]"
pytest                  # unit tests (no network)
pytest -m live          # live integration (~71 MB download)
```

## Reporting an issue

If you hit a "expected these columns but they were not in the parsed table"
error, that usually means WGEA has changed the public data file's schema.
File an issue with:

- The exact error message
- The dataset ID
- The output of `python -c "from wgea_mcp.discovery import resolve_latest_zip; ..."`

## Pull requests

Please:

1. Keep unit tests at 100% pass and the zero-flake bar — run the full
   suite 10 times before pushing.
2. Add tests for any new behaviour. The convention is one test file per
   module (`test_parsing.py` for `parsing.py`, etc.).
3. Update `CHANGELOG.md` under an "Unreleased" section.
4. Don't bump `pyproject.toml` version — the maintainer does that at
   release time.

## Code style

- Black-compatible 4-space indentation.
- Type hints on all public function signatures.
- Pydantic v2 models for any new response shape.
- Plain-English error messages that suggest the next step ("Try
  list_curated() to see ...").
