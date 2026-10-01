# Nyanpasu packages and runtime libraries

The core package is required. GitHub functionality is installed through workspace packages and enabled in configuration.

| Package                    | When needed                              | Dependencies                                                                                                                               |
| -------------------------- | ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `nyanpasu`                 | Always                                   | [Python](python.md), runtime libraries in [pyproject.toml](../pyproject.toml), and a selected agent backend                                |
| `nyanpasu-github`          | Either GitHub plugin                     | Core, [Git](git.md), and [gh](github-cli.md); [manifest](../packages/nyanpasu-github/pyproject.toml)                                       |
| `nyanpasu-github-reviewer` | Automatic PR review                      | Shared GitHub package, [reviewer tools and skills](index.md#agent-skills); [manifest](../packages/nyanpasu-github-reviewer/pyproject.toml) |
| `nyanpasu-github-pr-maker` | Automatic implementation and PR creation | Shared GitHub package and push/create permissions; [manifest](../packages/nyanpasu-github-pr-maker/pyproject.toml)                         |

The package manager installs AnyIO, FastAPI, Loguru, Pydantic, Typer, Uvicorn, and their transitive dependencies from the manifests and [uv.lock](../uv.lock). They do not need separate global installations. [SQLite](sqlite.md) is embedded.

## Installation from source

After installing [uv](uv.md), [Python](python.md), and [Git](git.md):

```bash
git clone https://github.com/ShigureLab/nyanpasu.git
cd nyanpasu
uv sync --locked --all-extras --dev --python 3.14
uv run nyanpasu --help
```

The development group includes all three GitHub workspace packages. Installing them does not enable them: select plugins using `enabled_plugins` in [configuration](../README.md#configuration).

Build [Dashboard assets](frontend.md) if serving the UI from this checkout. Set up the selected [agent backend](index.md#runtime-and-github-plugins), GitHub credentials, and [config.toml](../examples/config.toml) before following the [run instructions](../README.md#run). External CLIs and skills are separate installations.
