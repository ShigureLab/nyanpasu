# Python development tools

Required for the corresponding Nyanpasu development checks; not needed as global tools merely to run the service.

## Prerequisites and installation

Use [Python](python.md) and [uv](uv.md). From the repository root:

```bash
uv sync --locked --all-extras --dev --python 3.14
```

The `dev` group in [pyproject.toml](../pyproject.toml) includes pytest and pytest-rerunfailures for tests, HTTPX for HTTP tests, Ruff for lint/format checks, ty for type checks, and the GitHub workspace packages. Versions are resolved in [uv.lock](../uv.lock).

Use `uv run` to invoke tools from that environment. The [justfile](../justfile) defines the project's check commands; [just](just.md) is an optional command runner. Dashboard contract generation also uses this environment through `pnpm run types`.
