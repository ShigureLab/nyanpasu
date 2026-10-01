# Node.js

Required to build or develop the Dashboard. Also needed when choosing npm/npx installation methods for external tools or skills. Serving already-built Dashboard assets does not require Node.js.

## Prerequisites and installation

Install a supported OS build from the [official Node.js download page](https://nodejs.org/en/download). For Dashboard work, match the Node.js version used in the [Dashboard CI job](../.github/workflows/unit-test.yml).

Verify Node.js and, when using npm/npx installers, npm:

```bash
node --version
npm --version
```

Then install [pnpm](pnpm.md) and the [frontend dependencies](frontend.md). On older systems, check upstream binary/platform compatibility rather than assuming an installed executable can start.
