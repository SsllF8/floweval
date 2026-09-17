/* FlowEval 控制台交互
   只做三件事：按 schema 渲染表单、发起评测并轮询进度、把结果渲染成可读结论。
   所有指标都由后端算好，这里不重复计算 —— 保证和终端报告同一个数字。 */

const $ = (id) => document.getElementById(id);
const state = {
  targets: [],
  kind: null,
  datasets: [],
  runs: [],
  polling: null,
};

/* ---------------------------------------------------------------- 请求 */

async function api(path, options) {
  const res = await fetch(path, options);
  const text = await res.text();
  let data = null;
  try {
    data = JSON.parse(text);
  } catch (_) {
    data = { error: text.slice(0, 200) };
  }
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

const esc = (s) =>
  String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));

const pct = (v) => `${(Number(v) * 100).toFixed(1)}%`;
const num = (v, d = 2) => (v == null ? "—" : Number(v).toFixed(d));

/* ---------------------------------------------------------------- 被测对象 */

function renderTargets() {
  const grid = $("target-grid");
  grid.innerHTML = state.targets
    .map((t) => `
      <button class="target-card ${t.name === state.kind ? "is-active" : ""}" data-kind="${esc(t.name)}" type="button">
        <div class="target-card-top">
          <span class="target-card-name">${esc(t.label || t.name)}</span>
          ${t.badge ? `<span class="badge badge-soft">${esc(t.badge)}</span>` : ""}
        </div>
        <div class="target-card-desc">${esc(t.description || "")}</div>
      </button>`)
    .join("");

  grid.querySelectorAll(".target-card").forEach((el) => {
    el.addEventListener("click", () => {
      state.kind = el.dataset.kind;
      renderTargets();
      renderConfigForm();
    });
  });
}

function currentSchema() {
  return state.targets.find((t) => t.name === state.kind) || null;
}

function renderConfigForm() {
  const schema = currentSchema();
  const box = $("config-form");
  if (!schema) {
    box.innerHTML = `<div class="caption">请先选择被测对象</div>`;
    return;
  }

  box.innerHTML = schema.fields
    .map((f) => {
      const wide = f.type === "textarea";
      const id = `cfg-${f.key}`;
      const req = f.required ? `<span class="req">*</span>` : "";
      let control = "";

      if (f.type === "select") {
        control = `<select class="select" id="${id}">${f.options
          .map((o) => `<option value="${esc(o)}" ${String(o) === String(f.default) ? "selected" : ""}>${esc(o)}</option>`)
          .join("")}</select>`;
      } else if (f.type === "textarea") {
        control = `<textarea class="textarea ${["body_template", "headers", "workflow"].includes(f.key) ? "mono" : ""}" id="${id}" placeholder="${esc(f.placeholder || "")}">${esc(f.default || "")}</textarea>`;
      } else if (f.type === "password") {
        control = `<input class="input" type="password" id="${id}" placeholder="${esc(f.placeholder || "")}" autocomplete="off">`;
      } else if (f.type === "number") {
        control = `<input class="input" type="number" id="${id}" value="${esc(f.default)}">`;
      } else {
        control = `<input class="input" type="text" id="${id}" value="${esc(f.default || "")}" placeholder="${esc(f.placeholder || "")}">`;
      }

      return `<div class="field ${wide ? "span-2" : ""}">
        <label class="field-label" for="${id}">${esc(f.label)}${req}</label>
        ${control}
        ${f.help ? `<div class="field-help">${esc(f.help)}</div>` : ""}
      </div>`;
    })
    .join("");
}

function collectConfig() {
  const schema = currentSchema();
  const out = {};
  if (!schema) return out;
  for (const f of schema.fields) {
    const el = $(`cfg-${f.key}`);
    if (!el) continue;
    const v = el.value;
    if (v !== "") out[f.key] = v;
    if (f.required && v === "") throw new Error(`「${f.label}」是必填项`);
  }
  return out;
}

/* ---------------------------------------------------------------- 数据集 */

function renderDatasets(selected) {
  const sel = $("dataset-select");
  if (!state.datasets.length) {
    sel.innerHTML = `<option value="">（暂无数据集，请上传或粘贴）</option>`;
    return;
  }
  sel.innerHTML = state.datasets
    .map((d, i) => {
      const count = d.cases < 0 ? "格式有误" : `${d.cases} 条`;
      const picked = selected ? d.path === selected : i === 0;
      return `<option value="${esc(d.path)}" ${picked ? "selected" : ""}>${esc(d.name)} · ${esc(count)}${d.source ? ` · ${esc(d.source)}` : ""}</option>`;
    })
    .join("");
}

async function uploadDataset(filename, content) {
  const saved = await api("/api/datasets", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filename, content }),
  });
  state.datasets = (await api("/api/datasets")).datasets;
  renderDatasets(saved.path);
  return saved;
}

async function renderRuns() {
  state.runs = (await api("/api/runs")).runs || [];

  const base = $("opt-baseline");
  const keep = base.value;
  base.innerHTML =
    `<option value="">（不对比）</option>` +
    state.runs
      .map((r) => `<option value="${esc(r.run_id)}">${esc(r.run_id)} · ${esc(r.target_name)} · ${pct(r.pass_rate)}</option>`)
      .join("");
  if (keep) base.value = keep;

  $("side-foot").textContent = `${state.runs.length} 次历史评测`;

  const body = $("history-body");
  if (!state.runs.length) {
    body.innerHTML = `<div class="empty">还没有评测记录，先发起一次。</div>`;
    return;
  }
  body.innerHTML = state.runs
    .map((r) => `
      <div class="run-row">
        <div class="run-row-id mono">${esc(r.run_id)}</div>
        <div class="run-row-target">${esc(r.target_name)} · ${esc(r.dataset || "")}</div>
        <div class="run-row-num">${pct(r.pass_rate)}</div>
        <div class="run-row-num">${num(r.mean_score, 3)}</div>
        <div class="run-row-actions">
          <button class="btn btn-sm" data-view-run="${esc(r.run_id)}" type="button">查看</button>
          <button class="btn btn-sm btn-danger" data-del-run="${esc(r.run_id)}" type="button">删除</button>
        </div>
      </div>`)
    .join("");

  body.querySelectorAll("[data-view-run]").forEach((el) =>
    el.addEventListener("click", () => loadRun(el.dataset.viewRun)));
  body.querySelectorAll("[data-del-run]").forEach((el) =>
    el.addEventListener("click", async () => {
      if (!confirm(`删除 ${el.dataset.delRun}？`)) return;
      await api(`/api/runs/${el.dataset.delRun}`, { method: "DELETE" });
      await renderRuns();
    }));
}

/* ---------------------------------------------------------------- 发起评测 */

function setProgress(done, total, status, message) {
  const fill = $("progress-fill");
  const text = $("progress-text");
  const ratio = total ? Math.min(1, done / total) : 0;
  fill.style.width = `${(ratio * 100).toFixed(1)}%`;
  fill.classList.toggle("is-error", status === "error");
  text.classList.toggle("is-error", status === "error");
  text.textContent = message;
}

async function startRun() {
  const btn = $("btn-run");
  const panel = $("result-panel");
  panel.innerHTML = "";

  let payload;
  try {
    const dataset = $("dataset-select").value;
    if (!dataset) throw new Error("请先选择或上传一个数据集");
    payload = {
      target: state.kind,
      config: collectConfig(),
      dataset,
      workers: Number($("opt-workers").value || 4),
      retries: Number($("opt-retries").value || 0),
      baseline: $("opt-baseline").value || null,
      gate: $("opt-gate").checked
        ? {
            min_pass_rate: Number($("gate-pass").value || 0.9),
            min_mean_score: Number($("gate-score").value || 0.85),
            max_p95_latency_ms: $("gate-p95").value || null,
            max_total_cost: $("gate-cost").value || null,
          }
        : null,
    };
  } catch (err) {
    setProgress(0, 0, "error", err.message);
    return;
  }

  btn.disabled = true;
  setProgress(0, 1, "running", "正在创建任务…");

  let job;
  try {
    job = await api("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (err) {
    btn.disabled = false;
    setProgress(0, 1, "error", `发起失败：${err.message}`);
    return;
  }

  // 轮询进度。任务跑完后 job 会被回收，此时 status 接口的 run_id 就是真实 id。
  try {
    const runId = await new Promise((resolve, reject) => {
      const timer = setInterval(async () => {
        let s;
        try {
          s = await api(`/api/runs/${job.job_id}/status`);
        } catch (err) {
          clearInterval(timer);
          reject(err);
          return;
        }
        if (s.status === "running") {
          setProgress(s.done, s.total, "running", `运行中 ${s.done}/${s.total}`);
          return;
        }
        clearInterval(timer);
        if (s.status === "error") {
          setProgress(1, 1, "error", s.error || "任务失败");
          reject(new Error(s.error || "任务失败"));
          return;
        }
        setProgress(s.total, s.total, "done", "完成");
        resolve(s.run_id);
      }, 400);
      state.polling = timer;
    });

    const data = await api(`/api/runs/${runId}`);
    renderResult(data);
    await renderRuns();
  } catch (err) {
    panel.innerHTML = `<div class="notice is-error" style="margin-top:16px">${esc(err.message)}</div>`;
  } finally {
    btn.disabled = false;
  }
}

async function loadRun(runId) {
  switchView("new");
  const panel = $("result-panel");
  panel.innerHTML = `<div class="caption">加载中…</div>`;
  try {
    const data = await api(`/api/runs/${runId}`);
    renderResult(data);
  } catch (err) {
    panel.innerHTML = `<div class="notice is-error" style="margin-top:16px">${esc(err.message)}</div>`;
  }
}

/* ---------------------------------------------------------------- 结果 */

function renderResult(data) {
  const s = data.summary || {};
  const gate = data.gate;
  const reg = data.regression;
  const panel = $("result-panel");

  const verdict = gate
    ? gate.ok
      ? `<span class="result-verdict">通过 · 可以上线</span>`
      : `<span class="result-verdict is-fail">不通过 · 不许上线</span>`
    : `<span class="result-verdict">评测完成</span>`;

  const ruleCard = gate && gate.rules
    ? `<div class="card" style="margin-top:16px">
         <div class="card-head"><h2>判定规则</h2></div>
         <div class="card-body"><div class="rule-list">
           ${gate.rules.map((r) => `
             <div class="rule">
               <span class="rule-mark" style="color:${r.passed ? "var(--color-ink)" : "var(--color-ember)"}">${r.passed ? "✓" : "✕"}</span>
               <span class="rule-name">${esc(r.name)}</span>
               <span class="rule-msg">${esc(r.message)}</span>
             </div>`).join("")}
         </div></div>
       </div>`
    : "";

  // blame_distribution 在 run 顶层，不在 summary 里
  const blame = data.blame_distribution || {};
  const blameKeys = Object.keys(blame);
  const blameCard = blameKeys.length
    ? `<div class="card" style="margin-top:16px">
         <div class="card-head"><h2>失败归因到节点</h2></div>
         <div class="card-body">
           ${blameKeys.sort((a, b) => blame[b] - blame[a]).map((k) => `
             <div class="blame-row"><span>${esc(k)}</span><span class="num">${blame[k]} 条</span></div>`).join("")}
           <div class="field-help" style="margin-top:12px">归因逻辑：失败节点优先；若没有失败节点，则取最后一个真正改变输出的节点（原样透传的审查节点不背锅）。</div>
         </div>
       </div>`
    : "";

  const regDeltas = reg ? (reg.deltas || []).filter((d) => d.status === "regression") : [];
  const regCard = reg
    ? `<div class="card" style="margin-top:16px">
         <div class="card-head">
           <h2>回归对比</h2>
           <span class="badge ${reg.summary.regressions ? "badge-danger" : "badge-soft"}">
             ${reg.summary.regressions ? `${reg.summary.regressions} 条退化` : "无退化"}
           </span>
         </div>
         <div class="card-body">
           <div class="result-grid">
             <div class="stat"><div class="stat-label">退化</div><div class="stat-value" style="color:${reg.summary.regressions ? "var(--color-ember)" : "inherit"}">${reg.summary.regressions}</div></div>
             <div class="stat"><div class="stat-label">改进</div><div class="stat-value">${reg.summary.improvements}</div></div>
             <div class="stat"><div class="stat-label">新增用例</div><div class="stat-value">${(reg.new_cases || []).length}</div></div>
             <div class="stat"><div class="stat-label">移除用例</div><div class="stat-value">${(reg.removed_cases || []).length}</div></div>
           </div>
           ${regDeltas.length ? `
             <div class="field-help" style="margin-top:16px;font-weight:500;color:var(--color-ink)">退化用例 · 归因节点</div>
             ${regDeltas.slice(0, 15).map((d) => `
               <div class="blame-row">
                 <span>${esc(d.case_id)} · ${esc(d.input || "")}</span>
                 <span class="num">${esc(d.current_blame || "unknown")}</span>
               </div>`).join("")}` : ""}
         </div>
       </div>`
    : "";

  const failures = (data.results || []).filter((r) => r.passed === false);
  const failCard = failures.length
    ? `<div class="card" style="margin-top:16px">
         <div class="card-head"><h2>未通过用例（${failures.length}）</h2></div>
         <div class="card-body">
           ${failures.slice(0, 20).map((r) => `
             <div class="blame-row">
               <span>${esc(r.case_id)} · ${esc(r.input || "")}</span>
               <span class="num">${esc((r.failed_scorers || []).join(", ") || r.error || "—")}</span>
             </div>`).join("")}
         </div>
       </div>`
    : "";

  panel.innerHTML = `
    <div class="card" style="margin-top:16px">
      <div class="card-body">
        <div class="result-head">
          ${verdict}
          <span class="badge badge-outline mono">${esc(data.run_id)}</span>
          <span class="badge badge-soft">${esc(data.target_name)}</span>
          <a class="btn btn-sm" href="/index.html" target="_blank" rel="noopener">在看板中查看</a>
        </div>
        <div class="result-grid">
          <div class="stat"><div class="stat-label">通过率</div><div class="stat-value">${pct(s.pass_rate)}</div><div class="stat-foot">${s.passed || 0} / ${s.total || 0}</div></div>
          <div class="stat"><div class="stat-label">平均分</div><div class="stat-value">${num(s.mean_score, 3)}</div></div>
          <div class="stat"><div class="stat-label">P95 延迟</div><div class="stat-value">${s.p95_latency_ms == null ? "—" : `${Math.round(s.p95_latency_ms)}<span style="font-size:13px;color:var(--color-mid-gray)"> ms</span>`}</div></div>
          <div class="stat"><div class="stat-label">总成本</div><div class="stat-value">${num(s.total_cost, 4)}</div></div>
        </div>
      </div>
    </div>
    ${ruleCard}${regCard}${blameCard}${failCard}`;
}

/* ---------------------------------------------------------------- 视图切换 */

const VIEW_META = {
  new: { crumb: "发起评测", title: "发起一次评测", sub: "选被测对象 → 填连接配置 → 选数据集 → 点开始" },
  history: { crumb: "历史记录", title: "历史评测", sub: "点「查看」把某次结果载回结果区" },
  guide: { crumb: "怎么用", title: "怎么用", sub: "数据集长什么样、每个选项管什么" },
};

function switchView(name) {
  document.querySelectorAll(".nav-item").forEach((el) =>
    el.classList.toggle("is-active", el.dataset.view === name));
  for (const key of Object.keys(VIEW_META)) {
    $(`view-${key}`).hidden = key !== name;
  }
  const meta = VIEW_META[name];
  $("crumb-view").textContent = meta.crumb;
  $("page-title").textContent = meta.title;
  $("page-sub").textContent = meta.sub;
  if (name === "history") renderRuns();
}

/* ---------------------------------------------------------------- 启动 */

async function boot() {
  document.querySelectorAll(".nav-item").forEach((el) =>
    el.addEventListener("click", () => switchView(el.dataset.view)));

  $("btn-run").addEventListener("click", startRun);
  $("opt-gate").addEventListener("change", (e) => {
    $("gate-form").hidden = !e.target.checked;
  });
  $("btn-refresh-history").addEventListener("click", renderRuns);

  $("dataset-file").addEventListener("change", async (e) => {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    try {
      const text = await file.text();
      await uploadDataset(file.name, text);
      setProgress(0, 1, "done", `已上传 ${file.name}`);
    } catch (err) {
      setProgress(0, 1, "error", `上传失败：${err.message}`);
    }
  });

  $("btn-save-dataset").addEventListener("click", async () => {
    const content = $("dataset-text").value.trim();
    if (!content) {
      setProgress(0, 1, "error", "请先粘贴数据集内容");
      return;
    }
    const name = $("dataset-name").value.trim() || "my-cases.jsonl";
    try {
      await uploadDataset(name, content);
      setProgress(0, 1, "done", `已保存 ${name}`);
    } catch (err) {
      setProgress(0, 1, "error", `保存失败：${err.message}`);
    }
  });

  try {
    const health = await api("/api/health");
    $("side-foot").textContent = health.ok ? "后端已连接" : "后端异常";
  } catch (err) {
    $("side-foot").textContent = "后端未连接";
    $("result-panel").innerHTML =
      `<div class="notice is-error" style="margin-top:16px">连不上后端：${esc(err.message)}</div>`;
    return;
  }

  state.targets = await api("/api/targets");
  state.kind = state.targets[0] ? state.targets[0].name : null;
  renderTargets();
  renderConfigForm();

  state.datasets = (await api("/api/datasets")).datasets;
  renderDatasets();

  await renderRuns();
}

boot();
