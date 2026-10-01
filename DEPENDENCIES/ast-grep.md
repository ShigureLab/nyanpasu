# ast-grep CLI

Optional. It searches code by syntax structure, complementing [ripgrep](ripgrep.md)'s text search.

## Prerequisites and installation

Use the [official quick start](https://ast-grep.github.io/guide/quick-start.html) to choose an OS package, binary, npm installation, or Cargo build. npm installation requires [Node.js/npm](nodejs.md); a Cargo source build requires a Rust toolchain and the platform build dependencies.

Verify the installed command:

```bash
ast-grep --version
```

Use `ast-grep` explicitly: on some Unix systems `sg` names a different system command. Install the separate [ast-grep skill](ast-grep-skill.md) to teach the agent its search workflow.
