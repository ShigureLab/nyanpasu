# SQLite

Nyanpasu requires Python's standard-library `sqlite3` module for persistent state. A standalone SQLite server is not needed. The `sqlite3` command-line tool is optional for diagnostics.

## Prerequisites and installation

Use a [Python](python.md) distribution with SQLite support. After installing the project environment, verify the module:

```bash
uv run python -c 'import sqlite3; print(sqlite3.sqlite_version)'
```

If a custom Python build omits the module, use a distribution that includes it or follow [Python's sqlite3 documentation](https://docs.python.org/3/library/sqlite3.html). Installing the CLI alone does not add the Python module.

For optional manual inspection, install your OS's SQLite package or the command-line tools from the [official download page](https://www.sqlite.org/download.html). Keep the service's state under persistent `NYANPASU_HOME`; its [configuration](../README.md#configuration) controls the location.
