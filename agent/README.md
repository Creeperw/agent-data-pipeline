# Agent 蒸馏环境：意图识别到工具调用

本目录用于构建主模型的第一阶段 SFT 数据：

- 输入不带历史对话；
- 主模型在第一步自己做意图识别，seed 中的 `expected_intent` 只作离线质检参考，不会进入 planner 输入；
- 根据现有信息是否足够决定继续调用工具、追问或结束规划；
- 支持多轮工具调用；
- 规划结束时必须输出 `planning_finish`。

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `config.py` | API、模型、路径、并发配置，默认沿用 `g_s3.py` 的 DeepSeek 配置 |
| `prompts.py` | planner system prompt、工具列表、输出格式 |
| `executor_prompts.py` | 执行阶段最终回答 agent 的 system prompt 和上下文模板 |
| `tools.py` | 本地 RAG 摘要检索、网络搜索包装工具和执行阶段写文件工具执行层 |
| `generator.py` | 异步构建完整规划轨迹 |
| `convert_to_sft.py` | 将完整轨迹切成 step-wise SFT 样本 |
| `executor_generator.py` | 从规划轨迹中提取意图和工具信息，合成执行阶段最终回答数据 |
| `reviewer_generator.py` | 审核 executor 回答，输出严格 JSON 的通过/拒绝、分数、问题和建议 |
| `filter_reviewed_sft.py` | 按显式样本 ID 筛选审核通过的 final prompt，并保留拒绝样本 |
| `data/seed_intent_tool.jsonl` | 初始 seed 输入 |
| `core/` | 领域无关的 DomainSpec、prompt renderer 等通用能力 |
| `domains/` | 可插拔领域包，例如 `health`、`customer_service`、`health_talent` |
| `tools/` | 全局工具库：跨领域复用的通用工具，领域通过 `tools.json` 按需开放 |
| `ui/` | 本地 Web 控制台，把上述脚本编排成一个可视化操作界面 |

## Web 控制台

除了逐个敲命令行，也可以用 `agent/ui/` 提供的本地 Web 控制台完成同一套流程：

```bash
python -m agent.ui          # 默认 http://127.0.0.1:8770
```

控制台只是编排层：它不修改任何既有脚本，每次执行都是独立的
`python -m agent.<module>` 子进程，参数拼装方式与 `pipeline_launcher.py` 一致。
因此两条路径可以混用，产物文件完全相同。

主要能力：

- 勾选阶段、填参数、一键跑主链路；每个阶段可单独重跑或断点续跑；
- SSE 实时日志（含 tqdm 进度条），可随时取消并杀掉整个进程组；
- 产物清单（存在/大小/行数/修改时间）与在线预览；
- 在线编辑 `domains/<领域>/seeds.jsonl`，保存前自动备份并逐行校验 JSON；
- 渲染 `data_stats` 的 JSON 与 markdown 报告。

### Reviewer 角色

主链路现在支持完整的第三个角色：

```text
planner → executor → reviewer → merged final prompt → reviewed final prompt → dedup
```

Reviewer 只读取 `executor_answer_sft.jsonl`，不会覆盖 planner、executor 或 merged
原始产物。每条审核结果至少包含：

```text
decision: pass | reject
score: 1..5
issues: [{type, message, evidence?}]
suggestions: [string]
```

程序只消费 `decision`、`score` 等协议字段，不通过关键词判断自然语言意见。审核
结果写入 `review_results.jsonl`；筛选阶段按 `source_sample_id` 与 merged 样本的
`id` 精确关联，输出 `reviewed_final_prompt.jsonl` 和 `review_rejected.jsonl`。
审核筛选每次按完整输入重建两个输出，因此重复运行不会重复追加；原始
`merged_final_prompt.jsonl` 始终保留。

命令行示例：

```bash
python -m agent.reviewer_generator \
  --domain health_talent \
  --input agent/outputs/health_talent/<任务>/executor_answer_sft.jsonl \
  --output agent/outputs/health_talent/<任务>/review_results.jsonl

python -m agent.filter_reviewed_sft \
  --merged-input agent/outputs/health_talent/<任务>/merged_final_prompt.jsonl \
  --reviews-input agent/outputs/health_talent/<任务>/review_results.jsonl \
  --output agent/outputs/health_talent/<任务>/reviewed_final_prompt.jsonl \
  --rejected-output agent/outputs/health_talent/<任务>/review_rejected.jsonl
```

`health_talent` 领域定义了专门的 `reviewer_system_prompt`，审核维度包括相关性、
完整性、证据一致性、个性化可执行性、健康安全边界和表达质量。其他领域即使暂时
没有专门 prompt，也会使用 `DomainSpec` 提供的通用审核兜底 prompt。

控制台的「角色」页按当前项目隔离。每个项目都会展示当前流水线的三个内置角色：
`planner`、`executor` 和 `reviewer`。它们不是普通项目文件，但都提供了可复制的内置
模板；复制后可保存为当前项目自己的自定义角色，文件位于
`agent/domains/<项目>/roles/*.json`。角色类型就是模板选择，不再另设重复的模板下拉框。角色输入 / 输出协议
以实际流水线调用为准：planner 接收用户问题、历史步骤和当前 tools，输出
`intent`、`action` 以及可选的 `tool_calls` / `ask` / `finish_reason`；executor 接收
用户问题、意图、外部信息、回答模板和历史上下文，输出最终回答文本；reviewer 接收
用户问题、执行回答、意图和外部信息，输出 `decision`、`score`、`issues` 和
`suggestions`。

完整说明见 `agent/ui/README.md`。

## 领域包与 `--domain`

当前合成系统已经从单一健康管理领域解耦为 domain pack 架构。Planner 的动作协议保持不变，所有领域仍统一输出：

- `tool_call`
- `planning_finish`
- `ask_clarification`

但每个领域可以独立定义：

- planner / executor system prompt；
- executor 最终训练 prompt 拼装时的可选独立 system prompt；
- 是否启用 `intent`；
- 是否启用回答模板 `answer_template`；
- 工具 schema；
- 工具运行时 `tool_runner`；
- 工具采样策略 `tool_selection`；
- demo seeds。

已有领域：

| 领域 | 说明 |
| --- | --- |
| `health` | 默认健康管理领域，兼容旧训练分布。 |
| `customer_service` | 通用客服 demo，用于验证无意图、无模板领域。 |
| `health_talent` | 健康管理个性化人才培养领域，覆盖健康管理指导、学情分析、技能培训、实训任务设计、教学方案设计、评价反馈、学习资源推荐等能力。 |

`health_talent` 的典型 seed 字段包括 `scenario`、`student_profile`、`target_competency`、`required_tools` 和 `metadata`。这些字段会在 planner / executor 输出中透传，便于后续按教学场景、学生画像和目标能力做数据筛选与评测。

Prompt 和工具现在统一由领域包提供。默认健康管理领域也已经迁移到 `agent/domains/health/`，其中：

- `prompts.py` 保存 planner prompt 和搜索类工具 schema。
- `executor_prompts.py` 保存 executor 合成 prompt 和 user prompt builder。
- `templates.py` 保存 intent taxonomy 和回答模板。
- `tools.py` 保存健康领域工具运行入口。
- `tools.json` 记录该领域从全局工具库 `agent/tools/` 开放了哪些通用工具。
- `spec.py` 通过 `DomainSpec` 汇总以上资源。

### 全局工具库

`agent/tools/` 是跨领域复用的通用工具库，一个工具一个 `.py` 文件，文件导出：

- `SPEC`（必填）：OpenAI function-calling schema，必须是**字面量 dict**（控制台用 `ast` 静态读取，不执行文件）。
- `MOCK_RESPONSE`（可选）：没有 `run` 实现时返回的模拟文本，支持 `{参数名}` 占位符。
- `run(arguments, sample=None)`（可选）：真实实现；存在时按真实逻辑执行，否则返回模拟文本。

领域通过 `agent/domains/<领域>/tools.json` 声明开放哪些工具：

```json
{"enabled": ["calculate_bmi", "get_current_datetime"]}
```

合并发生在 `load_domain()`：领域自带的同名工具优先，全局工具追加在后面。因为每个阶段都是独立子进程重新加载领域，所以新增或收回工具对所有脚本（`generator` / `executor_generator` / `convert_to_sft` / `multiturn_cli`）立即生效，无需改动这些脚本。

控制台的「工具」页可以勾选开放、新建、编辑代码和删除；保存时会校验语法、`SPEC` 是字面量 dict、以及工具名与文件名一致。

### 执行阶段的 system prompt

执行阶段有两个 system prompt 概念：

- `executor_system_prompt`：`executor_generator.py` 调 teacher 合成最终回答时使用。
- `executor_assembly_system_prompt`：`merge_final_sft.py` 拼装最终训练 prompt 时可选使用；为 `None` 时直接沿用合成数据中保存的 `messages[0]`。

可通过命令行指定领域：

```bash
python -m agent.generator --domain health
python -m agent.generator --domain customer_service
python -m agent.generator --domain health_talent
```

也可以通过环境变量设置默认领域：

```bash
export AGENT_DOMAIN=health_talent
```

领域包开发说明见：

```text
agent/domains/README.md
```

`health_talent` smoke 示例输出路径：

```text
agent/outputs/health_talent/smoke_planner_trajectories.jsonl
agent/outputs/health_talent/smoke_executor_answer_sft.jsonl
```

## 如何扩展

三件事的完整清单：加工具（两种）、加智能体角色。都按「改哪个文件、写什么」列出。

### 加一个全局工具

全局工具放在 `agent/tools/<工具名>.py`，**文件名就是模型看到的函数名**。三条路建它：

1. 控制台「工具」页 → 编辑器 → **新建**，填文件名，会生成一个只有空 `SPEC` 的骨架；
2. 复制 `agent/tools/_template.py`（开头的下划线让它不被加载器当成工具）；
3. 直接手写。

文件里导出：

| 导出 | 必需 | 说明 |
| --- | --- | --- |
| `SPEC` | 是 | OpenAI function-calling schema，**必须是纯字面量 dict**（加载器和控制台都用 `ast` 读源码文本，不执行文件） |
| `MOCK_RESPONSE` | 否 | 没有 `run` 时返回的模拟文本，`{参数名}` 会被调用参数替换 |
| `run(arguments, sample=None)` | 否 | 真实实现，返回字符串；不写就返回模拟文本 |

三条硬约束：

- 模块顶层不要 import `agent.core` 或任何重依赖（torch / transformers / openai …）。加载工具时领域正在加载中，顶层导入会形成加载期循环依赖；需要时在 `run()` 里延迟导入。
- `from agent.tools import FatalToolError` 同样只能写在 `run()` 里。
- 凭据只写变量名（`os.getenv("<服务名>_API_KEY")`），值放 `agent/.env`；控制台「工具」页的「工具凭据」面板可以增删改任意键，键名由工具自己定，不需要在代码里登记。

然后给项目开启：控制台「工具」页勾选，或直接写 `agent/domains/<领域>/tools.json`：

```json
{"enabled": ["calculate_bmi", "search_web"]}
```

合并发生在 `load_domain()` → `merge_global_tools()`：**领域自带的同名工具优先**，全局工具追加在后面。每个阶段都是独立子进程重新加载领域，所以改完立刻生效，不用重启控制台。

### 加一个项目独有工具

只对某一个领域有意义、或者要用到该领域语义时用它。改 `agent/domains/<领域>/spec.py` 三处：

1. 加 schema：`TOOLS = [_tool("工具名", "说明", {参数}, ["必填参数"])]`
2. 加实现：`def run_xxx_tool(name, arguments, sample=None) -> DomainToolResult`
3. 挂上去：`DomainSpec(tools=TOOLS, tool_runner=run_xxx_tool, ...)`

和全局工具的区别：

| | 全局工具 | 项目独有工具 |
| --- | --- | --- |
| 位置 | `agent/tools/<名字>.py` | `agent/domains/<领域>/spec.py` |
| 开关 | 有，按项目勾选 | 无，进了 `DomainSpec.tools` 就无条件生效 |
| 重名 | 被项目独有工具盖掉 | 优先 |
| 复用 | 跨项目 | 只在这个项目 |

判断标准：能跨领域复用的（计算、时间、网络搜索、本地检索）放全局库；只说这个领域的话（`analyze_learning_profile`、`generate_assessment_rubric`）放 `spec.py`。

### 加一个智能体角色

角色 = 一次独立的模型调用 + 一份自己的输出协议 + 一个流水线阶段。以 `reviewer` 为例，要动 8 处：

| # | 文件 | 改什么 |
| --- | --- | --- |
| 1 | `agent/schemas.py` | 角色的输出协议和校验函数（`validate_review_payload` 那类） |
| 2 | `agent/core/domain.py` | `DomainSpec` 加 `xxx_system_prompt` 字段 + `get_xxx_system_prompt()` |
| 3 | `agent/core/renderer.py` | `build_xxx_messages()`：把上游产物拼成角色输入 |
| 4 | `agent/domains/<领域>/spec.py` | 该角色的 system prompt 常量 |
| 5 | `agent/config.py` | `resolve_xxx_model()` + 输出文件路径常量 |
| 6 | `agent/<role>_generator.py` | 角色主逻辑（采样、调模型、重试、落盘），新文件 |
| 7 | `agent/ui/registry.py` | 产物 key + `_PREFIXED_PATHS` + `MERGEABLE_KEYS` + `STAGES` 一行 + `_build_xxx` + 注册进 `_BUILDERS` + `DEFAULT_PIPELINE` |
| 8 | `agent/tests/test_<role>.py` | 协议校验 + 落盘筛选的单测 |

1、6、8 是角色自己的东西；2–5、7 是把它接进现有骨架。

> 改完 `agent/core/**` 之后**必须重启控制台**：领域包的热加载只覆盖 `agent/domains/<领域>.*`，不覆盖 `agent/core.*`。不重启会看到 `DomainSpec.__init__() got an unexpected keyword argument ...`。顶栏的重启按钮可以直接原地重启（见 `agent/ui/README.md` 的「重启控制台」）。

## 多轮对话合成

多轮对话合成模块在现有单轮流程之上再包一层会话编排，默认不影响单轮数据生成。

关键规则：

- planner 阶段仍使用规划阶段 system prompt；executor 阶段仍使用执行阶段 system prompt。
- `planner` 的 JSON 输出、执行阶段的 `<think>`、工具调用过程都不进入历史对话。
- 会话开始时只随机一次工具集合与工具出错计划，后续轮次复用同一份会话级工具状态。
- 会话开始时只随机一次工具集合与工具出错计划，后续轮次复用同一份会话级工具状态；如果输入里已经带了固定工具名，也直接按该集合复用，不再重抽。
- 用户对话过长时，较早对话会先压缩成 `【压缩历史对话】`。
- 最近几轮原文对话放在 `【近期历史对话】`。
- 两个历史块都放在 `【外部参考信息】` 后面。

历史输入格式示例：

```text
【外部参考信息】
...

【压缩历史对话】
...

【近期历史对话】
用户：...
助手：...
```

如果要开启多轮模式，可以通过环境变量控制：

```bash
export AGENT_DISTILL_MULTI_TURN_ENABLED=1
export AGENT_DISTILL_RECENT_HISTORY_TURN_LIMIT=3
export AGENT_MULTI_TURN_COMPRESS_EVERY_TURNS=2
```

多轮会话级输出默认写入：

```text
agent/outputs/<domain>/multiturn_sessions.jsonl
agent/outputs/<domain>/multiturn_planner_trajectories.jsonl
agent/outputs/<domain>/multiturn_executor_answers.jsonl
```

如果要单独压缩历史，可以用 `agent.multiturn` 里的会话对象把当前会话序列化后喂给压缩模型，压缩结果只负责生成 `【压缩历史对话】` 内容；下一轮用户草稿由会话编排器另行生成，不和压缩模型混在一起。

也可以直接跑多轮会话编排 CLI：

```bash
python -m agent.multiturn_cli --domain health_talent --input agent/domains/health_talent/seeds.jsonl
```

该 CLI 当前已经具备：

- 会话级随机工具冻结
- 会话级工具失败计划
- 压缩历史对话生成
- 近期历史对话拼接
- 下一轮用户追问模拟器入口

多轮模块会复用现有的单轮样本结构，因此不需要改动现有单轮数据生成命令。当前新增的只是历史文本的拼接与会话级工具冻结能力。

## 统一路径配置

所有默认输入/输出文件名都集中在 `config.py` 中维护，其他脚本只从 `config.py` 读取默认值。常用变量包括：

| 变量 | 作用 |
| --- | --- |
| `RAG_SOURCE_FILE` / `RAG_SUMMARY_FILE` | RAG 摘要输入/输出 |
| `PLANNER_INPUT_FILE` / `PLANNER_TRAJECTORY_FILE` | 规划轨迹生成输入/输出 |
| `CONVERT_TO_SFT_INPUT_FILE` / `PLANNER_STEP_SFT_FILE` | 轨迹转 step-wise SFT 输入/输出 |
| `CONVERT_TO_FINAL_INPUT_FILE` / `PLANNER_FINAL_PROMPT_FILE` | step-wise SFT 渲染 final prompt 输入/输出 |
| `EXECUTOR_INPUT_FILE` / `EXECUTOR_ANSWER_FILE` | 执行阶段回答生成输入/输出 |
| `REVIEW_RESULTS_FILE` / `REVIEW_REJECTED_FILE` | reviewer 审核结果与拒绝样本 |
| `MERGE_PLANNER_FINAL_FILE` / `MERGE_EXECUTOR_INPUT_FILE` / `MERGED_FINAL_PROMPT_FILE` | planner/executor 合并输入/输出 |
| `REVIEWED_FINAL_PROMPT_FILE` | 仅审核通过的 final prompt |
| `DEDUP_FINAL_PROMPT_FILE` / `DEDUP_DUPLICATES_LOG_FILE` | 去重输出和重复簇日志 |
| `QWEN_BASE_MODEL_PATH` / `SFT_TRAIN_PROMPT_FILE` / `SFT_TRAIN_OUTPUT_DIR` | 训练模型、训练数据和输出目录 |

默认输出现在按领域分目录，并用一个统一前缀生成每个阶段的文件名：

```text
agent/outputs/<domain>/<prefix>_<stage>.jsonl
```

默认 `domain=health`、`prefix=health_train`，因此一套完整健康管理数据会默认写到：

```text
agent/outputs/health/health_train_planner_trajectories.jsonl
agent/outputs/health/health_train_planner_step_sft.jsonl
agent/outputs/health/health_train_planner_final_prompt.jsonl
agent/outputs/health/health_train_executor_answer_sft.jsonl
agent/outputs/health/health_train_merged_final_prompt.jsonl
agent/outputs/health/health_train_dedup_final_prompt.jsonl
agent/outputs/health/health_train_duplicates.jsonl
```

前缀默认由 `<领域名>_<数据集>` 拼成（`config.output_prefix_for`），数据集取自领域包
的 `dataset_splits`（基类默认 `train` / `valid`）。这样同名产物能一眼看出属于哪个
项目，训练集与测试集也不会互相覆盖。控制台顶栏的「构建任务」输入框（展开后能选到
本项目声明的各档数据集）和 `--prefix` 走的是同一套规则。

只想换同一批数据的文件名前缀时，改一个环境变量即可：

```bash
export AGENT_OUTPUT_PREFIX=train2
```

只想切换领域输出目录时，改领域即可：

```bash
export AGENT_DOMAIN=health_talent
```

也可以显式覆盖输出领域目录：

```bash
export AGENT_OUTPUT_DOMAIN=health_talent
export AGENT_DOMAIN_OUTPUTS_DIR=agent/outputs/health_talent
```

这些路径都支持环境变量覆盖。例如：

```bash
export AGENT_PLANNER_INPUT_FILE=agent/data/train2_summary.jsonl
export AGENT_OUTPUT_PREFIX=train2
export AGENT_SFT_TRAIN_PROMPT_FILE=agent/outputs/health/train2_dedup_final_prompt.jsonl
```

相对路径会自动按项目根目录解析。

## 生成轨迹

如果要使用真实网络搜索，先配置搜索服务 API Key。密钥统一写在 `agent/.env`
（参考 `agent/.env.example`），也可以在控制台「齿轮 → 运行时配置」里直接填：

```bash
# agent/.env
EXA_API_KEY=你的 Exa API Key
```

未配置搜索服务 API Key 或未安装对应 SDK 时，网络搜索包装工具会返回明确的不可用错误，不再回退到虚拟本地工具。

当前工具包括（搜索类工具定义在 `agent/domains/health/prompts.py`；计算类工具定义在全局库 `agent/tools/`，由 `agent/domains/health/tools.json` 开放）：

| 工具 | 后端 | 用途 |
| --- | --- | --- |
| `search_rag` | 本地 JSONL 样本 | 模型生成检索语句，工具按当前用户问题索引返回该样本的 `rag_summary` |
| `search_web` | 网络搜索 | 通用互联网搜索 |
| `search_food_web` | 网络搜索 | 食疗、食材、营养、饮食禁忌、慢病饮食建议 |
| `search_exercise_web` | 网络搜索 | 运动指导、运动处方、练习频率、运动注意事项 |
| `search_tcm_web` | 网络搜索 | 中医体质辨识、体质表现和调理方向 |
| `search_emotion_web` | 网络搜索 | 情志调节、焦虑睡眠、压力管理和放松训练 |
| `search_safety_web` | 网络搜索 | 红旗症状、何时就医、用药禁忌和风险判断 |
| `calculate_bmi` | 本地计算 | 根据身高体重计算 BMI 和体重分类 |
| `calculate_daily_water_intake` | 本地计算 | 按体重和活动水平估算每日饮水量 |
| `calculate_target_heart_rate` | 本地计算 | 按年龄和运动强度估算目标心率区间 |
| `generate_random_number` | 本地随机数 | 用户明确要求随机选择、抽签、编号或分组时调用 |
| `get_current_datetime` | 本地时间 | 问题依赖当前日期、时间、星期或计划起始日期时调用 |

`write_file` 属于执行阶段工具，不会暴露给规划阶段 SFT 数据。执行阶段如需写文件，默认写入：

```text
agent/outputs/health_plans/
```

为了提升泛化性，生成每条轨迹时不会总是暴露全部工具。`generator.py` 会先随机一个工具数量，再从工具全集中随机抽取子集，同时保留当前意图、场景或样本的必要规划工具。例如食疗样本会保留 `search_food_web`；包含 `rag_summary` 的样本会保留 `search_rag`。

随机工具数量上限可通过 `--max-tool-count` 或环境变量 `AGENT_DISTILL_MAX_TOOL_COUNT` 配置，默认值为 `4`；设置为 `0` 或负数表示不限制。若必要工具数量超过上限，会优先保留全部必要工具。

工具组合还会随机注入两类泛化场景：

- 无工具场景：通过 `--no-tool-ratio` 或 `AGENT_DISTILL_NO_TOOL_RATIO` 控制，默认 `0.08`。
- 无相关工具场景：通过 `--irrelevant-tool-ratio` 或 `AGENT_DISTILL_IRRELEVANT_TOOL_RATIO` 控制，默认 `0.08`。该场景只暴露与当前问题意图无关的工具，用于训练模型在工具不合适时追问或结束规划。

这里的 `ratio` 是本次待处理数据的配额比例，不是逐条样本独立概率。例如本次处理 1000 条且 `--no-tool-ratio 0.1` 时，会预分配约 100 条无工具样本。为兼容旧命令，`--no-tool-probability`、`--irrelevant-tool-probability` 和对应旧环境变量仍可使用，但语义按 ratio 处理。

工具执行还支持失败注入：

- `--tool-failure-ratio` / `AGENT_DISTILL_TOOL_FAILURE_RATIO`：工具调用返回失败 observation 的样本比例，默认 `0.06`。
- `--tool-failure-max-calls` / `AGENT_DISTILL_TOOL_FAILURE_MAX_CALLS`：每条失败样本最多注入失败的工具调用次数，默认 `1`。
- `--tool-failure-targets` / `AGENT_DISTILL_TOOL_FAILURE_TARGETS`：只对指定工具注入失败，多个工具用英文逗号分隔；为空表示所有工具都可能失败。

为兼容旧命令，`--tool-failure-probability` 和 `AGENT_DISTILL_TOOL_FAILURE_PROBABILITY` 仍可使用，但语义按 ratio 处理。

失败样本不是每轮工具调用都失败，而是在一条样本内按 `tool_failure_max_calls` 控制最多失败次数。默认每条失败样本只随机让 1 个符合条件的工具调用失败，其余工具调用仍正常执行。

失败 observation 的内容形如：`error：工具调用失败。失败工具：<tool_name>。原因：请求超时，未能获取有效结果。`

在项目根目录执行：

```bash
python -m agent.generator \
  --max-samples 5 \
  --max-workers 2 \
  --max-tool-rounds 3 \
  --max-tool-count 4 \
  --no-tool-ratio 0.08 \
  --irrelevant-tool-ratio 0.08 \
  --tool-failure-ratio 0.06 \
  --tool-failure-max-calls 1 \
  --tool-failure-targets search_rag,search_food_web
```

默认支持断点续跑：未指定 `--force-rebuild` 时，会读取已有输出文件中的 `id`，自动跳过已生成轨迹的数据，只处理未完成样本。需要从头重建时再加 `--force-rebuild`。

生成轨迹时会自动修复一种 OpenAI tool-calls 常见现象：如果中间工具调用 step 的 `planner_raw_output.raw_content` 为空，且该 step 的意图缺失或为 `其他`，系统会用轨迹最后一步的非 `其他` 意图回填该工具调用 step，避免把“工具调用时 content 为空”误训练成“意图为其他”。修复数量记录在 `empty_tool_call_intent_backfill_count`。

历史轨迹文件可用下面脚本离线修复：

```bash
python -m agent.fix_empty_tool_call_intents --dry-run
python -m agent.fix_empty_tool_call_intents --in-place
```

`--in-place` 会先生成 `.bak_时间戳` 备份；也可以使用 `--output` 写到新文件。

输出：

```text
agent/outputs/health/health_train_planner_trajectories.jsonl
```

## 转 SFT

```bash
python -m agent.convert_to_sft
```

默认同样支持断点续跑：未指定 `--force-rebuild` 时，会根据输出样本 `id` 自动跳过已转换的 step-wise SFT 样本。需要清空并重新转换时执行：

```bash
python -m agent.convert_to_sft --force-rebuild
```

默认会把模型原始输出作为 assistant 内容，包含：

```text
<think>
模型 reasoning_content
</think>
模型 JSON 输出
```

## 合并 planner 与 executor 训练数据

如果需要把 `convert_to_final.py` 生成的 planner 最终 prompt 数据，与 `executor_generator.py` 生成的最终回答数据合并成一个可直接交给 `SFT_TRAIN2.py` 的 JSONL，可执行：

```bash
python -m agent.merge_final_sft
```

默认输入：

```text
agent/outputs/health/health_train_planner_final_prompt.jsonl
agent/outputs/health/health_train_executor_answer_sft.jsonl
```

默认输出：

```text
agent/outputs/health/health_train_merged_final_prompt.jsonl
```

输出格式与 `convert_to_final.py` 完全一致：

```json
{"id": "...", "prompt": "...", "metadata": {}}
```

其中 planner 数据直接复用已有 `prompt`，executor 数据会先用同一 tokenizer 的 `apply_chat_template` 渲染成 `prompt`。默认支持断点续跑；如需重新合并全部样本，使用：

```bash
python -m agent.merge_final_sft --force-rebuild
```

## 数据统计

可以使用统一统计模块查看不同阶段数据质量、比例和长度分布：

```bash
python -m agent.data_stats
```

不传路径时会自动统计 `agent/outputs` 下的默认数据文件。也可以指定一个或多个 JSONL：

```bash
python -m agent.data_stats agent/outputs/health/health_train_planner_trajectories.jsonl
python -m agent.data_stats agent/outputs/health/*.jsonl --output agent/outputs/health/health_train_data_stats.md
python -m agent.data_stats agent/outputs/health/*.jsonl --format json --output agent/outputs/health/health_train_data_stats.json
```

模块会自动识别以下数据类型并采用不同统计方式：

- seed / 原始输入数据：意图分布、场景分布、问题长度、RAG 摘要覆盖率。
- planner trajectory：轨迹长度、动作分布、最终动作/意图、工具暴露与调用、no_tools / irrelevant_tools 比例、step-wise 估算比例、失败注入、空 raw_content 回填、意图冲突、异常工具调用等。
- planner step-wise SFT：消息角色、动作分布、工具暴露与调用、assistant 监督文本长度、reasoning 覆盖率。
- executor answer SFT：意图分布、外部参考长度、最终回答长度、工具错误、写文件计划、追问建议覆盖率。
- final prompt / merged prompt：prompt 长度、assistant 轮数、是否含 reasoning、是否含工具 schema、最终动作推断、合并来源分布。

大文件调试时可限制读取条数：

```bash
python -m agent.data_stats --max-rows 1000
```

## 数据去重

可以使用内置去重模块对合成好的最终训练数据做相似去重，默认输入就是 `merge_final_sft.py` 之后的最终格式：

```text
agent/outputs/health/health_train_merged_final_prompt.jsonl
```

算法参考根目录 `MinHash.py`，默认使用字符 n-gram 精确 Jaccard，无额外依赖；如果安装了 `datasketch`，也可以切换到 MinHash LSH。

当前环境里默认已经切回 **MinHash LSH**，不会再用全量 exact 候选比较。只有你显式传 `--method exact` 时才会使用慢速精确模式。

先 dry-run 查看重复率：

```bash
python -m agent.deduplicate --dry-run
```

正式写出：

```bash
python -m agent.deduplicate
```

默认输出：

```text
agent/outputs/health/health_train_dedup_final_prompt.jsonl
agent/outputs/health/health_train_duplicates.jsonl
```

覆盖原最终文件时会自动备份：

```bash
python -m agent.deduplicate --in-place
```

对最终 prompt 格式，默认 `--dedup-key auto`：

- executor answer 样本按“最后一轮 user + 最终 assistant 回答”去重。
- planner final 样本按“最后 assistant 规划 JSON”去重。
- 会去掉 `<think>...</think>` 后再比较最终回答，避免思考过程差异掩盖重复。

也可以手动指定：

```bash
python -m agent.deduplicate --dedup-key qa
python -m agent.deduplicate --dedup-key assistant
python -m agent.deduplicate --dedup-key user
python -m agent.deduplicate --dedup-key prompt
```

## 一键启动全流程

如果你想让脚本自动串起“数据合成 -> 清洗 -> 格式转化 -> 合并 -> 去重 -> 统计”这一整条链路，可以直接启动 launcher：

```bash
python -m agent.pipeline_launcher --domain health_talent --force-rebuild
```

它会依次执行：

- planner trajectory synthesis
- empty tool-call intent clean
- planner step-wise conversion
- planner final-prompt rendering
- executor answer synthesis
- planner/executor merge
- final prompt deduplication
- data statistics report

默认会把所有中间文件和最终结果写入 `agent/outputs/<domain>/`，统计报告默认输出为 `<prefix>_data_stats.md`。

其他常用参数：

- `--text-field user_query`：强制按某个字段去重；不填则按数据格式自动抽取文本。
- `--keep longest|first|last`：重复簇保留最长、最早或最后一条，默认 `longest`。
- `--method auto|exact|minhash`：默认 `auto`，优先 MinHash LSH；`exact` 仅用于小文件或调试。
- `--max-rows 1000`：只读前 N 条用于快速测试。

重复簇抽检日志会记录保留项和被移除项，方便人工抽查。

如果只想训练解析后的 JSON 动作，不训练思维过程，可执行：

```bash
python -m agent.convert_to_sft --no-reasoning
```

输出：

```text
agent/outputs/health/health_train_planner_step_sft.jsonl
```

## 执行阶段最终回答数据合成

执行阶段与规划阶段隔离实现，只读取规划阶段输出作为前置信息，不修改 planner 流程。输入默认来自：

```text
agent/outputs/health/health_train_planner_trajectories.jsonl
```

`executor_generator.py` 会从每条规划轨迹中提取：

- 用户问题 `user_query`
- 已识别意图 `intent`
- 工具返回内容 `observations[].content`
- 工具名、失败工具标记等 metadata

然后构造执行阶段上下文：

```text
【用户问题】
...

【识别意图】
...

【外部参考信息】
...
```

再调用 teacher 模型生成正式面向用户的回答。执行阶段不调用工具，不输出规划动作，不输出 JSON。

运行示例：

```bash
python -m agent.executor_generator \
  --max-samples 100 \
  --max-workers 10 \
  --max-external-chars 6000
```

如果只想保存最终回答、不保存 `<think>`：

```bash
python -m agent.executor_generator --no-reasoning
```

如果工具失败 observation 不希望进入外部参考信息：

```bash
python -m agent.executor_generator --exclude-tool-errors
```

输出：

```text
agent/outputs/health/health_train_executor_answer_sft.jsonl
```

每条数据包含：

```json
{
  "id": "xxx_executor",
  "source_id": "xxx",
  "user_query": "...",
  "intent": "食疗咨询",
  "external_info": "...",
  "messages": [
    {"role": "system", "content": "...执行阶段 system prompt..."},
    {"role": "user", "content": "...用户问题+意图+外部信息..."},
    {"role": "assistant", "content": "<think>...</think>\n正式回答"}
  ]
}
```

SFT 输出中的 `messages` 可直接传给 tokenizer 的 `apply_chat_template`。工具调用消息会保留思维链内容，结构如下：

```json
{
  "role": "assistant",
  "content": "<think>...\n</think>\n{\"intent\": \"...\", \"action\": \"tool_call\"}",
  "tool_calls": [
    {
      "type": "function",
      "function": {
        "name": "search_food_web",
        "arguments": {"query": "..."}
      }
    }
  ]
}
```

工具返回消息采用：

```json
{"role": "tool", "content": "..."}
```

生成和转换过程中会自动清洗 reasoning 里的提示词元叙述，例如“我们被要求”“第一轮”“根据要求”等，不会进入训练集。

## 轨迹格式

每条数据包含：

```json
{
  "id": "seed_0001",
  "user_query": "...",
  "expected_intent": "食疗咨询，仅作离线评估，不输入模型",
  "tool_count": 5,
  "trajectory": [
    {
      "step": 1,
      "planner_raw_output": {
        "reasoning_content": "...",
        "content": "{...}",
        "text": "<think>\n...\n</think>\n{...}"
      },
      "planner_output": {
        "intent": "食疗咨询",
        "action": "tool_call",
        "tool_calls": [
          {
            "id": "call_1",
            "name": "search_food_web",
            "arguments": {"query": "..."}
          }
        ]
      },
      "observation": {"tool_call_id": "call_1", "name": "search_food_web", "content": "..."}
    },
    {
      "step": 2,
      "planner_output": {
        "intent": "食疗咨询",
        "action": "planning_finish",
        "finish_reason": "已有足够工具信息。"
      },
      "observation": null
    }
  ]
}
```
