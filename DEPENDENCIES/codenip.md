# Codenip

[Codenip](https://github.com/ShigureLab/codenip) is an optional experimental GitHub reviewer skill. For each pinned diff, a Sol coordinator selects up to eight relevant rules from the installed catalog. One Luna worker per selected rule decides applicability and returns its verdict. The service freezes each selection and runs up to 32 rule workers across all reviews, starting the next queued worker as soon as a slot opens.

## Install and enable

Use authenticated [Git](git.md) with access to the Codenip repository. Run the following with the service account's environment and substitute its native skill directory for `skill_dir`.

```sh
set -eu
skill_dir=/absolute/path/to/skills
install_dir=$(mktemp -d)
trap 'rm -rf "$install_dir"' EXIT
git clone --no-checkout https://github.com/ShigureLab/codenip.git "$install_dir/codenip"
git -C "$install_dir/codenip" checkout --detach c9e63447ce259b5989aa117448ce2ee785444b42
mkdir -p "$skill_dir"
test ! -e "$skill_dir/codenip"
cp -R "$install_dir/codenip/skills/codenip" "$skill_dir/codenip"
```

Configure the installed `SKILL.md` path as seen by the service and the coordinator. Configure both model routes on the selected backend.

```toml
[plugins.settings.github_reviewer]
codenip_skill = "/absolute/path/to/skills/codenip/SKILL.md"

[tasks.kinds."github_reviewer.codenip".execution]
backend = "codex"
model = "gpt-6.2-sol"
reasoning = "medium"

[tasks.kinds."github_reviewer.codenip-rule".execution]
backend = "codex"
model = "gpt-6-luna"
reasoning = "xhigh"
```

After restarting the service, a code review with an applicable rule can dispatch a managed `codenip` child. Its `codenip-rule` children each record a model and rule ID. The root reviewer verifies the aggregate result and prefixes supported advice with an experimental marker and the actual rule ID, for example `[experimental · REF001]`.
