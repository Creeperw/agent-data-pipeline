# Agent 数据流水线控制台

本项目为 `agent/` 下的蒸馏数据构建脚本提供一个本地 Web 控制台，用于替代逐条敲命令行。
控制台是**纯编排层**：它不修改、不重写任何既有脚本，每一次执行都是独立的
`python -m agent.<module>` 子进程，参数拼装方式与 `pipeline_launcher.py` 完全一致。

因此命令行与控制台两条路径不会产生行为分叉——控制台跑出来的产物，与手工敲同一条命令得到的产物完全相同。

## 启动

```bash
# 在仓库根目录（即 agent/ 的上一级）执行
python -m agent.ui
# → http://127.0.0.1:8770
```

用 `./install.sh` 装过之后，`.venv/bin/agent-pipeline` 是同一个入口，激活虚拟环境后
可以直接敲 `agent-pipeline`。

可用参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--host` | `127.0.0.1` | 监听地址。改成 `0.0.0.0` 时同网段机器可访问，启动时会打印提示。 |
| `--port` | `8770` | 监听端口。端口被占用时换一个即可。 |
| `--reload` | 关闭 | 开发模式，改动 `agent/ui/` 下的 Python 文件后自动重启（前端静态文件不需要重启）。 |
| `--log-level` | `info` | uvicorn 日志级别。 |

> **注意解释器选择。** 控制台自身只需要 fastapi / uvicorn，不 import 任何重依赖，
> 所以启动是秒级的。但阶段执行时会由子进程加载 openai、transformers、datasketch
> 等库，因此启动控制台用的解释器必须能 import 到它们，否则阶段会失败。

### 在 Windows 侧浏览器访问

服务在 WSL 中监听 `127.0.0.1`，Windows 浏览器不能直接访问，三种方式任选：

1. **VS Code 端口转发（推荐）**：VS Code 检测到监听端口后会自动转发，在「端口」
   面板点开 `8770` 即可，链接形如 `http://localhost:8770`。
2. **手动转发**：在 Windows PowerShell 执行
   `ssh -N -L 8770:127.0.0.1:8770 <WSL 用户>@<WSL IP>`。
3. **直接监听**：`python -m agent.ui --host 0.0.0.0`，然后用 `localhost:8770` 访问。
   注意该端口**没有任何鉴权**，只适合本机或可信内网。

## 界面

顶栏固定显示当前领域、输出前缀和两个环境徽章：

- **API** — `DEEPSEEK_API_KEY` 是否已从 `agent/.env` 读到。合成类阶段（planner
  轨迹、executor 回答、多轮会话、身份样本等）依赖它。
- **Tokenizer** — 本地 tokenizer 路径（默认 `MODELS/Qwen3.5-0.8B-Base`）是否存在。
  prompt 渲染类阶段（planner final prompt）依赖它。

> `MODELS/` 不随仓库发布：tokenizer 得自己准备。要么把本地模型放到仓库根的
> `MODELS/<名字>/`，要么在顶栏齿轮里把路径改指到你已有的 tokenizer。没配也不会拦
> 住整个流程——只有第 4、6 阶段需要它，其余阶段照常跑，这两个阶段会标红提示。

**输出前缀**决定产物文件名，默认 `valid`，也就是 `valid_planner_trajectories.jsonl`
这样的命名。想跑 smoke 试验就把前缀改成 `smoke`，两套产物互不覆盖。

页面分六个标签页：

| 标签页 | 作用 |
| --- | --- |
| **运行** | 勾选阶段、填参数、执行、看实时日志。 |
| **产物** | 列出当前领域/前缀下所有产物：存在与否、大小、行数、修改时间，可点「预览」。 |
| **seed 数据** | 在线编辑 `agent/domains/<领域>/seeds.jsonl`：增删改行、逐行 JSON 校验。 |
| **统计** | 渲染 `data_stats` 产出的 JSON，按文件分组展示分布与比例。 |
| **报告** | 渲染 `data_stats` 产出的 markdown 报告全文。 |
| **历史** | 本次服务启动以来提交过的所有作业及其状态、耗时、日志。 |

### 运行视图

左侧是阶段列表，每张卡片显示：

- 序号与标题、预估开销（`API` / `Tokenizer` / `耗时`）、是否就地修改输入；
- 一句话说明该阶段做什么；
- **输入与输出的实际状态**（存在 / 缺失，行数、体积），缺失的输入标红；
- 可折叠的参数表单，按「常用 / 高级」分组。

右侧是执行面板：进度 chips 显示每个阶段的运行状态与耗时，下方是日志区，
通过 SSE 实时推送 stdout/stderr，包含 tqdm 进度条（进度条会在原地刷新，不刷屏）。

三个快捷按钮：**主链路**（勾选 8 个主阶段）、**全选**、**清空**。

### 参数约定

**所有参数留空 = 使用脚本自身的默认值**，控制台不会为留空的参数补一个默认值再传下去。
只有真正填了值的参数才会出现在命令行里。所以「不填」和「填脚本默认值」在语义上一致，
但你看到的命令就是脚本实际收到的命令。

输入框里的灰色提示就是这个「留空时会用的值」，由后端按 `.env` 和进程环境解析后给出，
所以各阶段显示的可能不同（同一个「样本上限」在不同阶段可能是不限、也可能是 15000）。
使用者因此不必去翻脚本源码确认留空的后果。

参数类型：

| 类型 | 控件 | 说明 |
| --- | --- | --- |
| `int` / `float` | 数字输入 | 留空即不传；填非数字会被拒绝并提示。 |
| `str` | 文本框 | 留空即不传。 |
| `bool` | 复选框 | 勾选才追加对应的开关参数。 |
| `choice` | 下拉框 | 空选项表示「用脚本默认值」。 |
| `path` | 文本框 + 提示 | 绝对路径，留空即不传。 |
| `text` | 多行文本 | 用于 JSON 片段等长文本。 |

## 阶段一览

主链路（`主链路` 按钮会勾选前 8 个）：

| # | 阶段 | 模块 | 输入 → 输出 | 依赖 |
| --- | --- | --- | --- | --- |
| 1 | planner 轨迹合成 | `agent.generator` | seed → planner 轨迹 | API |
| 2 | 修复空 tool-call intent | `agent.fix_empty_tool_call_intents` | planner 轨迹 → 同文件（先备份） | — |
| 3 | planner step-wise 转换 | `agent.convert_to_sft` | planner 轨迹 → planner step-wise SFT | — |
| 4 | planner final prompt 渲染 | `agent.convert_to_final` | planner step-wise SFT → planner final prompt | Tokenizer |
| 5 | executor 回答合成 | `agent.executor_generator` | planner 轨迹 → executor 回答 SFT | API |
| 6 | planner/executor 合并 | `agent.merge_final_sft` | planner final prompt + executor 回答 → 合并后的 final prompt | — |
| 7 | final prompt 去重 | `agent.deduplicate` | 合并后的 final prompt → 去重后的 final prompt + 重复簇日志 | — |
| 8 | 数据统计报告 | `agent.data_stats` | 上述全部产物 → 统计 markdown + JSON | — |

旁支阶段（按需单独勾选）：

| 阶段 | 模块 | 说明 |
| --- | --- | --- |
| RAG 摘要生成 | `agent.rag_summary_data` | 把原始语料压成 RAG 摘要，供 planner 工具检索使用。 |
| 多轮会话合成 | `agent.multiturn_cli` | 在单轮流程之上再包一层会话编排，产出多轮 sessions / planner / executor 三份数据。 |
| 身份样本注入 | `agent.inject_identity` | 生成身份类样本，无需输入文件。 |
| 无意图 executor SFT | `agent.build_executor_no_intent_sft` | 从 executor 回答中筛出无意图样本。 |
| 轨迹意图降采样 | `agent.downsample_intent_data` | 按意图比例降采样 planner 轨迹，同时输出被剔除的样本。 |
| executor 意图降采样 | `agent.downsample_executor_data` | 同上，作用于 executor 回答。 |

### 前置检查

提交作业前，控制台会逐个阶段检查输入文件是否存在，缺文件直接返回 409 并列出缺什么，
避免跑到一半才失败。

第 8 步「数据统计报告」是**非严格**阶段：它的输入是前面所有产物，允许部分缺失——
已经生成的文件会被统计，还没生成的自动跳过。阶段卡片上会额外提示这一点。

### 并发与取消

- 同一时刻**只允许一个作业在运行**。重复提交会返回 409；确认后可以强制取消旧作业再提交。
- 「取消」会杀掉整个进程组（`SIGTERM`，5 秒后升级为 `SIGKILL`），不会留下游离的
  Python 子进程。
- 作业在服务重启后不保留（历史列表是内存态），但产物文件在磁盘上，刷新即可看到。

## 产物与 seed 编辑

**产物页**按 `agent/ui/registry.py` 里声明的 22 个产物条目逐一检查状态。预览支持
JSONL（分页读取，默认 20 行、最多 200 行）、JSON（最多 40 万字符）、markdown
（最多 20 万字符）、目录（列前 200 个文件名），超出部分会标出截断。

**seed 编辑**直接读写 `agent/domains/<领域>/seeds.jsonl`：

- 保存前会写一份 `.bak_<时间戳>` 备份；
- 先写 `.jsonl.tmp` 再 `os.replace`，避免写到一半崩掉留下半个文件；
- 逐行做 JSON 校验，非法行整批拒绝，不会写坏文件；
- 超过 3000 行的文件拒绝通过界面整体覆盖，请改用脚本或手工编辑。

## 与 `pipeline_launcher.py` 的关系

| | `pipeline_launcher.py` | 控制台 |
| --- | --- | --- |
| 形态 | 一次性命令行 | 常驻 Web 服务 |
| 执行方式 | 顺序调用子进程 | 顺序调用子进程（相同） |
| 产物命名 | `--prefix` | 顶栏「输出前缀」（相同） |
| 领域选择 | `--domain` | 顶栏「领域」（相同） |
| 参数 | 命令行开关 | 表单（留空即不传，等价） |
| 日志 | 终端 | SSE 实时推送 + 历史留存 |
| 中途干预 | 无 | 可取消、可只跑单个阶段、可断点续跑 |

两者可以混用：控制台跑了一半的产物，命令行接着跑也不会冲突——因为它们读写的是同一批文件。

## HTTP 接口

服务端不依赖任何前端框架，接口都是普通 JSON。需要脚本化调用时可以直接用：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/meta` | 版本、解释器、领域列表、默认领域、API/Tokenizer 状态。 |
| GET | `/api/stages` | 阶段定义 + 参数 + 输入输出状态（需 `domain`、`prefix`）。 |
| GET | `/api/artifacts` | 22 个产物的状态（需 `domain`、`prefix`）。 |
| GET | `/api/artifacts/preview` | 预览产物内容（`key`、`domain`、`prefix`）。 |
| POST | `/api/jobs` | 创建作业。`{"domain":…, "prefix":…, "stages":[…], "params":{…}, "force":false}` |
| GET | `/api/jobs` | 作业列表（不含日志行）。 |
| GET | `/api/jobs/{id}` | 单个作业详情（含日志行）。 |
| POST | `/api/jobs/{id}/cancel` | 取消作业，杀掉整个进程组。 |
| GET | `/api/jobs/{id}/stream?since=N` | SSE 日志流，断线后带 `since` 重连可补齐。 |
| GET | `/api/seeds` | 读取 seed 文件。 |
| POST | `/api/seeds` | 覆盖写入 seed 文件（先备份）。 |
| GET | `/api/stats` | 读取 `data_stats` 的 JSON 报告。 |

`domain` 与 `prefix` 都会经过路径安全校验，含 `/`、`..` 等字符直接返回 400。

## 文件结构

```text
agent/ui/
├── __init__.py      # 版本号
├── __main__.py      # python -m agent.ui 入口，argparse
├── registry.py      # 阶段/产物声明、参数定义、命令行拼装、路径解析
├── jobs.py          # 作业调度、子进程管理、日志缓冲、取消
├── server.py        # FastAPI 路由、静态文件、前置检查
├── _check_params.py # 开发工具：核对 UI 传的参数是否被脚本接受
└── static/
    ├── index.html   # 单页结构
    ├── style.css    # 深色主题
    └── app.js       # 全部前端逻辑，零外部依赖
```

`registry.py` 是唯一的「真相来源」：阶段、参数、产物、前置关系都声明在那里，
前端表单和前置检查都从它自动派生。新增一个阶段或调整参数，只需要改 `registry.py`。

改完 `registry.py` 后建议跑一次参数核对，确认新参数名确实被对应脚本的 argparse 接受：

```bash
python agent/ui/_check_params.py
```

它会为每个阶段生成一条「所有参数都填满」的命令，用 ast 解析脚本的
`add_argument` 逐项比对，并处理「复用其他模块的 `parse_args`」和
「根本没有 argparse」两种例外。

## 已知限制

- 作业历史是内存态，服务重启后清空（产物文件不受影响）。
- 不支持并行作业，一次只能跑一个。
- 前端不引入任何 CDN 依赖，图表是手写的 CSS 条形图，不做复杂可视化。
- 控制台只做编排和展示，不解析产物的业务语义——质检仍以 `data_stats` 报告为准。
