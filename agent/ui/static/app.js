/* Agent 数据流水线控制台 —— 原生 JS，无外部依赖。 */
"use strict";

const state = {
  meta: null,
  domain: "health",
  // 构建任务名，形如 ``health_talent_train``：产物落到 outputs/<项目>/<任务>/ 下。
  // 留空表示「按当前项目自动取」，真正的值在 loadMeta 拿到项目清单后由 renderPrefixUi 填上。
  prefix: "",
  tasks: [],             // 当前项目下的构建任务，来自 /api/tasks
  legacyTasks: [],       // 产物还在老位置、可以一键归位的任务名
  stages: [],
  selected: new Set(),
  autoFilled: new Set(),  // 为满足上游依赖自动勾上的阶段 id（只做标记，手动一碰就取消）
  expanded: new Set(),    // 展开了参数区的阶段 id
  params: {},            // stageId -> { key: value }
  artifacts: [],
  runs: [],              // 运行批次（按时间倒序），来自 /api/runs
  activeRun: null,       // 选中的批次 id；null 表示「当前全部产物」
  currentJob: null,
  lastJob: null,         // 最近一次作业的快照，切页/重建列表后用来回放状态
  eventSource: null,
  lastSeq: 0,
  logCount: 0,
  seedRows: [],          // [{ raw, error, open }]
  seedEditable: true,
  statsReport: null,
  jobTimer: null,
  returnView: null,      // 统计/报告类作业跑完后要切回的 tab
};

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------------------
// 工具
// ---------------------------------------------------------------------------

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body && body.detail) detail = body.detail;
    } catch (_) { /* 非 JSON 错误体 */ }
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

// ---------------------------------------------------------------------------
// 通用零件（与 style.css 的 .empty / .skeleton 一一对应）
//
// 加这两个是为了收掉一个模式：全站有 35 处拿 `<p class="muted">一句话</p>` 当内容，
// 其中 8 处顶替加载态、10 处顶替错误态、17 处顶替空状态。
// 空状态把「现在是什么状况」和「下一步该点哪」挤在同一行灰字里，没有层次；
// 加载态什么都不占位，数据回来时整块内容会跳一下；错误态跟空状态长得一模一样。
// 现在统一走这三个函数，视觉和语气都只有一处可改。
// ---------------------------------------------------------------------------

/** 图标路径表。只描边不填充，跟侧边栏导航是同一套视觉。 */
const ICON_PATHS = {
  inbox: '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
  file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>',
  chart: '<line x1="12" y1="20" x2="12" y2="10"/><line x1="18" y1="20" x2="18" y2="4"/><line x1="6" y1="20" x2="6" y2="16"/>',
  folder: '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>',
  clock: '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
  play: '<polygon points="6 3 20 12 6 21 6 3"/>',
  alert: '<circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>',
  tool: '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>',
};
const ICON_DEFAULT = "inbox";

function iconSvg(name, size = 20) {
  const path = ICON_PATHS[name] || ICON_PATHS[ICON_DEFAULT];
  return `<svg class="ico" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" `
    + `stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" `
    + `aria-hidden="true">${path}</svg>`;
}

/**
 * 空状态：图标 + 标题 + 说明 +（可选）动作按钮。
 *
 * `desc` 与 `actions` 都是**已转义好的 HTML 片段**，不是纯文本 —— 说明里常要
 * 用 <b> 强调文件名、动作区要放按钮。纯文本请调用方自己 escapeHtml()。
 * `title` 走 escapeHtml；文案是固定串，转义只是防御。
 *
 * variant: "" | "inline"（面板内，间距收窄）| "compact" | "error"
 */
function emptyHtml({ title, desc = "", actions = "", icon = ICON_DEFAULT, variant = "" }) {
  return `<div class="empty${variant ? ` ${variant}` : ""}">`
    + `<span class="empty-ico">${iconSvg(icon)}</span>`
    + `<p class="empty-title">${escapeHtml(title)}</p>`
    + (desc ? `<p class="empty-desc">${desc}</p>` : "")
    + (actions ? `<div class="empty-actions">${actions}</div>` : "")
    + "</div>";
}

/**
 * 加载骨架。两种形态：
 *   loadingHtml({ rows: n })      —— 一条标题 + n 条正文，给列表 / 表格类内容
 *   loadingHtml({ block: true })  —— 一整块 96px 高的方块，给日志 / 预览这类大块内容
 * 用 aria-busy 告诉辅助技术「这里在加载」，别让读屏软件念出一堆空 div。
 */
function loadingHtml({ rows = 3, block = false } = {}) {
  const inner = block
    ? '<span class="skeleton skeleton-block"></span>'
    : `<span class="skeleton skeleton-title"></span>`
      + `<div class="skeleton-lines">${"<i class=\"skeleton\"></i>".repeat(rows)}</div>`;
  return `<div class="loading" aria-busy="true" aria-label="加载中">${inner}</div>`;
}

/**
 * 加载失败。跟空状态同一套骨架，只换图标和配色 —— 全站原来有 10 处
 * `<p class="muted">加载失败：{msg}</p>`，一行灰字，看不出是错误也没有重试入口。
 */
function errorHtml(message, { title = "加载失败", variant = "error inline" } = {}) {
  return emptyHtml({ icon: "alert", title, desc: escapeHtml(message), variant });
}

/**
 * 空状态里的动作按钮写成 data-click-target="<已有按钮的 id>"，由这里转发点击。
 * 不写第二份实现，也不给空状态按钮新绑一套事件 —— 它点的永远就是页面上
 * 那个同名的真按钮，禁用了就一起禁用。
 */
document.addEventListener("click", (event) => {
  const hit = event.target.closest("[data-click-target]");
  if (!hit) return;
  const target = $(hit.dataset.clickTarget);
  if (target && !target.disabled) target.click();
});

function formatBytes(bytes) {
  if (bytes === null || bytes === undefined) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes, index = 0;
  while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
  return `${value < 10 && index > 0 ? value.toFixed(1) : Math.round(value)} ${units[index]}`;
}

function formatTime(seconds) {
  if (!seconds) return "—";
  const date = new Date(seconds * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** 相对时间，只到「几秒/分/小时/天/月/年前」这一档；界面上看「多久没动过」比看绝对时间快。 */
function formatAgo(seconds) {
  if (!seconds) return "—";
  const diff = Math.max(0, Date.now() / 1000 - seconds);
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  const days = Math.floor(diff / 86400);
  if (days < 30) return `${days} 天前`;
  if (days < 365) return `${Math.floor(days / 30)} 个月前`;
  return `${Math.floor(days / 365)} 年前`;
}

/**
 * 去掉项目根前缀，只留项目内相对路径。
 *
 * 项目根对使用者是个常量（永远是这一份代码），写全了只是噪音：
 * `<项目根>/agent/.env` 里真正有用的只有 `agent/.env`。
 * 拿不到项目根（meta 还没拉回来）时原样返回，至少不丢信息。
 */
function relPath(absolute) {
  if (!absolute) return "";
  const root = state.meta && state.meta.projectRoot;
  if (!root || !String(absolute).startsWith(root)) return String(absolute);
  return String(absolute).slice(root.length).replace(/^[\\/]+/, "") || ".";
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  if (minutes < 60) return `${minutes}m ${rest}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

function formatNumber(value) {
  if (typeof value !== "number") return String(value);
  return Number.isInteger(value) ? value.toLocaleString("en-US") : value.toFixed(3);
}

let toastTimer = null;
function toast(message, kind = "") {
  const el = $("toast");
  el.textContent = message;
  el.className = `toast ${kind}`;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, kind === "err" ? 9000 : 4000);
}

// ---------------------------------------------------------------------------
// 初始化
// ---------------------------------------------------------------------------

async function init() {
  bindSidebar();
  bindTabs();
  bindActions();
  bindDomainManager();
  bindFileEditor();
  bindEnv();
  bindTools();
  bindRoles();
  bindConfig();
  bindRestart();
  // 先恢复上次停留的页面、再拉数据：否则刷新时会先闪一下「运行」页。
  const startView = showView(readStoredView());
  await loadMeta();
  // 阶段列表和产物列表首屏就拉；停在统计、报告这类懒加载页面时额外补一次该页数据。
  const tasks = [loadStages(), loadArtifacts()];
  if (startView !== "artifacts" && VIEW_LOADERS[startView]) tasks.push(VIEW_LOADERS[startView]());
  await Promise.all(tasks);
}

/** 侧边栏折叠：手动切换（localStorage 记忆）+ 窄窗口自动折叠。 */
function bindSidebar() {
  const sidebar = document.querySelector(".sidebar");
  const button = $("btn-collapse");
  if (!sidebar || !button) return;

  const KEY = "agentUi.sidebarCollapsed";
  const AUTO_WIDTH = 880;
  const readStored = () => {
    try { return localStorage.getItem(KEY) === "1"; } catch (_) { return false; }
  };
  const writeStored = (value) => {
    try { localStorage.setItem(KEY, value ? "1" : "0"); } catch (_) { /* 隐私模式下忽略 */ }
  };

  const sync = () => {
    const auto = window.innerWidth <= AUTO_WIDTH;
    const collapsed = !auto && readStored();
    sidebar.classList.toggle("auto-collapsed", auto);
    sidebar.classList.toggle("collapsed", collapsed);
    button.title = collapsed ? "展开侧边栏" : "折叠侧边栏";
    button.setAttribute("aria-expanded", String(!collapsed));
  };

  button.addEventListener("click", () => {
    writeStored(!readStored());
    sync();
  });
  window.addEventListener("resize", sync);
  sync();
}

/** 当前停留的页面：刷新（浏览器重载）后要回到这一页，而不是每次都跳回「运行」。 */
const VIEW_KEY = "agentUi.view";

function readStoredView() {
  try { return localStorage.getItem(VIEW_KEY) || "run"; } catch (_) { return "run"; }
}

function writeStoredView(view) {
  try { localStorage.setItem(VIEW_KEY, view); } catch (_) { /* 隐私模式下忽略 */ }
}

/** 各视图的懒加载器：切到哪个视图才拉哪个视图的数据。 */
const VIEW_LOADERS = {
  artifacts: loadArtifacts,
  seeds: loadSeeds,
  stats: loadStats,
  report: loadReport,
  jobs: loadJobs,
  tools: loadTools,
  roles: loadRoles,
  domain: loadDomainPack,
};

/** 只切 tab 与面板高亮，不拉数据（首屏恢复用，避免和初始化请求重复）。 */
function showView(view) {
  const tabs = [...document.querySelectorAll(".tab")];
  const target = tabs.find((tab) => tab.dataset.view === view) || tabs[0];
  for (const tab of tabs) tab.classList.toggle("active", tab === target);
  for (const section of document.querySelectorAll(".view")) {
    section.classList.toggle("active", section.id === `view-${target.dataset.view}`);
  }
  return target.dataset.view;
}

/** 切视图并拉取该视图的数据。 */
function activateView(view) {
  const name = showView(view);
  const loader = VIEW_LOADERS[name];
  if (loader) loader();
  return name;
}

function bindTabs() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      writeStoredView(activateView(tab.dataset.view));
    });
  });
}

function bindActions() {
  $("domain").addEventListener("change", (event) => {
    if (!selectDomain(event.target.value)) return;
    // 换了项目，弹窗里还是上一个项目的文件，直接关掉免得看错。
    closePreview();
    resetStatsUi();
    resetReportUi();
    if (roleUi.opened) loadRoles();
    // 页面内容已经跟着换了，再弹一句「已重新读取」是噪音；失败仍会提示。
    refreshAll({ quiet: true });
  });
  $("prefix").addEventListener("change", (event) => {
    setPrefix(event.target.value);
    commitPrefix();
  });
  $("prefix").addEventListener("input", (event) => renderTaskOptions(event.target.value));
  $("prefix").addEventListener("focus", () => openTaskOptions());
  $("btn-task-options").addEventListener("click", () => {
    const list = $("task-options");
    if (list.hidden) openTaskOptions();
    else closeTaskOptions();
  });
  document.addEventListener("click", (event) => {
    if (!$("task-picker").contains(event.target)) closeTaskOptions();
  });
  $("btn-migrate-tasks").addEventListener("click", () => migrateLegacyTasks());
  $("btn-refresh").addEventListener("click", () => {
    withBusy($("btn-refresh"), "刷新中…", () => refreshAll());
  });

  // 阶段侧栏宽度是 minmax(360px, 470px)，跟着窗口变，标签放不放得下要重测。
  let fitTimer = null;
  window.addEventListener("resize", () => {
    if (fitTimer) clearTimeout(fitTimer);
    fitTimer = setTimeout(fitStageTags, 120);
  });

  $("btn-stats-expand").addEventListener("click", () => {
    const cards = [...document.querySelectorAll("#stats-body .file-card")];
    const allOpen = cards.length > 0 && cards.every((card) => card.classList.contains("expanded"));
    for (const card of cards) toggleStatsCard(card, !allOpen);
  });

  document.querySelectorAll("[data-select]").forEach((button) => {
    button.addEventListener("click", () => selectPreset(button.dataset.select));
  });

  $("btn-expand-all").addEventListener("click", () => {
    const stages = [...document.querySelectorAll(".stage")];
    const allOpen = stages.length > 0 && stages.every((node) => node.classList.contains("expanded"));
    setAllStagesExpanded(!allOpen);
  });

  // 必须包一层箭头函数：runSelected 有个 force 形参，直接当处理器传的话
  // 点击事件对象会顶进 force 位，序列化成 {"isTrusted":false} 被后端当成
  // 「用户已确认忽略」，前置检查于是永远问不出口（曾导致缺输入直接开跑）。
  $("btn-run").addEventListener("click", () => runSelected());
  $("btn-cancel").addEventListener("click", cancelJob);
  $("btn-clear-log").addEventListener("click", clearLog);
  // 批次卡片是每次重建的，所以用委托而不是逐张绑定。
  $("run-list").addEventListener("click", (event) => {
    const card = event.target.closest(".run-card");
    if (card) selectRun(card.dataset.runId || null);
  });
  $("btn-runs-refresh").addEventListener("click", () => loadArtifacts());

  $("btn-preview-close").addEventListener("click", closePreview);
  document.querySelectorAll("[data-preview-close]").forEach((node) => node.addEventListener("click", closePreview));
  $("btn-seed-add").addEventListener("click", addSeedRow);
  $("btn-seed-save").addEventListener("click", saveSeeds);
  // 只要还有收着的就全部展开，否则全部收起。
  $("btn-seed-toggle").addEventListener("click", () => {
    const open = !state.seedRows.every((row) => row.open);
    for (const row of state.seedRows) row.open = open;
    renderSeeds();
  });
  $("btn-stats-run").addEventListener("click", () => runDataStats("json", "stats"));
  $("btn-report-run").addEventListener("click", () => runDataStats("markdown", "report"));
  $("btn-report-export").addEventListener("click", exportReportMarkdown);
  $("btn-report-copy").addEventListener("click", copyReportMarkdown);
  $("btn-report-expand").addEventListener("click", () => {
    const cards = [...document.querySelectorAll("#report-body .report-section")];
    const allOpen = cards.length > 0 && cards.every((card) => card.classList.contains("expanded"));
    for (const card of cards) toggleReportSection(card, !allOpen);
  });

  $("btn-domain-pack-refresh").addEventListener("click", () => {
    withBusy($("btn-domain-pack-refresh"), "刷新中…", async () => {
      const ok = await loadDomainPack();
      toast(ok ? "已重新读取领域包。" : "领域包没能刷新，具体错误见下方。", ok ? "ok" : "err");
    });
  });
}

/** 视图 id → 页面名，刷新失败时用它说清是哪个页面出了问题。 */
const VIEW_LABELS = {
  run: "运行",
  artifacts: "产物",
  seeds: "seed 数据",
  tools: "工具",
  stats: "统计",
  report: "报告",
  jobs: "历史",
  domain: "领域包",
};

/** 刷新时要跑的 loader 清单；打开过工具页之后，工具也一起刷。 */
function refreshJobs() {
  const jobs = [
    { view: "run", run: loadStages },
    { view: "artifacts", run: loadArtifacts },
    { view: "seeds", run: loadSeeds },
    { view: "stats", run: loadStats },
    { view: "report", run: loadReport },
    { view: "jobs", run: loadJobs },
    { view: "domain", run: loadDomainPack },
  ];
  if (toolUi.opened) jobs.push({ view: "tools", run: loadTools });
  if (roleUi.opened) jobs.push({ view: "roles", run: loadRoles });
  return jobs;
}

/**
 * 并发跑一组 loader，返回没能成功的那些。
 *
 * 每个 loader 都自己 catch 了异常并把错误写进自己的面板，Promise 永远是
 * fulfilled —— 所以成败只能看返回值（loader 成功 true、失败 false）。
 */
async function runLoaders(jobs) {
  const results = await Promise.allSettled(jobs.map((job) => job.run()));
  return jobs.filter((job, index) => {
    const result = results[index];
    return result.status === "rejected" || result.value === false;
  });
}

/**
 * 刷新按钮的外壳：按下后变「刷新中…」并禁用，至少停留 300ms 再复原。
 *
 * 本地接口很快，不加这个下限的话按钮状态一闪而过，看起来仍然像「没反应」。
 */
async function withBusy(button, busyText, action) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = busyText;
  const started = Date.now();
  try {
    return await action();
  } finally {
    const elapsed = Date.now() - started;
    if (elapsed < 300) await new Promise((resolve) => setTimeout(resolve, 300 - elapsed));
    button.disabled = false;
    button.textContent = original;
  }
}

async function refreshAll({ quiet = false } = {}) {
  // 切项目 / 切任务 / 手动刷新时，所有视图一起重拉：否则停在统计页切领域，
  // 看到的还是上一个项目的报告，得手动切走再切回来才会更新。
  const failed = await runLoaders(refreshJobs());
  if (!failed.length) {
    // 调用方自己会弹提示的场合（切领域等）压掉成功提示，免得两条 toast 打架。
    if (!quiet) toast("已重新读取磁盘状态。", "ok");
    return;
  }
  toast(`以下页面没能刷新：${failed.map((job) => VIEW_LABELS[job.view]).join("、")}。`, "err");
  // 直接切到出错的页面，让用户看到面板里的具体报错，而不是只看到一句提示。
  writeStoredView(showView(failed[0].view));
}

// ---------------------------------------------------------------------------
// 构建任务（产物隔离）
// ---------------------------------------------------------------------------
// 一次构建 = 一个任务，产物写在 ``outputs/<项目>/<任务>/<阶段>.jsonl``。
// 分任务是为了让不同批次的数据各占一个目录：同一批的完整链路放在一起，
// 想合成就去「11. 多任务整合」把它们并成一份，而不是靠文件名前缀在一堆文件里认。
//
// 任务名可以手填，所以这里只负责：换项目时把任务名从上一个项目的同一档换算过来，
// 以及把「本项目声明的数据集档位 + 磁盘上已有的任务」列成候选。
// 档位不再单列一排快捷按钮 —— 它就是任务名本身（``train`` 档 = ``<项目>_train``），
// 同一个值摆在按钮、输入框和候选列表三处，是同一份状态的三条入口。

/** 某个项目声明了哪几种数据集。meta 里没有这一项时返回空数组。 */
function splitsOf(name = state.domain, meta = state.meta) {
  const domain = ((meta && meta.domains) || []).find((item) => item.name === name);
  return (domain && domain.datasetSplits) || [];
}

/** 项目的默认任务名，即声明里的第一档（基类默认是「训练数据」）。 */
function defaultPrefixOf(name = state.domain) {
  const first = splitsOf(name)[0];
  return (first && first.prefix) || `${name}_train`;
}

/** 换项目时任务名怎么走：同档位换算，手填的原样保留。
 *
 * 任务名是项目隔离用的——切了项目还挂着上一个项目的名字，读到的就是别人的产物。
 * 所以原来那一档（训练数据 / 测试数据……）在新项目里换成同一档；而用户手填的
 * 名字（例如 smoke）两边都对不上任何一档，就原样保留。
 */
function pickPrefix(prefix, fromSplits, toSplits, fallback) {
  const source = fromSplits.find((item) => item.prefix === prefix);
  const hit = source && toSplits.find((item) => item.split === source.split);
  if (hit) return hit.prefix;
  if (prefix) return prefix;
  return (toSplits[0] && toSplits[0].prefix) || fallback;
}

/** 上一个项目的任务清单不能留到新项目：留着它，候选列表会把别人的任务名当成
 *  本项目的任务列出来，点一下就切过去，读到的是另一个项目的数据。 */
function clearTaskList() {
  state.tasks = [];
  state.legacyTasks = [];
}

/** 候选任务：项目声明的档位固定在前面（还没跑过也列出来），磁盘上已有的其它任务按
 *  最近改动排后面。
 *
 *  声明过的档位必须无条件列出：新项目、新档位在磁盘上还没有目录，只列「已有任务」
 *  的话，那排快捷按钮被删掉之后就再也没地方知道合法的档位名了。 */
function taskChoices() {
  const known = new Map((state.tasks || []).map((task) => [task.name, task]));
  const choices = [];
  const seen = new Set();
  for (const item of splitsOf()) {
    const task = known.get(item.prefix);
    choices.push({
      name: item.prefix,
      label: item.label,
      files: task ? task.files : 0,
      exists: Boolean(task),
      legacy: Boolean(task && task.legacy),
      declared: true,
    });
    seen.add(item.prefix);
  }
  for (const task of state.tasks || []) {
    if (seen.has(task.name)) continue;
    choices.push({
      name: task.name,
      label: "",
      files: task.files,
      exists: true,
      legacy: task.legacy,
      declared: false,
    });
  }
  return choices;
}

/** 任务名输入框与候选列表一起重画。 */
function renderPrefixUi() {
  const input = $("prefix");
  input.value = state.prefix;
  input.placeholder = defaultPrefixOf();
  renderTaskOptions();
}

/** 把候选任务填进下拉列表。
 *
 * ``query`` 只在用户主动输入时传：点开下拉时必须列出全部任务，拿输入框里
 * 已经填好的当前任务名去过滤，会把别的任务全滤掉——那正是最需要看到它们的时候。
 * 名字和档位标签都参与匹配，输入「训练」也能定位到 ``<项目>_train``。
 */
function renderTaskOptions(query = "") {
  const list = $("task-options");
  if (!list) return;
  list.innerHTML = "";
  const needle = String(query || "").trim().toLowerCase();
  const choices = taskChoices().filter((item) => !needle
    || item.name.toLowerCase().includes(needle)
    || item.label.toLowerCase().includes(needle));
  if (!choices.length) {
    const empty = document.createElement("div");
    empty.className = "task-option-empty";
    empty.textContent = needle ? "没有匹配的历史任务，可直接使用当前名称" : "暂无历史任务";
    list.appendChild(empty);
    return;
  }
  for (const item of choices) {
    const option = document.createElement("button");
    option.type = "button";
    option.className = "task-option";
    option.setAttribute("role", "option");
    option.dataset.value = item.name;
    // 档位按钮删掉之后，这里是唯一显示「现在落在哪一档」的地方。
    const current = item.name === state.prefix;
    option.classList.toggle("active", current);
    option.setAttribute("aria-selected", String(current));
    option.innerHTML = '<span class="task-option-main"><span class="task-option-name"></span></span>'
      + '<span class="task-option-meta"></span>';
    option.querySelector(".task-option-name").textContent = item.name;
    if (item.label) {
      const tag = document.createElement("span");
      tag.className = "task-option-tag";
      tag.textContent = item.label;
      option.querySelector(".task-option-main").appendChild(tag);
    }
    const meta = option.querySelector(".task-option-meta");
    if (!item.exists) {
      meta.textContent = "尚未创建";
      meta.classList.add("muted");
    } else {
      meta.textContent = item.legacy ? `${item.files} 个文件 · 老布局` : `${item.files} 个文件`;
    }
    option.addEventListener("click", () => {
      setPrefix(item.name);
      commitPrefix();
      closeTaskOptions();
    });
    list.appendChild(option);
  }
}

function openTaskOptions() {
  const list = $("task-options");
  const toggle = $("btn-task-options");
  if (!list || !toggle) return;
  renderTaskOptions();
  list.hidden = false;
  toggle.setAttribute("aria-expanded", "true");
}

function closeTaskOptions() {
  const list = $("task-options");
  const toggle = $("btn-task-options");
  if (!list || !toggle) return;
  list.hidden = true;
  toggle.setAttribute("aria-expanded", "false");
}

/** 写入任务名；留空表示「用当前项目的默认档」。 */
function setPrefix(value) {
  state.prefix = (value || "").trim() || defaultPrefixOf();
  // 历史批次属于旧任务；切换任务后必须回到「当前全部产物」，否则右侧会
  // 继续沿用旧任务的 activeRun，再拿新任务的盘面状态渲染，造成跨任务串档。
  state.activeRun = null;
  $("prefix").value = state.prefix;
  // 候选列表里的「当前」标记跟着走。
  renderTaskOptions();
}

/** 任务定下来之后重拉所有视图，否则界面还停在上一档数据上。 */
function commitPrefix() {
  closePreview();
  resetStatsUi();
  resetReportUi();
  refreshAll({ quiet: true });
}

async function loadMeta() {
  const meta = await api("/api/meta");
  // 任务名要按新项目的档位换算，得先用旧 meta 认出「现在是哪一档」——所以这几行
  // 必须排在 state.meta 被覆盖之前。
  const wasPrefix = state.prefix;
  const wasSplits = splitsOf();
  const wasDomain = state.domain;
  state.meta = meta;
  $("version").textContent = `v${meta.version}`;

  const select = $("domain");
  select.innerHTML = "";
  for (const domain of meta.domains) {
    const option = document.createElement("option");
    option.value = domain.name;
    // 只留领域名：seed 条数这类随时会变的东西放这里只会误导（保存 seed 后
    // 不会重拉 meta，数字会滞后），要看详情去「领域包」页。
    option.textContent = domain.name;
    select.appendChild(option);
  }
  const preferred = meta.domains.find((item) => item.name === meta.defaultDomain) || meta.domains[0];
  const target = preferred ? preferred.name : state.domain;
  if (preferred) {
    state.domain = target;
    select.value = target;
  }
  // 项目真的换了才丢任务清单：只是重读 meta（保存配置、刷新徽章）时清掉它，
  // 会让候选列表白闪一下空档位。
  if (target !== wasDomain) clearTaskList();
  state.prefix = pickPrefix(
    wasPrefix, wasSplits, splitsOf(target, meta),
    meta.defaultPrefix || defaultPrefixOf(target),
  );
  renderPrefixUi();

  // 「API」徽章看的是完整模型接入，不只是有没有 Key：端点或教师模型没填，
  // 作业同样跑不起来，只报 Key 会让人白排查一轮。
  const apiBadge = $("badge-api");
  apiBadge.className = `badge ${meta.configReady ? "ok" : "bad"}`;
  if (meta.configReady) {
    apiBadge.textContent = "模型接入 ✓";
    apiBadge.title = "API 端点、密钥与教师模型都已配置";
  } else {
    const names = (meta.missingConfig || []).map((item) => item.label);
    apiBadge.textContent = "模型接入 ✗";
    apiBadge.title = names.length ? `还缺：${names.join("、")}（点齿轮填写）` : "模型接入未配置";
  }

  const modelBadge = $("badge-model");
  modelBadge.className = `badge ${meta.modelPathExists ? "ok" : "bad"}`;
  modelBadge.textContent = meta.modelPathExists ? "Tokenizer ✓" : "Tokenizer ✗";
  // 可见文字里不放本机绝对路径（截图会带出去），完整路径挂在悬停提示上。
  modelBadge.title = meta.modelPath;

  // 重启按钮：能不能点、点了执行哪条命令都由后端说（uvicorn --reload 起的
  // 进程就没有启动命令行可重放）。不改启动方式就不会变，所以只在 meta 回来时
  // 刷一次标题，不额外拉接口。
  const restartBtn = $("btn-restart");
  restartBtn.title = meta.restartable
    ? `重启控制台（改完 agent/core/** 后需要）${meta.launchCommand ? `\n将执行：${meta.launchCommand}` : ""}`
    : meta.restartBlockedReason || "当前进程不支持原地重启";
  // 点不动的按钮就置灰：原来点一下才弹提示，得先试一次才知道不行。
  restartBtn.disabled = !meta.restartable;

  // 任务清单是顶栏候选列表和「源任务」提示的来源，首屏就拉一次：
  // 否则直接进运行页时，那个输入框只看得见「默认档位」一个值。
  await loadTasks();
}

// ---------------------------------------------------------------------------
// 运行时配置（agent/.env）
//
// 教师模型接入（模型名 / API 端点 / Key）和超时只活在 .env 与环境变量里，
// 之前界面上既看不到也改不了。这里做成一个弹窗：读的是实际生效值，写的是
// .env，保存后下一次运行作业就生效（子进程重新读 .env）。
// ---------------------------------------------------------------------------

const configUi = { data: null, dirty: new Set(), saving: false };

const CONFIG_SOURCE_TEXT = {
  file: "写在 .env 里",
  env: "来自系统环境变量",
  default: "内置默认",
  unset: "未设置",
};

function bindConfig() {
  $("btn-config").addEventListener("click", openConfig);
  $("btn-config-close").addEventListener("click", closeConfig);
  document.querySelectorAll("[data-config-close]").forEach((node) => node.addEventListener("click", closeConfig));
  $("btn-config-save").addEventListener("click", saveConfig);
}

function openConfig() {
  $("config-modal").hidden = false;
  $("config-msg").textContent = "";
  loadConfig();
}

function closeConfig() {
  $("config-modal").hidden = true;
  configUi.dirty.clear();
}

async function loadConfig() {
  const container = $("config-groups");
  container.innerHTML = loadingHtml({ rows: 4 });
  try {
    configUi.data = await api("/api/config");
  } catch (error) {
    container.innerHTML = errorHtml(error.message, { title: "读不到运行时配置" });
    return;
  }
  configUi.dirty.clear();
  $("config-env-path").textContent = relPath(configUi.data.envPath);
  renderConfig();
  updateConfigMsg();
}

function configSourceText(field) {
  if (field.source === "fallback") return `跟随「${field.sourceLabel}」`;
  return CONFIG_SOURCE_TEXT[field.source] || field.source;
}

/** 弹窗顶部的「还差什么」提示；都配好了就藏起来。 */
function renderConfigAlert() {
  const alert = $("config-alert");
  const missing = (configUi.data && configUi.data.missing) || [];
  if (!missing.length) {
    alert.hidden = true;
    alert.textContent = "";
    return;
  }
  alert.hidden = false;
  alert.textContent = `还缺 ${missing.map((item) => item.label).join("、")}，补齐前无法运行需要调用模型的阶段。`;
}

function renderConfig() {
  const container = $("config-groups");
  container.innerHTML = "";
  renderConfigAlert();
  for (const group of configUi.data.groups) {
    const section = document.createElement("section");
    section.className = "config-group";

    const head = document.createElement("h4");
    head.className = "config-group-title";
    head.textContent = group.name;
    section.appendChild(head);

    const grid = document.createElement("div");
    grid.className = "config-grid";
    for (const field of group.fields) grid.appendChild(renderConfigField(field));
    section.appendChild(grid);

    container.appendChild(section);
  }
}

function renderConfigField(field) {
  const wrap = document.createElement("div");
  wrap.className = `config-field${field.missing ? " missing" : ""}`;
  wrap.dataset.key = field.key;

  const inputId = `cfg-${field.key}`;
  const isSecret = field.type === "secret";
  const inputType = isSecret ? "password" : field.type === "int" ? "number" : "text";

  // 密钥不回显：输入框永远是空的，占位符只告诉用户「已经配了一把什么」。
  // 其它项没有默认值，占位符只说「该填什么」，不暗示某个具体值。
  const placeholder = isSecret
    ? (field.effective === "未配置" ? "未配置" : `已配置 ${field.effective}（留空不改）`)
    : field.required ? "必填" : "留空即跟随";

  // path 类型的生效值可能是本机绝对路径（tokenizer 默认值就是），只显示项目内
  // 的相对段；完整值挂在 title 上，需要时悬停可见。
  const effectiveShown = field.type === "path" ? relPath(field.effective) : field.effective;
  const effectiveTitle = field.type === "path" ? ` title="${escapeHtml(field.effective || "")}"` : "";
  const effective = isSecret
    ? ""
    : `<span class="config-effective">当前生效：<b${effectiveTitle}>${escapeHtml(effectiveShown || "（空）")}</b></span>`;

  const required = field.required ? `<span class="config-required">必填</span>` : "";

  wrap.innerHTML = `
    <label for="${inputId}">
      <span class="config-label">${escapeHtml(field.label)}${required}</span>
      <span class="config-source ${escapeHtml(field.source)}">${escapeHtml(configSourceText(field))}</span>
    </label>
    <input id="${inputId}" type="${inputType}" value="${escapeHtml(field.value)}"
      placeholder="${escapeHtml(placeholder)}" spellcheck="false" autocomplete="off">
    <span class="help">${escapeHtml(field.help)} ${effective}</span>`;

  const input = wrap.querySelector("input");
  input.addEventListener("input", () => {
    configUi.dirty.add(field.key);
    wrap.classList.add("dirty");
    updateConfigMsg();
  });
  return wrap;
}

function updateConfigMsg() {
  const count = configUi.dirty.size;
  $("config-msg").textContent = count ? `有 ${count} 项改动待保存` : "没有改动";
  $("btn-config-save").disabled = count === 0 || configUi.saving;
}

async function saveConfig() {
  if (configUi.saving || !configUi.dirty.size) return;
  const values = {};
  for (const key of configUi.dirty) {
    const input = document.getElementById(`cfg-${key}`);
    if (input) values[key] = input.value;
  }

  configUi.saving = true;
  updateConfigMsg();
  try {
    configUi.data = await api("/api/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values }),
    });
    configUi.dirty.clear();
    $("config-env-path").textContent = relPath(configUi.data.envPath);
    renderConfig();
    toast("配置已保存，下一次运行作业生效。", "ok");
    // 徽章和阶段卡上的 tokenizer 警告都取自 meta，跟着一起刷新。
    await loadMeta();
    await loadStages();
  } catch (error) {
    toast(error.message, "err");
    $("config-msg").textContent = error.message;
  } finally {
    configUi.saving = false;
    updateConfigMsg();
  }
}

// ---------------------------------------------------------------------------
// 重启控制台
//
// 控制台是常驻进程：agent/core/**、agent/config.py、agent/ui/*.py 只在启动时导入
// 一次，改完不重启就还是旧代码（agent/domains/<领域>.* 除外，那个每次现加载）。
//
// 重启 = 后端 os.execv 原地换掉进程镜像，所以 **PID 不变**：判断「新进程回来了没」
// 只能看 startedAt（模块重新导入的时刻）。这期间后端是断的，页面上所有按钮点下去
// 只会静默失败，所以用一块遮罩把进度摊开说清楚。
// ---------------------------------------------------------------------------

const RESTART_POLL_MS = 600;
const RESTART_TIMEOUT_MS = 45000;

const restartUi = {
  oldStartedAt: 0,   // 重启前那次 /api/meta 的 startedAt，用来认出新进程
  tries: 0,
  beganMs: 0,
  deadline: 0,
  timer: null,
  done: false,
};

function bindRestart() {
  $("btn-restart").addEventListener("click", () => {
    const meta = state.meta || {};
    // meta 没读到时按钮不会置灰（见 loadMeta），这里兜一层。
    if (!meta.restartable) {
      toast(meta.restartBlockedReason || "当前进程不支持原地重启。", "err");
      return;
    }
    if (!window.confirm(
      "重启会换掉控制台进程，这个页面会短暂断开。\n\n"
      + "磁盘上的产物、配置和运行历史都不受影响，页面会在新进程就绪后自动刷新。\n\n"
      + "确定重启吗？"
    )) return;
    startRestart();
  });
  $("btn-restart-retry").addEventListener("click", () => {
    restartUi.deadline = Date.now() + RESTART_TIMEOUT_MS;
    $("btn-restart-retry").hidden = true;
    $("btn-restart-reload").hidden = true;
    setRestartProgress("仍在等新进程…", "已经等超过一轮，继续探测。", "");
    pollRestart();
  });
  $("btn-restart-reload").addEventListener("click", () => location.reload());
}

/** 秒级时钟。重启过程中要看的粒度是秒，不是 formatTime 的分钟。 */
function clockTime(seconds) {
  if (!seconds) return "—";
  const date = new Date(seconds * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function setRestartProgress(title, detail, kind) {
  $("restart-state").textContent = title;
  $("restart-detail").textContent = detail || "";
  $("restart-spinner").className = `spinner${kind ? ` ${kind}` : ""}`;
}

async function startRestart() {
  restartUi.oldStartedAt = (state.meta && state.meta.startedAt) || 0;
  restartUi.tries = 0;
  restartUi.done = false;
  restartUi.beganMs = Date.now();
  restartUi.deadline = Date.now() + RESTART_TIMEOUT_MS;
  $("restart-modal").hidden = false;
  $("restart-msg").textContent = "";
  $("btn-restart-retry").hidden = true;
  $("btn-restart-reload").hidden = true;
  $("restart-old").textContent = restartUi.oldStartedAt
    ? `旧进程启动于 ${clockTime(restartUi.oldStartedAt)}`
    : "";
  setRestartProgress("正在停止旧进程…", "已发出重启指令，等新进程接管端口。", "");
  try {
    await api("/api/restart", { method: "POST" });
  } catch (error) {
    // 「有作业在跑」和「不支持原地重启」都是在这里才拿到原因；别让遮罩空转。
    finishRestart(false, error.message);
    return;
  }
  pollRestart();
}

async function pollRestart() {
  clearTimeout(restartUi.timer);
  if (restartUi.done) return;
  if (Date.now() > restartUi.deadline) {
    finishRestart(false, "等太久了，新进程一直没有响应。");
    return;
  }
  restartUi.tries += 1;
  const waited = ((Date.now() - restartUi.beganMs) / 1000).toFixed(1);
  $("restart-msg").textContent = `第 ${restartUi.tries} 次探测 · 已等待 ${waited}s`;
  try {
    // 时间戳参数是为了绕开浏览器缓存：/api/meta 没带缓存头，一旦命中启发式缓存
    // 就会一直读到旧进程的响应，看起来像「重启了但代码没变」。
    const meta = await api(`/api/meta?_=${Date.now()}`, { cache: "no-store" });
    if (meta.startedAt && meta.startedAt !== restartUi.oldStartedAt) {
      finishRestart(true, `新进程已就绪（启动于 ${clockTime(meta.startedAt)}）。`);
      return;
    }
  } catch (_) {
    // 进程正在换，连不上是预期内的，接着探。
  }
  restartUi.timer = setTimeout(pollRestart, RESTART_POLL_MS);
}

function finishRestart(ok, message) {
  restartUi.done = true;
  clearTimeout(restartUi.timer);
  $("restart-msg").textContent = "";
  if (ok) {
    setRestartProgress("重启完成", message, "done");
    $("restart-note").textContent = "页面马上重新加载，加载完读到的就是新代码。";
    setTimeout(() => location.reload(), 600);
    return;
  }
  setRestartProgress("没能确认新进程", message, "failed");
  $("restart-note").textContent =
    "先看启动控制台的那个终端（或日志文件）里报了什么；确认进程还在之后，"
    + "可以点「继续等待」，或直接手动刷新页面。";
  $("btn-restart-retry").hidden = false;
  $("btn-restart-reload").hidden = false;
}

// ---------------------------------------------------------------------------
// 阶段列表
// ---------------------------------------------------------------------------

async function loadStages() {
  const container = $("stage-list");
  container.innerHTML = loadingHtml({ rows: 6 });
  let payload;
  try {
    payload = await api(`/api/stages?domain=${encodeURIComponent(state.domain)}&prefix=${encodeURIComponent(state.prefix)}`);
  } catch (error) {
    container.innerHTML = errorHtml(error.message, { title: "读不到阶段列表" });
    return false;
  }
  state.stages = payload.stages;
  state.selected = new Set(state.meta.defaultPipeline.filter((id) => payload.stages.some((s) => s.id === id)));
  // 换了一批阶段，自动补的标记跟着作废。默认流水线本身就是条完整的链，再补一遍
  // 只是为它万一被改短时不至于缺上游 —— 补不到就算了，行上会标出来。
  state.autoFilled = new Set();
  fillUpstream();
  // 切领域后阶段集合可能变化，丢掉已经不存在的展开记忆
  const alive = new Set(payload.stages.map((stage) => stage.id));
  state.expanded = new Set([...state.expanded].filter((id) => alive.has(id)));

  container.innerHTML = "";
  renderStageGroups(container, payload.stages);
  // 必须在节点进了 DOM 之后才能量宽度。
  fitStageTags();
  updateSelectionHint();
  updateExpandAllLabel();
  // 节点是刚重建的，把最近一次作业的状态画回去——否则跑完一次再看这一页，
  // 「哪一步失败了」会被一次刷新抹掉，只剩一排干净的勾选框。
  if (state.lastJob) renderProgress(state.lastJob);
  return true;
}

/**
 * 缺本地 tokenizer 的前置警告。
 *
 * 阶段 4 / 6 要拿 tokenizer 渲染 prompt，路径不存在时跑起来必报错。后端
 * （_check_prerequisites）也会拦，但那要等点了「开始运行」才看得到；这里提前
 * 标在卡片上。判据与后端一致：参数里手填了 model_path 就不算缺。
 */
function tokenizerWarning(stage) {
  if (!stage.requires || !stage.requires.includes("model")) return "";
  if (state.meta && state.meta.modelPathExists) return "";
  if ((state.params[stage.id] || {}).model_path) return "";
  const path = state.meta ? relPath(state.meta.modelPath) : "";
  return `需要本地 tokenizer，但 ${path} 不存在。可以在本阶段参数的高级里填 tokenizer 路径，或点顶栏齿轮改默认路径。`;
}

/** 参数改动后重新评估警告，只改标记不重建列表（重建会丢输入焦点）。 */
function syncTokenizerWarnings() {
  for (const node of document.querySelectorAll(".stage")) {
    const stage = state.stages.find((item) => item.id === node.dataset.stageId);
    const warn = stage ? tokenizerWarning(stage) : "";
    node.classList.toggle("missing-tokenizer", Boolean(warn));
    // 只认准「缺 tokenizer」那一枚标记：按 `.stage-flag.bad` 找会先撞上「缺 N」
    // —— 它跑不通时也带 bad，于是这里一改就把整枚「缺 N」藏了（2026-09-20
    // 用户报「阶段 7 没红」，其实是它整个标记被这里 `hidden` 掉了）。
    const flag = node.querySelector(".stage-signal .stage-tok");
    if (flag) {
      flag.hidden = !warn;
      flag.title = warn;
    }
  }
}

/**
 * 标签放不下时整块收起。
 *
 * 一行里序号、名字、标签、状态抢同一段宽度。标签区被容器硬裁时会出现
 * 半个胶囊贴在行尾，比不显示更难读；名字被压短只是省略号，两害相权取轻。
 * 但「名字可以让一让」不等于「名字可以一直被截」：名字是扫视列表时唯一认得出
 * 阶段的东西，所以名字一旦真的出现省略号，标签就整体退到展开态里去。
 *
 * 判据必须覆盖两个方向，缺一不可：
 *   ① name.scrollWidth > name.clientWidth —— 名字被截出了省略号；
 *   ② row.scrollWidth > row.clientWidth —— 名字已经缩到 min-width 保底值、
 *      再没有可让的了，多出来的宽度把整行顶出容器（右侧「缺 tokenizer」和箭头
 *      会伸到面板外面，被 overflow:hidden 裁掉）。
 * 只看 ① 会漏掉 ②：名字缩到保底值时内容反而比 clientWidth 窄，scrollWidth
 * 取的是两者较大值，于是「名字没被截」成立、整行却已经溢出（阶段 11 有 4 个
 * 标签，168px，就是这样顶出去的）。
 *
 * 列宽跟着窗口变，所以 resize 后要重测。
 */
function fitStageTags() {
  for (const node of document.querySelectorAll("#stage-list .stage")) {
    const tags = node.querySelector(".stage-tags");
    const name = node.querySelector(".stage-name");
    const row = node.querySelector(".stage-row");
    if (!tags || !name || !row) continue;
    // 先按「标签全部显示」量一次，否则量到的是上一次的结论。
    tags.hidden = false;
    tags.hidden = name.scrollWidth > name.clientWidth || row.scrollWidth > row.clientWidth;
    // 行内收起了就放进展开态，属性本身不丢。
    const attrs = node.querySelector(".stage-attrs");
    if (attrs) attrs.hidden = !tags.hidden;
  }
}

// ---------------------------------------------------------------------------
// 阶段之间的前置关系
//
// 阶段不是各跑各的：去重要吃「合并」写出的文件，合并要吃「渲染」和「executor
// 回答」写出的文件。这层关系原本只存在于文档和日志里，界面上看不见，于是
// 「勾了去重却没勾合并」这种事只能等跑到一半报错才发现（2026-09-20 实际发生过）。
//
// 判据与后端 _check_prerequisites 一致：只看文件在不在盘上，不看谁勾了谁 ——
// 产物路径固定，盘上有上一轮的旧文件时，上游不跑也能接着往下走。
// ---------------------------------------------------------------------------

function stageById(id) {
  return state.stages.find((item) => item.id === id) || null;
}

/** 产物 key → 产出它的阶段（存在先后写同一个产物的，所以是复数）。 */
function producersOfKey(key) {
  return state.stages.filter((stage) => (stage.outputs || []).includes(key));
}

/** 这个输入的产出者，不含阶段自己（就地改写的阶段自己产出自己吃的东西）。 */
function producersFor(stage, item) {
  return producersOfKey(item.key).filter((producer) => producer.id !== stage.id);
}

/** 阶段跑不起来的硬门槛（眼下只有本地 tokenizer 路径不存在）。 */
function stageBlocker(stage) {
  return tokenizerWarning(stage);
}

/**
 * 某个输入还没着落：盘上没有，且没有已选阶段会把它写出来。
 *
 * 不能只看「盘上没有」—— 刚勾上「合并」时它的产物还没生成，但这次运行会生成，
 * 那时报缺就是假警报。也不能只看「有没有人勾」—— 盘上有旧文件时上游不跑也
 * 照样能用，硬拉着人重跑一遍上游是白烧算力。
 */
function inputOutstanding(stage, selection) {
  return (stage.inputsStatus || []).filter(
    (item) => !producesItself(stage, item) && !item.exists
      && !producersFor(stage, item).some((producer) => selection.has(producer.id))
  );
}

/** 就地改写的阶段（阶段 2 把轨迹改一遍）自己产出自己吃的东西，不算依赖。 */
function producesItself(stage, item) {
  return (stage.outputs || []).includes(item.key);
}

/**
 * 这批选择下「注定跑不出来」的阶段，以及每个阶段还没着落的输入。
 *
 * 两件事一起算：它们都取决于勾了谁，而且互相依赖 —— 一个阶段跑不出来，它该产出
 * 的文件就不会出现，靠它吃饭的下游也跟着跑不出来。所以用不动点迭代传播到稳定，
 * 比逐条判断准得多。2026-09-20 那次就是这种情形：去重本身没有任何毛病，坏在它
 * 上游的「合并」缺 tokenizer，一条链从中间断了，界面上却只有合并自己被标红。
 *
 * 失败判据与后端 _check_prerequisites 一致：缺 tokenizer 直接算；缺输入则分两种，
 * 严格阶段缺哪一项都跑不了，非严格阶段（比如数据统计）缺的会自动跳过，只有全缺
 * 才真跑不了。
 */
let selectionDiag = { doomed: new Set(), outstanding: new Map() };

function refreshSelectionDiag() {
  const selection = state.selected;
  const doomed = new Set();
  const missingOf = (stage) => (stage.inputsStatus || []).filter(
    (item) => !producesItself(stage, item) && !item.exists
      && !producersFor(stage, item).some(
        (producer) => selection.has(producer.id) && !doomed.has(producer.id)
      )
  );

  let changed = true;
  while (changed) {
    changed = false;
    for (const stage of state.stages) {
      if (!selection.has(stage.id) || doomed.has(stage.id)) continue;
      if (stageBlocker(stage)) {
        doomed.add(stage.id);
        changed = true;
        continue;
      }
      const missing = missingOf(stage);
      if (!missing.length) continue;
      const total = (stage.inputsStatus || []).length;
      if (stage.strictInputs === false && total && missing.length < total) continue;
      doomed.add(stage.id);
      changed = true;
    }
  }

  const outstanding = new Map();
  for (const stage of state.stages) outstanding.set(stage.id, missingOf(stage));
  selectionDiag = { doomed, outstanding };
}

/** 行上的「缺 N」提示该说什么：缺哪一项、该先跑谁、跑不跑得动。 */
function needNote(stage, items) {
  return items.map((item) => {
    const producers = producersFor(stage, item);
    if (!producers.length) return `${item.label}：盘上没有，也没有哪个阶段能产出它`;
    const names = producers.map((producer) => `「${producer.title}」`).join(" 或 ");
    // 上游跑不动时要说清卡在哪，否则「需要先跑 X」看着像个可行的建议，人勾上 X
    // 才发现照样跑不了。卡的原因不止缺 tokenizer：上游自己缺输入时同样跑不出来。
    const stuck = producers.map((producer) => {
      const blocker = stageBlocker(producer);
      if (blocker) return blocker;
      return selectionDiag.doomed.has(producer.id) ? "自己也缺输入，这次跑不出来" : "";
    }).filter(Boolean);
    if (stuck.length === producers.length) {
      return `${item.label}：需要 ${names}，但它跑不了 —— ${stuck[0]}`;
    }
    return `${item.label}：需要先跑 ${names}`;
  }).join("\n");
}

/**
 * 把已选阶段缺的上游逐级补上，返回补了哪些。
 *
 * 只补跑得动的上游：把缺 tokenizer 的阶段也补进来，只会让整批跑到它那儿断掉，
 * 而「勾满了」的样子还会掩盖真正的问题。补不上的就留在行上如实标出来。
 */
function fillUpstream() {
  const added = [];
  let changed = true;
  while (changed) {
    changed = false;
    for (const stage of state.stages) {
      if (!state.selected.has(stage.id)) continue;
      for (const item of inputOutstanding(stage, state.selected)) {
        const producer = producersFor(stage, item).find(
          (candidate) => !state.selected.has(candidate.id) && !stageBlocker(candidate)
        );
        if (!producer) continue;
        state.selected.add(producer.id);
        state.autoFilled.add(producer.id);
        added.push(producer.id);
        changed = true;
      }
    }
  }
  return added;
}

/**
 * 退掉「当初为别人补进来、现在没人需要了」的阶段。
 *
 * 取消掉一个阶段后，它拖进来的那串上游如果没别的人用，就该一并退回去 ——
 * 留着它们只会在下一次运行里白跑一遍。手动勾的那些不动。
 */
function dropUnneededAuto() {
  let changed = true;
  while (changed) {
    changed = false;
    for (const id of [...state.autoFilled]) {
      const stage = stageById(id);
      if (!stage || !state.selected.has(id)) {
        state.autoFilled.delete(id);
        continue;
      }
      const stillNeeded = state.stages.some((other) => {
        if (other.id === id || !state.selected.has(other.id)) return false;
        const own = new Set(other.outputs || []);
        return (other.inputsStatus || []).some(
          (item) => !own.has(item.key) && !item.exists && (stage.outputs || []).includes(item.key)
        );
      });
      if (stillNeeded) continue;
      state.selected.delete(id);
      state.autoFilled.delete(id);
      changed = true;
    }
  }
}

/**
 * 取消一个（或一组）阶段时，把因此跑不起来的下游一并退掉，返回退了哪些。
 *
 * 上游不跑了，它那份产物这次就不会更新；下游还留着勾选，要么拿上一轮的旧文件
 * 接着跑，要么跑到一半发现读不到 —— 两种都不是取消时的本意。所以取消要顺着链
 * 往下传，跟勾选时自动补上游是同一条规则的两个方向。
 *
 * 判据与「跑不通」完全一致：只有那份输入**因此断了供**才算牵连 ——
 *   · 盘上已有上一轮的旧文件 → 下游照样能用，不牵连；
 *   · 那份输入还有别的已选阶段会产出（或那个产出者自己也被退了）→ 看后者；
 *   · 非严格阶段还留着别的输入可吃 → 它仍跑得出结果，也不退。
 */
function dropDependents(gone) {
  const dropped = [];
  const queue = Array.isArray(gone) ? [...gone] : [gone];
  while (queue.length) {
    const stage = stageById(queue.shift());
    if (!stage) continue;
    for (const other of state.stages) {
      if (other.id === stage.id || !state.selected.has(other.id)) continue;
      const own = new Set(other.outputs || []);
      const items = (other.inputsStatus || []).filter((item) => !own.has(item.key));
      const willProduce = (item) => producersFor(other, item).some(
        (producer) => state.selected.has(producer.id) && !dropped.includes(producer.id)
      );
      const lost = items.filter(
        (item) => !item.exists && producersFor(other, item).some((p) => p.id === stage.id)
          && !willProduce(item)
      );
      if (!lost.length) continue;
      if (other.strictInputs === false && items.some((item) => item.exists || willProduce(item))) {
        continue;
      }
      state.selected.delete(other.id);
      state.autoFilled.delete(other.id);
      dropped.push(other.id);
      queue.push(other.id);
    }
  }
  return dropped;
}

/** 勾选一个阶段，顺带补齐它缺的上游。 */
function checkStage(stageId) {
  state.selected.add(stageId);
  state.autoFilled.delete(stageId);   // 手动勾的，不能再挂着「自动补」的牌子
  const added = fillUpstream();
  syncSelection();
  return added;
}

/** 取消勾选一个阶段，连带退掉依赖它的下游，以及没人再需要的自动补入项。 */
function uncheckStage(stageId) {
  state.selected.delete(stageId);
  state.autoFilled.delete(stageId);
  dropDependents(stageId);
  dropUnneededAuto();
  syncSelection();
}

/**
 * 阶段分组的定义。
 *
 * 14 个阶段其实是两件事：前 8 个是默认流水线（主链路），后 6 个是按需单独补跑的
 * 旁支。混在一个列表里时只能靠行尾一枚「旁支」小标签区分，扫视成本很高，也看不出
 * 「默认会跑哪些」。分组后每组一个头，顺带给了个整组全选的入口。
 */
const STAGE_GROUPS = [
  { id: "main", label: "主链路", hint: "按顺序跑完整条链" },
  { id: "side", label: "旁支", hint: "按需单独跑" },
];

function renderStageGroups(container, stages) {
  let order = 0;
  for (const group of STAGE_GROUPS) {
    const items = stages.filter((stage) => (stage.category || "main") === group.id);
    if (!items.length) continue;

    const section = document.createElement("section");
    section.className = "stage-group";
    section.dataset.group = group.id;

    const head = document.createElement("div");
    head.className = "stage-group-head";
    head.innerHTML = `
      <input type="checkbox" class="stage-group-check" title="全选 / 取消这一组">
      <span>${escapeHtml(group.label)}</span>
      <span class="stage-group-count"></span>
      <span class="stage-group-hint">${escapeHtml(group.hint)}</span>`;

    const body = document.createElement("div");
    body.className = "stage-group-body";
    for (const stage of items) {
      // 序号沿用全表顺序（01…14）：标题里已经写着「阶段 8」这类编号，两套编号
      // 对不上会让日志、文档和界面对不上号。
      order += 1;
      body.appendChild(renderStage(stage, order));
    }

    section.append(head, body);
    container.appendChild(section);
  }
  bindStageGroupHeads();
  // 行是新建的，「缺 N」和「自动补」得按当前选择画一遍（它们取决于勾了谁）。
  syncSelection();
}

function bindStageGroupHeads() {
  for (const head of document.querySelectorAll(".stage-group-head")) {
    const box = head.querySelector(".stage-group-check");
    const group = head.closest(".stage-group");
    if (!box || !group) continue;
    box.addEventListener("click", (event) => event.stopPropagation());
    box.addEventListener("change", () => {
      const ids = [...group.querySelectorAll(".stage")].map((node) => node.dataset.stageId);
      if (box.checked) {
        // 整组一起勾的时候也走同一条路：组里若有人缺上游，照样给补上。
        for (const id of ids) {
          state.selected.add(id);
          state.autoFilled.delete(id);
        }
        fillUpstream();
      } else {
        for (const id of ids) {
          state.selected.delete(id);
          state.autoFilled.delete(id);
        }
        // 整组取消也要顺着链退下游：旁支吃主链路的产物，取消主链路后
        // 留着的旁支只会拿着旧文件跑。
        dropDependents(ids);
        dropUnneededAuto();
      }
      syncSelection();
    });
  }
}

function renderStage(stage, index) {
  const node = document.createElement("div");
  node.className = "stage";
  node.dataset.stageId = stage.id;

  const inputs = stage.inputsStatus || [];
  const outputs = stage.outputsStatus || [];
  const produced = outputs.filter((item) => item.exists).length;

  const tokenizerWarn = tokenizerWarning(stage);
  node.classList.toggle("missing-tokenizer", Boolean(tokenizerWarn));

  const tags = [];
  if (stage.category === "side") tags.push(`<span class="tag side">旁支</span>`);
  if (stage.cost === "heavy") tags.push(`<span class="tag heavy">耗时</span>`);
  if (stage.requires.includes("api")) tags.push(`<span class="tag">API</span>`);
  if (stage.requires.includes("model")) tags.push(`<span class="tag">Tokenizer</span>`);
  if (stage.mutatesInput) tags.push(`<span class="tag warn">就地改输入</span>`);
  // 「缺 tokenizer」不在这里：它属于状态而不是属性，只放在右侧信号区。
  // 两处都写等于同一句话说两遍，而且标签区放不下时会被裁掉半个胶囊。

  // 收起态只留几个最有用的信号：能不能跑（缺多少输入）、跑过没有（产物计数）
  const signal = [];
  if (outputs.length) {
    signal.push(
      `<span class="stage-count ${produced === outputs.length ? "done" : ""}" title="已生成产物 / 全部产物">${produced}/${outputs.length}</span>`
    );
  }
  // 「缺 N」不能在这里定死：缺的那一项有没有人会产出，取决于当前勾了谁。
  // 文案与红色标记由 updateStageFlags 按当前选择填。
  signal.push(`<span class="stage-flag stage-need" hidden></span>`);
  signal.push(`<span class="stage-flag stage-auto" hidden title="为满足上游依赖自动勾上；手动改一下就归你管">自动补</span>`);
  // 带 `stage-tok` 专属类：按 `.stage-flag.bad` 找标记的代码会先命中「缺 N」，
  // 两个标记抢同一个选择器会让其中一个被当成另一个改掉（见 syncTokenizerWarnings）。
  signal.push(`<span class="stage-flag bad stage-tok" title="${escapeHtml(tokenizerWarn)}" ${tokenizerWarn ? "" : "hidden"}>缺 tokenizer</span>`);

  // 行内放不下的标签会整块收起（见 fitStageTags），属性就只剩这一份拷贝：
  // 展开时在详情里补上，免得「旁支」「需要 Tokenizer」这类信息彻底看不到。
  const attrRow = tags.length ? `<p class="stage-attrs" hidden>${tags.join("")}</p>` : "";

  node.innerHTML = `
    <div class="stage-row" role="button" tabindex="0" aria-expanded="false">
      <input type="checkbox" title="勾选后参与本次运行">
      <span class="stage-idx">${String(index).padStart(2, "0")}</span>
      <span class="stage-name">${escapeHtml(stage.title)}</span>
      <span class="stage-tags">${tags.join("")}</span>
      <span class="stage-state" hidden></span>
      <span class="stage-signal">${signal.join("")}</span>
      <svg class="stage-caret" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="6 9 12 15 18 9"/></svg>
    </div>
    <div class="stage-detail">
      <p class="stage-desc">${escapeHtml(stage.description)}</p>
      ${attrRow}
      <div class="io-block">
        ${ioGroup("输入", inputs, "读", stage)}
        ${ioGroup("输出", outputs, "写", null)}
        ${stage.strictInputs === false ? `<p class="io-note">缺失的输入会被自动跳过</p>` : ""}
      </div>
      <div class="param-sections"></div>
    </div>`;

  const checkbox = node.querySelector('input[type="checkbox"]');
  checkbox.checked = state.selected.has(stage.id);
  checkbox.addEventListener("click", (event) => event.stopPropagation());
  checkbox.addEventListener("change", () => {
    if (checkbox.checked) checkStage(stage.id);
    else uncheckStage(stage.id);
  });

  const row = node.querySelector(".stage-row");
  const toggle = () => {
    applyStageExpanded(node, !node.classList.contains("expanded"));
    updateExpandAllLabel();
  };
  row.addEventListener("click", toggle);
  row.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      toggle();
    }
  });

  node.classList.toggle("selected", checkbox.checked);
  applyStageExpanded(node, state.expanded.has(stage.id));
  node.querySelector(".param-sections").appendChild(renderParams(stage));
  return node;
}

/** 展开/收起单个阶段，同时同步记忆与无障碍属性。 */
function applyStageExpanded(node, expanded) {
  node.classList.toggle("expanded", expanded);
  const row = node.querySelector(".stage-row");
  if (row) row.setAttribute("aria-expanded", String(expanded));
  if (expanded) state.expanded.add(node.dataset.stageId);
  else state.expanded.delete(node.dataset.stageId);
}

function setAllStagesExpanded(expanded) {
  for (const node of document.querySelectorAll(".stage")) applyStageExpanded(node, expanded);
  updateExpandAllLabel();
}

function updateExpandAllLabel() {
  const stages = [...document.querySelectorAll(".stage")];
  const allOpen = stages.length > 0 && stages.every((node) => node.classList.contains("expanded"));
  $("btn-expand-all").textContent = allOpen ? "收起参数" : "展开参数";
}

function ioGroup(kind, items, direction, stage) {
  if (!items.length) return "";
  return `<div class="io-group">
      <span class="io-kind">${kind}</span>
      <div class="io-chips">${items.map((item) => ioChip(item, direction, stage)).join("")}</div>
    </div>`;
}

/** 阶段在列表里的序号，与行首那个 01…14 一致。 */
function stageNumber(stage) {
  const at = state.stages.indexOf(stage);
  return at >= 0 ? at + 1 : 0;
}
/**
 * 一枚输入/输出芯片。
 *
 * `stage` 只在画输入时传（输出传 null）：缺的那份输入要顺手说清该由谁产出，
 * 而输出是这一行自己写的，说「需先跑阶段 X」就成了胡说。
 */
function ioChip(item, direction, stage) {
  let detail = item.exists
    ? [item.lines !== undefined ? `${item.lines} 行` : null, formatBytes(item.size)].filter(Boolean).join(" · ")
    : "缺失";
  if (!item.exists && stage) {
    // 否则人还得回列表里一格格找是谁写它的，而「该勾谁」正是这张卡片眼下最该回答的事。
    const owners = producersFor(stage, item);
    detail = owners.length
      ? `缺失 · 需先跑 ${owners.map((owner) => `阶段 ${stageNumber(owner)}`).join("/")}`
      : "缺失 · 没有阶段能产出它";
  }
  return `<span class="io-chip ${item.exists ? "ok" : "missing"}" title="${escapeHtml(item.path)}">
      <span class="io-dir">${direction}</span>${escapeHtml(item.label)}<i>${escapeHtml(detail)}</i>
    </span>`;
}

function renderParams(stage) {
  const wrapper = document.createElement("div");
  if (!stage.params.length) {
    wrapper.innerHTML = `<p class="help">该阶段没有可调参数。</p>`;
    return wrapper;
  }
  if (!state.params[stage.id]) state.params[stage.id] = {};

  const groups = new Map();
  for (const param of stage.params) {
    if (!groups.has(param.group)) groups.set(param.group, []);
    groups.get(param.group).push(param);
  }
  // 参考 LLaMA-Factory：常用项直接平铺，其余分组收进折叠面板且默认关闭。
  const entries = [...groups.entries()].sort((a, b) => {
    if (a[0] === "常用") return -1;
    if (b[0] === "常用") return 1;
    return 0;
  });

  for (const [groupName, params] of entries) {
    const primary = groupName === "常用";
    const section = document.createElement("div");
    section.className = primary ? "param-section primary" : "param-section folded";

    const grid = document.createElement("div");
    grid.className = "param-grid";
    for (const param of params) grid.appendChild(renderParam(stage, param));

    if (primary) {
      section.appendChild(grid);
    } else {
      const head = document.createElement("button");
      head.type = "button";
      head.className = "param-section-head";
      head.innerHTML = `
        <svg class="caret" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="9 6 15 12 9 18"/></svg>
        <span class="param-section-name">${escapeHtml(groupName)}</span>
        <span class="param-count">${params.length}</span>`;
      head.addEventListener("click", () => section.classList.toggle("open"));
      section.append(head, grid);
    }
    wrapper.appendChild(section);
  }
  return wrapper;
}

function renderParam(stage, param) {
  const store = state.params[stage.id];
  const wrap = document.createElement("div");
  // 「源任务」这类字段的候选值来自磁盘现状，声明里写不出来——把已知任务名
  // 补在说明后面，省得让人先去产物页把目录名一个个抄下来。
  const hint = param.key === "source_tasks" && state.tasks.length
    ? `可选任务：${state.tasks.map((task) => task.name).join("、")}`
    : "";
  const helpText = [param.help, hint].filter(Boolean).join(" ");
  const help = helpText ? `<span class="help">${escapeHtml(helpText)}</span>` : "";

  if (param.type === "bool") {
    wrap.className = "param check";
    const checked = store[param.key] !== undefined ? store[param.key] : !!param.default;
    store[param.key] = checked;
    wrap.innerHTML = `<input type="checkbox" id="p-${stage.id}-${param.key}" ${checked ? "checked" : ""}>
      <label for="p-${stage.id}-${param.key}" title="${escapeHtml(param.help)}">${escapeHtml(param.label)}</label>`;
    wrap.querySelector("input").addEventListener("change", (event) => {
      store[param.key] = event.target.checked;
    });
    return wrap;
  }

  wrap.className = "param";
  const current = store[param.key];
  const value = current === undefined || current === null ? "" : String(current);

  let control;
  if (param.type === "choice") {
    const options = [`<option value=""></option>`].concat(
      param.choices.map((choice) => `<option value="${escapeHtml(choice)}">${escapeHtml(choice)}</option>`)
    );
    control = `<select>${options.join("")}</select>`;
  } else {
    const inputType = param.type === "int" || param.type === "float" ? "number" : "text";
    const step = param.type === "float" ? ' step="any"' : "";
    // 占位符是「留空时实际会用的值」，由后端按 .env / 环境变量解析后给出。
    // 路径类的值去掉项目根前缀：根目录对使用者是常量，写全了只是噪音。
    const hint = param.type === "path" ? relPath(param.placeholder) : param.placeholder;
    control = `<input type="${inputType}"${step} value="${escapeHtml(value)}"
      placeholder="${escapeHtml(hint)}" spellcheck="false">`;
  }
  wrap.innerHTML = `<label title="${escapeHtml(param.help)}">${escapeHtml(param.label)}</label>${control}${help}`;

  const input = wrap.querySelector("input, select");
  if (param.type === "choice") input.value = value;
  input.addEventListener("change", () => {
    store[param.key] = input.value;
    // 手填了 tokenizer 路径就不算缺，卡片上的警告要跟着消掉。
    if (param.key === "model_path") syncTokenizerWarnings();
  });
  return wrap;
}

function selectPreset(mode) {
  if (mode === "main") {
    state.selected = new Set(state.stages.filter((s) => s.category === "main").map((s) => s.id));
  } else if (mode === "all") {
    state.selected = new Set(state.stages.map((s) => s.id));
  } else {
    state.selected = new Set();
  }
  // 预设是整批重选，之前自动补的标记全部作废；整条链式的预设本来就不会缺上游，
  // 再补一遍只是把不完整的预设（有人改过默认流水线）也拉成能跑的。
  state.autoFilled = new Set();
  fillUpstream();
  syncSelection();
}

/**
 * 把 state.selected 刷到界面上：每张卡片的勾选框与选中底色，以及分组头的
 * `已选/总数` 和全选框的三态。
 *
 * 勾选状态有好几个改动入口（预设按钮、单张卡片、分组全选），全都得走这一个出口，
 * 否则分组头上会留着上一次的数量，看起来像没生效。
 */
function syncSelection() {
  // 先算一遍诊断，行上的标记和运行条上的计数都取自它。
  refreshSelectionDiag();
  for (const node of document.querySelectorAll(".stage")) {
    const checked = state.selected.has(node.dataset.stageId);
    node.querySelector('input[type="checkbox"]').checked = checked;
    node.classList.toggle("selected", checked);
    updateStageFlags(node);
  }
  updateSelectionHint();
}

/**
 * 行上「缺 N」与「自动补」两个标记。
 *
 * 它们取决于**当前勾了谁** —— 缺的那项只要有人会产出就不算问题 —— 所以必须能
 * 单独重刷。重建整个列表能省事，但会丢掉展开态、参数输入焦点和滚动位置。
 */
function updateStageFlags(node) {
  const stage = stageById(node.dataset.stageId);
  if (!stage) return;
  const need = node.querySelector(".stage-need");
  if (need) {
    const out = selectionDiag.outstanding.get(stage.id) || [];
    need.hidden = !out.length;
    need.textContent = `缺 ${out.length}`;
    // 红＝「勾了但这批跑不出来」。只缺「本次运行会产出的」不算红：那不是问题，
    // 只是还没轮到。缺 tokenizer 也要红 —— 它同样跑不出来，曾经把它排除在外
    // （右面另有一枚「缺 tokenizer」说明），结果同一条链上有的红有的黄，反而
    // 让人以为黄的那些能跑。原因写在悬浮提示里。
    need.classList.toggle("bad", selectionDiag.doomed.has(stage.id));
    need.title = needNote(stage, out);
  }
  const auto = node.querySelector(".stage-auto");
  if (auto) auto.hidden = !state.autoFilled.has(stage.id);
}

function updateSelectionHint() {
  const count = state.selected.size;
  const total = state.stages.length;
  $("btn-run").disabled = count === 0 || !!state.currentJob;
  // 从作业进度那一侧也会调进来，所以这里自己把诊断算一遍，不依赖调用方先刷过。
  refreshSelectionDiag();
  updateStageGroupCounts();
  // 运行中这一行归进度条管：作业提交时已经锁定了阶段表，此时报「已选 N/M」
  // 会让人以为改勾选能影响正在跑的东西，该报的是「跑完几个」。
  if (state.currentJob) return;
  // 只报「已选 8 个」看不出还剩多少没选，补上总数才是一眼能判断的进度。
  if (!count) {
    $("selection-hint").textContent = `未选择阶段（共 ${total} 个）`;
    $("selection-hint").classList.remove("bad");
    return;
  }
  // 有几个阶段在本次选择下注定跑不通（缺 tokenizer，或缺跑得出来的上游）。
  // 这事以前只能等到点下运行被拦一次才知道，现在提前摆在按钮旁边。
  const broken = selectionDiag.doomed.size;
  $("selection-hint").textContent = [
    `已选 ${count} / ${total} 个阶段`,
    broken ? `${broken} 个跑不通` : "",
  ].filter(Boolean).join(" · ");
  $("selection-hint").classList.toggle("bad", Boolean(broken));
}

/**
 * 分组头的选择状态：`已选/总数` + 全选框的三种态。
 *
 * 全选框不能只写 checked —— 选中一半时它必须显 indeterminate，否则界面会
 * 谎报「整组都选了」，而那是批量运行前最需要看准的一件事。
 */
function updateStageGroupCounts() {
  for (const group of document.querySelectorAll(".stage-group")) {
    const ids = [...group.querySelectorAll(".stage")].map((node) => node.dataset.stageId);
    const picked = ids.filter((id) => state.selected.has(id)).length;
    const label = group.querySelector(".stage-group-count");
    if (label) label.textContent = `${picked}/${ids.length}`;
    const box = group.querySelector(".stage-group-check");
    if (box) {
      box.checked = ids.length > 0 && picked === ids.length;
      box.indeterminate = picked > 0 && picked < ids.length;
    }
  }
}

function orderedSelection() {
  const order = state.stages.map((s) => s.id);
  return order.filter((id) => state.selected.has(id));
}

// ---------------------------------------------------------------------------
// 运行作业
// ---------------------------------------------------------------------------

async function runSelected(force = false) {
  const stages = orderedSelection();
  if (!stages.length) return;
  const body = {
    stages,
    domain: state.domain,
    prefix: state.prefix,
    params: state.params,
    force,
  };
  try {
    const job = await api("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    attachJob(job);
  } catch (error) {
    if (error.status === 409 && !force) {
      const detail = error.message.replace(/\n\n确认要忽略并继续吗？$/, "");
      if (confirm(`${detail}\n\n忽略前置检查并继续运行吗？`)) {
        return runSelected(true);
      }
      return;
    }
    toast(error.message, "err");
  }
}

function attachJob(job) {
  state.currentJob = job.id;
  state.lastSeq = 0;
  clearLog();
  $("log-title").textContent = `日志 · ${job.id}`;
  $("btn-run").disabled = true;
  $("btn-cancel").disabled = false;
  renderProgress(job);
  connectStream(job.id);
  startJobPolling(job.id);
}

function connectStream(jobId) {
  if (state.eventSource) state.eventSource.close();
  const source = new EventSource(`/api/jobs/${jobId}/stream?since=${state.lastSeq}`);
  state.eventSource = source;

  source.onmessage = (event) => {
    let payload;
    try { payload = JSON.parse(event.data); } catch (_) { return; }
    handleEvent(payload);
  };
  source.onerror = () => {
    source.close();
    state.eventSource = null;
    if (!state.currentJob) return;
    // 手动重连，带上最新 seq，避免历史日志重复。
    setTimeout(() => {
      if (state.currentJob === jobId) connectStream(jobId);
    }, 1500);
  };
}

function handleEvent(payload) {
  if (payload.type === "snapshot" || payload.type === "status") {
    renderProgress(payload.job);
    return;
  }
  if (payload.type === "log") {
    appendLog(payload.line, payload.replace);
    return;
  }
  if (payload.type === "done") {
    finishJob(payload.job);
  }
}

/**
 * 日志区空状态。
 *
 * `#log` 是纯文本容器（appendLog 只往里塞 span），空态做不成它的子节点，
 * 只能是旁边一个盖在深色表面上的兄弟节点：有输出就收起，清空再放回来。
 */
function setLogEmpty(show) {
  const node = $("log-empty");
  if (node) node.hidden = !show;
}

function clearLog() {
  $("log").textContent = "";
  state.logCount = 0;
  setLogEmpty(true);
}

function appendLog(line, replace) {
  const log = $("log");
  setLogEmpty(false);
  const cls = line.stream === "system" ? "l-sys" : classifyLog(line.text);
  if (replace && log.lastElementChild && log.lastElementChild.dataset.transient === "1") {
    log.lastElementChild.textContent = line.text + "\n";
    log.lastElementChild.className = cls;
    log.lastElementChild.dataset.transient = line.transient ? "1" : "0";
    if ($("autoscroll").checked) log.scrollTop = log.scrollHeight;
    if (line.seq > state.lastSeq) state.lastSeq = line.seq;
    return;
  }
  const span = document.createElement("span");
  span.className = cls;
  span.dataset.transient = line.transient ? "1" : "0";
  span.textContent = `${line.text}\n`;
  log.appendChild(span);
  state.logCount += 1;

  if (state.logCount > 2500) {
    log.removeChild(log.firstElementChild);
    state.logCount -= 1;
  }
  if ($("autoscroll").checked) log.scrollTop = log.scrollHeight;
  if (line.seq > state.lastSeq) state.lastSeq = line.seq;
}

function classifyLog(text) {
  if (/\b(traceback|error|exception|failed)\b/i.test(text) || /失败/.test(text)) return "l-err";
  if (/\bwarn(ing)?\b/i.test(text) || /警告|⚠/.test(text)) return "l-warn";
  if (/^\s*\d+%\|/.test(text) || /\d+it\/s/.test(text)) return "l-muted";
  return "";
}

function renderProgress(job) {
  if (!job || !job.stages) return;
  state.lastJob = job;
  applyJobState(job);
  updateRunBar(job);
}

/** 行内状态文案。没有条目就是「还没轮到这一步」，不画状态。 */
const STAGE_STATE_TEXT = {
  running: "运行中",
  succeeded: "完成",
  failed: "失败",
  skipped: "跳过",
};

/**
 * 把作业状态画到每个阶段行上。
 *
 * 整批跑到哪一步、哪一步卡住了，答案应该长在那一行上，而不是让人去看另一处
 * 的一排 chip 再回来对名字——所以状态跟行一起，按 stage id 就近贴上。
 */
function applyJobState(job) {
  // 作业里的阶段条目来自 StageRun.to_json()，键名是 stageId（阶段「定义」才叫 id）。
  // 这里两种都认，纯粹是免得下次再写错一个字母就静默不显示状态。
  const byId = new Map((job.stages || []).map((stage) => [stage.stageId || stage.id, stage]));
  for (const node of document.querySelectorAll("#stage-list .stage")) {
    const badge = node.querySelector(".stage-state");
    if (!badge) continue;
    const stage = byId.get(node.dataset.stageId);
    const text = stage && STAGE_STATE_TEXT[stage.status];
    badge.hidden = !text;
    badge.textContent = text
      ? `${text}${stage.duration ? ` · ${formatDuration(stage.duration)}` : ""}`
      : "";
    badge.className = `stage-state ${stage ? stage.status : ""}`;
  }
}

/**
 * 顶部控制条：状态点、一句话状态、总进度。
 *
 * 进度按「已有结论的阶段数 / 总数」算（成功和失败都算有结论），而不是按成功数：
 * 中途失败时按成功数算会让进度条停在半路，看起来像还在跑。
 */
function updateRunBar(job) {
  const stages = (job && job.stages) || [];
  const running = Boolean(state.currentJob);
  const done = stages.filter((stage) => stage.status === "succeeded").length;
  const failed = stages.filter((stage) => stage.status === "failed").length;
  const total = stages.length;
  const current = stages.find((stage) => stage.status === "running");

  $("run-dot").className = `run-dot ${
    !total ? "" : running ? "live" : failed ? "err" : done === total ? "ok" : "warn"
  }`.trim();
  $("run-fill").style.width = total ? `${Math.round(((done + failed) / total) * 100)}%` : "0";
  $("run-fill").className = "run-bar-fill"
    + (failed ? " fail" : (total && done === total && !running ? " done" : ""));

  if (!total) {
    $("run-status").textContent = "未开始";
    updateSelectionHint();
    return;
  }

  $("run-status").textContent = running
    ? (current ? `正在运行：${current.title}` : "准备中…")
    : (failed ? `运行结束 · ${failed} 个阶段失败`
      : (done === total ? "运行完成" : `运行结束 · 完成 ${done}/${total}`));

  const start = job.startedAt || job.createdAt || 0;
  const elapsed = start ? Math.max(0, (job.finishedAt || Date.now() / 1000) - start) : 0;
  $("selection-hint").textContent = [
    `已完成 ${done}/${total}`,
    failed ? `失败 ${failed}` : "",
    `用时 ${formatDuration(elapsed)}`,
  ].filter(Boolean).join(" · ");
}

function finishJob(job) {
  if (state.eventSource) { state.eventSource.close(); state.eventSource = null; }
  stopJobPolling();
  state.currentJob = null;
  $("btn-run").disabled = state.selected.size === 0;
  $("btn-cancel").disabled = true;
  renderProgress(job);
  const kind = job.status === "succeeded" ? "ok" : "err";
  toast(`作业 ${job.id} 结束：${job.status}`, kind);
  loadArtifacts();
  loadStages();
  const back = state.returnView;
  state.returnView = null;
  if (back) {
    // 从统计/报告页发起的作业：跑完回到那一页，tab 点击会顺带刷新内容。
    const tab = document.querySelector(`#tabs button[data-view="${back}"]`);
    if (tab) tab.click();
  }
}

async function cancelJob() {
  if (!state.currentJob) return;
  if (!confirm("确定要取消当前作业吗？正在运行的阶段会被终止。")) return;
  try {
    await api(`/api/jobs/${state.currentJob}/cancel`, { method: "POST" });
  } catch (error) {
    toast(error.message, "err");
  }
}

function startJobPolling(jobId) {
  stopJobPolling();
  state.jobTimer = setInterval(async () => {
    try {
      const job = await api(`/api/jobs/${jobId}`);
      renderProgress(job);
      if (["succeeded", "failed", "canceled"].includes(job.status)) {
        if (state.currentJob === jobId) finishJob(job);
      }
    } catch (_) { /* 轮询失败忽略 */ }
  }, 4000);
}

function stopJobPolling() {
  if (state.jobTimer) { clearInterval(state.jobTimer); state.jobTimer = null; }
}

// ---------------------------------------------------------------------------
// 产物：按运行批次查看
//
// 产物路径是固定的（`outputs/<项目>/<任务>/<阶段stem>.<后缀>`），下一次运行会往同一个
// 文件里追加或重写。
// 于是「盘面上现在有什么」和「某次运行产出了什么」是两个不同的问题：
//   · 前者是当前快照，看 /api/artifacts；
//   · 后者是每次运行结束时的 stat 快照，看 /api/runs。
// 左栏选批次、右栏看明细，默认停在「当前全部产物」。
// ---------------------------------------------------------------------------

/** 批次在时间轴上的落点：今天 / 昨天 / 几天前 / 具体日期。 */
function runDayLabel(seconds) {
  const date = new Date(seconds * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  const midnight = (value) => {
    const copy = new Date(value);
    copy.setHours(0, 0, 0, 0);
    return copy.getTime();
  };
  const days = Math.round((midnight(new Date()) - midnight(date)) / 86400000);
  if (days <= 0) return "今天";
  if (days === 1) return "昨天";
  if (days < 7) return `${days} 天前`;
  if (date.getFullYear() === new Date().getFullYear()) {
    return `${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  }
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function runClockLabel(seconds) {
  const date = new Date(seconds * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

const RUN_STATUS_TEXT = {
  succeeded: "成功", failed: "失败", canceled: "已取消", running: "运行中", queued: "排队中",
};

function runDotClass(status) {
  if (status === "succeeded") return "ok";
  if (status === "failed") return "err";
  if (status === "running" || status === "queued") return "live";
  return "warn";  // canceled：不是失败，但也没跑完
}

function runCardHtml({ id, dot, clock, tally, meta, title }) {
  const active = (state.activeRun || "") === (id || "");
  return `<button class="run-card${active ? " active" : ""}" data-run-id="${escapeHtml(id || "")}">`
    + `<span class="run-card-head">`
    + `<span class="run-dot ${dot}"></span>`
    + `<span class="run-clock">${escapeHtml(clock)}</span>`
    + `<span class="run-tally">${escapeHtml(tally)}</span>`
    + `</span>`
    + `<span class="run-card-meta" title="${escapeHtml(title || meta)}">${escapeHtml(meta)}</span>`
    + `</button>`;
}

function renderRunList() {
  const alive = state.artifacts.filter((item) => item.exists).length;
  const parts = [
    '<div class="run-group">',
    '<div class="run-group-label">现在</div>',
    runCardHtml({
      id: "",
      dot: state.currentJob ? "live" : "ok",
      clock: "当前全部产物",
      tally: `${alive}/${state.artifacts.length} 存在`,
      meta: "盘面上现有的文件，不区分是哪次跑出来的",
    }),
    "</div>",
  ];

  const byDay = new Map();
  for (const run of state.runs) {
    const label = runDayLabel(run.startedAt || run.createdAt || 0);
    if (!byDay.has(label)) byDay.set(label, []);
    byDay.get(label).push(run);
  }
  for (const [label, items] of byDay) {
    parts.push(`<div class="run-group"><div class="run-group-label">${escapeHtml(label)}</div>`);
    for (const run of items) {
      const stages = run.stages || [];
      const done = stages.filter((item) => item.status === "succeeded").length;
      const produced = (run.artifacts || []).filter((item) => producedInRun(item, run)).length;
      // 输入框留空时后端按项目默认档位取任务名，此时 state.prefix 是空串，
      // 直接比较会把「默认档位」误报成「另一次换过任务的运行」。
      const mine = state.prefix || defaultPrefixOf();
      const meta = [
        RUN_STATUS_TEXT[run.status] || run.status,
        `${done}/${stages.length} 个阶段`,
        run.duration ? formatDuration(run.duration) : "",
        run.prefix && run.prefix !== mine ? `任务 ${run.prefix}` : "",
      ].filter(Boolean).join(" · ");
      parts.push(runCardHtml({
        id: run.id,
        dot: runDotClass(run.status),
        clock: runClockLabel(run.startedAt || run.createdAt || 0),
        tally: `${produced} 个产物`,
        meta,
      }));
    }
    parts.push("</div>");
  }

  if (!state.runs.length) {
    parts.push(
      '<div class="run-group">',
      '<div class="run-group-label">还没有运行记录</div>',
      '<p class="run-note">从这一版开始，每跑一次流水线都会记下这次运行的时间、'
        + "阶段和产物清单，跑完就会出现在这里。</p>",
      "</div>"
    );
  }
  $("run-list").innerHTML = parts.join("");
}

/** 「当前全部产物」视图的行：盘面上现存的文件。 */
function rowsForCurrent() {
  return state.artifacts.map((item) => ({
    key: item.key,
    label: item.label,
    path: item.path,
    prefix: state.prefix,
    previewable: Boolean(item.exists) && !item.isDir,
    statusKind: item.exists ? "ok" : "bad",
    statusText: item.exists ? "存在" : "缺失",
    size: item.exists ? (item.isDir ? "目录" : formatBytes(item.size)) : "—",
    lines: item.lines !== undefined ? item.lines.toLocaleString("en-US") : "—",
    mtime: item.exists ? formatTime(item.mtime) : "—",
  }));
}

/**
 * 一次运行到底写没写出某个产物。
 *
 * 光看「文件在不在」不行：seed 输入、RAG 语料这类产物常年躺在盘面上，任何一次
 * 运行都会把它们算成「本次产出」；反过来，某个阶段这次跑了但没写出新内容
 * （比如报告里的「新增转换 0 条」），文件的 mtime 还停在上一轮，也不该算进来。
 * 判据就一条：文件是不是在这次运行的时间窗里被写过。
 */
const RUN_MTIME_SLACK = 5;      // 秒：给文件系统时间戳的精度留余量

function producedInRun(item, run) {
  if (!item.exists) return false;
  const start = run.startedAt || run.createdAt || 0;
  // 时间信息不全时退回「文件在就算」：宁可多列一行，也别把整批产物藏起来。
  if (!start || !item.mtime) return true;
  const end = (run.finishedAt || Number.POSITIVE_INFINITY) + RUN_MTIME_SLACK;
  return item.mtime >= start - RUN_MTIME_SLACK && item.mtime <= end;
}

/**
 * 某个批次视图的行：这次运行写出了什么，以及那些文件现在还在不在。
 *
 * 只列这次运行确实写出的文件。盘面上现存、但这次没产出的东西不列 —— 列出来
 * 它们只能是「—」，还得让人自己一行行扫过去排除，而这一页要回答的是「这次跑
 * 出了什么」。想看盘面全貌有「当前全部产物」那一档。
 *
 * 产物路径固定，所以「某次运行产出的文件」随时可能被后来的运行覆盖。两个状态
 * 分开报：快照是运行结束那一刻的事实，`changed` 是它和当前盘面的差异。
 */
/**
 * 这次运行写出的文件，和现在盘面上的样子差在哪。
 *
 * 产物路径是固定的，后续运行会往同一个文件上写，所以「现在的大小」和「那次运行
 * 结束时的大小」经常不一样 —— 但不一样不等于出了问题：
 *
 * - 变大：后续运行往里追加了。这是流水线的正常行为（默认就是追加），内容一条
 *   没丢，不该标红，否则每次接着跑都会看到一片红，红色也就没人当真了；
 * - 变小：后续运行清空重写过，那次那批内容确实不在了 —— 这才需要提醒；
 * - 一样但 mtime 变了：等量重写，内容量没变，按中性处理；
 * - 文件没了：真删了。
 */
function artifactDrift(item, now) {
  if (!now.exists) {
    return { kind: "bad", text: "已删除", note: "这次运行写出的文件，现在盘面上已经没有了。" };
  }
  if (item.isDir || now.isDir) {
    return { kind: "ok", text: "本次产出", note: "" };
  }
  const before = item.size || 0;
  const after = now.size || 0;
  if (after > before) {
    return {
      kind: "ok",
      text: "后续有追加",
      note: `这次运行写出 ${formatBytes(before)}，之后又有运行往里追加到 ${formatBytes(after)}。`
        + "追加是默认写入模式，两次的内容都在，不用处理。",
    };
  }
  if (after < before) {
    return {
      kind: "bad",
      text: "被覆盖变少",
      note: `这次运行写出 ${formatBytes(before)}，现在只剩 ${formatBytes(after)}。`
        + "后续运行应该是用了「覆盖重建」，这次那批内容已经不在了。",
    };
  }
  const rewritten = item.mtime && now.mtime && Math.abs(now.mtime - item.mtime) > 2;
  if (rewritten) {
    return {
      kind: "",
      text: "后续被重写",
      note: "文件被后续运行重写过，体积和这次一样，内容条数没变。",
    };
  }
  return { kind: "ok", text: "本次产出", note: "" };
}

function rowsForRun(run) {
  const rows = [];
  for (const item of run.artifacts || []) {
    if (!producedInRun(item, run)) continue;
    // /api/runs 已按 run.prefix 计算过当前盘面。不能从 state.artifacts 取：
    // state.artifacts 属于顶栏当前任务，切换任务后会给历史批次制造假告警。
    const now = {
      exists: Boolean(item.current),
      path: item.currentPath || item.path || "",
      size: item.currentSize,
      mtime: item.currentMtime,
      lines: item.currentLines,
      isDir: Boolean(item.currentIsDir),
    };
    const stillThere = Boolean(now.exists);
    const isDir = Boolean(item.isDir || now.isDir);
    const drift = artifactDrift(item, now);
    rows.push({
      key: item.key,
      label: item.label,
      path: item.path || now.path || "",
      prefix: run.prefix || state.prefix || defaultPrefixOf(),
      // 只有「这次确实写出过、并且现在还在盘面上」的行才给预览入口。
      previewable: stillThere && !isDir,
      statusKind: drift.kind,
      statusText: drift.text,
      size: item.isDir ? "目录" : formatBytes(item.size),
      lines: now.lines !== undefined && now.lines !== null ? now.lines.toLocaleString("en-US") : "—",
      mtime: item.mtime ? formatTime(item.mtime) : "—",
      note: drift.note,
    });
  }
  return rows;
}

function buildArtifactRow(row) {
  const tr = document.createElement("tr");
  const path = row.path ? relPath(row.path) : "";
  tr.innerHTML = `
    <td>
      <div>${escapeHtml(row.label)}</div>
      <div class="path" title="${escapeHtml(row.path)}">${escapeHtml(path || "—")}</div>
    </td>
    <td><span class="pill ${row.statusKind}"${row.note ? ` title="${escapeHtml(row.note)}"` : ""}>${escapeHtml(row.statusText)}</span></td>
    <td class="num">${escapeHtml(row.size)}</td>
    <td class="num">${escapeHtml(row.lines)}</td>
    <td>${escapeHtml(row.mtime)}</td>
    <td></td>`;
  if (row.previewable) {
    const button = document.createElement("button");
    button.className = "ghost small";
    button.textContent = "预览";
    if (row.note) button.title = row.note;
    button.addEventListener("click", () => previewArtifact(row.key, row.label, 0, row.prefix));
    tr.lastElementChild.appendChild(button);
  }
  return tr;
}

function renderArtifactTable() {
  const tbody = $("artifact-table").querySelector("tbody");
  const run = state.runs.find((item) => item.id === state.activeRun) || null;
  const when = run ? (run.startedAt || run.createdAt || 0) : 0;

  $("artifacts-title").textContent = run
    ? `${runDayLabel(when)} ${runClockLabel(when)} 的产物`
    : "当前全部产物";

  const rows = run ? rowsForRun(run) : rowsForCurrent();
  if (!rows.length) {
    // 这一档为空是有意义的结论（空跑或前面就失败了），得说清楚为什么，
    // 否则看着像页面坏了 —— 盘面上明明有文件，这里却一行都没有。
    tbody.innerHTML = `<tr><td colspan="6">${emptyHtml(
      run
        ? {
          icon: "file",
          title: "这次运行没有写出产物",
          desc: "产物按写入时间归属：只有这次运行期间写出的文件才列在这里。"
            + "如果这次是空跑、或者在前面就失败了，就会是空的。",
          variant: "inline",
        }
        : {
          icon: "file",
          title: "还没有产物文件",
          desc: "跑一次流水线后，这里会列出每个阶段写出的文件。",
          variant: "inline",
        }
    )}</td></tr>`;
    return true;
  }

  tbody.innerHTML = "";
  for (const row of rows) tbody.appendChild(buildArtifactRow(row));
  return true;
}

function selectRun(runId) {
  const next = runId || null;
  if ((state.activeRun || null) === next) return;
  state.activeRun = next;
  renderRunList();
  renderArtifactTable();
}

async function loadArtifacts() {
  let payload;
  try {
    payload = await api(`/api/artifacts?domain=${encodeURIComponent(state.domain)}&prefix=${encodeURIComponent(state.prefix)}`);
  } catch (error) {
    $("artifact-table").querySelector("tbody").innerHTML =
      `<tr><td colspan="6">${errorHtml(error.message, { title: "读不到产物清单" })}</td></tr>`;
    return false;
  }
  state.artifacts = payload.artifacts;
  $("artifact-dir").textContent = relPath(payload.outputDir);
  await loadTasks();

  // 批次和产物清单要一起刷新：右栏拿「运行结束时」的快照跟「现在」的盘面比对，
  // 两次请求之间文件被覆盖就会报错状态，所以宁可放在同一个流程里串起来。
  try {
    const runs = await api(`/api/runs?domain=${encodeURIComponent(state.domain)}&limit=40`);
    state.runs = runs.runs || [];
  } catch (_) {
    // 读不到运行记录仍然要能看产物，左栏退化成只有「现在」一项。
    state.runs = [];
  }
  if (state.activeRun && !state.runs.some((run) => run.id === state.activeRun)) {
    state.activeRun = null;   // 切领域后原来选中的批次已经不在了
  }
  renderRunList();
  renderArtifactTable();
  return true;
}

/** 拉当前项目的构建任务清单（任务目录 + 每个任务有多少文件、是否还在老位置）。 */
async function loadTasks() {
  try {
    const payload = await api(`/api/tasks?domain=${encodeURIComponent(state.domain)}`);
    state.tasks = payload.tasks || [];
    state.legacyTasks = payload.legacyTasks || [];
  } catch (_) {
    // 任务清单读不到不该挡住产物页：下面的表格照常显示。
    clearTaskList();
  }
  renderTaskBar();
  renderTaskOptions();
}

/** 产物页的任务条：一个任务一个 pill，点一下就切过去；老布局的文件给一键归位。 */
function renderTaskBar() {
  const bar = $("task-bar");
  if (!bar) return;
  const tasks = state.tasks || [];
  bar.hidden = !tasks.length;
  const pills = $("task-pills");
  pills.innerHTML = "";
  for (const task of tasks) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "task-pill";
    button.classList.toggle("active", task.name === state.prefix);
    button.title = [
      relPath(task.path),
      `${task.files} 个文件`,
      task.legacy ? "其中一部分还散在目录根部，可以一键归位" : "",
    ].filter(Boolean).join("\n");
    const name = document.createElement("span");
    name.className = "task-name";
    name.textContent = task.name;
    const count = document.createElement("span");
    count.className = "task-count";
    count.textContent = String(task.files);
    button.append(name, count);
    button.addEventListener("click", () => {
      if (task.name === state.prefix) return;
      setPrefix(task.name);
      commitPrefix();
    });
    pills.appendChild(button);
  }

  const migrate = $("btn-migrate-tasks");
  const pending = state.legacyTasks || [];
  migrate.hidden = !pending.length;
  if (pending.length) {
    migrate.textContent = `旧产物归位（${pending.length}）`;
    migrate.title = "把还散在目录根部的产物文件搬进各自的任务目录，搬完产物页就只剩任务文件夹了：\n"
      + pending.join("、");
  }
}

/** 把散在目录根部的产物文件搬进各自的任务目录。 */
async function migrateLegacyTasks() {
  const pending = (state.legacyTasks || []).slice();
  if (!pending.length) return;
  const button = $("btn-migrate-tasks");
  let result;
  try {
    result = await withBusy(button, "归位中…", () =>
      api("/api/tasks/migrate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ domain: state.domain, tasks: pending }),
      })
    );
  } catch (error) {
    toast(error.message, "err");
    return;
  }
  const moved = (result.moved || []).length;
  const skipped = (result.skipped || []).length;
  if (skipped) {
    // 目标位置已经有同名文件时不覆盖：把跳过的名单报出来，让用户自己决定留哪个。
    const head = (result.skipped || []).slice(0, 3).map((item) => item.to).join("、");
    const more = skipped > 3 ? " 等" : "";
    toast(`已归位 ${moved} 个文件；${skipped} 个因为目标位置已有同名文件而跳过：${head}${more}`, "warn");
  } else {
    toast(`已归位 ${moved} 个文件。`, "ok");
  }
  await loadArtifacts();
}

function closePreview() {
  $("preview-modal").hidden = true;
  $("preview-body").innerHTML = "";
  $("preview-meta").textContent = "";
}

async function previewArtifact(key, label, offset = 0, prefix = "") {
  // 批次视图里的产物属于那次运行的任务，可能跟顶栏当前选的任务不同，
  // 所以任务名由调用方带进来（不传则用当前项目的）。
  const usePrefix = prefix || state.prefix;
  $("preview-modal").hidden = false;
  $("preview-title").textContent = label;
  $("preview-meta").textContent = "加载中…";
  $("preview-body").innerHTML = loadingHtml({ block: true });

  let payload;
  try {
    payload = await api(`/api/artifacts/preview?key=${encodeURIComponent(key)}&domain=${encodeURIComponent(state.domain)}&prefix=${encodeURIComponent(usePrefix)}&offset=${offset}&limit=20`);
  } catch (error) {
    $("preview-body").innerHTML = emptyHtml({
      title: "打不开这个产物",
      desc: escapeHtml(error.message),
      icon: "alert",
      variant: "error inline",
    });
    return;
  }
  $("preview-meta").textContent = `${relPath(payload.path)}${payload.total !== undefined ? ` · 共 ${payload.total} 行` : ""}`;
  // 换文件或翻页后从顶部开始看，不然会停在上一份内容的位置。
  $("preview-body").scrollTop = 0;

  if (payload.kind === "directory") {
    $("preview-body").innerHTML = `<ul>${payload.entries.map((entry) =>
      `<li>${escapeHtml(entry.name)} ${entry.size ? formatBytes(entry.size) : ""}</li>`).join("")}</ul>`;
    return;
  }
  if (payload.kind === "text") {
    $("preview-body").innerHTML = `<pre class="sample">${escapeHtml(payload.text)}</pre>`;
    return;
  }

  const body = $("preview-body");
  body.innerHTML = "";
  if (!payload.rows.length) {
    body.innerHTML = emptyHtml({ icon: "file", title: "该位置没有数据", variant: "inline compact" });
    return;
  }
  for (const row of payload.rows) {
    const card = document.createElement("div");
    card.className = "sample";
    const content = row.data !== undefined
      ? JSON.stringify(row.data, null, 2)
      : (row.raw || "");
    card.innerHTML = `
      <div class="sample-head">
        <span>#${row.index}${row.truncated ? "（已截断）" : ""}</span>
        ${row.error ? `<span class="hint">${escapeHtml(row.error)}</span>` : ""}
      </div>
      <pre>${escapeHtml(content)}</pre>`;
    body.appendChild(card);
  }
  const nav = document.createElement("div");
  nav.className = "btn-row";
  nav.style.marginTop = "8px";
  if (offset > 0) {
    const prev = document.createElement("button");
    prev.className = "ghost small";
    prev.textContent = "上一页";
    prev.addEventListener("click", () => previewArtifact(key, label, Math.max(0, offset - 20), usePrefix));
    nav.appendChild(prev);
  }
  if (payload.total !== undefined && offset + 20 < payload.total) {
    const next = document.createElement("button");
    next.className = "ghost small";
    next.textContent = "下一页";
    next.addEventListener("click", () => previewArtifact(key, label, offset + 20, usePrefix));
    nav.appendChild(next);
  }
  body.appendChild(nav);
}

// ---------------------------------------------------------------------------
// seed 管理
// ---------------------------------------------------------------------------

async function loadSeeds() {
  const list = $("seed-list");
  list.innerHTML = loadingHtml({ block: true });
  let payload;
  try {
    payload = await api(`/api/seeds?domain=${encodeURIComponent(state.domain)}&limit=200`);
  } catch (error) {
    list.innerHTML = errorHtml(error.message, { title: "读不到 seed 数据" });
    return false;
  }
  $("seed-path").textContent = relPath(payload.path);
  state.seedEditable = payload.editable;

  if (!payload.exists) {
    // 提示条和「全部展开/收起」按钮描述的也是上一份文件：不在这里清掉，
    // 切到还没建 seeds.jsonl 的领域后会继续显示上个领域的条数和一个空按钮。
    $("seed-hint").textContent = "尚未创建";
    state.seedRows = [];
    list.innerHTML = emptyHtml({
      icon: "file",
      title: "这个领域还没有 seeds.jsonl",
      desc: "种子数据是整条流水线的起点。点<b>新增一条</b>开始写，第一次保存时会自动建文件。",
      actions: '<button class="ghost small" data-click-target="btn-seed-add">新增一条</button>',
    });
    updateSeedToggle();
    return true;
  }
  $("seed-hint").textContent = payload.editable
    ? `共 ${payload.total} 条，可直接编辑（保存前自动备份）。`
    : payload.editableHint;

  state.seedRows = payload.rows.map((row) => ({
    raw: JSON.stringify(row.data, null, 2),
    error: null,
  }));
  renderSeeds();
  return true;
}

/** 超过这个条数就默认收起正文 —— 否则十几条 seed 摊开就是十几屏。 */
const SEED_AUTO_COLLAPSE = 6;

/** 从 seed 的 JSON 文本里挑几个字段做摘要；解析不出来就返回 null。 */
function seedSummary(raw) {
  let data;
  try {
    data = JSON.parse(raw);
  } catch (_) {
    return null;
  }
  if (!data || typeof data !== "object" || Array.isArray(data)) return null;
  const text = (key) => (typeof data[key] === "string" ? data[key].trim() : "");
  return {
    id: text("id"),
    intent: text("expected_intent") || text("intent"),
    scenario: text("scenario"),
    preview: text("user_query") || text("query") || text("question") || text("instruction"),
  };
}

/** 文本框按内容撑开：内容多高就多高，不出现内部滚动条。 */
function fitSeedEditor(textarea) {
  textarea.style.height = "auto";
  const height = textarea.scrollHeight;
  // 收起状态下 scrollHeight 是 0，这时别把高度写死，展开后会再算一次。
  if (height > 0) textarea.style.height = `${height}px`;
}

function renderSeeds() {
  const list = $("seed-list");
  const rows = state.seedRows;
  list.innerHTML = "";

  // 首次渲染按条数决定默认展开还是收起；用户手动切过的条目不再被覆盖。
  for (const row of rows) {
    if (row.open === undefined) row.open = rows.length <= SEED_AUTO_COLLAPSE;
  }

  rows.forEach((row, index) => {
    const node = renderSeedRow(row, index);
    list.appendChild(node);
    // 必须等节点进了 DOM 才能量高度，否则 scrollHeight 是 0。
    if (row.open) fitSeedEditor(node.querySelector("textarea"));
  });
  updateSeedToggle();
}

/** 头部按钮文字跟着整体状态走。 */
function updateSeedToggle() {
  const button = $("btn-seed-toggle");
  const rows = state.seedRows;
  if (!rows.length) {
    button.hidden = true;
    return;
  }
  button.hidden = false;
  button.textContent = rows.every((row) => row.open) ? "全部收起" : "全部展开";
}

function renderSeedRow(row, index) {
  const node = document.createElement("div");
  node.className = "seed-row";
  if (row.error) node.classList.add("invalid");
  if (!row.open) node.classList.add("collapsed");

  node.innerHTML = `
    <div class="seed-head" title="${row.open ? "点击收起" : "点击展开"}">
      <span class="seed-index">#${index + 1}</span>
      <span class="seed-id"></span>
      <span class="tag seed-tag" data-tag="intent" hidden></span>
      <span class="tag seed-tag" data-tag="scenario" hidden></span>
      <span class="seed-state"></span>
      <div class="btn-row">
        <button class="ghost small" data-act="dup">复制</button>
        <button class="danger small" data-act="del">删除</button>
      </div>
    </div>
    <p class="seed-preview" hidden></p>
    <div class="seed-code">
      <div class="seed-highlight"></div>
      <textarea spellcheck="false"></textarea>
    </div>`;

  const head = node.querySelector(".seed-head");
  const idNode = node.querySelector(".seed-id");
  const previewNode = node.querySelector(".seed-preview");
  const stateNode = node.querySelector(".seed-state");
  const tags = {
    intent: node.querySelector('[data-tag="intent"]'),
    scenario: node.querySelector('[data-tag="scenario"]'),
  };

  /** 摘要跟着正文实时更新，但只改文本不重建 DOM，否则输入框会丢焦点。 */
  const paint = () => {
    const info = seedSummary(row.raw);
    idNode.textContent = (info && info.id) || "（无 id）";
    for (const [key, element] of Object.entries(tags)) {
      const value = (info && info[key]) || "";
      element.textContent = value;
      element.hidden = !value;
    }
    const preview = (info && info.preview) || "";
    previewNode.textContent = preview;
    previewNode.hidden = !preview;
    stateNode.textContent = row.error ? "JSON 非法" : "JSON 合法";
    stateNode.className = `seed-state ${row.error ? "bad" : "ok"}`;
    stateNode.title = row.error || "";
  };
  paint();

  const textarea = node.querySelector("textarea");
  const highlight = node.querySelector(".seed-highlight");
  const paintCode = () => {
    const html = highlightJson(textarea.value);
    highlight.innerHTML = textarea.value.endsWith("\n") ? `${html}\n` : html;
  };

  textarea.value = row.raw;
  textarea.disabled = !state.seedEditable;
  if (row.open) paintCode();

  textarea.addEventListener("input", () => {
    row.raw = textarea.value;
    try {
      JSON.parse(textarea.value);
      row.error = null;
    } catch (error) {
      row.error = `JSON 非法：${error.message}`;
    }
    node.classList.toggle("invalid", !!row.error);
    paint();
    paintCode();
    fitSeedEditor(textarea);
  });

  // 点头部空白处折叠/展开；点按钮不算。
  head.addEventListener("click", (event) => {
    if (event.target.closest("button")) return;
    row.open = !row.open;
    node.classList.toggle("collapsed", !row.open);
    head.title = row.open ? "点击收起" : "点击展开";
    if (row.open) {
      paintCode();
      fitSeedEditor(textarea);
    }
    updateSeedToggle();
  });

  node.querySelector('[data-act="del"]').addEventListener("click", () => {
    if (!confirm(`删除第 ${index + 1} 条？`)) return;
    state.seedRows.splice(index, 1);
    renderSeeds();
  });
  node.querySelector('[data-act="dup"]').addEventListener("click", () => {
    state.seedRows.splice(index + 1, 0, { raw: row.raw, error: row.error, open: row.open });
    renderSeeds();
  });

  return node;
}

function addSeedRow() {
  // 新加的这条固定展开：加它就是为了马上编辑它。
  state.seedRows.push({
    raw: JSON.stringify({ id: "", user_query: "", expected_intent: "" }, null, 2),
    error: null,
    open: true,
  });
  renderSeeds();
}

async function saveSeeds() {
  if (!state.seedEditable) { toast("当前文件过大，禁止整体覆盖。", "err"); return; }
  const rows = [];
  for (let index = 0; index < state.seedRows.length; index += 1) {
    const row = state.seedRows[index];
    try {
      rows.push(JSON.parse(row.raw));
    } catch (error) {
      toast(`第 ${index + 1} 条 JSON 非法，未保存。`, "err");
      return;
    }
  }
  if (!confirm(`将覆盖 ${state.domain} 的 seeds.jsonl（共 ${rows.length} 条），原文件会自动备份。继续？`)) return;
  try {
    const result = await api("/api/seeds", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ domain: state.domain, rows }),
    });
    toast(`已保存 ${result.count} 条${result.backup ? `，备份：${result.backup}` : ""}`, "ok");
    await loadStages();
  } catch (error) {
    toast(error.message, "err");
  }
}

// ---------------------------------------------------------------------------
// 统计
// ---------------------------------------------------------------------------

async function loadStats() {
  const body = $("stats-body");
  body.innerHTML = loadingHtml({ rows: 4 });
  let payload;
  try {
    payload = await api(`/api/stats?domain=${encodeURIComponent(state.domain)}&prefix=${encodeURIComponent(state.prefix)}`);
  } catch (error) {
    body.innerHTML = errorHtml(error.message, { title: "读不到统计报告" });
    return false;
  }
  $("stats-hint").textContent = payload.jsonExists ? relPath(payload.jsonPath) : "尚未生成 JSON 统计";
  if (!payload.jsonExists) {
    $("btn-stats-expand").hidden = true;
    body.innerHTML = emptyHtml({
      icon: "chart",
      title: "还没有生成统计报告",
      desc: "点<b>重新生成统计</b>会跑阶段 10（数据统计报告），只统计当前已经存在的文件，不会重新生成数据。",
      actions: '<button class="ghost small" data-click-target="btn-stats-run">重新生成统计</button>',
    });
    return true;
  }
  if (payload.error) {
    $("btn-stats-expand").hidden = true;
    body.innerHTML = errorHtml(payload.error, { title: "统计阶段报错了" });
    return true;
  }
  renderStats(payload.report || []);
  return true;
}

/** base_stats() 固定产出的字段，永远算「基础」，不参与 0 值收纳。 */
const STATS_BASE_KEYS = ["rows", "ids", "unique_ids", "missing_id", "duplicate_ids"];
/** 字段分布默认只画前几条，其余点开看。 */
const STATS_BAR_PREVIEW = 5;
/** 统计页的展开状态；默认全部收起，用户点开的记在这里。 */
const statsUi = { expanded: new Set(), zeroOpen: new Set(), barsOpen: new Set() };

function resetStatsUi() {
  statsUi.expanded.clear();
  statsUi.zeroOpen.clear();
  statsUi.barsOpen.clear();
}

/**
 * 统计报告整页铺开有十几屏，其中近四成数字是 0。
 * 这里按「值的形状」重新组织：0 值收进一行、xxx 与 xxx_pct 并成一格、
 * 长度统计压成一行、分布默认只画前几条；卡片本身默认收起。
 * 顶部再压一排 KPI，让「一共多少数据、几个文件要处理」在首屏就能读到。
 */
function renderStats(files) {
  const body = $("stats-body");
  body.innerHTML = "";
  if (!Array.isArray(files) || !files.length) {
    $("btn-stats-expand").hidden = true;
    body.innerHTML = emptyHtml({
      icon: "chart",
      title: "这份统计里没有任何分组",
      desc: "统计文件已经生成，但里面没有可展示的内容 —— 通常是上游还没有产出数据文件。",
      variant: "inline",
    });
    return;
  }

  // merged / dedup 这类前后内容完全一致的产物，只留一份说明。
  const firstOf = new Map();
  const duplicates = new Map();
  for (const file of files) {
    const fingerprint = JSON.stringify(file.stats || {});
    if (firstOf.has(fingerprint)) duplicates.set(file.path, firstOf.get(fingerprint));
    else firstOf.set(fingerprint, String(file.path).split("/").pop());
  }

  const parsed = files.map((file, index) => ({ file, index, split: splitStats(file.stats || {}) }));
  $("btn-stats-expand").hidden = false;
  body.appendChild(renderStatsKpi(parsed));
  body.appendChild(renderStatsOverview(parsed, duplicates));
  for (const item of parsed) body.appendChild(renderStatsCard(item, duplicates));
  updateStatsExpandLabel();
}

/**
 * id 健康度的唯一判据，KPI / 总览 / 卡片三处共用，免得三份判断各写一遍走岔。
 * unchecked = 字段不全，没资格下结论；empty = 统计过但 0 行；bad / ok = 真有数据。
 */
function statsStatus(split) {
  const cellOf = (key) => split.base.find((cell) => cell.key === key);
  const rowsCell = cellOf("rows");
  const checked = Boolean(rowsCell && cellOf("missing_id") && cellOf("duplicate_ids"));
  const bad = ["missing_id", "duplicate_ids"].reduce((sum, key) => {
    const cell = cellOf(key);
    return sum + (cell ? cell.value : 0);
  }, 0);
  if (!checked) return { state: "unchecked", bad: 0, rows: rowsCell ? rowsCell.value : null };
  if (!rowsCell.value) return { state: "empty", bad: 0, rows: 0 };
  return { state: bad > 0 ? "bad" : "ok", bad, rows: rowsCell.value };
}

/** 顶部 KPI 条：整份报告最该先看到的几个数，放大到 26px 等宽数字。 */
function renderStatsKpi(parsed) {
  const sum = (pick) => parsed.reduce((total, item) => total + pick(item), 0);
  const attention = parsed.filter((item) => statsStatus(item.split).state === "bad").length;
  const cards = [
    { label: "文件", value: parsed.length, tone: 1 },
    { label: "总行数", value: sum((item) => item.file.json_rows || 0), tone: 3 },
    { label: "长度分布组", value: sum((item) => item.split.lengths.length), tone: 4 },
    { label: "字段分布组", value: sum((item) => item.split.maps.length), tone: 2 },
    {
      label: "待关注文件",
      value: attention,
      tone: 5,
      alert: attention > 0,
      sub: attention ? "存在 id 异常" : "id 全部正常",
    },
  ];
  const wrap = document.createElement("div");
  wrap.className = "stats-kpi";
  wrap.innerHTML = cards
    .map(
      (card) => `<div class="kpi-card${card.alert ? " is-alert" : ""}" style="--tone: var(--tone-${card.tone})">
      <span class="kpi-label">${card.label}</span>
      <span class="kpi-value">${formatNumber(card.value)}</span>
      <span class="kpi-sub">${card.sub || ""}</span>
    </div>`
    )
    .join("");
  return wrap;
}

/**
 * 只按数据结构分类（数字 / 长度统计 / 计数映射），不猜字段名的业务含义，
 * 后端加字段时这里不用跟着改。
 */
function splitStats(stats) {
  const lengths = [];
  const maps = [];
  const others = [];
  const numbers = new Map();

  for (const [key, value] of Object.entries(stats)) {
    if (typeof value === "number") numbers.set(key, value);
    else if (isLengthStats(value)) lengths.push([key, value]);
    else if (isCountMap(value)) maps.push([key, value]);
    else others.push([key, value]);
  }

  // xxx_pct 并回 xxx 那一格，免得「4」和「100」分开占两个位置。
  const cells = [];
  const zeroKeys = [];
  const consumed = new Set();
  for (const [key, value] of numbers) {
    if (consumed.has(key)) continue;
    consumed.add(key);
    // 先跳过 pct 键，等它的主键来收编；主键不存在时才当普通数字。
    if (key.endsWith("_pct") && numbers.has(key.slice(0, -4))) continue;
    const pctKey = `${key}_pct`;
    const pct = numbers.has(pctKey) ? numbers.get(pctKey) : null;
    if (pct !== null) consumed.add(pctKey);
    // 基础字段常驻可见：0 行的空产物也要能一眼看到「rows 0」，不参与 0 值收纳。
    if (value === 0 && !pct && !STATS_BASE_KEYS.includes(key)) zeroKeys.push(key);
    else cells.push({ key, value, pct });
  }

  return {
    base: cells.filter((cell) => STATS_BASE_KEYS.includes(cell.key)),
    quality: cells.filter((cell) => !STATS_BASE_KEYS.includes(cell.key)),
    zeroKeys,
    lengths,
    maps,
    others,
  };
}

function renderStatsOverview(parsed, duplicates) {
  const wrap = document.createElement("div");
  wrap.className = "stats-overview";
  wrap.innerHTML = `<div class="ov-head">
      <span>总览</span>
      <span class="hint">${parsed.length} 个文件 · 点一行跳到对应卡片</span>
    </div>`;
  // 行数条按最大值归一化：谁大谁小扫一眼就知道，不用逐个读数字。
  const maxRows = Math.max(...parsed.map((item) => item.file.json_rows || 0), 1);
  for (const { file, index, split } of parsed) {
    const name = String(file.path || "").split("/").pop();
    // id 结论只在「确实统计过」时才给：缺字段或 0 行都不能说「id 唯一」，那是假阳性。
    const status = statsStatus(split);
    const idHtml = {
      bad: `<span class="bad">id 异常 ${formatNumber(status.bad)}</span>`,
      ok: `<span class="ok">id 唯一</span>`,
      empty: `<span class="na">无数据</span>`,
      unchecked: `<span class="na">无 id 统计</span>`,
    }[status.state];
    const dot = status.state === "bad" ? "bad" : status.state === "ok" ? "ok" : "na";
    const counts = [
      split.lengths.length ? `${split.lengths.length} 长度` : "",
      split.maps.length ? `${split.maps.length} 分布` : "",
      split.zeroKeys.length ? `${split.zeroKeys.length} 项为 0` : "",
    ].filter(Boolean).join(" · ");
    const rows = file.json_rows ?? 0;
    const row = document.createElement("button");
    row.type = "button";
    row.className = "ov-row";
    row.innerHTML = `
      <span class="ov-dot ${dot}"></span>
      <span class="ov-name">${escapeHtml(name)}</span>
      <span class="ov-rows"><span class="ov-meter" style="width:${Math.round((rows / maxRows) * 100)}%"></span><b>${formatNumber(rows)}</b> 行</span>
      <span class="ov-id">${idHtml}</span>
      <span class="ov-counts">${counts || `<span class="na">无长度/分布</span>`}</span>
      ${duplicates.has(file.path) ? `<span class="ov-dup">与 ${escapeHtml(duplicates.get(file.path))} 相同</span>` : "<span></span>"}`;
    row.addEventListener("click", () => {
      const card = document.querySelector(`.file-card[data-stats-index="${index}"]`);
      if (!card) return;
      toggleStatsCard(card, true);
      card.scrollIntoView({ behavior: "smooth", block: "start" });
    });
    wrap.appendChild(row);
  }
  return wrap;
}

function renderStatsCard({ file, index, split }, duplicates) {
  const key = file.path;
  const name = String(file.path || "").split("/").pop();
  const card = document.createElement("div");
  card.className = "file-card";
  card.dataset.statsKey = key;
  card.dataset.statsIndex = String(index);
  // 左侧状态条：不展开就知道这个文件要不要管。报告页同类卡片没有 data-status，走默认灰。
  const state = statsStatus(split).state;
  card.dataset.status = state === "bad" ? "bad" : state === "ok" ? "ok" : "na";
  if (statsUi.expanded.has(key)) card.classList.add("expanded");

  const summary = [
    `${formatNumber(file.json_rows ?? 0)} 行`,
    `${split.base.length + split.quality.length} 项有值`,
    split.zeroKeys.length ? `${split.zeroKeys.length} 项为 0` : "",
    split.lengths.length ? `${split.lengths.length} 组长度` : "",
    split.maps.length ? `${split.maps.length} 组分布` : "",
  ].filter(Boolean).join(" · ");

  const header = document.createElement("header");
  header.innerHTML = `
    <span class="fc-caret">▸</span>
    <span class="fc-index">${index + 1}</span>
    <span class="fc-name">${escapeHtml(name)}</span>
    ${duplicates.has(key) ? `<span class="ov-dup">与 ${escapeHtml(duplicates.get(key))} 相同</span>` : ""}
    <span class="hint fc-summary">${summary}</span>`;
  header.addEventListener("click", () => toggleStatsCard(card));
  card.appendChild(header);

  const body = document.createElement("div");
  body.className = "body";
  if (split.base.length) body.appendChild(statGroup("基础", split.base.map(statCell).join(""), "stat-grid", 1));
  if (split.quality.length) body.appendChild(statGroup("质量与规模", split.quality.map(statCell).join(""), "stat-grid", 2));
  if (split.zeroKeys.length) body.appendChild(renderZeroFold(key, split.zeroKeys));
  if (split.lengths.length) body.appendChild(renderLengthGroup(split.lengths));
  if (split.maps.length) {
    const group = statGroup("字段分布", "", "dist-list", 4);
    const list = group.querySelector(".stat-group-body");
    // 每个分布换一种色板色，整页滚动时有色彩节奏；图内部仍只用一色，不至于花。
    split.maps.forEach(([distKey, distValue], position) => {
      list.appendChild(renderDistribution(key, distKey, distValue, (position % 6) + 1));
    });
    body.appendChild(group);
  }
  if (split.others.length) {
    const pre = document.createElement("pre");
    pre.className = "sample";
    pre.textContent = JSON.stringify(Object.fromEntries(split.others), null, 2);
    body.appendChild(pre);
  }
  card.appendChild(body);
  return card;
}

/** 长度分布整组：同表共用一个刻度（所有 max 里的最大值），行与行之间才能直接比长短。 */
function renderLengthGroup(lengths) {
  const scale = Math.max(...lengths.map(([, value]) => value.max || 0), 1);
  const group = statGroup("长度分布", "", "len-list", 3);
  const list = group.querySelector(".stat-group-body");
  for (const entry of lengths) list.appendChild(lengthRow(entry, scale));
  return group;
}

function toggleStatsCard(card, expanded) {
  const key = card.dataset.statsKey;
  const next = expanded === undefined ? !card.classList.contains("expanded") : expanded;
  card.classList.toggle("expanded", next);
  if (next) statsUi.expanded.add(key); else statsUi.expanded.delete(key);
  updateStatsExpandLabel();
}

function updateStatsExpandLabel() {
  const button = $("btn-stats-expand");
  if (!button || button.hidden) return;
  const cards = [...document.querySelectorAll("#stats-body .file-card")];
  const allOpen = cards.length > 0 && cards.every((card) => card.classList.contains("expanded"));
  button.textContent = allOpen ? "全部收起" : "全部展开";
}

function statGroup(title, innerHtml, bodyClass, tone) {
  const node = document.createElement("div");
  node.className = "stat-group";
  // 分组色向下传给组内的指标格底线与标题小竖条（CSS 里的 var(--tone)）。
  if (tone) node.style.setProperty("--tone", `var(--tone-${tone})`);
  node.innerHTML = `<div class="stat-group-title">${escapeHtml(title)}</div>
    <div class="stat-group-body ${bodyClass}">${innerHtml}</div>`;
  return node;
}

function statCell(cell) {
  const pct = cell.pct === null || cell.pct === undefined ? "" : ` <span class="pct">${formatPercent(cell.pct)}%</span>`;
  return `<div class="stat-cell${cell.value === 0 ? " is-zero" : ""}" title="${escapeHtml(cell.key)}">
      <span class="k">${escapeHtml(cell.key)}</span>
      <span class="v">${formatMetric(cell.value)}${pct}</span>
    </div>`;
}

/**
 * 长度统计一行：文字摘要 + 一条 min→max 区间条（p50 打竖线）。
 * scale 由同组所有 max 取最大值，所以行与行之间能直接比长短，而不是各画各的。
 */
function lengthRow([key, value], scale) {
  const at = (number) => Math.min(Math.max((number / scale) * 100, 0), 100);
  const left = at(value.min || 0);
  // 至少留一点宽度：min === max 时（比如全是固定长度的字段）否则会完全看不见。
  const width = Math.max(at(value.max || 0) - left, 1.5);
  const p50 = value.p50 === undefined ? null : at(value.p50);
  const full = ["count", "min", "max", "avg", "p50", "p90", "p95", "p99"]
    .filter((field) => value[field] !== undefined)
    .map((field) => `${field} ${formatMetric(value[field])}`)
    .join(" · ");
  const parts = [
    `<b>n=${formatMetric(value.count)}</b>`,
    `<span>${formatMetric(value.min)}–${formatMetric(value.max)}</span>`,
    `<span>均值 ${formatMetric(value.avg)}</span>`,
    `<span>p50 ${formatMetric(value.p50)}</span>`,
  ];
  if (value.p95 !== undefined) parts.push(`<span>p95 ${formatMetric(value.p95)}</span>`);

  const row = document.createElement("div");
  row.className = "len-row";
  row.title = full;
  row.innerHTML = `
      <span class="len-name">${escapeHtml(key)}</span>
      <span class="len-summary">${parts.join(" ")}</span>
      <span class="range-track"><span class="range-fill" style="left:${left}%;width:${width}%"></span>${
        p50 === null ? "" : `<span class="range-mark" style="left:${p50}%"></span>`}</span>`;
  return row;
}

function renderZeroFold(key, zeroKeys) {
  const open = statsUi.zeroOpen.has(key);
  const node = document.createElement("div");
  node.className = "zero-fold";
  node.innerHTML = `<button class="zero-toggle" type="button">
      <span class="zero-mark">✓</span>
      其余 ${zeroKeys.length} 项均为 0
      <span class="zero-caret">${open ? "▾" : "▸"}</span>
    </button>
    <div class="zero-keys"${open ? "" : " hidden"}>${zeroKeys.map((item) => `<code>${escapeHtml(item)}</code>`).join("")}</div>`;
  node.querySelector(".zero-toggle").addEventListener("click", () => {
    if (statsUi.zeroOpen.has(key)) statsUi.zeroOpen.delete(key);
    else statsUi.zeroOpen.add(key);
    const keys = node.querySelector(".zero-keys");
    keys.hidden = !keys.hidden;
    node.querySelector(".zero-caret").textContent = keys.hidden ? "▸" : "▾";
  });
  return node;
}

function formatMetric(value) {
  if (typeof value !== "number") return String(value);
  return Number.isInteger(value) ? value.toLocaleString("en-US") : value.toFixed(1);
}

function formatPercent(value) {
  if (typeof value !== "number") return String(value);
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}

function isLengthStats(value) {
  return value && typeof value === "object" && !Array.isArray(value)
    && "count" in value && "avg" in value && "p50" in value;
}

function isCountMap(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const entries = Object.entries(value);
  if (!entries.length || entries.length > 60) return false;
  return entries.every(([, item]) => typeof item === "number");
}

function renderDistribution(cardKey, distKey, value, tone) {
  const stateKey = `${cardKey}::${distKey}`;
  const open = statsUi.barsOpen.has(stateKey);
  const entries = Object.entries(value).sort((a, b) => b[1] - a[1]);
  // 刻度用「占全部的百分比」，不用「占最大值的百分比」：
  // 4 个类别各 1 条时，按最大值归一化会把 4 条全画成满格，看着像"全选满了"，
  // 实际什么也没表达；按占比则是各 25%，既画得出来也读得出来。
  // 报告页的分布图（reportBars）用的是同一套绝对刻度，两边必须一致。
  const total = entries.reduce((sum, [, count]) => sum + count, 0) || 1;

  const wrapper = document.createElement("div");
  wrapper.className = "dist";
  // 色板色由调用方轮转分配（CSS 的 .dist[data-tone]），图内部仍只用一色。
  if (tone) wrapper.dataset.tone = String(tone);
  wrapper.innerHTML = `<h4>${escapeHtml(distKey)}<span class="hint">${entries.length} 项 · 合计 ${formatNumber(total)}</span></h4>
    <div class="bar-list"></div>`;
  const list = wrapper.querySelector(".bar-list");

  // 只重建条形列表本身，不重建整张卡片，展开时视口不会跳。
  const fill = (expanded) => {
    list.innerHTML = "";
    const shown = expanded ? entries : entries.slice(0, STATS_BAR_PREVIEW);
    for (const [name, count] of shown) {
      const pct = (count / total) * 100;
      const row = document.createElement("div");
      row.className = "bar-row";
      row.innerHTML = `
        <span class="bar-label" title="${escapeHtml(name)}">${escapeHtml(name)}</span>
        <span class="bar-track"><span class="bar-fill${count > 0 ? " is-nonzero" : ""}" style="width:${pct}%"></span></span>
        <span class="bar-value">${count.toLocaleString("en-US")}<span class="bar-pct">${formatPercent(pct)}%</span></span>`;
      list.appendChild(row);
    }
  };
  fill(open);

  if (entries.length > STATS_BAR_PREVIEW) {
    const more = document.createElement("button");
    more.type = "button";
    more.className = "ghost small dist-more";
    more.textContent = open ? "收起" : `展开全部 ${entries.length} 项`;
    more.addEventListener("click", () => {
      const next = !statsUi.barsOpen.has(stateKey);
      if (next) statsUi.barsOpen.add(stateKey); else statsUi.barsOpen.delete(stateKey);
      fill(next);
      more.textContent = next ? "收起" : `展开全部 ${entries.length} 项`;
    });
    wrapper.appendChild(more);
  }
  return wrapper;
}

async function runDataStats(format, returnView) {
  if (state.currentJob) { toast("已有作业在运行。", "err"); return; }
  const body = {
    stages: ["data_stats"],
    domain: state.domain,
    prefix: state.prefix,
    params: { data_stats: { format } },
    force: true,
  };
  try {
    const job = await api("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    // 跑完自动切回发起页并刷新，用户不用手动点刷新。
    state.returnView = returnView;
    toast(`统计作业已启动（${job.id}），完成后会自动回到本页。`, "ok");
    document.querySelector('#tabs button[data-view="run"]').click();
    attachJob(job);
  } catch (error) {
    toast(error.message, "err");
  }
}

// ---------------------------------------------------------------------------
// 报告
//
// markdown 是导出格式，界面不直接铺 markdown：报告里近五十张表都带一列
// ASCII 进度条（████░░░░），铺出来又乱又长。这里把 markdown 解析成结构化
// 块，按内容重新渲染成 HTML —— 进度条换成真条形、级别换成彩色标签、每个
// 文件一张可折叠卡片，原文件仍可原样导出。
// ---------------------------------------------------------------------------

const reportUi = {
  text: "",               // 原始 markdown，导出时原样下载
  path: "",
  truncated: false,        // 超过预览上限（20 万字符）时为 true，复制会缺尾部
  expanded: new Set(),     // 展开的章节（按章节标题）
  barsOpen: new Set(),     // 展开了全部条目的分布表
};

function resetReportUi() {
  reportUi.expanded.clear();
  reportUi.barsOpen.clear();
}

async function loadReport() {
  const body = $("report-body");
  body.innerHTML = loadingHtml({ rows: 5 });
  try {
    const payload = await api(`/api/artifacts/preview?key=data_stats_md&domain=${encodeURIComponent(state.domain)}&prefix=${encodeURIComponent(state.prefix)}`);
    $("report-path").textContent = relPath(payload.path);
    reportUi.text = payload.text || "";
    reportUi.path = payload.path || "";
    reportUi.truncated = Boolean(payload.truncated);
    renderReport(reportUi.text);
  } catch (error) {
    $("report-path").textContent = "";
    reportUi.text = "";
    reportUi.path = "";
    reportUi.truncated = false;
    $("btn-report-export").hidden = true;
    $("btn-report-copy").hidden = true;
    $("btn-report-expand").hidden = true;
    // 「报告还没生成」是正常状态，不是故障：接口用 404 表示文件不存在。
    // 当成失败上报，切到新领域就会被强行弹到报告页并弹一个红提示，看起来像出错。
    if (error.status === 404) {
      body.innerHTML = emptyHtml({
        icon: "chart",
        title: "该领域还没有统计报告",
        desc: "到<b>运行</b>页跑一次阶段 10（数据统计报告），生成的 markdown 会显示在这里。",
        actions: '<button class="ghost small" data-click-target="btn-report-run">生成报告</button>',
      });
      return true;
    }
    body.innerHTML = emptyHtml({
      icon: "alert",
      title: "报告读取失败",
      desc: escapeHtml(error.message),
      variant: "error inline",
    });
    return false;
  }
  return true;
}

/** 导出原始 markdown：走后端下载，文件名由服务端定。
 *
 * 注意：VS Code 内置浏览器会直接中断下载（实测 net::ERR_ABORTED），普通浏览器
 * 正常。所以文案只说「已请求下载」，不谎报已保存，旁边另有「复制 markdown」兜底。
 */
function exportReportMarkdown() {
  if (!reportUi.text) { toast("还没有报告可以导出。", "err"); return; }
  // 任务名本身就带了项目名（health_talent_train），别再拼一次 state.domain。
  const name = `${state.prefix}_data_stats.md`;
  const url = `/api/artifacts/download?key=data_stats_md`
    + `&domain=${encodeURIComponent(state.domain)}&prefix=${encodeURIComponent(state.prefix)}`;
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  toast(`已请求下载 ${name}；若浏览器拦下了，请用「复制 markdown」。`, "ok");
}

/** 复制原始 markdown；下载被拦时这是唯一能拿走报告的通道。 */
async function copyReportMarkdown() {
  if (!reportUi.text) { toast("还没有报告可以复制。", "err"); return; }
  const ok = await writeClipboard(reportUi.text);
  if (!ok) { toast("复制失败，可直接打开报告文件。", "err"); return; }
  if (reportUi.truncated) {
    // 不能默默给半份：预览有上限，完整内容只能下载。
    toast("报告过大，复制的只是前面一段；完整内容请用「导出 markdown」。", "err");
    return;
  }
  toast(`已复制 markdown，共 ${reportUi.text.length.toLocaleString("en-US")} 字符。`, "ok");
}

/** 剪贴板：优先用异步 API，被拒（文档未聚焦等）时退回 execCommand。 */
async function writeClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (_) { /* 继续走旧接口 */ }
  try {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.top = "-1000px";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    return ok;
  } catch (_) {
    return false;
  }
}

/** 行内 markdown：行内代码与粗体。 */
function markdownInline(value) {
  return escapeHtml(value)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}

/** 把报告 markdown 拆成块：标题 / 表格 / 列表 / 段落 / 分隔线。 */
function parseReportBlocks(text) {
  const lines = String(text).split("\n");
  const blocks = [];
  let list = null;
  let table = null;
  const flushList = () => { if (list) { blocks.push(list); list = null; } };
  const flushTable = () => { if (table) { blocks.push(table); table = null; } };

  for (const line of lines) {
    const trimmed = line.trim();
    if (trimmed.startsWith("|")) {
      flushList();
      if (!table) table = { kind: "table", rows: [] };
      // 分隔行（| --- | --- |）不是数据。
      if (!/^\|[\s:|-]+\|$/.test(trimmed)) {
        table.rows.push(trimmed.replace(/^\||\|$/g, "").split("|").map((cell) => cell.trim()));
      }
      continue;
    }
    flushTable();

    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      flushList();
      blocks.push({ kind: "h", level: heading[1].length, text: heading[2].trim() });
      continue;
    }
    if (/^\s*[-*]\s+/.test(line)) {
      if (!list) list = { kind: "list", items: [] };
      list.items.push(line.replace(/^\s*[-*]\s+/, ""));
      continue;
    }
    flushList();
    if (/^(---|\*\*\*)$/.test(trimmed)) { blocks.push({ kind: "hr" }); continue; }
    if (!trimmed) continue;
    blocks.push({ kind: "p", text: trimmed });
  }
  flushList();
  flushTable();
  return blocks;
}

/** 按表头认表格类型；认不出就当普通表格原样渲染。 */
function classifyReportTable(rows) {
  const head = rows[0] || [];
  const has = (name) => head.includes(name);
  if (has("文件") && has("类型")) return "summary";
  if (has("级别") && has("问题")) return "issues";
  if (has("count") && has("p50")) return "length";
  if (has("分布")) return "bars";
  return "raw";
}

/** 报告正文渲染：标题 + 文件总览 + 章节导航 + 每文件一张折叠卡片。 */
function renderReport(text) {
  const body = $("report-body");
  body.innerHTML = "";
  const blocks = parseReportBlocks(text);
  if (!blocks.length) {
    $("btn-report-export").hidden = true;
    $("btn-report-copy").hidden = true;
    $("btn-report-expand").hidden = true;
    body.innerHTML = emptyHtml({
      icon: "chart",
      title: "报告是空的",
      desc: "markdown 已读到，但没有解析出任何分块。多数情况下意味着上游数据为空。",
      variant: "inline",
    });
    return;
  }

  let title = "";
  const head = [];
  const sections = [];
  let current = head;
  for (const block of blocks) {
    if (block.kind === "h" && block.level === 1) { title = block.text; continue; }
    if (block.kind === "h" && block.level === 2) {
      const section = { title: block.text, blocks: [] };
      sections.push(section);
      current = section.blocks;
      continue;
    }
    current.push(block);
  }

  if (title) {
    const node = document.createElement("div");
    node.className = "report-title";
    node.textContent = title;
    body.appendChild(node);
  }
  for (const block of head) {
    const node = renderReportBlock(block, "head", head.indexOf(block));
    if (node) body.appendChild(node);
  }
  if (sections.length) body.appendChild(renderReportToc(sections));
  sections.forEach((section, index) => body.appendChild(renderReportSection(section, index)));

  $("btn-report-export").hidden = false;
  $("btn-report-copy").hidden = false;
  $("btn-report-expand").hidden = sections.length === 0;
  updateReportExpandLabel();
}

/** 章节摘要：几组分布、几组长度、几条关注（按级别计数）。 */
function reportSectionStats(blocks) {
  const stats = { bars: 0, lengths: 0, issues: 0, levels: { "高": 0, "中": 0, "低": 0 }, other: 0 };
  for (const block of blocks) {
    if (block.kind !== "table" || !block.rows.length) { stats.other += 1; continue; }
    const kind = classifyReportTable(block.rows);
    if (kind === "bars") stats.bars += 1;
    else if (kind === "length") stats.lengths += 1;
    else if (kind === "issues") {
      for (const row of block.rows.slice(1)) {
        stats.issues += 1;
        if (row[0] in stats.levels) stats.levels[row[0]] += 1;
      }
    }
  }
  return stats;
}

/** 章节标题形如「health_train_planner_trajectories.jsonl（final_prompt）」，拆成文件名 + 类型。 */
function splitReportTitle(title) {
  const matched = /^(.*?)（(.*)）\s*$/.exec(title);
  return matched ? { name: matched[1], type: matched[2] } : { name: title, type: "" };
}

function issueBadgesHtml(stats) {
  const parts = [];
  for (const [level, cls] of [["高", "err"], ["中", "warn"], ["低", "info"]]) {
    if (stats.levels[level]) parts.push(`<span class="lv ${cls}">${level} ${stats.levels[level]}</span>`);
  }
  return parts.join("");
}

function renderReportToc(sections) {
  const wrap = document.createElement("div");
  wrap.className = "stats-overview report-toc";
  wrap.innerHTML = `<div class="ov-head">
      <span>文件</span>
      <span class="hint">${sections.length} 个 · 点一行跳到对应章节</span>
    </div>`;
  sections.forEach((section, index) => {
    const { name, type } = splitReportTitle(section.title);
    const stats = reportSectionStats(section.blocks);
    const counts = [
      stats.bars ? `${stats.bars} 组分布` : "",
      stats.lengths ? `${stats.lengths} 组长度` : "",
    ].filter(Boolean).join(" · ");
    const row = document.createElement("button");
    row.type = "button";
    row.className = "toc-row";
    row.innerHTML = `
      <span class="ov-name">${escapeHtml(name)}</span>
      <span class="hint">${escapeHtml(type)}</span>
      <span class="ov-counts">${counts || "—"}</span>
      <span class="toc-issues">${stats.issues ? issueBadgesHtml(stats) : `<span class="lv ok">无关注项</span>`}</span>`;
    row.addEventListener("click", () => {
      const card = document.querySelector(`.report-section[data-report-index="${index}"]`);
      if (!card) return;
      toggleReportSection(card, true);
      card.scrollIntoView({ behavior: "smooth", block: "start" });
    });
    wrap.appendChild(row);
  });
  return wrap;
}

function renderReportSection(section, index) {
  const key = section.title;
  const { name, type } = splitReportTitle(section.title);
  const stats = reportSectionStats(section.blocks);
  const card = document.createElement("div");
  card.className = "file-card report-section";
  card.dataset.reportKey = key;
  card.dataset.reportIndex = String(index);
  if (reportUi.expanded.has(key)) card.classList.add("expanded");

  const summary = [
    stats.bars ? `${stats.bars} 组分布` : "",
    stats.lengths ? `${stats.lengths} 组长度` : "",
  ].filter(Boolean).join(" · ");
  const header = document.createElement("header");
  header.innerHTML = `
    <span class="fc-caret">▸</span>
    <span class="fc-name">${escapeHtml(name)}</span>
    ${type ? `<span class="hint">${escapeHtml(type)}</span>` : ""}
    <span class="fc-summary">${stats.issues ? issueBadgesHtml(stats) : `<span class="lv ok">无关注项</span>`}${summary ? `<span class="hint">${summary}</span>` : ""}</span>`;
  header.addEventListener("click", () => toggleReportSection(card));
  card.appendChild(header);

  const body = document.createElement("div");
  body.className = "body";
  // 子标题（###）开一个新块，块内的表格 / 列表归到它下面。
  let target = body;
  section.blocks.forEach((block, blockIndex) => {
    if (block.kind === "h") {
      const group = document.createElement("div");
      group.className = "report-block";
      group.innerHTML = `<h4>${markdownInline(block.text)}</h4><div class="report-block-body"></div>`;
      body.appendChild(group);
      target = group.querySelector(".report-block-body");
      return;
    }
    const node = renderReportBlock(block, key, blockIndex);
    if (node) target.appendChild(node);
  });
  card.appendChild(body);
  return card;
}

/** 渲染单个块；表格按类型分发，其余走基础渲染。 */
function renderReportBlock(block, sectionKey, blockIndex) {
  if (block.kind === "hr") return null;
  if (block.kind === "p") {
    const node = document.createElement("p");
    node.className = "report-text";
    node.innerHTML = markdownInline(block.text);
    return node;
  }
  if (block.kind === "list") {
    const node = document.createElement("ul");
    node.className = "report-list";
    node.innerHTML = block.items.map((item) => `<li>${markdownInline(item)}</li>`).join("");
    return node;
  }
  if (block.kind !== "table" || !block.rows.length) return null;
  const kind = classifyReportTable(block.rows);
  if (kind === "summary") return reportSummaryTable(block.rows);
  if (kind === "issues") return reportIssues(block.rows);
  if (kind === "length") return reportLengthTable(block.rows);
  if (kind === "bars") return reportBars(block.rows, `${sectionKey}::${blockIndex}`);
  return reportRawTable(block.rows);
}

/** 顶层文件总览：无效行 / 重复 id 为 0 标绿，非 0 标红。 */
function reportSummaryTable(rows) {
  const head = rows[0];
  const table = document.createElement("table");
  table.className = "report-table";
  const bodyRows = rows.slice(1).map((row) => {
    const cells = row.map((cell, index) => {
      const column = head[index] || "";
      if (column === "无效行" || column === "重复 id") {
        return `<td class="num ${Number(cell) ? "bad" : "ok"}">${escapeHtml(cell)}</td>`;
      }
      if (index === 0) return `<td class="path">${escapeHtml(cell)}</td>`;
      return `<td class="${/^\d+$/.test(cell) ? "num" : ""}">${escapeHtml(cell)}</td>`;
    }).join("");
    return `<tr>${cells}</tr>`;
  }).join("");
  table.innerHTML = `<thead><tr>${head.map((cell) => `<th>${escapeHtml(cell)}</th>`).join("")}</tr></thead><tbody>${bodyRows}</tbody>`;
  return table;
}

/** 需要关注：级别做成彩色标签，正文一行一条。 */
function reportIssues(rows) {
  const list = document.createElement("div");
  list.className = "issue-list";
  for (const row of rows.slice(1)) {
    const level = row[0] || "";
    const cls = level === "高" ? "err" : level === "中" ? "warn" : "info";
    const item = document.createElement("div");
    item.className = `issue ${cls}`;
    item.innerHTML = `<span class="issue-level">${escapeHtml(level)}</span><span class="issue-text">${markdownInline(row[1] || "")}</span>`;
    list.appendChild(item);
  }
  return list;
}

/** 关键长度分布：一行文字 + 一条 min→max 区间条，p50 打标记。 */
function reportLengthTable(rows) {
  const list = document.createElement("div");
  list.className = "len-list report-len";
  const items = rows.slice(1).map((row) => ({
    name: row[0] || "",
    count: row[1] || "",
    min: Number(row[2]) || 0,
    avg: row[3] || "",
    p50: Number(row[4]) || 0,
    p95: Number(row[7]) || 0,
    max: Number(row[8]) || 0,
  }));
  // 同一张表共用一个刻度，行与行之间才可比。
  const scale = Math.max(...items.map((item) => item.max), 1);
  const at = (value) => Math.min(Math.max((value / scale) * 100, 0), 100);
  for (const item of items) {
    const full = `count ${item.count} · min ${item.min} · avg ${item.avg} · p50 ${item.p50} · p95 ${item.p95} · max ${item.max}`;
    const node = document.createElement("div");
    node.className = "len-row";
    node.title = full;
    node.innerHTML = `
      <span class="len-name">${escapeHtml(item.name)}</span>
      <span class="len-summary">
        <span>n=${escapeHtml(item.count)}</span>
        <span>${formatNumber(item.min)}–${formatNumber(item.max)}</span>
        <span>均值 ${escapeHtml(item.avg)}</span>
        <span>p50 ${formatNumber(item.p50)}</span>
        <span>p95 ${formatNumber(item.p95)}</span>
      </span>
      <span class="range-track">
        <span class="range-fill" style="left:${at(item.min)}%;width:${Math.max(((item.max - item.min) / scale) * 100, 1.5)}%"></span>
        <span class="range-mark" style="left:${at(item.p50)}%"></span>
      </span>`;
    list.appendChild(node);
  }
  return list;
}

/** 带「分布」列的表格：ASCII 进度条换成真条形。 */
function reportBars(rows, stateKey) {
  const head = rows[0];
  const countIndex = head.indexOf("数量");
  const pctIndex = head.indexOf("占比");
  const valueIndex = head.indexOf("值");
  const entries = rows.slice(1).map((row) => {
    const raw = (pctIndex >= 0 ? row[pctIndex] : row[valueIndex]) || "";
    const pct = Number.parseFloat(raw) || 0;
    return { label: row[0] || "", count: countIndex >= 0 ? row[countIndex] : "", pct, raw };
  });

  const wrapper = document.createElement("div");
  wrapper.className = "dist report-bars";
  wrapper.innerHTML = `<div class="bar-list"></div>`;
  const list = wrapper.querySelector(".bar-list");

  const fill = (expanded) => {
    list.innerHTML = "";
    const shown = expanded ? entries : entries.slice(0, REPORT_BAR_PREVIEW);
    for (const entry of shown) {
      const row = document.createElement("div");
      row.className = "bar-row";
      row.innerHTML = `
        <span class="bar-label" title="${escapeHtml(entry.label)}">${escapeHtml(entry.label)}</span>
        <span class="bar-track"><span class="bar-fill" style="width:${Math.min(Math.max(entry.pct, 0), 100)}%"></span></span>
        <span class="bar-value">${entry.count ? `${escapeHtml(entry.count)} · ` : ""}${escapeHtml(entry.raw)}</span>`;
      list.appendChild(row);
    }
  };
  fill(reportUi.barsOpen.has(stateKey));

  if (entries.length > REPORT_BAR_PREVIEW) {
    const more = document.createElement("button");
    more.type = "button";
    more.className = "ghost small dist-more";
    const label = (open) => (open ? "收起" : `展开全部 ${entries.length} 项`);
    more.textContent = label(reportUi.barsOpen.has(stateKey));
    more.addEventListener("click", () => {
      const open = !reportUi.barsOpen.has(stateKey);
      if (open) reportUi.barsOpen.add(stateKey); else reportUi.barsOpen.delete(stateKey);
      fill(open);
      more.textContent = label(open);
    });
    wrapper.appendChild(more);
  }
  return wrapper;
}

/** 认不出的表格原样渲染，后端加新表时不会漏掉。 */
function reportRawTable(rows) {
  const table = document.createElement("table");
  table.className = "report-table";
  const head = rows[0].map((cell) => `<th>${markdownInline(cell)}</th>`).join("");
  const bodyRows = rows.slice(1).map((row) =>
    `<tr>${row.map((cell) => `<td>${markdownInline(cell)}</td>`).join("")}</tr>`).join("");
  table.innerHTML = `<thead><tr>${head}</tr></thead><tbody>${bodyRows}</tbody>`;
  return table;
}

function toggleReportSection(card, expanded) {
  const key = card.dataset.reportKey;
  const next = expanded === undefined ? !card.classList.contains("expanded") : expanded;
  card.classList.toggle("expanded", next);
  if (next) reportUi.expanded.add(key); else reportUi.expanded.delete(key);
  updateReportExpandLabel();
}

function updateReportExpandLabel() {
  const button = $("btn-report-expand");
  if (!button || button.hidden) return;
  const cards = [...document.querySelectorAll("#report-body .report-section")];
  const allOpen = cards.length > 0 && cards.every((card) => card.classList.contains("expanded"));
  button.textContent = allOpen ? "全部收起" : "全部展开";
}

/** 分布表默认只画前几条，其余点开看。 */
const REPORT_BAR_PREVIEW = 8;

/** 极简 markdown 渲染：标题、表格、列表、代码块、粗体、行内代码、分隔线。 */
function renderMarkdown(text) {
  const lines = String(text).split("\n");
  const html = [];
  let index = 0;
  let inCode = false;
  let codeBuffer = [];
  let listBuffer = [];
  let tableBuffer = [];

  const flushList = () => {
    if (listBuffer.length) {
      html.push(`<ul>${listBuffer.map((item) => `<li>${inline(item)}</li>`).join("")}</ul>`);
      listBuffer = [];
    }
  };
  const flushTable = () => {
    if (!tableBuffer.length) return;
    const rows = tableBuffer.filter((row) => !/^\|[\s:|-]+\|$/.test(row.trim()));
    const cells = rows.map((row) => row.trim().replace(/^\||\|$/g, "").split("|").map((cell) => cell.trim()));
    if (cells.length) {
      const head = cells[0].map((cell) => `<th>${inline(cell)}</th>`).join("");
      const bodyRows = cells.slice(1).map((row) =>
        `<tr>${row.map((cell) => `<td>${inline(cell)}</td>`).join("")}</tr>`).join("");
      html.push(`<table><thead><tr>${head}</tr></thead><tbody>${bodyRows}</tbody></table>`);
    }
    tableBuffer = [];
  };

  const inline = markdownInline;
  while (index < lines.length) {
    const line = lines[index];
    if (line.trim().startsWith("```")) {
      if (inCode) {
        html.push(`<pre><code>${escapeHtml(codeBuffer.join("\n"))}</code></pre>`);
        codeBuffer = [];
      }
      inCode = !inCode;
      index += 1;
      continue;
    }
    if (inCode) { codeBuffer.push(line); index += 1; continue; }

    if (line.trim().startsWith("|")) { flushList(); tableBuffer.push(line); index += 1; continue; }
    flushTable();

    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      flushList();
      const level = heading[1].length;
      html.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      index += 1;
      continue;
    }
    if (/^\s*[-*]\s+/.test(line)) { listBuffer.push(line.replace(/^\s*[-*]\s+/, "")); index += 1; continue; }
    flushList();

    if (/^\s*(---|\*\*\*)\s*$/.test(line)) { html.push("<hr>"); index += 1; continue; }
    if (!line.trim()) { index += 1; continue; }
    html.push(`<p>${inline(line)}</p>`);
    index += 1;
  }
  flushList();
  flushTable();
  if (inCode && codeBuffer.length) html.push(`<pre><code>${escapeHtml(codeBuffer.join("\n"))}</code></pre>`);
  return html.join("\n");
}

// ---------------------------------------------------------------------------
// 历史
// ---------------------------------------------------------------------------

/**
 * 运行历史。
 *
 * 两个来源合起来才是完整的历史：`/api/jobs` 是内存里的作业（有日志、能取消，
 * 但控制台一重启就没了），`/api/runs` 是落盘的运行记录（重启后依然在，但没有
 * 日志）。以前只看前者，于是重启一次这一页就空成「还没有运行记录」——明明刚
 * 跑过。同一次运行两边都有时以内存那条为准：它的状态更新，日志也还在。
 */
async function loadJobs() {
  const tbody = $("job-table").querySelector("tbody");
  let live = [];
  let history = [];
  try {
    live = (await api("/api/jobs")).jobs || [];
  } catch (error) {
    tbody.innerHTML = `<tr><td colspan="6">${errorHtml(error.message, { title: "读不到运行记录" })}</td></tr>`;
    return false;
  }
  try {
    const payload = await api(
      `/api/runs?domain=${encodeURIComponent(state.domain)}&limit=100`
    );
    history = payload.runs || [];
  } catch (_) {
    history = [];   // 历史读不到不该让整页失败：内存里那几条照样能看
  }

  const inMemory = new Set(live.map((job) => job.id));
  const entries = [
    ...live.map((job) => ({ ...job, live: true })),
    ...history.filter((run) => !inMemory.has(run.id)),
  ].sort(
    (a, b) => (b.startedAt || b.createdAt || 0) - (a.startedAt || a.createdAt || 0)
  );

  if (!entries.length) {
    tbody.innerHTML = `<tr><td colspan="6">${emptyHtml({
      icon: "clock",
      title: "还没有运行记录",
      desc: "每跑一次流水线，这次运行的状态、耗时和产物数量都会记在这里，"
        + "控制台重启也不会丢。",
      variant: "inline",
    })}</td></tr>`;
    return true;
  }

  tbody.innerHTML = "";
  for (const entry of entries) {
    const stages = entry.stages || [];
    const duration = entry.duration !== undefined && entry.duration !== null
      ? entry.duration
      : (entry.startedAt && entry.finishedAt ? entry.finishedAt - entry.startedAt : null);
    // 失败了就得能立刻知道是哪个阶段、为什么，不用点进日志里翻。
    const reason = stages
      .filter((item) => item.status === "failed")
      .map((item) => `${item.title || item.stageId}：${item.error || `退出码 ${item.exitCode}`}`)
      .join("\n");
    const kind = entry.status === "succeeded" ? "ok" : entry.status === "failed" ? "bad" : "";
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="path">${escapeHtml(entry.id)}</td>
      <td><span class="pill ${kind}"${reason ? ` title="${escapeHtml(reason)}"` : ""}>${escapeHtml(RUN_STATUS_TEXT[entry.status] || entry.status)}</span></td>
      <td class="path">${escapeHtml(stages.map((item) => item.stageId).join(" → "))}</td>
      <td>${formatTime(entry.startedAt || entry.createdAt)}</td>
      <td>${formatDuration(duration)}</td>
      <td></td>`;
    const button = document.createElement("button");
    button.className = "ghost small";
    if (entry.live) {
      button.textContent = "查看日志";
      button.addEventListener("click", () => openJobLog(entry.id));
    } else {
      // 日志没落盘，但产物快照落了：历史记录能提供的是「那次跑出了哪些文件」。
      button.textContent = "看产物";
      button.title = "切到产物页，只看这一次运行写出的文件";
      button.addEventListener("click", () => openRunArtifacts(entry.id));
    }
    tr.lastElementChild.appendChild(button);
    tbody.appendChild(tr);
  }
  return true;
}

/** 从历史记录跳到产物页，并选中这一次运行。 */
function openRunArtifacts(runId) {
  // 先把选中项认下来再切页：切页触发的那次加载会顺手把它渲染出来。反过来先切页
  // 再 selectRun 会赶在异步加载前面，那时批次表还是空的，选中项会被当成已失效。
  state.activeRun = runId;
  const tab = document.querySelector('.tab[data-view="artifacts"]');
  if (tab) tab.click();
}

async function openJobLog(jobId) {
  const job = await api(`/api/jobs/${jobId}`);
  document.querySelector('.tab[data-view="run"]').click();
  state.currentJob = null;
  clearLog();
  $("log-title").textContent = `日志 · ${job.id}（历史）`;
  for (const line of job.lines) appendLog(line, false);
  renderProgress(job);
}

// ---------------------------------------------------------------------------
// 领域包概览
//
// 「我现在在哪个包里、它由什么组成、有多少东西、最近动过没」——一屏答完。
// 数据全部来自已有的 /api/domains 与 /api/tools，没有新增后端接口。
// ---------------------------------------------------------------------------

async function loadDomainPack() {
  const body = $("domain-pack-body");
  body.innerHTML = loadingHtml({ rows: 4 });
  let payload;
  try {
    payload = await api("/api/domains");
  } catch (error) {
    body.innerHTML = errorHtml(error.message, { title: "读不到领域包概览" });
    return false;
  }
  // 顺手把领域管理弹窗的数据源也刷新了，两边不用各拉一次。
  domainUi.domains = payload.domains;
  domainUi.templates = payload.templates;
  domainUi.activeJobId = payload.activeJobId || null;

  const current = payload.domains.find((item) => item.name === state.domain) || null;
  if (!current) {
    body.innerHTML = emptyHtml({
      icon: "alert",
      title: `没找到领域 ${state.domain}`,
      desc: "它可能刚被删掉。用顶栏的下拉框换一个领域。",
      variant: "inline",
    });
    return true;
  }

  // 自有工具得问 /api/tools（会重新 import 项目定义，约半秒）；
  // 它读不出来（比如 spec.py 有语法错误）不该把整页拖垮，降级成一行提示。
  let own = { tools: [], error: "" };
  try {
    const tools = await api(`/api/tools?domain=${encodeURIComponent(current.name)}`);
    own = tools.own || own;
  } catch (error) {
    own = { tools: [], error: error.message };
  }

  renderDomainPack(current, own);
  return true;
}

/** 概览页的一张数字卡；沿用统计页的 .kpi-card，只有需要行动的数字才上色。 */
function packKpi(label, value, sub, alert = false) {
  return `
    <div class="kpi-card${alert ? " is-alert" : ""}">
      <span class="kpi-label">${label}</span>
      <span class="kpi-value">${value}</span>
      <span class="kpi-sub">${sub}</span>
    </div>`;
}

function renderDomainPack(domain, own) {
  const body = $("domain-pack-body");
  const ownTools = own.tools || [];
  const shadows = ownTools.filter((tool) => tool.shadows).length;
  const name = escapeHtml(domain.name);
  const lastTouched = Math.max(domain.seedMtime || 0, domain.artifactMtime || 0);

  const composition = domain.extraModules.length
    ? `由 <b>spec.py</b> 和 ${domain.extraModules.length} 个扩展模块（<b>${escapeHtml(domain.extraModules.join("、"))}</b>）组成`
    : "只有 <b>spec.py</b> 一个文件";
  const seeds = domain.hasSeeds
    ? `已有 <b>${domain.seedLines.toLocaleString("en-US")}</b> 条 seed`
    : `<span class="warn">还没有 seeds.jsonl</span>`;
  const tools = ownTools.length
    ? `自己定义了 <b>${ownTools.length}</b> 个工具${shadows ? `，其中 <b>${shadows}</b> 个会盖住全局同名工具` : ""}`
    : "没有自己定义的工具（工具全部来自全局库）";
  const artifacts = domain.artifactFiles
    ? `产出 <b>${domain.artifactFiles}</b> 个文件（${formatBytes(domain.artifactBytes)}）`
    : `<span class="warn">还没有产物</span>`;
  const touched = lastTouched
    ? `最近一次改动在 <b>${formatTime(lastTouched)}</b>（${formatAgo(lastTouched)}）。`
    : "还没有 seed 或产物，看不出改动时间。";
  const broken = domain.hasSpec ? "" : ` <span class="warn">缺少 spec.py，跑不起来。</span>`;

  const modules = ["spec.py", ...domain.extraModules];
  const sources = [...new Set(ownTools.map((tool) => tool.source && tool.source.path).filter(Boolean))];
  const ownNote = own.error
    ? `<span class="warn">读取失败：${escapeHtml(own.error)}</span>`
    : ownTools.length
      ? `${ownTools.length} 个${sources.length ? `，定义在 <b>${escapeHtml(sources.join("、"))}</b>` : ""}`
      : "没有";

  body.innerHTML = `
    <div class="pack-head">
      <h2 class="pack-title">${escapeHtml(domain.displayName)}</h2>
      <span class="pill">${name}</span>
      ${domain.active ? `<span class="pill ok">当前使用</span>` : ""}
      ${domain.hasSpec ? "" : `<span class="pill bad">缺少 spec.py</span>`}
      ${domain.hasSeeds ? "" : `<span class="pill bad">没有 seeds.jsonl</span>`}
    </div>
    <p class="pack-summary">这个领域包${composition}。${seeds}，${tools}，${artifacts}。${touched}${broken}</p>
    <div class="stats-kpi">
      ${packKpi("seed 条数", domain.seedLines.toLocaleString("en-US"), domain.hasSeeds ? formatBytes(domain.seedBytes) : "缺文件", !domain.hasSeeds)}
      ${packKpi("自有工具", ownTools.length, own.error ? "读取失败" : shadows ? `${shadows} 个覆盖全局` : "无覆盖")}
      ${packKpi("产物文件", domain.artifactFiles, formatBytes(domain.artifactBytes))}
      ${packKpi("扩展模块", domain.extraModules.length, `共 ${modules.length} 个 .py`)}
    </div>
    <div class="pack-rows">
      <div class="pack-row"><span class="k">目录</span><span class="v path">${escapeHtml(relPath(domain.path))}/</span></div>
      <div class="pack-row"><span class="k">组成</span><span class="v">${modules.map(escapeHtml).join("、")}</span></div>
      <div class="pack-row"><span class="k">产物目录</span><span class="v path">${escapeHtml(relPath(domain.outputDir))}/</span></div>
      <div class="pack-row"><span class="k">自有工具</span><span class="v">${ownNote}</span></div>
    </div>`;
}

// ---------------------------------------------------------------------------
// 领域（项目）管理
//
// 一个领域就是一份自包含项目：代码包在 agent/domains/<name>/，产物在
// agent/outputs/<name>/，两者完全隔离。这里的界面负责增删改与切换。
// ---------------------------------------------------------------------------

const domainUi = { mode: "create", source: null, domains: [], templates: [], confirmName: null, activeJobId: null };

function bindDomainManager() {
  $("btn-domains").addEventListener("click", openDomainModal);
  $("btn-domain-close").addEventListener("click", closeDomainModal);
  document.querySelectorAll("[data-domain-close]").forEach((node) => node.addEventListener("click", closeDomainModal));
  $("btn-domain-new").addEventListener("click", () => showDomainForm("create", null));
  $("btn-domain-cancel").addEventListener("click", hideDomainForm);
  $("domain-form").addEventListener("submit", (event) => {
    event.preventDefault();
    submitDomainForm();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    // 弹窗是叠着开的：编辑器 / 预览优先，领域面板最后关。
    if (!$("file-modal").hidden) {
      closeFileEditor();
      return;
    }
    if (!$("preview-modal").hidden) {
      closePreview();
      return;
    }
    if (!$("domain-modal").hidden) closeDomainModal();
  });
}

async function openDomainModal() {
  $("domain-modal").hidden = false;
  hideDomainForm();
  await loadDomains();
}

function closeDomainModal() {
  $("domain-modal").hidden = true;
  hideDomainForm();
}

async function loadDomains() {
  const list = $("domain-list");
  list.innerHTML = loadingHtml({ rows: 2 });
  let payload;
  try {
    payload = await api("/api/domains");
  } catch (error) {
    list.innerHTML = errorHtml(error.message, { title: "读不到领域清单" });
    return;
  }
  domainUi.domains = payload.domains;
  domainUi.templates = payload.templates;
  domainUi.activeJobId = payload.activeJobId || null;
  $("domain-root-hint").textContent = `领域包 ${relPath(payload.domainsDir)}　·　产物 ${relPath(payload.outputsDir)}`;
  renderDomainCards();
}

function renderDomainCards() {
  const list = $("domain-list");
  list.innerHTML = "";
  if (!domainUi.domains.length) {
    list.innerHTML = emptyHtml({
      icon: "folder",
      title: "还没有领域",
      desc: "领域决定这次跑哪份业务数据。新建会生成一份最小可运行的配置。",
      actions: '<button class="ghost small" data-click-target="btn-domain-new">新建领域</button>',
    });
    return;
  }
  for (const domain of domainUi.domains) list.appendChild(renderDomainCard(domain));
}

function renderDomainCard(domain) {
  const card = document.createElement("div");
  card.className = "domain-card";
  const isCurrent = domain.name === state.domain;
  if (isCurrent) card.classList.add("current");

  const modules = domain.extraModules.length ? domain.extraModules.join("、") : "仅 spec.py";
  const lastTouched = Math.max(domain.seedMtime || 0, domain.artifactMtime || 0);
  card.innerHTML = `
    <div class="domain-card-main">
      <div class="domain-card-title">
        <span>${escapeHtml(domain.displayName)}</span>
        ${isCurrent ? `<span class="pill ok">当前使用</span>` : ""}
        ${domain.hasSpec ? "" : `<span class="pill bad">缺少 spec.py</span>`}
        ${domain.hasSeeds ? "" : `<span class="pill">无 seed</span>`}
      </div>
      <div class="domain-card-dir" title="${escapeHtml(domain.path)}">${escapeHtml(domain.name)}　${escapeHtml(relPath(domain.path))}</div>
      <div class="domain-stats">
        <span>seed <b>${domain.seedLines.toLocaleString("en-US")}</b> 条</span>
        <span>产物 <b>${domain.artifactFiles}</b> 个 · <b>${formatBytes(domain.artifactBytes)}</b></span>
        <span>最近改动 <b>${lastTouched ? formatTime(lastTouched) : "—"}</b></span>
        <span>模块 <b>${escapeHtml(modules)}</b></span>
      </div>
    </div>
    <div class="domain-card-actions"></div>`;

  const actions = card.querySelector(".domain-card-actions");

  if (domainUi.confirmName === domain.name) {
    card.classList.add("confirming");
    const warning = document.createElement("span");
    warning.className = "hint";
    warning.textContent = "会移入 .trash，可手工恢复";
    const confirm = document.createElement("button");
    confirm.className = "danger small";
    confirm.textContent = "确认删除";
    confirm.addEventListener("click", () => doDeleteDomain(domain.name));
    const cancel = document.createElement("button");
    cancel.className = "ghost small";
    cancel.textContent = "取消";
    cancel.addEventListener("click", () => {
      domainUi.confirmName = null;
      renderDomainCards();
    });
    actions.append(warning, confirm, cancel);
    return card;
  }

  const busyHint = domainUi.activeJobId ? `作业 ${domainUi.activeJobId} 运行中，暂时不能改名或删除` : "";
  const use = document.createElement("button");
  use.className = "primary small";
  use.textContent = isCurrent ? "使用中" : "切换到该领域";
  use.disabled = isCurrent;
  use.addEventListener("click", () => switchDomain(domain.name));

  const edit = document.createElement("button");
  edit.className = "ghost small";
  edit.textContent = "编辑文件";
  edit.title = "在线编辑该领域的提示词 / 意图分类 / 工具定义";
  edit.disabled = !domain.hasSpec;
  edit.addEventListener("click", () => openFileEditor("domain", domain.name));

  const duplicate = document.createElement("button");
  duplicate.className = "ghost small";
  duplicate.textContent = "复制";
  duplicate.addEventListener("click", () => showDomainForm("duplicate", domain.name));

  const rename = document.createElement("button");
  rename.className = "ghost small";
  rename.textContent = "重命名";
  rename.title = busyHint;
  rename.disabled = Boolean(domainUi.activeJobId);
  rename.addEventListener("click", () => showDomainForm("rename", domain.name));

  const remove = document.createElement("button");
  remove.className = "danger small";
  remove.textContent = "删除";
  remove.title = busyHint;
  remove.disabled = Boolean(domainUi.activeJobId);
  remove.addEventListener("click", () => {
    domainUi.confirmName = domain.name;
    renderDomainCards();
  });

  actions.append(use, edit, duplicate, rename, remove);
  return card;
}

function showDomainForm(mode, source) {
  domainUi.mode = mode;
  domainUi.source = source;
  const isCreate = mode === "create";
  const isDuplicate = mode === "duplicate";

  $("domain-form-title").textContent = isCreate
    ? "新建领域"
    : isDuplicate
      ? `复制领域 ${source}`
      : `重命名领域 ${source}`;
  $("domain-form-template-wrap").hidden = !isCreate;
  $("domain-form-outputs-wrap").hidden = !isDuplicate;
  $("domain-form-seeds-label").textContent = isCreate ? "沿用模板的 seed 数据（默认留空）" : "连同 seed 数据一起复制";

  const templateSelect = $("domain-form-template");
  templateSelect.innerHTML = "";
  if (domainUi.templates.length) {
    for (const item of domainUi.templates) {
      const option = document.createElement("option");
      option.value = item.name;
      option.textContent = `${item.name}（${item.displayName}）`;
      templateSelect.appendChild(option);
    }
    const preferred = domainUi.templates.find((item) => item.name === state.domain);
    if (preferred) templateSelect.value = preferred.name;
  } else {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "没有可用模板（需要一个带 spec.py 的领域）";
    templateSelect.appendChild(option);
  }

  const current = domainUi.domains.find((item) => item.name === (source || state.domain));
  $("domain-form-name").value = isCreate ? "" : `${source}_copy`;
  $("domain-form-display").value = isCreate ? "" : `${current ? current.displayName : source} 副本`;
  $("domain-form-seeds").checked = !isCreate;
  $("domain-form-outputs").checked = false;
  $("domain-form-note").textContent = isCreate
    ? "新领域会复制模板的 spec.py / prompts / tools，并把 name、display_name 改成新值；想调整提示词、意图分类或工具，编辑 spec.py 即可。"
    : isDuplicate
      ? "复制会带上整个领域包；是否连已有产物一起复制由下面的勾选项决定。"
      : "重命名会同时改动领域目录、spec.py 里的 name 和产物目录，避免产物丢失。";
  $("btn-domain-submit").textContent = isCreate ? "创建" : isDuplicate ? "复制" : "重命名";

  $("domain-form").hidden = false;
  $("domain-form-name").focus();
}

function hideDomainForm() {
  $("domain-form").hidden = true;
  domainUi.mode = "create";
  domainUi.source = null;
}

async function submitDomainForm() {
  const name = $("domain-form-name").value.trim();
  const displayName = $("domain-form-display").value.trim();
  const copySeeds = $("domain-form-seeds").checked;
  const copyOutputs = $("domain-form-outputs").checked;
  const submit = $("btn-domain-submit");

  const json = { method: "POST", headers: { "Content-Type": "application/json" } };
  let request;
  if (domainUi.mode === "create") {
    request = ["/api/domains", { ...json, body: JSON.stringify({
      name, displayName: displayName || null, template: $("domain-form-template").value || null, copySeeds,
    }) }];
  } else if (domainUi.mode === "duplicate") {
    request = [`/api/domains/${encodeURIComponent(domainUi.source)}/duplicate`, { ...json, body: JSON.stringify({
      newName: name, displayName: displayName || null, copySeeds, copyOutputs,
    }) }];
  } else {
    request = [`/api/domains/${encodeURIComponent(domainUi.source)}/rename`, { ...json, body: JSON.stringify({
      newName: name, displayName: displayName || null,
    }) }];
  }

  submit.disabled = true;
  try {
    const result = await api(request[0], request[1]);
    await applyDomainResult(result, result.domain.name);
  } catch (error) {
    toast(error.message, "err");
  } finally {
    submit.disabled = false;
  }
}

/** 领域变更后：刷新选项、切到目标领域、重载所有数据视图。 */
async function applyDomainResult(result, targetName) {
  hideDomainForm();
  await loadMeta();
  selectDomain(targetName);
  // 下面自带「领域已就绪」提示，这里不再重复弹刷新提示。
  await refreshAll({ quiet: true });
  await loadDomains();
  const notes = (result.notes || []).filter(Boolean);
  toast(notes.length ? `领域 ${targetName} 已就绪。\n${notes.join("\n")}` : `领域 ${targetName} 已就绪。`, "ok");
}

/** 把下拉框切到指定领域（不存在则不动）。 */
function selectDomain(name) {
  const select = $("domain");
  const found = Array.from(select.options).some((option) => option.value === name);
  if (!found) return false;
  const previous = state.domain;
  select.value = name;
  state.domain = name;
  // 前缀跟着项目走同一档，否则切完项目还挂着上一个项目的名字，读的是别人的产物。
  if (previous !== name) {
    state.prefix = pickPrefix(state.prefix, splitsOf(previous), splitsOf(name), defaultPrefixOf(name));
    // 上一个项目的任务清单不能留：候选列表会把别人的任务名当成当前项目的任务
    // 列出来（点一下就切过去）。loadTasks() 随后会重新填。
    clearTaskList();
    // 阶段表是全项目共用的，切项目后按 id 回放上一批的状态会把「上个项目跑过」
    // 显示成「这个项目跑过」，所以整批状态一起清掉。
    state.lastJob = null;
    renderPrefixUi();
  }
  return true;
}

async function switchDomain(name) {
  if (!selectDomain(name)) return;
  closeDomainModal();
  await refreshAll({ quiet: true });
  toast(`已切换到领域 ${name}。`, "ok");
}

async function doDeleteDomain(name) {
  domainUi.confirmName = null;
  try {
    const result = await api(`/api/domains/${encodeURIComponent(name)}`, { method: "DELETE" });
    // loadMeta 会重建下拉框；若删掉的正是当前领域，这里会自动落到默认领域。
    // 之后必须无条件 refreshAll，否则产物/阶段视图会停留在已删除的领域上。
    await loadMeta();
    await refreshAll({ quiet: true });
    await loadDomains();
    toast(`领域 ${name} 已移入回收站：${result.trashDir}`, "ok");
  } catch (error) {
    toast(error.message, "err");
    await loadDomains();
  }
}

// ---------------------------------------------------------------------------
// 领域文件在线编辑
//
// 提示词、意图分类、工具定义都写在领域包里，所以「编辑领域」就是编辑这些文件。
// 前端只做读写 + 行号 + 脏标记，语法校验、备份、路径校验都在服务端。
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// 工具凭据（全局 agent/.env）
//
// 「运行时配置」只认一张白名单（模型接入、本地模型、超时），因为那几个键结构稳定、
// 有类型和默认值，值得逐个登记。工具凭据不是这样：search_web 读 EXA_API_KEY，换成
// 别的搜索后端就是另一个键名，每个项目用的工具都不一样，代码里登记不过来。
//
// 所以这里直接编辑整份 agent/.env：加一行、删一行都行，工具侧一行都不用改 ——
// 它们本来就是 os.getenv("XXX_API_KEY")。
// ---------------------------------------------------------------------------

// 键名像凭据的就按密钥处理（不回显、留空不改）。必须和 registry.looks_like_secret
// 保持一致，否则新增的键在前端显示成明文、保存后又被后端改成掩码。
const ENV_SECRET_RE = /(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|COOKIE)/i;
const ENV_KEY_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

const envUi = { path: "", entries: [], removals: new Set(), loaded: false };

function looksLikeSecret(key) {
  return ENV_SECRET_RE.test(String(key || ""));
}

function bindEnv() {
  $("btn-env-refresh").addEventListener("click", () => {
    withBusy($("btn-env-refresh"), "刷新中…", async () => {
      const ok = await loadEnvFile();
      toast(ok ? "已重新读取环境文件。" : "环境文件没能刷新，具体错误见下方。", ok ? "ok" : "err");
    });
  });
  $("btn-env-save").addEventListener("click", () => {
    withBusy($("btn-env-save"), "保存中…", saveEnvFile);
  });
}

async function loadEnvFile() {
  try {
    const payload = await api("/api/env", { cache: "no-store" });
    envUi.path = payload.path || "";
    envUi.entries = (payload.entries || []).map((entry) => ({ ...entry, value: entry.value || "", fresh: false }));
    envUi.removals.clear();
    envUi.loaded = true;
    renderEnvFile();
    return true;
  } catch (error) {
    envUi.loaded = false;
    $("env-summary").textContent = "读不到环境文件。";
    $("env-list").innerHTML = errorHtml(error.message, { title: "读不到环境文件" });
    return false;
  }
}

function renderEnvFile() {
  $("env-path").textContent = envUi.path ? relPath(envUi.path) : "";
  const list = $("env-list");
  list.innerHTML = "";
  const secrets = envUi.entries.filter((entry) => entry.secret).length;
  $("env-summary").innerHTML = envUi.entries.length
    ? `共 <b>${envUi.entries.length}</b> 个键，对整个控制台生效。工具靠 <span class="mono">os.getenv("XXX_API_KEY")</span> 读凭据，`
      + `键名由工具自己定 —— 加新键不用改任何代码。`
      + (secrets ? `其中 ${secrets} 个密钥只显示头尾，留空表示不修改；` : "")
      + `其它键的值清空等于删掉这一行。`
    : `环境文件还是空的。在下面填一个键名和值，就能加第一条。`;

  for (const entry of envUi.entries) list.appendChild(renderEnvRow(entry));
  list.appendChild(renderEnvNewRow());
  updateEnvMsg();
}

function renderEnvRow(entry) {
  const row = document.createElement("div");
  row.className = "env-row";
  row.dataset.key = entry.key;

  const key = document.createElement("div");
  key.className = "env-key";
  key.innerHTML = `<span class="mono">${escapeHtml(entry.key)}</span>`;
  if (entry.managed) {
    const tag = document.createElement("span");
    tag.className = "env-tag";
    tag.textContent = "运行时配置";
    tag.title = "「运行时配置」弹窗里也有这一项，两处改的是同一行。";
    key.appendChild(tag);
  }
  if (entry.override) {
    const tag = document.createElement("span");
    tag.className = "env-tag warn";
    tag.textContent = "环境变量覆盖";
    tag.title = "进程环境变量里有同名键且值不同，工具实际拿到的是环境变量那一份，不是这里写的。";
    key.appendChild(tag);
  }

  const input = document.createElement("input");
  input.className = "env-value";
  input.type = entry.secret ? "password" : "text";
  input.value = entry.value;
  input.placeholder = entry.secret
    ? (entry.effective ? `已配置 ${entry.effective}（留空不改）` : "未配置（留空不改）")
    : "（空值）";
  input.spellcheck = false;
  input.autocomplete = "off";
  input.addEventListener("input", () => {
    entry.value = input.value;
    row.classList.add("dirty");
    updateEnvMsg();
  });

  const actions = document.createElement("div");
  actions.className = "env-actions";
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "ghost small";
  remove.textContent = "删除";
  remove.addEventListener("click", () => {
    // 记下来，保存时才真正落盘；反悔刷新一次就回来了。
    // 刚在界面上新增、还没写进文件的键不用记（文件里本来就没有它）。
    if (!entry.fresh) envUi.removals.add(entry.key);
    envUi.entries = envUi.entries.filter((item) => item !== entry);
    renderEnvFile();
  });
  actions.appendChild(remove);

  row.append(key, input, actions);
  return row;
}

/** 最后一行是「新增」：填键名 + 值，点添加就并入上面的列表，保存时一起提交。 */
function renderEnvNewRow() {
  const row = document.createElement("div");
  row.className = "env-row env-row-new";

  const keyInput = document.createElement("input");
  keyInput.className = "env-key-input mono";
  keyInput.type = "text";
  keyInput.placeholder = "新键名，如 MY_SERVICE_API_KEY";
  keyInput.spellcheck = false;
  keyInput.autocomplete = "off";

  const valueInput = document.createElement("input");
  valueInput.className = "env-value";
  valueInput.type = "text";
  valueInput.placeholder = "值";
  valueInput.spellcheck = false;
  valueInput.autocomplete = "off";

  const submit = () => {
    const key = keyInput.value.trim();
    if (!key) {
      toast("先填键名。", "err");
      keyInput.focus();
      return;
    }
    if (!ENV_KEY_RE.test(key)) {
      toast("键名只能由字母、数字和下划线组成，且不能以数字开头。", "err");
      keyInput.focus();
      return;
    }
    const existing = envUi.entries.find((item) => item.key === key);
    if (existing) {
      toast(`${key} 已经在列表里，直接改它的值就行。`, "err");
      const target = document.querySelector(`.env-row[data-key="${key}"] .env-value`);
      if (target) target.focus();
      return;
    }
    envUi.removals.delete(key); // 删掉又加回来 = 没删
    envUi.entries.push({
      key,
      value: valueInput.value,
      effective: "",
      secret: looksLikeSecret(key),
      managed: false,
      override: false,
      fresh: true,
    });
    renderEnvFile();
    const next = document.querySelector(`.env-row[data-key="${key}"] .env-value`);
    if (next) next.focus();
  };

  keyInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      submit();
    }
  });
  valueInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      submit();
    }
  });

  const actions = document.createElement("div");
  actions.className = "env-actions";
  const add = document.createElement("button");
  add.type = "button";
  add.className = "ghost small";
  add.textContent = "添加";
  add.addEventListener("click", submit);
  actions.appendChild(add);

  row.append(keyInput, valueInput, actions);
  return row;
}

function updateEnvMsg() {
  const msg = $("env-msg");
  if (!msg) return;
  const edited = document.querySelectorAll(".env-row.dirty").length;
  const added = envUi.entries.filter((entry) => entry.fresh).length;
  const removed = envUi.removals.size;
  const parts = [];
  if (edited) parts.push(`${edited} 项已改`);
  if (added) parts.push(`${added} 项待新增`);
  if (removed) parts.push(`${removed} 项待删除`);
  msg.textContent = parts.length ? `${parts.join("、")}，点保存生效` : "";
}

async function saveEnvFile() {
  // 提交整个列表：非密钥的值就在输入框里，原样回写无害；清空即删除这一行。
  // 密钥留空表示「不修改」——空字符串对凭据没有意义，只会让人以为配好了。
  const values = {};
  for (const entry of envUi.entries) {
    if (entry.secret && !entry.value) continue;
    values[entry.key] = entry.value;
  }

  try {
    const payload = await api("/api/env", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values, removals: [...envUi.removals] }),
    });
    envUi.entries = (payload.entries || []).map((entry) => ({ ...entry, value: entry.value || "", fresh: false }));
    envUi.removals.clear();
    renderEnvFile();
    toast("已写入 agent/.env，下一次运行生效。", "ok");
  } catch (error) {
    toast(error.message, "err");
  }
}

// ---------------------------------------------------------------------------
// 全局工具库
//
// 工具在 agent/tools/<name>.py 里定义一次，项目通过 domains/<name>/tools.json
// 只记「开了哪些」。勾选立即写盘，下一个阶段运行就能用到。
// ---------------------------------------------------------------------------

const toolUi = { tools: [], enabled: new Set(), domain: "", dir: "", loaded: false, opened: false, own: { tools: [], error: "" } };

function bindTools() {
  $("btn-tool-new").addEventListener("click", openToolCreateForm);
  $("btn-tool-close").addEventListener("click", closeToolCreateForm);
  document.querySelectorAll("[data-tool-close]").forEach((node) => node.addEventListener("click", closeToolCreateForm));
  $("tool-form").addEventListener("submit", (event) => { event.preventDefault(); submitToolCreateForm(); });
  $("btn-tool-refresh").addEventListener("click", () => {
    withBusy($("btn-tool-refresh"), "刷新中…", async () => {
      const ok = await loadTools();
      toast(ok ? "已重新读取工具目录。" : "工具列表没能刷新，具体错误见下方。", ok ? "ok" : "err");
    });
  });
}

function openToolCreateForm() {
  $("tool-form-name").value = "";
  $("tool-form-description").value = "";
  $("tool-form-params").value = "[]";
  $("tool-form-mock").value = "";
  $("tool-modal").hidden = false;
  $("tool-form-name").focus();
}

function closeToolCreateForm() { $("tool-modal").hidden = true; }

async function submitToolCreateForm() {
  const name = $("tool-form-name").value.trim();
  const description = $("tool-form-description").value.trim();
  const mock = $("tool-form-mock").value;
  let params;
  try {
    params = JSON.parse($("tool-form-params").value || "[]");
    if (!Array.isArray(params)) throw new Error("参数必须是 JSON 数组。");
  } catch (error) {
    toast(`参数 JSON 无效：${error.message}`, "err");
    return;
  }
  try {
    await api("/api/tools", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, description, mock, params }),
    });
    closeToolCreateForm();
    await loadTools();
    toast(`已新建 ${name}.py，可点击“编辑代码”补充参数和 run()。`, "ok");
  } catch (error) {
    toast(error.message, "err");
  }
}

// ---------------------------------------------------------------------------
// 角色模板
// ---------------------------------------------------------------------------

const roleUi = { roles: [], templates: [], editing: null, domain: "", opened: false };

function bindRoles() {
  $("btn-role-new").addEventListener("click", () => openRoleModal(null));
  $("btn-role-refresh").addEventListener("click", () => loadRoles(true));
  $("btn-role-close").addEventListener("click", closeRoleModal);
  document.querySelectorAll("[data-role-close]").forEach((node) => node.addEventListener("click", closeRoleModal));
  $("role-form-type").addEventListener("change", applyRoleTypeTemplate);
  $("role-form").addEventListener("submit", (event) => { event.preventDefault(); submitRoleForm(); });
}

async function loadRoles(showToast = false) {
  roleUi.opened = true;
  try {
    const payload = await api(`/api/roles?domain=${encodeURIComponent(state.domain || "")}`, { cache: "no-store" });
    roleUi.roles = payload.roles || [];
    roleUi.templates = payload.templates || [];
    roleUi.domain = payload.domain || state.domain;
    renderRoles();
    if (showToast) toast("已刷新角色模板。", "ok");
    return true;
  } catch (error) {
    $("role-list").innerHTML = errorHtml(error.message, { title: "读不到角色模板" });
    if (showToast) toast(error.message, "err");
    return false;
  }
}

function renderRoles() {
  $("roles-target").textContent = roleUi.domain ? `当前项目：${roleUi.domain}` : "";
  $("roles-summary").innerHTML = roleUi.roles.length
    ? `当前项目有 <b>${roleUi.roles.length}</b> 个角色（包含 planner、executor、reviewer 内置角色）；自定义角色只保存在当前项目中。`
    : "还没有角色。可以从 planner、executor 或 reviewer 内置类型创建一个自定义角色。";
  const list = $("role-list");
  list.innerHTML = "";
  if (!roleUi.roles.length) {
    list.innerHTML = emptyHtml({ icon: "user", title: "还没有角色", desc: "点击“新建角色”，选择角色类型后会自动带入对应的内置协议。", actions: '<button class="ghost small" data-click-target="btn-role-new">新建角色</button>', variant: "inline" });
    return;
  }
  for (const role of roleUi.roles) list.appendChild(renderRoleRow(role));
}

function renderRoleRow(role) {
  const row = document.createElement("div");
  row.className = "role-row";
  const schema = role.outputSchema?.properties
    ? Object.keys(role.outputSchema.properties).join("、")
    : role.outputSchema?.type || "未定义";
  row.innerHTML = `<div class="role-main"><div class="tool-head"><span class="tool-name">${escapeHtml(role.displayName || role.name)}</span><span class="tag">${escapeHtml(role.type || "custom")}</span>${role.builtin ? '<span class="tag ok">内置</span>' : '<span class="tag">自定义</span>'}</div><p class="tool-desc">${escapeHtml(role.description || "（没有用途说明）")}</p><div class="tool-params"><span class="param-chip mono">${escapeHtml(role.name)}</span><span class="param-chip">输出：${escapeHtml(schema)}</span></div></div>`;
  const actions = document.createElement("div");
  actions.className = "tool-actions";
  const edit = document.createElement("button");
  edit.className = "ghost small";
  edit.textContent = role.builtin ? "复制为自定义" : "编辑";
  edit.addEventListener("click", () => role.builtin ? openRoleCopyModal(role) : openRoleModal(role));
  if (!role.builtin) {
    const remove = document.createElement("button");
    remove.className = "danger small"; remove.textContent = "删除"; remove.addEventListener("click", () => deleteRole(role.name));
    actions.append(remove);
  }
  row.append(actions);
  return row;
}

function openRoleModal(role) {
  roleUi.editing = role;
  $("role-modal-title").textContent = role ? `编辑角色 · ${role.name}` : "新建角色";
  $("role-form-name").value = role?.name || "";
  $("role-form-display").value = role?.displayName || "";
  $("role-form-type").value = role?.type || "custom";
  $("role-form-description").value = role?.description || "";
  $("role-form-prompt").value = role?.systemPrompt || "";
  $("role-form-input").value = JSON.stringify(role?.inputSchema || { user_query: "用户问题" }, null, 2);
  $("role-form-output").value = JSON.stringify(role?.outputSchema || { answer: "角色输出" }, null, 2);
  $("role-form-name").disabled = Boolean(role?.builtin);
  $("role-form-type").disabled = Boolean(role?.builtin);
  $("btn-role-submit").textContent = role ? "保存角色" : "创建角色";
  $("role-modal").hidden = false;
  if (!role) applyRoleTypeTemplate();
}

function closeRoleModal() { $("role-modal").hidden = true; roleUi.editing = null; }

function openRoleCopyModal(role) {
  openRoleModal(null);
  $("role-form-name").value = `${role.type}_custom`;
  $("role-form-display").value = `${role.displayName}（自定义）`;
  $("role-form-type").value = role.type;
  $("role-form-description").value = role.description || "";
  $("role-form-prompt").value = role.systemPrompt || "";
  $("role-form-input").value = JSON.stringify(role.inputSchema || {}, null, 2);
  $("role-form-output").value = JSON.stringify(role.outputSchema || {}, null, 2);
}

function applyRoleTypeTemplate() {
  if (roleUi.editing) return;
  const type = $("role-form-type").value;
  const template = roleUi.templates.find((item) => item.type === type);
  if (!template) return;
  $("role-form-name").value = type === "custom" ? "" : `${type}_custom`;
  $("role-form-display").value = template.displayName;
  $("role-form-type").value = template.type;
  $("role-form-description").value = template.description;
  $("role-form-prompt").value = template.systemPrompt;
  $("role-form-input").value = JSON.stringify(template.inputSchema, null, 2);
  $("role-form-output").value = JSON.stringify(template.outputSchema, null, 2);
}

async function submitRoleForm() {
  const parse = (id) => {
    try { return JSON.parse($(id).value || "{}"); } catch (_) { throw new Error(`${id} 必须是合法 JSON。`); }
  };
  const payload = { name: $("role-form-name").value.trim(), displayName: $("role-form-display").value.trim(), type: $("role-form-type").value, description: $("role-form-description").value.trim(), systemPrompt: $("role-form-prompt").value.trim(), inputSchema: parse("role-form-input"), outputSchema: parse("role-form-output") };
  try {
    const editing = roleUi.editing;
    const suffix = `?domain=${encodeURIComponent(state.domain || "")}`;
    const url = editing ? `/api/roles/${encodeURIComponent(editing.name)}${suffix}` : `/api/roles${suffix}`;
    await api(url, { method: editing ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    closeRoleModal(); await loadRoles(); toast(editing ? "角色已保存。" : "角色已创建。", "ok");
  } catch (error) { toast(error.message, "err"); }
}

async function deleteRole(name) {
  if (!window.confirm(`确定删除角色 ${name} 吗？`)) return;
  try { await api(`/api/roles/${encodeURIComponent(name)}?domain=${encodeURIComponent(state.domain || "")}`, { method: "DELETE" }); await loadRoles(); toast(`已删除角色 ${name}。`, "ok"); }
  catch (error) { toast(error.message, "err"); }
}

async function loadTools() {
  // 打开过工具页就记下来：之后全局刷新要连工具一起刷，哪怕这次没成功。
  toolUi.opened = true;
  // 环境文件和工具清单在同一页，并行拉；任一失败不影响另一个。
  const envTask = loadEnvFile();
  try {
    const payload = await api(`/api/tools?domain=${encodeURIComponent(state.domain || "")}`, { cache: "no-store" });
    toolUi.tools = payload.tools || [];
    toolUi.enabled = new Set(payload.enabled || []);
    toolUi.domain = payload.domain || state.domain;
    toolUi.dir = payload.dir || "";
    toolUi.own = payload.own || { tools: [], error: "" };
    toolUi.loaded = true;
    renderTools();
    return true;
  } catch (error) {
    toolUi.loaded = false;
    toolUi.own = { tools: [], error: "" };
    // 和其它视图一致：错误写进自己的面板，刷新按钮统一负责提示。
    $("tool-list").innerHTML = errorHtml(error.message, { title: "读不到工具清单" });
    $("own-tools-summary").textContent = "上面的工具库没读出来，这里也跟着空着。";
    $("own-tool-list").innerHTML = "";
    $("own-tools-target").textContent = "";
    return false;
  } finally {
    await envTask;
  }
}

function renderTools() {
  $("tools-target").textContent = toolUi.domain ? `当前项目：${toolUi.domain}` : "";
  const real = toolUi.tools.filter((tool) => tool.hasRun).length;
  $("tools-summary").innerHTML = toolUi.tools.length
    ? `共 <b>${toolUi.tools.length}</b> 个工具（真实实现 ${real}，模拟返回 ${toolUi.tools.length - real}）。`
      + `勾选表示向 <b>${escapeHtml(toolUi.domain)}</b> 开放，该项目的下一个阶段运行时就会带上这个工具；`
      + `取消勾选即收回。工具源码在 <span class="mono">${escapeHtml(relPath(toolUi.dir))}</span>。`
    : `工具库还是空的。工具源码放在 <span class="mono">${escapeHtml(relPath(toolUi.dir) || "agent/tools")}</span> 下，`
      + `在那里放一个 .py 文件后点「刷新」即可。`;

  const list = $("tool-list");
  list.innerHTML = "";
  if (!toolUi.tools.length) {
    list.innerHTML = emptyHtml({
      icon: "tool",
      title: "还没有工具",
      desc: "工具是教师模型在生成数据时可以调用的函数。放进 <code>agent/tools/</code> 的 .py 文件会被自动发现。",
      variant: "inline",
    });
  } else {
    for (const tool of toolUi.tools) list.appendChild(renderToolRow(tool));
  }
  renderOwnTools();
}

/** 项目自己定义的工具：无条件生效、没有开关，这里只展示 + 定位源码。 */
function renderOwnTools() {
  const own = toolUi.own || { tools: [], error: "" };
  const list = $("own-tool-list");
  $("own-tools-target").textContent = toolUi.domain ? `agent/domains/${toolUi.domain}/` : "";
  list.innerHTML = "";

  if (own.error) {
    $("own-tools-summary").textContent = own.error;
    list.innerHTML = errorHtml(own.error, { title: "读不到项目定义" });
    return;
  }
  if (!own.tools.length) {
    $("own-tools-summary").innerHTML =
      `<b>${escapeHtml(toolUi.domain)}</b> 没有自己的工具，只用上面勾选的全局工具。`;
    list.innerHTML = emptyHtml({
      icon: "tool",
      title: "这个项目只用全局工具",
      desc: "它的种子数据里没有声明自己的工具，上面勾选的全局工具对它全部有效。",
      variant: "inline compact",
    });
    return;
  }

  const shadows = own.tools.filter((tool) => tool.shadows).length;
  $("own-tools-summary").innerHTML =
    `共 <b>${own.tools.length}</b> 个，写在项目自己的模块里，<b>无条件全部生效</b>——上面的勾选框管不到它们。`
    + (shadows ? `其中 <b>${shadows}</b> 个和全局库重名，运行时以项目自己的为准。` : "");
  for (const tool of own.tools) list.appendChild(renderOwnToolRow(tool));
}

function renderOwnToolRow(tool) {
  const row = document.createElement("div");
  row.className = "tool-row";
  row.dataset.tool = tool.name;
  if (tool.shadows) row.classList.add("shadows");

  const badges = [`<span class="tag">${tool.params} 个参数</span>`];
  if (tool.shadows) badges.push(`<span class="tag warn">盖住全局同名工具</span>`);

  const main = document.createElement("div");
  main.className = "tool-main";
  main.innerHTML = `
    <div class="tool-head">
      <span class="tool-name">${escapeHtml(tool.name)}</span>
      ${badges.join("")}
    </div>
    <p class="tool-desc">${escapeHtml(tool.description || "（没有描述）")}</p>
    <div class="tool-params">
      <span class="param-chip">${escapeHtml(tool.source ? `${tool.source.path}:${tool.source.line}` : "项目定义")}</span>
    </div>`;

  const actions = document.createElement("div");
  actions.className = "tool-actions";
  const view = document.createElement("button");
  view.className = "ghost small";
  view.textContent = "查看源码";
  view.title = tool.source
    ? `在编辑器里打开 ${tool.source.path} 第 ${tool.source.line} 行`
    : "在编辑器里打开项目定义 spec.py";
  // 定位不到的（比如名字由无参构造函数生成）就退回项目入口 spec.py。
  view.addEventListener("click", () => openFileEditor("domain", toolUi.domain, tool.source?.path || "spec.py", tool.source?.line || 0));
  actions.append(view);

  row.append(main, actions);
  return row;
}

function renderToolRow(tool) {
  const row = document.createElement("div");
  row.className = "tool-row";
  row.dataset.tool = tool.name;
  if (toolUi.enabled.has(tool.name)) row.classList.add("on");
  if (tool.error) row.classList.add("broken");

  const checkId = `tool-check-${tool.name}`;
  const check = document.createElement("input");
  check.type = "checkbox";
  check.id = checkId;
  check.checked = toolUi.enabled.has(tool.name);
  check.addEventListener("change", () => toggleTool(tool.name, check.checked));

  const checkWrap = document.createElement("label");
  checkWrap.className = "tool-check";
  checkWrap.htmlFor = checkId;
  checkWrap.title = `勾选 = 向 ${toolUi.domain} 开放这个工具`;
  checkWrap.appendChild(check);

  const badges = [
    tool.hasRun ? `<span class="tag ok">真实实现</span>` : `<span class="tag warn">模拟返回</span>`,
    `<span class="tag">${tool.params} 个参数</span>`,
  ];
  const others = (tool.usedBy || []).filter((name) => name !== toolUi.domain);
  if (others.length) badges.push(`<span class="tag">另有 ${others.length} 个项目在用</span>`);
  if (tool.error) badges.push(`<span class="tag err">无法解析</span>`);

  const main = document.createElement("div");
  main.className = "tool-main";
  main.innerHTML = `
    <div class="tool-head">
      <span class="tool-name">${escapeHtml(tool.name)}</span>
      ${badges.join("")}
    </div>
    <p class="tool-desc">${escapeHtml(tool.description || "（没有描述）")}</p>
    ${tool.paramNames && tool.paramNames.length
      ? `<div class="tool-params">${tool.paramNames.map((name) => `<span class="param-chip">${escapeHtml(name)}</span>`).join("")}</div>`
      : ""}
    ${tool.error ? `<p class="tool-err">${escapeHtml(tool.error)}</p>` : ""}`;

  const actions = document.createElement("div");
  actions.className = "tool-actions";
  const editButton = document.createElement("button");
  editButton.className = "ghost small";
  editButton.textContent = "编辑代码";
  editButton.addEventListener("click", () => openFileEditor("tool", tool.name));
  const deleteButton = document.createElement("button");
  deleteButton.className = "ghost small";
  deleteButton.textContent = "删除";
  deleteButton.addEventListener("click", () => deleteTool(tool.name, tool.usedBy || []));
  actions.append(editButton, deleteButton);

  row.append(checkWrap, main, actions);
  return row;
}

async function toggleTool(name, enabled) {
  const next = new Set(toolUi.enabled);
  if (enabled) next.add(name);
  else next.delete(name);

  try {
    const result = await api(`/api/domains/${encodeURIComponent(toolUi.domain)}/tools`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: [...next] }),
    });
    toolUi.enabled = new Set(result.tools || []);
    renderTools();
    toast(enabled ? `已向 ${toolUi.domain} 开放 ${name}。` : `已从 ${toolUi.domain} 收回 ${name}。`, "ok");
  } catch (error) {
    toast(error.message, "err");
    renderTools();
  }
}

async function deleteTool(name, usedBy) {
  const extra = usedBy.length ? `\n它当前被 ${usedBy.join("、")} 启用，删除后这些项目会同时收回。` : "";
  if (!window.confirm(`确定删除工具 ${name} 吗？agent/tools/${name}.py 会被移除。${extra}`)) return;
  try {
    const result = await api(`/api/tools/${encodeURIComponent(name)}`, { method: "DELETE" });
    await loadTools();
    const released = result.releasedFrom || [];
    toast(`已删除 ${name}${released.length ? `，并从 ${released.join("、")} 收回` : ""}。`, "ok");
  } catch (error) {
    toast(error.message, "err");
  }
}

// 编辑器有两种来源：领域内的文件（domain）和全局工具源码（tool）。
// 两者的路径、请求体和标题不同，其余（行号栏 / Tab 缩进 / 历史 / 恢复）完全共用。
const editorUi = {
  source: "domain",
  target: null,
  path: null,
  baseline: "",
  dirty: false,
  files: [],
  badgeParts: { backups: 0, real: false, usedBy: [] },
};

// 行高与上内边距必须和 CSS 里的 #editor-text 一致，当前行底色的位置靠它们算。
const EDITOR_LINE_HEIGHT = 18;
const EDITOR_PAD_TOP = 12;
let gutterLines = [];
let gutterCount = -1;
let gutterActive = -1;

function editorUrl(suffix = "") {
  const base = editorUi.source === "tool"
    ? `/api/tools/${encodeURIComponent(editorUi.target)}`
    : `/api/domains/${encodeURIComponent(editorUi.target)}/file`;
  return `${base}${suffix}`;
}

function editorReadUrl() {
  return editorUi.source === "tool"
    ? editorUrl()
    : `${editorUrl()}?path=${encodeURIComponent(editorUi.path)}`;
}

/** 工具接口不需要 path，领域接口必须带。 */
function editorPayload(fields) {
  return editorUi.source === "tool" ? fields : { path: editorUi.path, ...fields };
}

function editorTitle() {
  return editorUi.source === "tool"
    ? `工具源码 · ${editorUi.target}.py`
    : `编辑领域文件 · ${editorUi.target}`;
}

function bindFileEditor() {
  $("btn-file-close").addEventListener("click", closeFileEditor);
  $("btn-file-save").addEventListener("click", saveDomainFile);
  $("btn-file-reload").addEventListener("click", reloadDomainFile);
  $("btn-file-history").addEventListener("click", showFileHistory);
  $("btn-history-close").addEventListener("click", hideFileHistory);
  $("btn-file-new").addEventListener("click", newGlobalTool);
  document.querySelectorAll("[data-file-close]").forEach((node) => node.addEventListener("click", closeFileEditor));

  const text = $("editor-text");
  text.addEventListener("input", onEditorInput);
  text.addEventListener("keyup", syncEditorState);
  text.addEventListener("click", syncEditorState);
  text.addEventListener("scroll", syncEditorScroll);
  text.addEventListener("keydown", (event) => {
    if (event.key === "Tab") {
      handleEditorTab(event);
      return;
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      saveDomainFile();
    }
  });
}

async function openFileEditor(source, target, path = null, focusLine = 0) {
  editorUi.source = source === "tool" ? "tool" : "domain";
  editorUi.target = target;
  editorUi.path = null;
  editorUi.baseline = "";
  editorUi.dirty = false;
  editorUi.files = [];

  $("editor-text").value = "";
  $("editor-msg").textContent = "";
  $("file-dirty").textContent = "";
  $("file-dirty").className = "hint";
  $("editor-status").textContent = "";
  editorUi.badgeParts = { backups: 0, real: false, usedBy: [] };
  renderEditorBadges();
  resetEditorChrome();
  $("file-modal-title").textContent = editorTitle();
  hideFileHistory();
  $("file-modal").hidden = false;

  const busy = Boolean(domainUi.activeJobId) && editorUi.source === "domain";
  $("btn-file-save").disabled = busy;
  $("btn-file-save").title = busy ? `作业 ${domainUi.activeJobId} 运行中，暂时不能保存` : "保存（Ctrl+S）";

  try {
    const payload = await loadEditorSide();
    if (editorUi.source === "tool") {
      await openEditorFile(target, true);
      return;
    }
    const preferred = path
      ? payload.files.find((file) => file.path === path && file.editable)
      : payload.files.find((file) => file.path === "spec.py" && file.editable) ||
        payload.files.find((file) => file.editable);
    if (preferred) {
      await openEditorFile(preferred.path, false, focusLine);
    } else if (path) {
      $("editor-msg").textContent = `${path} 不存在或不可编辑。`;
    } else {
      $("editor-msg").textContent = "这个领域里没有可编辑的文本文件。";
    }
  } catch (error) {
    toast(error.message, "err");
  }
}

function closeFileEditor() {
  if (editorUi.dirty && !window.confirm("有未保存的修改，确定放弃吗？")) return;
  $("file-modal").hidden = true;
  hideFileHistory();
  editorUi.dirty = false;
  editorUi.path = null;
  editorUi.target = null;
}

async function loadEditorSide() {
  if (editorUi.source === "tool") {
    const payload = await api("/api/tools", { cache: "no-store" });
    editorUi.files = (payload.tools || []).map((tool) => ({
      path: `${tool.name}.py`,
      name: tool.name,
      lines: -1,
      size: tool.size,
      editable: !tool.error,
      reason: tool.error || "",
      badge: tool.hasRun ? "真实实现" : "模拟返回",
    }));
    $("file-side-title").textContent = `工具库 · ${editorUi.files.length} 个`;
    $("btn-file-new").hidden = false;
    renderFileList();
    return { files: editorUi.files };
  }

  const payload = await api(`/api/domains/${encodeURIComponent(editorUi.target)}/files`, { cache: "no-store" });
  editorUi.files = payload.files || [];
  $("file-side-title").textContent = `${payload.displayName || payload.domain} · 文件`;
  $("btn-file-new").hidden = true;
  renderFileList();
  return payload;
}

function renderFileList() {
  const list = $("file-list");
  list.innerHTML = "";
  if (!editorUi.files.length) {
    list.innerHTML = editorUi.source === "tool"
      ? emptyHtml({
        icon: "file",
        title: "工具库是空的",
        desc: "工具库是放 .py 文件的地方，放进去会被自动发现。",
        actions: '<button class="ghost small" data-click-target="btn-file-new">新建</button>',
        variant: "inline compact",
      })
      : emptyHtml({
        icon: "folder",
        title: "这个领域目录里没有文件",
        desc: "领域包里的文件（seeds.jsonl、prompt 模板等）都可以在这里直接编辑。",
        variant: "inline compact",
      });
    return;
  }
  for (const file of editorUi.files) {
    const row = document.createElement("div");
    row.className = "file-item";
    if (file.path === editorUi.path) row.classList.add("active");
    if (!file.editable) row.classList.add("disabled");
    const meta = file.badge
      ? file.badge
      : file.lines >= 0
        ? `${file.lines.toLocaleString("en-US")} 行`
        : formatBytes(file.size);
    row.innerHTML = `
      <span class="file-item-main">
        <span class="file-name">${escapeHtml(file.path)}</span>
        ${file.reason ? `<span class="file-reason">${escapeHtml(file.reason)}</span>` : ""}
      </span>
      <span class="file-meta">${meta}</span>`;
    row.addEventListener("click", () => {
      if (!file.editable) {
        toast(file.reason || "这个文件无法在线编辑。", "err");
        return;
      }
      openEditorFile(file.path);
    });
    list.appendChild(row);
  }
}

async function openEditorFile(path, force = false, focusLine = 0) {
  // force 用于「放弃修改」：同一个文件也要重新拉一次磁盘内容。
  if (!force && path === editorUi.path && !editorUi.dirty) return;
  if (editorUi.dirty && !window.confirm(`「${editorUi.path}」有未保存的修改，确定放弃吗？`)) return;

  if (editorUi.source === "tool") editorUi.target = path.replace(/\.py$/, "");
  editorUi.path = path;

  try {
    const payload = await api(editorReadUrl(), { cache: "no-store" });
    editorUi.path = editorUi.source === "tool" ? `${payload.name}.py` : payload.path;
    editorUi.baseline = payload.content;
    editorUi.dirty = false;

    const text = $("editor-text");
    text.value = payload.content;
    text.scrollTop = 0;
    text.scrollLeft = 0;
    $("file-modal-title").textContent = editorUi.source === "tool"
      ? `工具源码 · ${editorUi.path}`
      : `${editorUi.target} / ${editorUi.path}`;
    editorUi.badgeParts = {
      backups: (payload.backups || []).length,
      real: Boolean((payload.meta || {}).hasRun),
      usedBy: payload.usedBy || [],
    };
    renderEditorBadges();
    $("editor-msg").textContent = "";
    renderFileList();
    onEditorInput();
    if (focusLine > 0) focusEditorLine(focusLine);
  } catch (error) {
    toast(error.message, "err");
  }
}

/** 定位到某一行：光标放行首，滚到视口上方 1/3，当前行高亮跟着走。 */
function focusEditorLine(line) {
  const text = $("editor-text");
  const lines = text.value.split("\n");
  if (line < 1 || line > lines.length) return;

  let offset = 0;
  for (let index = 0; index < line - 1; index += 1) offset += lines[index].length + 1;
  text.focus();
  text.setSelectionRange(offset, offset);
  text.scrollTop = Math.max(0, (line - 1) * EDITOR_LINE_HEIGHT - text.clientHeight / 3);
  syncEditorScroll();
  // syncEditorScroll 只同步滚动与当前行，状态栏的行列要另外刷一次。
  syncEditorState();
}

function onEditorInput() {
  renderHighlight();
  updateGutter();
  syncEditorScroll();
  syncEditorState();
}

// ---------------------------------------------------------------------------
// 编辑器着色
//
// 零依赖约束下不引第三方高亮库：用一个正则把源码切成 token，生成一层只读的
// <pre> 叠在 textarea 底下。textarea 的文字设成透明、只留光标，看到的颜色
// 全部来自这一层。所以两层必须严格同字体、同行高、同内边距，否则会错位。
// ---------------------------------------------------------------------------

const PY_KEYWORDS = new Set([
  "False", "None", "True", "and", "as", "assert", "async", "await", "break",
  "case", "class", "continue", "def", "del", "elif", "else", "except", "finally",
  "for", "from", "global", "if", "import", "in", "is", "lambda", "match",
  "nonlocal", "not", "or", "pass", "raise", "return", "try", "while", "with", "yield",
]);

const PY_BUILTINS = new Set([
  "abs", "all", "any", "bool", "bytes", "callable", "chr", "dict", "dir",
  "enumerate", "eval", "filter", "float", "format", "frozenset", "getattr",
  "hasattr", "hash", "id", "input", "int", "isinstance", "issubclass", "iter",
  "len", "list", "map", "max", "min", "next", "object", "open", "ord", "pow",
  "print", "range", "repr", "reversed", "round", "set", "setattr", "slice",
  "sorted", "str", "sum", "super", "tuple", "type", "vars", "zip",
  "BaseException", "Exception", "AttributeError", "IndexError", "KeyError",
  "RuntimeError", "StopIteration", "TypeError", "ValueError",
]);

// 组序：注释 / 字符串 / 数字 / 标识符 / 装饰器。正则取最左匹配，
// 所以字符串里的 # 不会被当成注释，注释里的引号也不会吃掉后面的代码。
const PY_TOKEN_RE = /(#[^\n]*)|("""[\s\S]*?(?:"""|$)|'''[\s\S]*?(?:'''|$)|"(?:\\[\s\S]|[^"\\\n])*"?|'(?:\\[\s\S]|[^'\\\n])*'?)|(\b(?:0[xX][0-9a-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|\d[\d_]*(?:\.[\d_]*)?(?:[eE][+-]?\d+)?)\b)|([A-Za-z_]\w*)|(@[A-Za-z_][\w.]*)/g;

/** 把 Python 源码转成带 tk-* 类名的 HTML。逐字符覆盖原文，不改动任何文本。 */
function highlightPython(source) {
  const out = [];
  let cursor = 0;
  let afterDef = false; // 上一个 token 是 def / class，下一个标识符就是函数名
  PY_TOKEN_RE.lastIndex = 0;

  let match = PY_TOKEN_RE.exec(source);
  while (match !== null) {
    if (match.index > cursor) out.push(escapeHtml(source.slice(cursor, match.index)));
    cursor = PY_TOKEN_RE.lastIndex;
    const [text, comment, str, num, ident, decorator] = match;

    let cls = "";
    if (comment !== undefined) {
      cls = "tk-com";
      afterDef = false;
    } else if (str !== undefined) {
      cls = "tk-str";
      afterDef = false;
    } else if (num !== undefined) {
      cls = "tk-num";
      afterDef = false;
    } else if (decorator !== undefined) {
      cls = "tk-dec";
      afterDef = false;
    } else if (ident !== undefined) {
      if (afterDef) cls = "tk-fn";
      else if (PY_KEYWORDS.has(text)) cls = "tk-kw";
      else if (PY_BUILTINS.has(text)) cls = "tk-blt";
      else if (text === "self" || text === "cls") cls = "tk-self";
      afterDef = text === "def" || text === "class";
    }
    out.push(cls ? `<span class="${cls}">${escapeHtml(text)}</span>` : escapeHtml(text));
    match = PY_TOKEN_RE.exec(source);
  }
  if (cursor < source.length) out.push(escapeHtml(source.slice(cursor)));
  return out.join("");
}

// 组序：字符串 / 数字 / 字面量 / 标点。字符串不允许跨行 —— 编辑到一半少个引号时，
// 后面的内容不会被整段染成字符串色，看起来更像"这里有问题"而不是"全都变了"。
// 数字前的 \b 是必需的：否则 health_talent_001 里的 001 也会被当成数字。
const JSON_TOKEN_RE = /("(?:\\[\s\S]|[^"\\\n])*")|(-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|\b(true|false|null)\b|([{}\[\],:])/g;

/** 把 JSON 文本转成带 tk-* 类名的 HTML。字符串后面跟冒号的算键名。 */
function highlightJson(source) {
  const out = [];
  let cursor = 0;
  JSON_TOKEN_RE.lastIndex = 0;

  let match = JSON_TOKEN_RE.exec(source);
  while (match !== null) {
    if (match.index > cursor) out.push(escapeHtml(source.slice(cursor, match.index)));
    cursor = JSON_TOKEN_RE.lastIndex;
    const [text, str, num, literal, punct] = match;

    let cls = "";
    if (str !== undefined) {
      // 跳过空白后是冒号 → 键名，否则是字符串值。
      let ahead = cursor;
      while (ahead < source.length && /\s/.test(source[ahead])) ahead += 1;
      cls = source[ahead] === ":" ? "tk-key" : "tk-str";
    } else if (num !== undefined) {
      cls = "tk-num";
    } else if (literal !== undefined) {
      cls = "tk-lit";
    } else if (punct !== undefined) {
      cls = "tk-punc";
    }
    out.push(cls ? `<span class="${cls}">${escapeHtml(text)}</span>` : escapeHtml(text));
    match = JSON_TOKEN_RE.exec(source);
  }
  if (cursor < source.length) out.push(escapeHtml(source.slice(cursor)));
  return out.join("");
}

/** 重画高亮层。末尾是换行时补一行，否则最后一行不占高度，光标会跑出可视区。 */
function renderHighlight() {
  const value = $("editor-text").value;
  const html = value ? highlightPython(value) : "";
  $("editor-highlight").innerHTML = value.endsWith("\n") ? `${html}\n` : html;
}

/** 清空行号栏 / 高亮层 / 当前行标记（换文件或关弹窗时用）。 */
function resetEditorChrome() {
  $("editor-highlight").innerHTML = "";
  $("editor-gutter").innerHTML = "";
  $("editor-current-line").hidden = true;
  gutterLines = [];
  gutterCount = -1;
  gutterActive = -1;
}

/** 重画行号栏。行数没变时直接复用已有节点，避免每次敲键都重建 DOM。 */
function updateGutter() {
  const total = $("editor-text").value.split("\n").length;
  if (total === gutterCount) return;
  const parts = new Array(total);
  for (let index = 0; index < total; index += 1) parts[index] = `<span class="gl">${index + 1}</span>`;
  const gutter = $("editor-gutter");
  gutter.innerHTML = parts.join("");
  gutterLines = gutter.querySelectorAll(".gl");
  gutterCount = total;
  gutterActive = -1;
}

/** 高亮层和行号栏跟着 textarea 一起滚。 */
function syncEditorScroll() {
  const text = $("editor-text");
  $("editor-highlight").scrollTop = text.scrollTop;
  $("editor-highlight").scrollLeft = text.scrollLeft;
  $("editor-gutter").scrollTop = text.scrollTop;
  updateActiveLine();
}

/** 当前行：正文画一条底色，行号栏对应数字加亮。 */
function updateActiveLine() {
  const text = $("editor-text");
  const marker = $("editor-current-line");
  const hasFile = Boolean(editorUi.path);
  const row = hasFile ? text.value.slice(0, text.selectionStart).split("\n").length - 1 : 0;

  if (gutterActive !== row && gutterLines[gutterActive]) gutterLines[gutterActive].classList.remove("active");
  gutterActive = row;
  if (hasFile && gutterLines[row]) gutterLines[row].classList.add("active");

  if (!hasFile) {
    marker.hidden = true;
    return;
  }
  const top = EDITOR_PAD_TOP + row * EDITOR_LINE_HEIGHT - text.scrollTop;
  if (top + EDITOR_LINE_HEIGHT <= 0 || top >= text.clientHeight) {
    marker.hidden = true;
    return;
  }
  marker.hidden = false;
  marker.style.transform = `translateY(${top}px)`;
}

/** 顶栏徽章：工具属性 / 开放范围 / 历史版本数。 */
function renderEditorBadges() {
  const { backups, real, usedBy } = editorUi.badgeParts;
  const items = [];
  if (editorUi.source === "tool") items.push(real ? "真实实现（有 run 函数）" : "模拟返回（没有 run 函数）");
  if (usedBy.length) items.push(`已向 ${usedBy.join("、")} 开放`);
  if (backups) items.push(`已有 ${backups} 个历史版本`);
  $("editor-badges").innerHTML = items
    .map((text) => `<span class="tag">${escapeHtml(text)}</span>`)
    .join("");
}

/** 只建一个最小骨架，SPEC 和实现全部自己写——控制台不再替你猜参数 schema。 */
async function newGlobalTool() {
  if (editorUi.source !== "tool") return;
  const input = window.prompt("新工具的文件名（小写字母 / 数字 / 下划线，字母开头，2-64 位）：\n例如 search_course_catalog");
  if (input === null) return;
  const name = input.trim();
  if (!name) return;
  try {
    await api("/api/tools", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    await loadTools();
    await loadEditorSide();
    await openEditorFile(`${name}.py`, true);
    $("editor-msg").textContent = `已新建 ${name}.py：骨架里只有空的 SPEC，描述、参数和 run() 都要自己写。`;
    toast(`已新建 ${name}.py。`, "ok");
  } catch (error) {
    toast(error.message, "err");
  }
}

function syncEditorState() {
  const text = $("editor-text");
  editorUi.dirty = text.value !== editorUi.baseline;

  const flag = $("file-dirty");
  flag.textContent = editorUi.dirty ? "● 未保存" : editorUi.path ? "已保存" : "";
  flag.className = editorUi.dirty ? "hint dirty" : "hint";

  updateActiveLine();

  if (!editorUi.path) {
    $("editor-status").textContent = "";
    return;
  }
  const before = text.value.slice(0, text.selectionStart);
  const row = before.split("\n").length;
  const column = text.selectionStart - before.lastIndexOf("\n");
  $("editor-status").textContent =
    `${editorUi.path}　第 ${row} 行 第 ${column} 列　共 ${text.value.split("\n").length} 行`;
}

/** Tab / Shift+Tab：单行插入 4 空格，多行整体缩进或反缩进。 */
function handleEditorTab(event) {
  const text = event.target;
  const value = text.value;
  const start = text.selectionStart;
  const end = text.selectionEnd;
  const indent = "    ";

  if (start === end) {
    if (event.shiftKey) return;
    event.preventDefault();
    text.setRangeText(indent, start, end, "end");
    onEditorInput();
    return;
  }

  event.preventDefault();
  const blockStart = value.lastIndexOf("\n", start - 1) + 1;
  let blockEnd = value.indexOf("\n", end);
  if (blockEnd === -1) blockEnd = value.length;

  let firstDelta = 0;
  let totalDelta = 0;
  const next = value
    .slice(blockStart, blockEnd)
    .split("\n")
    .map((line, index) => {
      if (event.shiftKey) {
        const matched = line.match(/^ {1,4}/);
        const removed = matched ? matched[0].length : 0;
        if (index === 0) firstDelta = -removed;
        totalDelta -= removed;
        return line.slice(removed);
      }
      if (index === 0) firstDelta = indent.length;
      totalDelta += indent.length;
      return indent + line;
    });

  text.setRangeText(next.join("\n"), blockStart, blockEnd, "preserve");
  text.setSelectionStart(Math.max(blockStart, start + firstDelta));
  text.setSelectionEnd(Math.max(blockStart, end + totalDelta));
  onEditorInput();
}

async function saveDomainFile() {
  if (!editorUi.path) return;
  const save = $("btn-file-save");
  save.disabled = true;
  $("editor-msg").textContent = "保存中…";

  try {
    const content = $("editor-text").value;
    const result = await api(editorUrl(), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(editorPayload({ content })),
    });
    editorUi.baseline = content;
    editorUi.dirty = false;
    syncEditorState();
    $("editor-msg").textContent = result.changed
      ? `已保存；旧内容备份为 ${result.backup}`
      : "内容没有变化，未写盘。";
    if (result.changed) {
      editorUi.badgeParts.backups += 1;
      renderEditorBadges();
    }
    if (result.changed) {
      toast(editorUi.source === "tool" ? `已保存工具 ${result.name}。` : `已保存 ${editorUi.target}/${result.path}。`, "ok");
    }

    await loadEditorSide();
    if (editorUi.source === "tool") {
      await loadTools();
      return;
    }
    // spec.py 的 display_name 会显示在领域卡片和下拉框里，改完要一起刷新。
    if (editorUi.path === "spec.py" || editorUi.path === "__init__.py") {
      await loadMeta();
      await loadDomains();
    }
  } catch (error) {
    $("editor-msg").textContent = error.message;
    toast(error.message, "err");
  } finally {
    save.disabled = Boolean(domainUi.activeJobId) && editorUi.source === "domain";
  }
}

function reloadDomainFile() {
  if (!editorUi.path) return;
  if (editorUi.dirty && !window.confirm("放弃未保存的修改？")) return;
  editorUi.dirty = false;
  openEditorFile(editorUi.path, true);
}

async function showFileHistory() {
  if (!editorUi.path) return;
  try {
    const payload = await api(editorUi.source === "tool" ? editorUrl("/history") : `${editorUrl("/history")}?path=${encodeURIComponent(editorUi.path)}`, { cache: "no-store" });
    const label = editorUi.source === "tool" ? payload.name : payload.path;
    $("history-title").textContent = `${label} 的历史版本（${payload.backups.length}）`;
    const list = $("history-list");
    list.innerHTML = "";
    if (!payload.backups.length) {
      list.innerHTML = emptyHtml({
        icon: "clock",
        title: "还没有历史版本",
        desc: "在编辑器里每保存一次，旧内容都会自动留一份备份，随时可以回看。",
        variant: "inline compact",
      });
    } else {
      for (const item of payload.backups) {
        const row = document.createElement("div");
        row.className = "history-row";
        row.innerHTML = `<span><span class="mono">${escapeHtml(item.backup)}</span><span class="hint">${formatTime(item.mtime)}　${formatBytes(item.size)}</span></span>`;
        const button = document.createElement("button");
        button.className = "ghost small";
        button.textContent = "恢复这一版";
        button.addEventListener("click", () => restoreDomainFile(item.backup));
        row.appendChild(button);
        list.appendChild(row);
      }
    }
    $("history-pane").hidden = false;
  } catch (error) {
    toast(error.message, "err");
  }
}

function hideFileHistory() {
  $("history-pane").hidden = true;
}

async function restoreDomainFile(backup) {
  try {
    const result = await api(editorUrl("/restore"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(editorPayload({ backup })),
    });
    $("editor-text").value = result.content;
    editorUi.baseline = result.content;
    editorUi.dirty = false;
    hideFileHistory();
    onEditorInput();
    $("editor-msg").textContent = `已恢复到 ${result.restored}（恢复前的版本也留了备份，可以再换回去）`;
    toast(`已恢复到 ${result.restored}。`, "ok");
    await loadEditorSide();
    if (editorUi.source === "tool") await loadTools();
  } catch (error) {
    toast(error.message, "err");
  }
}

init();
