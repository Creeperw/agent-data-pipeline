# Agent 数据流水线

合成主模型第一阶段 SFT 训练数据的工具集，附带一个本地 Web 控制台。

## 背景

主模型需要在第一轮对话里独立完成三件事：判断用户意图、决定是否调用工具以及调用哪个、
把工具返回的结构化结果讲成自然语言。这三种能力都不来自通用预训练语料，需要定向合成
的样本。

本项目从一组领域 seed 出发，用教师模型批量合成规划轨迹与执行回答，切成 step-wise
训练样本，再经去重、降采样与身份注入，产出可直接用于 SFT 的 JSONL。

## 流水线

共 14 个阶段，可在控制台里自由勾选、组合执行。

### 主链路

| # | 阶段 | 作用 |
| --- | --- | --- |
| 1 | `planner_trajectories` | 合成 planner 轨迹：意图判定 + 工具调用序列 |
| 2 | `fix_intents` | 修复空 tool-call 的 intent |
| 3 | `planner_step_sft` | 切成 planner step-wise 样本 |
| 4 | `planner_final_prompt` | 渲染 planner final prompt |
| 5 | `executor_answer` | 合成 executor 对工具结果的回答 |
| 6 | `merged_final_prompt` | 合并 planner 与 executor 训练数据 |
| 7 | `dedup_final_prompt` | 去重（MinHash / 精确匹配） |
| 8 | `data_stats` | 生成统计报告 |

### 可选增强

| 阶段 | 作用 |
| --- | --- |
| `rag_summary` | 生成 RAG 摘要样本 |
| `multiturn` | 多轮会话合成 |
| `identity_sft` | 注入身份认知样本 |
| `no_intent_executor` | 无意图场景的 executor SFT |

### 收尾

| 阶段 | 作用 |
| --- | --- |
| `downsample_intent` | 按意图降采样轨迹 |
| `downsample_executor` | 按意图降采样 executor 数据 |

各阶段的参数、输入输出与前置依赖见 [`agent/ui/README.md`](agent/ui/README.md)；
数据格式与 prompt 约定见 [`agent/README.md`](agent/README.md)。

## 目录结构

```
agent/
├── README.md                流水线详解：数据格式、prompt 约定、各阶段用法
├── config.py                所有环境变量与默认值集中在此
├── generator.py             planner 轨迹合成
├── executor_generator.py    executor 回答合成
├── convert_to_sft.py        切成 step-wise 样本
├── convert_to_final.py      合并 planner 与 executor 数据
├── merge_final_sft.py       最终 prompt 渲染与合并
├── deduplicate.py           去重
├── data_stats.py            统计报告
├── downsample_*.py          按意图降采样
├── multiturn/               多轮会话合成
├── core/                    领域无关的会话、渲染与领域加载
├── domains/                 领域包：prompt、工具、seed、回答模板
├── tools/                   全局工具库
└── ui/                      本地 Web 控制台
```

仓库根的 `install.sh` 是一键安装脚本，`pyproject.toml` 声明包元数据与依赖
（版本号取自 `agent/ui/__init__.py`，依赖取自 `requirements.txt`，都不重复写第二遍）。

## 快速开始

### 一键安装（推荐）

```bash
git clone https://github.com/Creeperw/agent-data-pipeline.git
cd agent-data-pipeline
./install.sh
```

脚本会检查 Python 版本（需 3.10+）、在仓库里建 `.venv`、安装依赖，并复制出一份
`agent/.env`。已经存在的 `agent/.env` 不会被覆盖，里面的密钥保留原样。

装好后启动控制台：

```bash
.venv/bin/python -m agent.ui
# → http://127.0.0.1:8770
```

`source .venv/bin/activate` 之后也可以直接敲 `agent-pipeline`，效果相同。

### 手动安装

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置教师模型（三项必填）
cp agent/.env.example agent/.env
# 然后编辑 agent/.env，填入自己的 key、base_url 与模型名

# 3. 启动控制台（在仓库根目录执行）
python -m agent.ui
# → http://127.0.0.1:8770
```

### 为什么要留在仓库里运行

`pip install agent-data-pipeline` 那种装进 `site-packages` 的用法**不适用**于本项目。
控制台、seed 编辑器和新工具都要往包目录里写文件：

| 运行时写入 | 用途 |
| --- | --- |
| `agent/.env` | 顶栏齿轮里改配置 |
| `agent/tools/*.py` | 在界面上新建或编辑工具 |
| `agent/domains/<领域>/seeds.jsonl` | 在线编辑 seed |
| `agent/data/`、`agent/outputs/` | 中间产物与合成结果 |

包目录本身就是工作目录，必须可读可写、且看得见。装进 `site-packages` 会让这些文件
散落到库目录里，升级或重装时被覆盖，在只读环境下还会直接报权限错。所以请用
`pip install -e .`（可编辑安装，包仍指回仓库），或者干脆不用 pip，直接
`pip install -r requirements.txt` 后从仓库根目录运行。

真装错了也不会静默出问题：控制台启动时会检测到并打印上面这些说明。确实知道后果的话，
可以设 `AGENT_ALLOW_SITE_PACKAGES=1` 跳过检查。

控制台只监听 `127.0.0.1`，默认不对外暴露。如需在另一台机器的浏览器访问，见
[`agent/ui/README.md`](agent/ui/README.md) 的「在 Windows 侧浏览器访问」一节。

也可以不用控制台，直接以命令行方式驱动各阶段：

```bash
python -m agent.pipeline_launcher --help
```

## 领域包

领域相关的 prompt、工具、seed 与回答模板都收在 `agent/domains/<name>/` 下，通过
`--domain` 或环境变量 `AGENT_DOMAIN` 选择。`agent/core/` 保持领域无关，切换领域不需要
改动任何流水线代码。

仓库自带三个领域包：

| 领域 | intent | 回答模板 | 说明 |
| --- | --- | --- | --- |
| `health` | 有 | 有 | 健康管理领域，保留完整的 intent 与回答模板 |
| `health_talent` | 有 | 有 | 健康管理人才培养：学情分析、培养路径、实训任务、评价反馈 |
| `customer_service` | 无 | 无 | 最小领域包，用于验证无 intent、无模板的领域 |

新增领域包的结构与约定见 [`agent/domains/README.md`](agent/domains/README.md)。

## 关于本仓库

仓库不包含 `agent/data/`（seed 与中间产物）与 `agent/outputs/`（合成结果）。两者体积
较大，且其中部分报告会记录运行时生成的本机路径。

首次使用时需要：

1. 按上文创建 `agent/.env`；
2. 准备自己的领域包与 seed 文件（`agent/domains/<name>/seeds.jsonl`）。

## 许可证

MIT，见 [`LICENSE`](LICENSE)。可以自由使用、修改、再分发（保留版权声明即可）。

## 文档

| 文档 | 内容 |
| --- | --- |
| [`agent/README.md`](agent/README.md) | 流水线详解：数据格式、prompt 约定、各阶段参数与用法 |
| [`agent/domains/README.md`](agent/domains/README.md) | 领域包协议、最小领域包结构、工具运行时约定 |
| [`agent/ui/README.md`](agent/ui/README.md) | Web 控制台：界面说明、阶段一览、HTTP 接口 |
