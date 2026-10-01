# just

Optional locally. It runs recipes from the repository's [justfile](../justfile); CI uses it for Python installation and checks. The underlying commands can also be run directly.

## Prerequisites and installation

Install an OS package using the [official package list](https://just.systems/man/en/packages.html). Then inspect the available recipes:

```bash
just --version
just --list
```

The recipes themselves require the tools they invoke: [uv](uv.md) for Python work and [pnpm](pnpm.md) for frontend or Markdown work. `just` does not install those tools automatically.
