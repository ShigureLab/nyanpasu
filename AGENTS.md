# 开发原则

- **简单且正确。** 尽量少的代码和组成部分，同时保持清晰可读。
- **只处理真实可能发生的失败。** 外部输入在边界校验；内部信任已建立的契约，不为不可能状态堆防御分支、兜底和恢复逻辑。
- **小而内聚的函数。** 职责、输入输出、依赖明确；按职责拆分，不为缩短行数机械拆函数。
- **偏好纯函数和不可变数据。** 副作用集中在明确边界，依赖显式传入，避免隐藏修改和共享可变状态。
- **重复或结构不合适时先重构。** 减少特殊分支、包装层和补丁；抽象围绕真实领域概念或已存在的共性，不预建通用框架。
- **每个事实只保留一个权威来源。** 能从规范状态推导的数据，就不再维护一份需要同步的状态。

# 提交与 PR 规范

- Commit 标题和 PR 标题必须使用 `<gitmoji> <type>: <subject>`，不能省略 gitmoji。遵循 [yutto 的提交约定](https://github.com/yutto-dev/yutto/blob/main/AGENTS.md#commit-and-pr-conventions)，使用与改动类型匹配的 gitmoji shortcode，subject 简明描述实际改动。
- 常用映射如下；其他类型参考 [yutto 的 PR 模板](https://github.com/yutto-dev/yutto/blob/main/.github/PULL_REQUEST_TEMPLATE.md)。

   | 类型   | 标题前缀                    |
   | ------ | --------------------------- |
   | 新功能 | `:sparkles: feat:`          |
   | 修复   | `:bug: fix:`                |
   | 文档   | `:pencil: docs:`            |
   | 重构   | `:recycle: refactor:`       |
   | 性能   | `:zap: perf:`               |
   | 测试   | `:white_check_mark: test:`  |
   | CI     | `:construction_worker: ci:` |
   | 依赖   | `:arrow_up: deps:`          |

- 这项约定同样适用于 merge / squash 生成的最终提交；提交或合并前检查最终标题。
- 由编码代理协助完成的提交必须添加对应的 `Co-authored-by` trailer；Codex 使用 `Co-authored-by: Codex <noreply@openai.com>`。
