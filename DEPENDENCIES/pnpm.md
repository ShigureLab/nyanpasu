# pnpm

Required to install and build the Dashboard's frontend dependencies from source.

## Prerequisites and installation

Install [Node.js](nodejs.md), then follow the [official pnpm installation guide](https://pnpm.io/installation). Use the version in `packageManager` in [package.json](../package.json), which is the authority for this checkout.

From the repository root, one npm-based installation method reads that exact version:

```bash
npm install --global "$(node -p 'require("./package.json").packageManager')"
pnpm --version
pnpm install --frozen-lockfile
```

Follow the upstream guide if your package-manager setup needs a different installation method. [pnpm-lock.yaml](../pnpm-lock.yaml) records resolved frontend dependencies; use [frontend instructions](frontend.md) for builds and checks.
