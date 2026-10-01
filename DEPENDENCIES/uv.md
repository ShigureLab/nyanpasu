# uv

Required for the source-installation commands documented here. It manages Python versions, the Nyanpasu workspace, and standalone tools such as `gh-llm`. It is not a Python runtime library imported by Nyanpasu.

## Prerequisites and installation

Use the [official installation guide](https://docs.astral.sh/uv/getting-started/installation/) for your OS. The standalone installer does not require an existing Python installation. Its shell installation method needs a shell and a download utility such as curl; packages and prebuilt binaries are also available.

Verify that the service account can run:

```bash
uv --version
```

Project packages go into the workspace environment with `uv sync`. External CLIs use `uv tool install` and their own environments; project synchronization does not install those CLIs or agent skills.
