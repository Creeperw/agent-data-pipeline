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
| `--reload` | 关闭 | 开发模式，改动 `agent/ui/` 下的 Python 文件后自动重启（前端静态文件不需要重启）。注意：reload 模式下顶栏的重启按钮不可用，因为子进程记不到启动命令行。 |
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

顶栏固定显示当前领域、构建任务和两个环境徽章，右侧是三个控制台级动作：**⟳ 重启**、
**刷新**、**⚙ 运行时配置**。重启和刷新都是带文字的按钮，运行时配置是齿轮图标 ——
重启原来只有个环形箭头图标，跟旁边的「刷新」不成套，也看不出是重启还是刷新。

- **API** — `DEEPSEEK_API_KEY` 是否已从 `agent/.env` 读到。合成类阶段（planner
  轨迹、executor 回答、多轮会话、身份样本等）依赖它。
- **Tokenizer** — 本地 tokenizer 路径（默认 `MODELS/Qwen3.5-0.8B-Base`）是否存在。
  prompt 渲染类阶段（planner final prompt）依赖它。

> `MODELS/` 不随仓库发布：tokenizer 得自己准备。要么把本地模型放到仓库根的
> `MODELS/<名字>/`，要么在顶栏齿轮里把路径改指到你已有的 tokenizer。没配也不会拦
> 住整个流程——只有第 4、6 阶段需要它，其余阶段照常跑，这两个阶段会标红提示。

**构建任务**决定产物写到哪个目录。任务名默认由领域名和数据集拼成，即
`<领域名>_<数据集>`，例如 `health_talent_train_planner_trajectories.jsonl`。名字里
带项目名，是为了让同一台机器上多个项目的数据即使被收集到一起也认得出出处，训练集
和测试集也不会互相覆盖。

点输入框会展开候选列表，里面是本项目声明的数据集档位（「训练数据」= `<领域名>_train`、
「测试数据」= `<领域名>_valid`）加上磁盘上已有的其它任务；当前落在哪一档在列表里
标出来。档位即使还没跑过也会列出来并标注「尚未创建」——新项目、新档位在磁盘上还
没有目录，不列的话就没地方知道合法的档位名了。输入内容会同时匹配任务名和档位标签，
所以敲「训练」也能定位到 `<领域名>_train`。

任务名也可以手填（例如跑 `smoke` 试验），手填的值在切换领域时原样保留；只有当它
正好是上一个领域声明的某一档时，才会被换成新领域的同一档。

页面分八个标签页：

| 标签页 | 作用 |
| --- | --- |
| **运行** | 勾选阶段、填参数、执行、看实时日志。 |
| **产物** | 列出当前领域/前缀下所有产物：存在与否、大小、行数、修改时间，可点「预览」。 |
| **seed 数据** | 在线编辑 `agent/domains/<领域>/seeds.jsonl`：增删改行、逐行 JSON 校验。 |
| **工具** | 三块：**工具凭据**（编辑全局 `agent/.env`，可增删改任意键）、**全局工具库**（勾选向当前项目开放哪些工具、新建/编辑/删除工具源码）、**本项目独有工具**（只读，定位到 `spec.py` 的行）。全局工具库标题栏可直接点击「新建工具」，填写描述、参数和模拟返回。 |
| **角色** | 按当前项目隔离展示角色。每个项目都有 `planner`、`executor`、`reviewer` 内置角色，并支持复制为自定义角色；自定义角色保存到 `agent/domains/<项目>/roles/*.json`，填写角色类型后自动带入对应输入 / 输出协议。 |
| **统计** | 渲染 `data_stats` 产出的 JSON，按文件分组展示分布与比例。 |
| **报告** | 渲染 `data_stats` 产出的 markdown 报告全文。 |
| **历史** | 本次服务启动以来提交过的所有作业及其状态、耗时、日志。 |
| **领域包** | 当前领域的 `spec.py` / prompt / 工具清单，可跳进在线编辑器。 |

> 顶栏齿轮里的「运行时配置」只管白名单里的键（模型接入、本地模型、超时），因为那几个键
> 结构稳定、有类型和默认值，值得逐个登记。工具凭据不在这张表里：`search_web` 读
> `EXA_API_KEY`，换个搜索后端就是另一个键名，每个项目用的工具都不一样。所以工具凭据
> 放在「工具」页，按 `KEY=VALUE` 平铺整份 `agent/.env`，加新键不用改代码。

### 重启控制台

顶栏的「⟳ 重启」按钮会**原地重启控制台进程**。控制台是常驻进程，`agent/core/**`、
`agent/config.py`、`agent/ui/*.py` 都只在启动时导入一次，改完不重启就还是旧代码
（`agent/domains/<领域>.*` 除外，那个每次现加载）。以前只能回终端 Ctrl-C 再起一遍。

实现是 `os.execv` 换掉进程镜像，所以：

- **PID、终端、日志重定向、会话都不变** —— 用
  `nohup python -m agent.ui > /tmp/agent-ui.log &` 起的进程，重启后还写同一个日志文件；
- 监听套接字带 CLOEXEC，exec 时由内核关掉，新进程能立刻重新 bind 同一端口；
- 重启后 `ps` 里看到的是模块文件的绝对路径（`python …/agent/ui/__main__.py --port 8770`），
  不再是 `python -m agent.ui`：重放的是当初**真实用过的**那条命令行，照搬才在任何启动目录下
  都起得来（从别的目录用绝对路径启动时，拼 `-m` 会因为当前目录不在 `sys.path` 而失败）。
  按包名找进程的写法（`pkill -f "agent.ui"`）不受影响；
- 重启后**作业历史（内存态）会清空**，磁盘上的产物、配置、`.env` 都不受影响。

前端在重启期间显示一块遮罩，写明「第几次探测、已等待多久、新进程什么时候起来的」。
判断新进程是否就绪靠 `/api/meta` 的 `startedAt`（模块重新导入的时刻）—— execv 不换
PID，比 PID 没有意义。

两种情况会被拒绝，界面上直接说明原因（按钮置灰，原因挂在悬停提示上）：

- **有作业正在跑**：作业是这个进程的子进程，重启后它还会继续写产物，但日志再没人读、
  也取消不了，事后只能靠猜。先等它结束或取消。
- **进程没有记录启动命令行**：用 `uvicorn --reload` 或自定义脚本启动时是这样
  （`--reload` 是父进程另起子进程跑 `agent.ui.server:create_app`，子进程不经过
  `__main__`，记不到命令行）。`--reload` 下改动本来就会自动生效，不需要手动重启。

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

主链路（`主链路` 按钮会勾选前 10 个）：

| # | 阶段 | 模块 | 输入 → 输出 | 依赖 |
| --- | --- | --- | --- | --- |
| 1 | planner 轨迹合成 | `agent.generator` | seed → planner 轨迹 | API |
| 2 | 修复空 tool-call intent | `agent.fix_empty_tool_call_intents` | planner 轨迹 → 同文件（先备份） | — |
| 3 | planner step-wise 转换 | `agent.convert_to_sft` | planner 轨迹 → planner step-wise SFT | — |
| 4 | planner final prompt 渲染 | `agent.convert_to_final` | planner step-wise SFT → planner final prompt | Tokenizer |
| 5 | executor 回答合成 | `agent.executor_generator` | planner 轨迹 → executor 回答 SFT | API |
| 6 | reviewer 质量审核 | `agent.reviewer_generator` | executor 回答 → 审核结果（严格 JSON） | API |
| 7 | planner/executor 合并 | `agent.merge_final_sft` | planner final prompt + executor 回答 → 合并后的 final prompt | — |
| 8 | 审核筛选 final prompt | `agent.filter_reviewed_sft` | 合并后的 final prompt + 审核结果 → 审核通过的 final prompt + 拒绝样本 | — |
| 9 | final prompt 去重 | `agent.deduplicate` | 审核通过的 final prompt → 去重后的 final prompt + 重复簇日志 | — |
| 10 | 数据统计报告 | `agent.data_stats` | 上述全部产物 → 统计 markdown + JSON | — |

旁支阶段（按需单独勾选）：

| 阶段 | 模块 | 说明 |
| --- | --- | --- |
| 11. 多任务整合 | `agent.merge_tasks` | 把多个任务里的同一类产物合并成一份写进当前任务。 |
| RAG 摘要生成 | `agent.rag_summary_data` | 把原始语料压成 RAG 摘要，供 planner 工具检索使用。 |
| 多轮会话合成 | `agent.multiturn_cli` | 在单轮流程之上再包一层会话编排，产出多轮 sessions / planner / executor 三份数据。 |
| 身份样本注入 | `agent.inject_identity` | 生成身份类样本，无需输入文件。 |
| 无意图 executor SFT | `agent.build_executor_no_intent_sft` | 从 executor 回答中筛出无意图样本。 |
| 轨迹意图降采样 | `agent.downsample_intent_data` | 按意图比例降采样 planner 轨迹，同时输出被剔除的样本。 |
| executor 意图降采样 | `agent.downsample_executor_data` | 同上，作用于 executor 回答。 |

### 前置检查

提交作业前，控制台会逐个阶段检查输入文件是否存在，缺文件直接返回 409 并列出缺什么，
避免跑到一半才失败。

第 10 步「数据统计报告」是**非严格**阶段：它的输入是前面所有产物，允许部分缺失——
已经生成的文件会被统计，还没生成的自动跳过。阶段卡片上会额外提示这一点。

### 并发与取消

- 同一时刻**只允许一个作业在运行**。重复提交会返回 409；确认后可以强制取消旧作业再提交。
- 「取消」会杀掉整个进程组（`SIGTERM`，5 秒后升级为 `SIGKILL`），不会留下游离的
  Python 子进程。
- 作业在服务重启后不保留（历史列表是内存态），但产物文件在磁盘上，刷新即可看到。

## 产物与 seed 编辑

**产物页**按 `agent/ui/registry.py` 里声明的 25 个产物条目逐一检查状态。预览支持
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
| 产物命名 | `--prefix`（不传则 `<领域名>_train`） | 顶栏「构建任务」（同一套规则） |
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
| GET | `/api/artifacts` | 25 个产物的状态（需 `domain`、`prefix`）。 |
| GET | `/api/artifacts/preview` | 预览产物内容（`key`、`domain`、`prefix`）。 |
| POST | `/api/jobs` | 创建作业。`{"domain":…, "prefix":…, "stages":[…], "params":{…}, "force":false}` |
| GET | `/api/jobs` | 作业列表（不含日志行）。 |
| GET | `/api/jobs/{id}` | 单个作业详情（含日志行）。 |
| POST | `/api/jobs/{id}/cancel` | 取消作业，杀掉整个进程组。 |
| GET | `/api/jobs/{id}/stream?since=N` | SSE 日志流，断线后带 `since` 重连可补齐。 |
| GET | `/api/seeds` | 读取 seed 文件。 |
| POST | `/api/seeds` | 覆盖写入 seed 文件（先备份）。 |
| GET | `/api/config` | 运行时配置（白名单键）的当前值，密钥只回显掩码。 |
| PUT | `/api/config` | 写入运行时配置，只认白名单里的键。 |
| GET | `/api/env` | 全局 `agent/.env` 的全部键值，密钥只回显掩码。 |
| PUT | `/api/env` | 自由增删改环境文件：`{"values":{…},"removals":[…]}`，不查白名单。 |
| POST | `/api/restart` | 原地重启控制台进程。有作业在跑时返回 409。 |
| GET | `/api/stats` | 读取 `data_stats` 的 JSON 报告。 |

`domain` 与 `prefix` 都会经过路径安全校验，含 `/`、`..` 等字符直接返回 400。

## 文件结构

```text
agent/ui/
├── __init__.py      # 版本号
├── __main__.py      # python -m agent.ui 入口，argparse
├── registry.py      # 阶段/产物声明、参数定义、命令行拼装、路径解析
├── jobs.py          # 作业调度、子进程管理、日志缓冲、取消
├── restart.py       # 原地重启（os.execv 换进程镜像）
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

- 作业历史是内存态，重启控制台后清空（产物文件不受影响）。
- 不支持并行作业，一次只能跑一个。
- 前端不引入任何 CDN 依赖，图表是手写的 CSS 条形图，不做复杂可视化。
- 控制台只做编排和展示，不解析产物的业务语义——质检仍以 `data_stats` 报告为准。
