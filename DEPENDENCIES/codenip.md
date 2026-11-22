# Codenip

[Codenip](https://github.com/ShigureLab/codenip) is an optional experimental GitHub reviewer skill. It checks focused rules for code, tests and documentation using one lightweight reviewer per rule and scope.

## Install and enable

Use authenticated [Git](git.md) with access to the Codenip repository. Run the following with the service account's environment and substitute its native skill directory for `skill_dir`.

```sh
set -eu
skill_dir=/absolute/path/to/skills
install_dir=$(mktemp -d)
trap 'rm -rf "$install_dir"' EXIT
git clone --no-checkout https://github.com/ShigureLab/codenip.git "$install_dir/codenip"
git -C "$install_dir/codenip" checkout --detach 6bfb8024cb217789708571b75cc69dbe63f6dfdc
mkdir -p "$skill_dir"
test ! -e "$skill_dir/codenip"
cp -R "$install_dir/codenip/skills/codenip" "$skill_dir/codenip"
```

Configure the installed `SKILL.md` path as seen by the service and the parent reviewer. Select a lightweight model available on the configured backend.

```toml
[plugins.settings.github_reviewer]
codenip_skill = "/absolute/path/to/skills/codenip/SKILL.md"

[tasks.kinds."github_reviewer.codenip".execution]
backend = "codex"
model = "gpt-6-luna"
reasoning = "low"
```

After restarting the service, a code review with an applicable rule can dispatch a managed `codenip` child. Check its kind, model and rule ID in the task record. The parent verifies the result and prefixes supported advice with an experimental marker and the actual rule ID, for example `[experimental · REF001]`.
