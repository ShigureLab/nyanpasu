# Python

Required to run Nyanpasu and its Python-based tools. The core's supported Python versions are declared in [pyproject.toml](../pyproject.toml). For a complete GitHub reviewer installation, Python 3.14 is a convenient baseline because [gh-llm requires Python 3.14 or newer](https://github.com/ShigureLab/gh-llm#requirements).

## Prerequisites and installation

Install [uv](uv.md), then let it install Python:

```bash
uv python install 3.14
```

Alternatively, use the [Python downloads](https://www.python.org/downloads/) or your OS package manager. The interpreter must include the standard-library `sqlite3` module; see [SQLite](sqlite.md).

See [uv's Python installation guide](https://docs.astral.sh/uv/guides/install-python/) for platform requirements. Follow [Nyanpasu installation](nyanpasu.md) to select this interpreter for the project.
