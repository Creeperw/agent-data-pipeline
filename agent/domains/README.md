# Agent Domain Packs

`agent/domains/` 用于把 agent 数据合成系统从健康管理领域解耦成可插拔领域包。

核心原则：

- `agent/core/` 保持领域无关，不写死健康管理、意图、模板或工具语义。
- 每个领域提供一个 `agent/domains/<domain>/spec.py`，并导出 `DOMAIN: DomainSpec`。
- Planner 的动作协议在所有领域保持一致：`tool_call` / `planning_finish` / `ask_clarification`。
- `intent` 和 `answer_template` 都是可选能力，由 `DomainSpec.has_intent` 和 `DomainSpec.has_answer_template` 控制。
- 通用工具（计算、时间、随机数等）放在全局库 `agent/tools/`，领域用 `tools.json` 按需开放，避免同一份实现散落在多个领域包里。

## 已有领域

| 领域                 | intent | answer_template | 说明                                                                                             |
| -------------------- | ------ | --------------- | ------------------------------------------------------------------------------------------------ |
| `health`           | 是     | 是              | 当前司宁健康管理领域，保持旧训练分布。                                                           |
| `customer_service` | 否     | 否              | 通用客服 demo，用于验证无意图、无模板领域。                                                      |
| `health_talent`    | 是     | 是              | 健康管理个性化人才培养领域，覆盖健康管理指导、学情分析、技能培训、实训任务设计、评价反馈等能力。 |

## `health_talent` 领域说明

`health_talent` 面向健康管理、护理、公共卫生、康养服务、运动健康、营养指导等相关专业的人才培养数据合成。它不是单纯健康咨询领域，而是把健康管理知识与教学培养任务结合起来，用于生成：

- 学生学习诊断与薄弱点分析；
- 个性化培养路径；
- 健康管理技能训练任务；
- 教师实训任务设计；
- 案例作业评价反馈；
- 学习资源推荐；
- 健康管理知识辅导。

该领域保留 `intent` 和 `answer_template`，适合合成“planner 先识别培养场景，再决定是否调用工具，executor 最终给出教学/学习建议”的两阶段训练数据。

标准 intent：

| intent             | 说明                                                                             |
| ------------------ | -------------------------------------------------------------------------------- |
| `健康管理指导`   | 健康管理理论、慢病管理、生活方式干预、营养运动情志管理、健康风险评估等知识辅导。 |
| `学情分析`       | 根据成绩、错题、实训表现、学习记录等分析学习基础、优势、薄弱点和学习风险。       |
| `个性化培养路径` | 根据学生基础、目标岗位和能力短板设计阶段性学习路径、课程安排和提升计划。         |
| `技能培训`       | 围绕健康评估、健康档案、膳食调查、运动处方、健康宣教、沟通访谈等技能设计训练。   |
| `实训任务设计`   | 为教师或培训者设计案例实训、情境模拟、项目任务、工作页和评价量规。               |
| `教学方案设计`   | 围绕健康管理课程、专题课或实训课设计教学目标、教学流程、课堂活动、作业与评价。   |
| `教学方案设计`   | 围绕健康管理课程、专题课或实训课设计教学目标、教学流程、课堂活动、作业与评价。   |
| `评价反馈`       | 根据学生作业、实训记录、反思日志或案例回答进行能力评价和改进反馈。               |
| `学习资源推荐`   | 根据目标和薄弱点推荐教材章节、课程资源、案例、练习题和拓展阅读。                 |
| `其他`           | 信息不足、与人才培养弱相关或无法归入上述类别的问题。                             |

领域工具：

| 工具                                   | 用途                                                           |
| -------------------------------------- | -------------------------------------------------------------- |
| `search_health_management_knowledge` | 检索健康管理、慢病管理、生活方式干预、营养运动情志调节等知识。 |
| `search_curriculum_standard`         | 检索课程标准、人才培养方案、岗位能力要求和教学目标。           |
| `analyze_learning_profile`           | 根据学生成绩、错题、实训记录和学习表现分析学情。               |
| `recommend_training_tasks`           | 根据能力短板推荐技能训练任务、实训项目或练习活动。             |
| `generate_assessment_rubric`         | 生成实训任务、案例分析或技能操作的评价量规。                   |
| `search_learning_resources`          | 检索教材章节、课程资源、案例材料、练习题或拓展阅读。           |

推荐 seed 字段：

```json
{
  "id": "health_talent_001",
  "user_query": "...",
  "expected_intent": "技能培训",
  "scenario": "skill_training",
  "student_profile": "学生学习画像或学习表现",
  "target_competency": "目标能力",
  "required_tools": ["recommend_training_tasks", "generate_assessment_rubric"],
  "metadata": {
    "course": "健康风险评估",
    "role": "student"
  }
}
```

其中 `student_profile`、`target_competency`、`scenario` 和 `metadata` 会从 planner 轨迹透传到 executor 输出，方便后续筛选、统计和评测。

### 多轮对话历史格式

多轮对话合成时，历史对话只保留自然语言内容，不保留 planner 的 JSON、tool call 细节或 `<think>` 思维链。

建议格式如下：

```text
【外部参考信息】
...

【压缩历史对话】
...  # 较早的轮次，过长时再压缩

【近期历史对话】
用户：...
助手：...
```

如果历史较短，可以只保留 `【近期历史对话】`；如果没有早期历史，则可以省略 `【压缩历史对话】`。planner 阶段仍只使用规划阶段 system prompt，executor 阶段仍只使用执行阶段 system prompt。

如果在会话开始阶段已经抽取了固定工具集合，那么后续轮次都复用同一份 `fixed_tool_names`；工具失败计划也建议在会话开始时一次性确定，后续轮次不再重抽，这样可以保证多轮对话的行为稳定且可复现。

如果对话历史过长，需要先交给压缩模型处理，压缩模型的输入应只包含自然语言轮次和下一轮用户草稿，不要带 planner JSON、tool call 或 `<think>`。压缩输出只负责生成 `【压缩历史对话】` 文本，不负责生成下一轮用户问题；下一轮用户问题应由会话编排器单独生成。

`health_talent` 多轮 smoke 可直接使用领域 seeds：

```bash
python -m agent.multiturn_cli \
  --domain health_talent \
  --input agent/domains/health_talent/seeds.jsonl \
  --max-samples 2
```

如果需要控制“多久压缩一次历史”，可以用 `AGENT_MULTI_TURN_COMPRESS_EVERY_TURNS`，例如设置为 `2` 表示每隔 2 轮检查一次是否需要压缩历史。

### 在 `health_talent` 里新增回答模板

如果你要给 `health_talent` 新增一个“回答模板”，通常有两步：

1. 先在 `agent/domains/health_talent/spec.py` 的 `ANSWER_TEMPLATES` 里加一个新键，例如：

```python
ANSWER_TEMPLATES = {
    "教学方案设计": "请先明确教学目标、学生基础、课时长度和教学重点，再给出教学流程、导入、讲授、练习、互动、评价和课后延伸建议。",
}
```

1. 再确认这个 intent 出现在 `INTENT_TAXONOMY` 和 `tool_selection.intent_required_tools` 里，这样 planner 和 executor 才知道这是一个合法场景。

完整示例已经写进 `health_talent`：

- `INTENT_TAXONOMY` 新增了 `教学方案设计`
- `ANSWER_TEMPLATES` 新增了 `教学方案设计`
- `tool_selection.intent_required_tools` 新增了 `教学方案设计`
- `seeds.jsonl` 新增了一个 `health_talent_004` 样本

这样一来，执行阶段就会自动把对应模板传进用户提示里，形成 `【回答建议】` 段。

## 最小领域包结构

```text
agent/domains/<domain>/
  __init__.py
  spec.py
  seeds.jsonl        # 可选：该领域的 smoke/demo seed
```

`spec.py` 示例：

```python
from agent.core.domain import DomainSpec

DOMAIN = DomainSpec(
    name="customer_service",
    display_name="通用客服助手",
    has_intent=False,
    has_answer_template=False,
    has_clarification=True,
    planner_system_prompt="...",
    executor_system_prompt="...",
    executor_assembly_system_prompt=None,
    tools=[...],
    tool_runner=None,  # 可选：领域自定义工具运行函数
    tool_selection={...},  # 可选：领域工具选择策略
)
```

### 数据集声明（`dataset_splits`）

领域包同时负责声明这个项目产出哪几种数据集。产物文件名是
`<领域名>_<数据集>_<阶段>.jsonl`，所以这一项决定了控制台顶栏前缀的默认值——默认是
`train` 和 `valid` 两档，对应界面上的「训练数据」「测试数据」：

```python
dataset_splits={
    "train": "训练数据",
    "valid": "测试数据",
}
```

键是数据集（会进文件名），值是界面上显示的名字（纯展示）。需要额外的一档（例如
只跑几条样本的 `smoke`）就在自己的 `spec.py` 里覆盖这一项：

```python
dataset_splits={
    "train": "训练数据",
    "valid": "测试数据",
    "smoke": "冒烟试验",
}
```

界面上的快捷按钮和 `config.output_prefix_for()` 都读这一项，所以改完 `spec.py`
顶栏立刻生效，不用重启控制台。手填的前缀不受限制——不在声明里的名字（例如直接敲
`tmp`）照样能用，只是没有快捷按钮、切换领域时也不会被换算。

## Prompt / Tool 标准化

每个领域都必须通过自己的 `DomainSpec` 声明 prompt 和 tools，不再让通用流程硬编码某个业务领域：

- `planner_system_prompt`：规划阶段合成和 planner SFT 拼装使用的 system prompt。
- `executor_system_prompt`：执行阶段 teacher 合成最终回答时使用的 system prompt。
- `executor_assembly_system_prompt`：执行阶段最终训练 prompt 拼装时的可选独立 system prompt。
- `tools`：该领域暴露给 planner 的工具 schema。
- `tool_runner`：该领域工具运行时。

`executor_assembly_system_prompt` 的规则：

- 如果设置为字符串，`merge_final_sft.py` 会在拼装 executor 训练 prompt 时替换 `messages[0]` 的 system prompt。
- 如果设置为 `None`，拼装阶段不替换 system prompt，直接使用 `executor_generator.py` 合成数据里保存的 `messages[0]`。

默认健康管理领域也已经标准化到 `agent/domains/health/`：

```text
agent/domains/health/
  prompts.py           # planner prompt + search tool schema
  executor_prompts.py  # executor 合成 prompt + user prompt builder
  templates.py         # intent taxonomy + answer templates
  tools.py             # health tool runtime adapter（搜索与写文件）
  tools.json           # 从全局工具库开放的工具名
  spec.py              # DOMAIN 汇总入口
```

## 全局工具库

通用工具只需实现一次，放在 `agent/tools/<name>.py`（`name` 同时是函数名和文件名）：

| 导出项 | 必填 | 说明 |
| --- | --- | --- |
| `SPEC` | 是 | OpenAI function-calling schema，必须是字面量 `dict`，`function.name` 要与文件名一致 |
| `MOCK_RESPONSE` | 否 | 没有 `run` 时的模拟返回文本，`{参数名}` 会被调用参数替换 |
| `run(arguments, sample=None)` | 否 | 真实实现，返回字符串；缺省则返回模拟文本 |

写新工具时复制 `agent/tools/_template.py`（开头的下划线让它不被加载器识别为工具）。
它把三条硬约束、两种失败语义（软失败返回文本 / 硬失败抛 `FatalToolError`）和凭据写法
都写在注释里。

**凭据一律走环境变量。** 工具代码里只出现变量名（`<服务名>_API_KEY`），值写进
`agent/.env`，控制台「工具」页最上面的「工具凭据」面板可以增删改任意键（不用改代码，
也不用先登记到哪张表里）。不要把 key 写成模块常量：那会跟着
仓库一起提交出去，而且换 key 得改代码、重启服务。工具在 `run()` 里
`os.getenv()` 即可——每个流水线入口都会导入 `agent/config.py`，它在导入时就执行了
`load_dotenv()`。

领域只记录开放了哪些工具，不复制实现：

```json
{"enabled": ["calculate_bmi", "get_current_datetime"]}
```

- 合并点是 `agent/core/domain.py` 的 `load_domain()`，调用 `merge_global_tools()` 把清单里的工具追加到 `DomainSpec.tools`。
- 领域自带的同名工具优先，全局工具会被跳过，因此库里的改动不会静默改变已有领域的行为。
- 工具库只依赖标准库，且 `SPEC` 由 `ast` 静态读取，`load_domain()` 不会执行工具代码。
- 新增领域时把 `tools.json` 一并复制即可继承同一份开放清单（模板复制会带上它）。

工具写完怎么接进流水线、以及领域自有工具与全局工具怎么选，见 `agent/README.md` 的「如何扩展」。

## Tool Selection

`tool_selection` 用于控制 planner 合成时“应该暴露哪些工具”。通用 core 会优先读取领域配置，不再在 `generator.py` 中写死健康管理规则。

常用字段：

```python
tool_selection={
  # 根据样本 intent / expected_intent 保证相关工具被暴露。
  "intent_required_tools": {
    "食疗咨询": ["search_food_web"],
  },

  # 根据 scenario 字符串保证相关工具被暴露。
  "scenario_required_tools": {
    "order_status": ["lookup_order_status"],
    "refund_policy": ["search_refund_policy"],
  },

  # 没有命中 intent/scenario/样本 required_tools 时的默认工具。
  "default_required_tools": ["search_web"],

  # irrelevant_tools 模式下默认排除的泛搜索工具。
  "generic_search_tools": ["search_rag", "search_web"],

  # 只有被 required 时才允许暴露的工具。
  "plan_only_tools": ["plan_write_file"],
}
```

样本本身也可以直接指定：

```json
{
  "user_query": "...",
  "required_tools": ["lookup_order_status"]
}
```

可识别字段包括：`required_tools` / `reference_tool_names` / `expected_tool_names`。

## Tool Runtime

每个领域建议显式设置 `tool_runner`。例如 `customer_service` 的订单/物流/售后查询工具，需要在 `DomainSpec` 中提供：

```python
def run_domain_tool(name: str, arguments: dict, sample: dict | None = None):
  ...

DOMAIN = DomainSpec(
  ...,
  tools=TOOLS,
  tool_runner=run_domain_tool,
)
```

`tool_runner` 返回对象只需要具备两个属性：

```python
result.name
result.content
```

`customer_service` 已内置一个 mock runner，用于返回订单状态、物流状态和售后政策文本，方便无外部 API 的 smoke test。

`health_talent` 也内置 mock runner，用于返回课程标准、学习资源、学情诊断、训练任务和评价量规等文本，便于先验证数据链路，后续可替换成真实课程知识库或教学资源检索服务。

## CLI 使用

Planner 合成：

```bash
python agent/generator.py --domain health
python agent/generator.py --domain customer_service
python agent/generator.py --domain health_talent
```

Executor 合成：

```bash
python agent/executor_generator.py --domain health
python agent/executor_generator.py --domain customer_service
python agent/executor_generator.py --domain health_talent
```

也可以用环境变量指定默认领域：

```bash
export AGENT_DOMAIN=customer_service
```

## Smoke Test

`customer_service` 提供最小 demo seeds：

```text
agent/domains/customer_service/seeds.jsonl
```

不调用模型、只验证领域加载和 CLI 流程：

```bash
python agent/generator.py \
  --domain customer_service \
  --input agent/domains/customer_service/seeds.jsonl \
  --output /tmp/customer_service_planner_smoke.jsonl \
  --max-samples 0 \
  --force-rebuild

python agent/executor_generator.py \
  --domain customer_service \
  --input /tmp/customer_service_planner_smoke.jsonl \
  --output /tmp/customer_service_executor_smoke.jsonl \
  --max-samples 0 \
  --force-rebuild
```

真实调用 teacher model 时，把 `--max-samples 0` 改成 `--max-samples 1` 或更大，并确保 API key / base url 已配置。

### `health_talent` smoke 合成

`health_talent` 提供健康管理个性化人才培养 demo seeds：

```text
agent/domains/health_talent/seeds.jsonl
```

Planner 轨迹合成：

```bash
python agent/generator.py \
  --domain health_talent \
  --input agent/domains/health_talent/seeds.jsonl \
  --output agent/outputs/health_talent/smoke_planner_trajectories.jsonl \
  --max-samples 2 \
  --max-workers 2 \
  --force-rebuild
```

Executor 最终回答合成：

```bash
python agent/executor_generator.py \
  --domain health_talent \
  --input agent/outputs/health_talent/smoke_planner_trajectories.jsonl \
  --output agent/outputs/health_talent/smoke_executor_answer_sft.jsonl \
  --max-samples 2 \
  --max-workers 2 \
  --force-rebuild
```

已验证输出文件：

```text
agent/outputs/health_talent/smoke_planner_trajectories.jsonl
agent/outputs/health_talent/smoke_executor_answer_sft.jsonl
```

当前 smoke 样本覆盖：

- `学情分析`：调用 `analyze_learning_profile`、`search_learning_resources`，再 `planning_finish`。
- `技能培训`：调用 `recommend_training_tasks`、`generate_assessment_rubric`，再 `planning_finish`。

如果要跑完当前 demo seeds，可把 `--max-samples 2` 改成 `--max-samples 3`；如果要批量合成正式训练数据，可替换 `--input` 为扩展后的 `health_talent` seed 文件，并适当提高 `--max-workers`。

输出会透传领域字段，便于后续评测、筛选和训练分析：

- `domain`
- `scenario`
- `student_profile`
- `target_competency`
- `metadata`

## 旧入口清理

根目录下的旧 health 兼容入口已移除，标准流程只从领域包读取资源：

- planner prompt / tool schema：`agent.domains.health.prompts`
- executor prompt：`agent.domains.health.executor_prompts`
- intent 模板：`agent.domains.health.templates`
- 工具运行时：`agent.domains.health.tools`

新增领域时不要再新增根目录 prompt/tool 模块，全部放入 `agent/domains/<domain>/` 并通过 `DomainSpec` 暴露。

## 后续迁移方向

1. 将更多领域的真实工具运行时做成 domain runtime。
2. 将 eval/rubric/reward 权重改成从 `domain.eval_config` 加载。
3. 将 seed 生成、数据清洗和评测集构造也统一加上 `--domain`。
