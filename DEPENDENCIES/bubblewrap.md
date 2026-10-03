# bubblewrap

Required for all Nyanpasu execution backends, including Claude Code. The service uses bubblewrap to separate context workspaces, native homes, process namespaces, and task control capabilities. Worker execution currently requires Linux. See the [upstream project](https://github.com/containers/bubblewrap) and [Nyanpasu isolation configuration](../docs/configuration.md#linux-execution-isolation).

## Prerequisites and installation

Install the distribution package providing `/usr/bin/bwrap`. On Debian or Ubuntu:

```bash
sudo apt-get update
sudo apt-get install bubblewrap
bwrap --version
```

The operating system must permit the namespaces used by the sandbox. Some distributions also require an AppArmor profile; use the upstream instructions for that platform.

Inside a container, the outer runtime's security policy must allow the nested sandbox to start. Docker's default seccomp profile can block namespace creation; see [Docker's seccomp documentation](https://docs.docker.com/engine/security/seccomp/). Configure the required namespace operations in the deployment's security profile while retaining the intended isolation.

Verify a real read-only shell command through each configured Nyanpasu backend under its isolation and approval settings. Check the tool's output and exit status: a model can return a completed turn even when its command failed to start.
