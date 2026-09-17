/* FlowEval 看板交互
   无框架、无构建 —— 直接改这个文件就能调整看板行为。
   数据契约见 build_dashboard_payload()。 */

(function () {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const pct = (v) => (v * 100).toFixed(1) + "%";
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])
  );

  let DATA = null;
  let filter = { status: "all", category: "all", text: "" };

  // ------------------------------------------------------------ 数据加载

  function loadData() {
    const inline = document.getElementById("floweval-data");
    if (inline && inline.textContent.trim()) {
      try {
        return Promise.resolve(JSON.parse(inline.textContent));
      } catch (e) {
        console.warn("内联数据解析失败", e);
      }
    }
    return fetch("data/latest.json").then((r) => {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }

  // ------------------------------------------------------------ 总览

  function renderHead() {
    const s = DATA.summary || {};
    $("#crumb-target").textContent = DATA.target_name || "—";
    $("#page-sub").textContent =
      `${DATA.dataset || "—"} · ${s.total || 0} 条用例 · ${DATA.started_at || ""}`;
    $("#side-run").textContent = DATA.run_id || "—";

    $("#s-pass-rate").textContent = s.pass_rate != null ? pct(s.pass_rate) : "—";
    $("#s-pass-detail").textContent = `${s.passed || 0} 通过 / ${s.failed || 0} 失败`;
    $("#s-score").textContent = s.mean_score != null ? s.mean_score.toFixed(3) : "—";
    $("#s-score-detail").textContent = "加权平均";
    $("#s-latency").textContent = s.p95_latency_ms != null ? Math.round(s.p95_latency_ms) + "ms" : "—";
    $("#s-latency-detail").textContent = "P95";
    $("#s-cost").textContent = s.total_cost != null ? s.total_cost.toFixed(4) : "—";
    $("#s-cost-detail").textContent = `${s.total_tokens || 0} tokens`;
  }

  function renderGate() {
    const gate = DATA.gate;
    const card = $("#gate-card");
    if (!gate) {
      card.hidden = true;
      $("#gate-detail").innerHTML = '<div class="empty">本次运行未启用上线判定（加 --gate 参数）</div>';
      return;
    }
    $("#gate-verdict").textContent = gate.ok ? "PASS · 可以发布" : "FAIL · 不允许发布";
    $("#gate-verdict").className = "gate-verdict " + (gate.ok ? "pass" : "fail");
    const failed = (gate.rules || []).filter((r) => !r.passed).length;
    $("#gate-note").textContent = gate.ok
      ? "全部规则通过"
      : `${failed} / ${gate.rules.length} 项规则未达标`;
    $("#gate-rules").replaceChildren(...(gate.rules || []).map(ruleRow));
    $("#gate-detail").replaceChildren(...(gate.rules || []).map(ruleRow));
  }

  function ruleRow(r) {
    const row = el("div", "rule");
    const mark = el("span", "rule-mark " + (r.passed ? "ok" : "no"), r.passed ? "✓" : "✗");
    row.append(mark, el("span", "rule-name", r.name), el("span", "rule-msg", r.message || ""));
    return row;
  }

  function renderBlame() {
    const dist = DATA.blame_distribution || {};
    const box = $("#blame-chart");
    const entries = Object.entries(dist).sort((a, b) => b[1] - a[1]);
    if (!entries.length) {
      box.innerHTML = '<div class="empty">无失败用例，无需归因</div>';
      return;
    }
    const total = entries.reduce((n, [, c]) => n + c, 0);
    const max = Math.max(...entries.map(([, c]) => c));
    box.replaceChildren(
      ...entries.map(([node, count], i) => {
        const row = el("div", "bar-row" + (i === 0 ? " is-top" : ""));
        const label = el("div", "bar-label", node);
        const track = el("div", "bar-track");
        const fill = el("div", "bar-fill");
        fill.style.width = Math.max(4, (count / max) * 100) + "%";
        track.append(fill);
        const value = el("div", "bar-value", `${count} 条 · ${pct(count / total)}`);
        row.append(label, track, value);
        return row;
      })
    );
  }

  function renderCategories() {
    const cats = DATA.by_category || {};
    const box = $("#category-table");
    if (!Object.keys(cats).length) {
      box.innerHTML = '<div class="empty">无分类数据</div>';
      return;
    }
    const table = el("table");
    const thead = el("thead");
    const hr = el("tr");
    ["类别", "用例", "通过率", "平均分"].forEach((h, i) => {
      const th = el("th", i >= 2 ? "num" : "", h);
      if (i >= 2) th.style.textAlign = "right";
      hr.append(th);
    });
    thead.append(hr);

    const tbody = el("tbody");
    Object.entries(cats)
      .sort((a, b) => a[1].pass_rate - b[1].pass_rate)
      .forEach(([name, st]) => {
        const tr = el("tr");
        tr.append(
          el("td", "", name),
          el("td", "num", String(st.total)),
          el("td", "num", pct(st.pass_rate)),
          el("td", "num", st.mean_score.toFixed(3))
        );
        tbody.append(tr);
      });
    table.append(thead, tbody);
    box.replaceChildren(table);
  }

  function renderNodes() {
    const stats = DATA.node_stats || {};
    const box = $("#node-table");
    const entries = Object.entries(stats);
    if (!entries.length) {
      box.innerHTML = '<div class="empty">被测对象未提供节点级信息</div>';
      return;
    }
    const table = el("table");
    const thead = el("thead");
    const hr = el("tr");
    ["节点", "调用次数", "平均耗时", "最大耗时", "失败率"].forEach((h, i) => {
      const th = el("th", i ? "num" : "", h);
      if (i) th.style.textAlign = "right";
      hr.append(th);
    });
    thead.append(hr);

    const tbody = el("tbody");
    entries
      .sort((a, b) => b[1].fail_rate - a[1].fail_rate)
      .forEach(([name, s]) => {
        const tr = el("tr");
        const rate = el("td", "num", pct(s.fail_rate));
        if (s.fail_rate > 0) rate.style.color = "var(--color-ember)";
        tr.append(
          el("td", "", name),
          el("td", "num", String(s.calls)),
          el("td", "num", s.avg_ms + " ms"),
          el("td", "num", s.max_ms + " ms"),
          rate
        );
        tbody.append(tr);
      });
    table.append(thead, tbody);
    box.replaceChildren(table);
  }

  function renderHistory() {
    const h = DATA.history || [];
    const card = $("#history-card");
    if (h.length < 2) {
      card.hidden = true;
      return;
    }
    card.hidden = false;
    const box = $("#history-chart");
    const w = 1000, hgt = 72, pad = 4;
    const pts = h.map((r, i) => {
      const x = pad + (i * (w - pad * 2)) / Math.max(1, h.length - 1);
      const y = hgt - pad - r.pass_rate * (hgt - pad * 2);
      return [x, y];
    });
    const line = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
    const area = line + ` L${w - pad} ${hgt - pad} L${pad} ${hgt - pad} Z`;
    const last = h[h.length - 1];

    const svg = `<svg class="spark" viewBox="0 0 ${w} ${hgt}" preserveAspectRatio="none">
      <path d="${area}" fill="rgba(23,23,23,0.06)"/>
      <path d="${line}" fill="none" stroke="#0a0a0a" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
    </svg>`;
    box.innerHTML = svg;
    const legend = el("div", "spark-legend");
    legend.append(
      el("span", "", `${h.length} 次运行`),
      el("span", "", `最新通过率 ${pct(last.pass_rate)}`),
      el("span", "", `最新平均分 ${last.mean_score.toFixed(3)}`)
    );
    box.append(legend);
  }

  // ------------------------------------------------------------ 用例明细

  function renderFilters() {
    const cats = ["all", ...new Set((DATA.results || []).map((r) => r.category))];
    const statuses = [
      ["all", "全部"],
      ["failed", "仅失败"],
      ["passed", "仅通过"],
    ];
    const box = $("#case-filters");
    box.replaceChildren();
    statuses.forEach(([key, label]) => {
      const b = el("button", "chip" + (filter.status === key ? " is-active" : ""), label);
      b.onclick = () => { filter.status = key; renderFilters(); renderCases(); };
      box.append(b);
    });
    cats.forEach((c) => {
      if (c === "all") return;
      const b = el("button", "chip" + (filter.category === c ? " is-active" : ""), c);
      b.onclick = () => {
        filter.category = filter.category === c ? "all" : c;
        renderFilters(); renderCases();
      };
      box.append(b);
    });
  }

  function visibleCases() {
    return (DATA.results || []).filter((r) => {
      if (filter.status === "failed" && r.passed) return false;
      if (filter.status === "passed" && !r.passed) return false;
      if (filter.category !== "all" && r.category !== filter.category) return false;
      if (filter.text) {
        const hay = (r.input + " " + r.output + " " + r.case_id).toLowerCase();
        if (!hay.includes(filter.text.toLowerCase())) return false;
      }
      return true;
    });
  }

  function renderCases() {
    const list = $("#case-list");
    const cases = visibleCases();
    if (!cases.length) {
      list.innerHTML = '<div class="card"><div class="empty">没有匹配的用例</div></div>';
      return;
    }
    list.replaceChildren(...cases.map(caseCard));
  }

  function caseCard(r) {
    const card = el("div", "case");
    const head = el("div", "case-head");

    const id = el("div", "case-id", r.case_id);
    const input = el("div", "case-input", r.input);
    const badge = r.passed
      ? el("span", "badge badge-soft", "通过")
      : el("span", "badge badge-danger", "失败");
    const score = el("div", "case-score", r.score.toFixed(2));
    const chev = document.createElement("span");
    chev.className = "case-chevron";
    chev.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 18l6-6-6-6"/></svg>';

    head.append(id, badge, input, score, chev);

    const detail = el("div", "case-detail");
    detail.hidden = true;

    head.onclick = () => {
      const open = card.classList.toggle("open");
      detail.hidden = !open;
      if (open && !detail.dataset.built) {
        buildDetail(detail, r);
        detail.dataset.built = "1";
      }
    };

    card.append(head, detail);
    return card;
  }

  function buildDetail(box, r) {
    // 归因
    if (!r.passed) {
      const hint = el("div", "blame-hint");
      hint.innerHTML =
        '<span>归因节点</span><strong>' + esc(r.blame || "unknown") + "</strong>" +
        (r.failed_scorers && r.failed_scorers.length
          ? "<span>· 未通过：" + esc(r.failed_scorers.join("、")) + "</span>"
          : "");
      box.append(hint);
    }

    // 执行链路
    if (r.steps && r.steps.length) {
      box.append(section("执行链路"));
      const steps = el("div", "steps");
      r.steps.forEach((s) => {
        const row = el("div", "step " + s.status);
        row.append(
          el("div", "step-name", s.name),
          el("div", "step-out", s.output || (s.status === "skipped" ? "（已跳过）" : "")),
          el("div", "step-ms", s.elapsed_ms != null ? s.elapsed_ms + " ms" : "")
        );
        steps.append(row);
      });
      box.append(steps);
    }

    // 评分明细
    if (r.scores && r.scores.length) {
      box.append(section("评分明细"));
      const table = el("table");
      const thead = el("thead");
      const hr = el("tr");
      ["评分器", "结果", "得分", "权重", "说明"].forEach((h) => hr.append(el("th", "", h)));
      thead.append(hr);
      const tbody = el("tbody");
      r.scores.forEach((s) => {
        const tr = el("tr");
        const res = s.skipped
          ? el("span", "badge badge-outline", "跳过")
          : s.passed
          ? el("span", "badge badge-soft", "通过")
          : el("span", "badge badge-danger", "未通过");
        const td = el("td");
        td.append(res);
        tr.append(
          el("td", "", s.name),
          td,
          el("td", "num", s.score.toFixed(2)),
          el("td", "num", String(s.weight)),
          el("td", "", s.reason || "")
        );
        tbody.append(tr);
      });
      table.append(thead, tbody);
      box.append(table);
    }

    // 输出
    box.append(section("实际输出"));
    const out = el("div", "kv-body", r.output || "（空）");
    box.append(out);

    if (r.expected) {
      box.append(section("期望输出"));
      box.append(el("div", "kv-body", r.expected));
    }

    if (r.error) {
      box.append(section("执行错误"));
      const e = el("div", "kv-body", r.error);
      e.style.color = "var(--color-ember)";
      box.append(e);
    }

    const meta = el("div", "blame-hint");
    meta.innerHTML =
      `<span>耗时 ${r.latency_ms} ms</span><span>· ${r.tokens} tokens</span>` +
      `<span>· 成本 ${r.cost}</span>`;
    box.append(meta);
  }

  function section(title) {
    const wrap = el("div", "kv");
    wrap.append(el("div", "kv-label", title));
    return wrap;
  }

  // ------------------------------------------------------------ 回归

  function renderRegression() {
    const reg = DATA.regression;
    const body = $("#regression-body");
    if (!reg) {
      body.innerHTML = '<div class="empty">本次运行未做回归对比（用 --baseline 指定基线）</div>';
      return;
    }
    const s = reg.summary || {};
    $("#regression-sub").textContent =
      `${reg.baseline_run_id} → ${reg.current_run_id}`;

    const stats = el("div", "stat-grid");
    [
      ["退化", String(s.regressions || 0), "基线通过、本次失败"],
      ["改进", String(s.improvements || 0), "基线失败、本次通过"],
      ["净变化", (s.net > 0 ? "+" : "") + (s.net || 0), "改进 - 退化"],
      ["新增用例", String(s.new_cases || 0), "基线中不存在的用例"],
    ].forEach(([label, value, foot]) => {
      const st = el("div", "stat");
      st.append(el("div", "stat-label", label), el("div", "stat-value", value), el("div", "stat-foot", foot));
      stats.append(st);
    });
    body.replaceChildren(stats);

    const blame = reg.blame_distribution || {};
    if (Object.keys(blame).length) {
      body.append(section("退化归因"));
      const wrap = el("div");
      const maxCount = Math.max(...Object.values(blame));
      Object.entries(blame).forEach(([node, count]) => {
        const row = el("div", "bar-row");
        const track = el("div", "bar-track");
        const fill = el("div", "bar-fill");
        fill.style.width = Math.max(4, (count / maxCount) * 100) + "%";
        track.append(fill);
        row.append(el("div", "bar-label", node), track, el("div", "bar-value", count + " 条"));
        wrap.append(row);
      });
      body.append(wrap);
    }

    const regs = (reg.deltas || []).filter((d) => d.status === "regression");
    if (regs.length) {
      body.append(section("退化用例"));
      const list = el("div", "case-list");
      regs.forEach((d) => {
        const card = el("div", "case");
        const head = el("div", "case-head");
        head.append(
          el("div", "case-id", d.case_id),
          el("span", "badge badge-danger", "退化"),
          el("div", "case-input", d.input),
          el("div", "case-score", `${d.baseline_score.toFixed(2)} → ${d.current_score.toFixed(2)}`)
        );
        const detail = el("div", "case-detail");
        detail.append(el("div", "kv-body", "基线：" + (d.baseline_output || "（空）")));
        detail.append(el("div", "kv-body", "当前：" + (d.current_output || "（空）")));
        head.onclick = () => { detail.hidden = !detail.hidden; };
        card.append(head, detail);
        list.append(card);
      });
      body.append(list);
    }
  }

  // ------------------------------------------------------------ 导航

  const TITLES = {
    overview: ["评测总览", "总体指标与失败归因"],
    cases: ["用例明细", "逐条查看执行链路与评分"],
    nodes: ["节点分析", "每个节点的耗时与失败率"],
    regression: ["回归对比", "与基线版本的差异"],
    gate: ["上线判定", "发布门槛逐条核查"],
  };

  function bindNav() {
    document.querySelectorAll(".nav-item").forEach((btn) => {
      btn.onclick = () => {
        document.querySelectorAll(".nav-item").forEach((b) => b.classList.remove("is-active"));
        btn.classList.add("is-active");
        const key = btn.dataset.section;
        ["overview", "cases", "nodes", "regression", "gate"].forEach((k) => {
          const sec = document.getElementById("sec-" + k);
          if (sec) sec.hidden = k !== key;
        });
        const t = TITLES[key];
        $("#page-title").textContent = t[0];
        $(".page-head .sub").textContent = t[1] + " · " + (DATA.dataset || "");
      };
    });
    const search = $("#case-search");
    if (search) {
      search.oninput = () => { filter.text = search.value.trim(); renderCases(); };
    }
  }

  // ------------------------------------------------------------ 启动

  loadData()
    .then((data) => {
      DATA = data;
      renderHead();
      renderGate();
      renderBlame();
      renderCategories();
      renderNodes();
      renderHistory();
      renderFilters();
      renderCases();
      renderRegression();
      bindNav();
    })
    .catch((err) => {
      $("#page-sub").textContent = "数据加载失败：" + err.message;
      $(".container").insertAdjacentHTML(
        "afterbegin",
        '<div class="card"><div class="empty">' +
          "没有找到评测数据。<br>先跑一次：<code>floweval run -d cases.jsonl -t customer_service --gate</code>" +
          "<br>再执行：<code>floweval serve</code></div></div>"
      );
    });
})();
