# Memory QA

## 范围说明
本文件总结了从“原始的 memory 是怎么做的”开始的问答，聚焦 nanobot 的 memory（legacy / memoryos）机制。

## Q1. 原始（legacy）memory 是怎么做的？有长期和短期吗？
A:
- 有，可理解为三层：
- 短期上下文：同一会话最近 `memory_window` 条消息。
- 长期记忆：`workspace/memory/MEMORY.md`。
- 历史日志：`workspace/memory/HISTORY.md`（事件摘要，便于 grep）。

## Q2. 短期上下文是什么？
A:
- 就是 session 最近 N 条消息（默认 50）直接拼进当前 LLM 请求。
- 不是独立的“短期向量库”。

## Q3. 为什么不把同一会话全量历史都拼给模型？
A:
- 超出窗口后不会继续全量拼接。
- 原因是 token 成本、延迟、上下文上限和噪声控制。
- 老内容会被“记忆整合”压缩到 `MEMORY.md/HISTORY.md`。

## Q4. 这里说的是同一会话吗？
A:
- 是。飞书这类 channel 往往长期落在同一个 `session_key`，所以必须用窗口截断。

## Q5. 长期记忆来自超窗口对话吗？
A:
- 主要来自超窗口的旧对话，但不是原文搬运。
- 是 LLM 提炼后的“事实/摘要”。

## Q6. HISTORY.md 存什么？全量还是提炼？
A:
- 存提炼后的事件摘要（`history_entry`），不是全量原文。
- 全量原始对话在 session `.jsonl` 文件里。

## Q7. MEMORY.md 和 HISTORY.md 的区别？
A:
- `MEMORY.md`：长期事实与偏好，偏“当前状态”，会更新覆盖。
- `HISTORY.md`：时间线事件摘要，偏“流水日志”，追加写入。

## Q8. 什么时候会读 HISTORY.md？
A:
- legacy 下不会自动每轮都读。
- 通常是模型在需要回溯时，按提示词自行用工具读/grep。

## Q9. legacy 模式的记忆系统可否概括为三种？
A:
- 可以：会话窗口 + `MEMORY.md` + `HISTORY.md`。
- 无向量检索。

## Q10. memoryos 模式和 legacy 模式的系统提示词有区别吗？
A:
- 主体模板基本一致。
- memoryos 检索到内容时会额外注入 `## Retrieved Memory`。
- legacy 下该段通常为空（不注入）。

## Q11. 也就是说只是额外拼接一段？
A:
- 对，是在 system prompt 末尾额外拼接检索记忆段。

## Q12. memoryos 模式还会写 MEMORY.md 吗？原项目也是这样吗？
A:
- 在当前 `nanobot-memoryos` 集成里，会继续写 `MEMORY.md/HISTORY.md`（兼容双轨）。
- 同时也写 MemoryOS 的结构化存储（`memoryos_data`）。
- 这属于当前集成策略，不是 MemoryOS 原仓库的唯一固定做法。

## Q13. memoryos 是“写死规则”，MEMORY.md 要模型自己判断吗？
A:
- MemoryOS 侧每轮会 `add_turn`，流程更“硬”。
- `MEMORY.md` 侧有两条路径：
- 自动整合：超窗触发 consolidation，LLM 产出 `memory_update` 后写入。
- 主动写入：agent 也可能自行用工具写文件。

## Q14. `## Retrieved Memory` 主要包含什么？
A:
- 常见四类：
- `User Profile`
- `User Knowledge`
- `Assistant Knowledge`
- `Relevant Past Dialogues`

## Q15. MemoryOS 原项目是不是靠相似度召回？
A:
- 是，核心是 embedding + 相似度检索（分层记忆）。

## Q16. 是不是会让模型改写 query 再多轮召回？
A:
- 当前这版基本是单次直接召回（当前 query -> 检索 -> 注入）。
- 没有内置“LLM 改写 query 后再次召回”的循环策略。

## Q17. AgentLoop 里工具结果会不会算一轮并持久化？
A:
- 一轮在实现里指“一次 LLM 调用”。
- 工具调用与工具结果会参与本次请求内部迭代，但不会直接持久化到 session 历史。
- session 持久化的是：用户消息 + assistant 最终回复。

## Q18. tool_call / tool_result 会出现在下一轮用户对话历史里吗？
A:
- 不会直接出现（不作为独立历史消息保存）。
- 但如果 assistant 最终回复中“转述了工具结果”，该转述文本会被保存并在后续历史中出现。

## Q19. 重启 nanobot 后是不是一定是新 session？
A:
- 不一定。session_key 通常是 `channel:chat_id`。
- 同一个飞书 chat_id 仍会命中同一 session 文件并继续历史。
- 你当前 `.nanobot/sessions` 里大量 `memtest*` 文件主要来自测试，不是飞书自动产生。

## Q20. 如果 legacy 会话没超过窗口，会不会丢“长期记忆沉淀”？
A:
- 会有这个风险：未超窗时不会触发 consolidation，内容只在 session jsonl。
- 若后续换了新的 session_key，这部分信息可能不被自动带入。

## Q21. memoryos 存储/更新/检索在 nanobot 里怎么走？
A:
- 存储：每次用户请求完成后调用一次 `add_turn(user_input, final_assistant_reply)`。
- 更新：由 MemoryOS 内部阈值/热度机制触发（短期满 -> 中期、热点触发画像/知识更新）。
- 检索：新 query 到来时检索并注入 `Retrieved Memory`。

## Q22. memoryos 的检索会不会和上下文窗口重复？
A:
- 会有重叠可能（尤其中期页与最近对话语义接近时）。
- 你已加了“与最近窗口冗余过滤”：若候选与 recent history 有包含关系则不注入。
- 当前设定是“不过滤检索内部重复、不截断条数，仅做窗口冗余过滤”。

## Q23. short_term_capacity 和 memory_window 该怎么理解？
A:
- `memory_window` 按“消息条数”截取（不是 token）。
- `short_term_capacity` 按“QA 对”计数（1 对约等于 2 条消息）。
- 两者不必强制一致；例如 `10/30`、`15/30` 都是可用组合。

## Q24. 最终采用的 memoryos 与 legacy 关系策略是什么？
A:
- 生成阶段：`memoryos` 模式不注入 legacy `# Memory` 块。
- 写入阶段：`memoryos` 模式仍镜像写入 `MEMORY.md/HISTORY.md`，便于将来切回 legacy 复用历史。
- 检索阶段：通过 `# Retrieved Memory` 注入，并加入简短使用规则。

## Q25. `# Retrieved Memory` 现在放在什么位置？
A:
- 已从“system prompt 末尾”调整到“与 `# Memory` 同层的前部上下文区块”。
- 目的：提高检索记忆在生成时的可见性与优先级。
