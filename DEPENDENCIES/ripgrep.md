# ripgrep (`rg`)

Optional, recommended for fast code and file searches by agents and developers.

## Prerequisites and installation

Use an OS package or prebuilt binary from the [upstream installation guide](https://github.com/BurntSushi/ripgrep#installation). Rust is needed only if choosing a source build. Verify it in the service environment:

```bash
rg --version
```

Some agent distributions provide their own copy. Install a separate copy only if the service or child commands cannot find a working `rg` on their `PATH`.
