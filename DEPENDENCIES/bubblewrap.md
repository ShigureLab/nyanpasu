# bubblewrap

Required for the current Codex Linux and WSL2 sandbox setup. It is not a Claude backend dependency. Follow the [official Codex sandbox prerequisites](https://learn.chatgpt.com/docs/sandboxing#prerequisites) for platform-specific requirements.

## Prerequisites and installation

Install the distribution package so `bwrap` is on the service's `PATH`. On Debian or Ubuntu:

```bash
sudo apt-get update
sudo apt-get install bubblewrap
bwrap --version
```

The operating system must permit the namespaces used by the sandbox. Some distributions also require an AppArmor profile; use the upstream instructions for that platform.

Inside a container, the outer runtime's security policy must allow the nested sandbox to start. Docker's default seccomp profile can block namespace creation; see [Docker's seccomp documentation](https://docs.docker.com/engine/security/seccomp/). Configure the required namespace operations in the deployment's security profile while retaining the intended isolation.

Verify a real read-only shell command through the Nyanpasu Codex backend under its configured sandbox and approval settings. Check the tool's output and exit status: a model can return a completed turn even when its command failed to start.
