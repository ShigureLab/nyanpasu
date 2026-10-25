# 真实 PR 评测

所有真实 PR 使用同一份 [cases.json](cases.json)，按 `case_id` 定位。来源仓库、参考证据、历史讨论、运行输入和评分标准都属于这条记录。语料包含参考案例和可运行案例，先查询状态，再选择要运行的案例。

| 文件                                                          | 用途                                                                                                                                     |
| ------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| [cases.json](cases.json)                                      | 唯一的案例数据。`evidence` 保存评论证据，`timeline` 保存历史讨论，`evaluation` 保存运行配置和评分标准；`evaluation: null` 表示仅作参考。 |
| [scripts/reviewer_eval.py](../../../scripts/reviewer_eval.py) | 公共执行工具：准备案例、录制真实 reviewer 输出、汇总独立判分。                                                                           |

每个可运行案例的 `evaluation` 包含评测类型 `kind`、目标环境 `environment`、截止时间、任务需求、固定源码列表 `inputs` 和评分标准 `expected`。每份源码使用 `commit`、仓库相对 `path`、SHA-256 和 Git blob 哈希；跨仓库的依赖输入另带 `repository`。后续案例也写入这一结构，不另建仓库或批次专用的案例表。

## 1. 查看全部案例和就绪状态

```sh
eval_data=evals/reviewer/real-pr
uv run python scripts/reviewer_eval.py list-cases --cases "$eval_data/cases.json"
```

输出包含全量案例的 `case_id`、仓库和 `status`，以及总数和可运行数量。`runnable` 表示已有输入与评分配置；`reference` 表示当前证据不足以确定可评分目标，包括问题已在绑定版本中解决、开放设计问题、偏好建议或缺少关键契约。它不表示尚未筛选。只看可运行案例时加 `--runnable`：

```sh
uv run python scripts/reviewer_eval.py list-cases --cases "$eval_data/cases.json" --runnable
```

参考证据不等于已就绪评测。现有可运行配置使用一份或多份历史文件快照和新写的中性任务说明，完整 PR diff 与原始需求尚未还原；它们属于校准集。同根因案例及各次修订应放在同一划分，不能再计作未见测试集。

## 2. 运行 reviewer

在仓库根目录运行，需要项目依赖和 Git。缓存和运行输出放在仓库外。公共工具按 `case_id` 下载并校验该案例的全部 reviewer 输入，不需要额外安装案例验证依赖：

```sh
eval_data=evals/reviewer/real-pr
eval_out="$(mktemp -d)"
case_id="<从列表选择一个 runnable 案例的 ID>"
uv run python scripts/reviewer_eval.py prepare-case \
  --cases "$eval_data/cases.json" --case-id "$case_id" \
  --output "$eval_out/run" --source-cache "$eval_out/sources" --arm A \
  --target codex/YOUR_MODEL:high
```

通过现有 `nyanpasu run-task` 运行，默认调用 reviewer 的 `build_review_instructions`，使用正式的 [reviewer.md](../../../packages/nyanpasu-github-reviewer/src/nyanpasu_github_reviewer/instructions/reviewer.md) 和测试策略。只补充离线环境约束：线上 PR、标签、CI、发布和插件专用控制工具在此实验中不可用，评审固定本地输入并私下返回结果。评审标准不另维护一份；这不是完整 GitHub reviewer 插件生命周期的端到端评测。

指定未知或未就绪案例会报错。首次按固定 commit 下载源码，复用缓存时仍校验 SHA-256 和 Git blob 哈希。`environment` 描述被审查代码的版本、依赖和可验证范围；它不表示工具已经安装了完整上游运行环境。工具本身使用项目环境。

工具只提取该案例的输入文件和中性需求，生成中性提交，并在仓库外冻结本次使用的评测配置。历史评论、讨论和 `expected` 不进入 reviewer 输入。完整案例 ID 保留在评测方的 manifest 中；实验目录宜使用中性名称。新案例或新实验使用新的输出目录。

运行前配置返回的 `home/config.toml`，然后依次执行返回的 `command` 和 `record_command`。例如使用 Codex 时：

```toml
[backends.codex]
driver = "codex"
[backends.codex.options]
sandbox = "read-only"
approval_policy = "never"
[backends.codex.process.env]
CODEX_HOME = "<独立且已认证的后端 home>"
[tasks.defaults.execution]
backend = "codex"
model = "YOUR_MODEL"
reasoning = "high"
[tasks.defaults.limits]
turn_timeout_seconds = 600
[memory]
enabled = false
consolidate = false
```

`tasks.defaults.execution` 应与 `--target` 一致，使自动创建的子任务也使用同一模型和推理强度。每个实验使用独立 runtime 和原生后端 home，不复用在线服务状态。只读设置不禁止读取其他目录或访问网络；严格隔离需要独立容器、挂载和网络限制。工具固定并校验输入、指令、runtime 和原生输出摘要，但不替你安装 reviewer 依赖或验证完整 GitHub 发布流程。

A/B 对比使用相同案例、模型、预算和环境，在同一实验输出目录准备 `--arm B`。默认使用当前正式指令；只有对照另一份已渲染的指令时才显式提供 `--instructions PATH`。两组会话相互隔离。工具冻结实际使用的指令，并记录正式指令及引用文档的摘要；这些文档变更后需要新实验，不能沿用旧记录。

## 3. 判分

独立裁判读取匿名 `blind/<sample-id>/report.txt`，对照冻结的 `evaluation.expected`、固定源码和引用证据判分。`expected.kind` 区分 `defect`（发现具体缺陷）、`simplification`（提出能保持契约的精简）和 `retention`（保留有必要的机制、避免错误删除）。`expected.behavior` 给出具体触发条件、正确结果及应拒绝的结论；来源评论本身不等于答案。

`criteria` 将该案例的目标判为 `pass`、`fail` 或 `inconclusive`；每项必须绑定 `expected.root_cause`、引用原始报告并提供核验证据。`findings` 另行记录缺陷、有效精简、误报等发现。保留机制和精简案例不应被强行计入缺陷分母。公共工具核验引用、冻结目标和摘要并汇总，语义判断由独立裁判完成。

在仓库外的 judgments 目录为每个样本保存 assessment：

```json
{
   "sample_id": "运行生成的匿名样本 ID",
   "adjudicator": "裁判身份",
   "status": "provisional",
   "expected_defects": null,
   "criteria": [
      {
         "root_cause": "evaluation.expected.root_cause 的值",
         "outcome": "pass",
         "quote": "从 report.txt 逐字复制的证据",
         "evidence": ["固定源码中支持或反驳该结论的契约与行为"]
      }
   ],
   "findings": []
}
```

其他判定包括 `useful_simplification`、`false_positive`、`harmful_suggestion`、`inconclusive`。同一根因只计一次，额外有效发现单独裁定，漏报仍保留在分母中。`expected_defects: null` 表示覆盖率未知，空对象表示经裁定没有已知缺陷。尚未完成独立证据核对的裁定保持 `provisional`；完成核对后使用 `confirmed`。同一案例的目标不会因某次模型漏报而改变。

```sh
uv run python scripts/reviewer_eval.py report "$eval_out/run" \
  --judgments "$eval_out/judgments"
```

工具核对原文引用和摘要，只把已确认裁定计入质量指标，未裁定和失败样本明确显示。参考评论、批准、合并、CI 通过都不能代替独立裁定。历史讨论的正文若经编辑且没有旧存档，不能仅按创建时间推断当时可见内容；review commit 的绑定也不一定指向正文讨论的修订。
