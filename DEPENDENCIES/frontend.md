# Frontend libraries and tooling

Required when building or developing the Web Dashboard. The Python service serves the generated static assets in production.

## Prerequisites and installation

Install [Node.js](nodejs.md) and [pnpm](pnpm.md). From the Nyanpasu repository root:

```bash
pnpm install --frozen-lockfile
pnpm run build
```

The package manager installs React/React DOM, react-markdown, remark-gfm, and the development toolchain: Vite+, TypeScript, Vitest, Happy DOM, Prettier, type declarations, and the schema-to-TypeScript generator. [package.json](../package.json) and [pnpm-lock.yaml](../pnpm-lock.yaml) define the complete list and versions; do not install global copies of these libraries.

The generated `src/nyanpasu/dashboard_static/` directory is ignored by Git. Build before serving `/dashboard` from a checkout or packaging a Python distribution with the Dashboard included.

For frontend checks and type generation, also install the [Python development environment](python-development.md). See the [Dashboard development commands](../README.md#run) and [Playwright](playwright.md) for browser tests.
