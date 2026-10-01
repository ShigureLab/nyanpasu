# Target repository environments

Required only when a task needs to build or test the repository being reviewed or modified. These requirements are independent of Nyanpasu's own runtime dependencies.

## Prerequisites and installation

Use the target repository's README/contribution guide, dependency manifests, lockfiles, and CI definitions as the installation authority. Depending on that project, this may include Python environments, Node.js packages, Rust/Go/Java toolchains, C/C++ compilers, databases, browsers, model files, or accelerator runtimes.

Prepare the environment where the agent's commands actually execute. Verify an existing suitable environment before installing another one.

Nyanpasu itself does not require a GPU or NPU. Install hardware-specific drivers, SDKs, and test dependencies only for target workloads that require them, following that project's vendor documentation. Record unavailable integration or hardware tests as validation gaps.
