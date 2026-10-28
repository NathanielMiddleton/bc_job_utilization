/* Job Utilization front end. Plain JS; Chart.js is optional. */
(function () {
  "use strict";

  const body = document.body;
  const BASE = body.dataset.base.endsWith("/") ? body.dataset.base : body.dataset.base + "/";
  const MAX_DAYS = parseInt(body.dataset.maxDays, 10) || 92;

  const form = document.getElementById("query");
  const f = form.elements;
  const els = {
    status: document.getElementById("status"),
    results: document.getElementById("results"),
    notes: document.getElementById("notes"),
    ledger: document.getElementById("ledger"),
    advice: document.getElementById("advice"),
    byPartition: document.getElementById("by-partition"),
    byUser: document.getElementById("by-user"),
    byAccount: document.getElementById("by-account"),
    byGpuType: document.getElementById("by-gpu-type"),
    cluster: document.getElementById("cluster"),
    jobsTitle: document.getElementById("jobs-title"),
    drill: document.getElementById("drill"),
    jobsHeader: document.getElementById("jobs-header"),
    jobsBody: document.getElementById("jobs-body"),
    filter: document.getElementById("filter"),
    modehelp: document.getElementById("modehelp"),
    go: document.getElementById("go"),
  };

  const MODE_HELP = {
    ended: "A job counts toward the window it finished in, and all of its consumption is attributed there — even the part of a long job that ran before the window opened. Jobs still running are not shown.",
    prorate: "Every job that was running at any point in the window counts, and its consumption is scaled to the share of its run time that fell inside the window. Efficiency percentages are always for the whole job.",
  };

  // ---------- formatting ----------
  const fmt = {
    pct(v, digits = 0) { return v == null ? "—" : (v * 100).toFixed(digits) + "%"; },
    hours(h) {
      if (h == null) return "—";
      if (h >= 10000) return Math.round(h).toLocaleString();
      if (h >= 100) return h.toFixed(0);
      if (h >= 10) return h.toFixed(1);
      return h.toFixed(2);
    },
    bytes(b) {
      if (b == null) return "—";
      const u = ["B", "KiB", "MiB", "GiB", "TiB"];
      let i = 0;
      while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
      return (b >= 100 ? b.toFixed(0) : b.toFixed(1)) + " " + u[i];
    },
    dur(s) {
      if (s == null) return "—";
      s = Math.round(s);
      const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
      const two = (n) => String(n).padStart(2, "0");
      return (d ? d + "-" : "") + two(h) + ":" + two(m) + ":" + two(sec);
    },
    time(iso) {
      if (!iso) return "—";
      return iso.replace("T", " ").slice(0, 16);
    },
    int(n) { return n == null ? "—" : Number(n).toLocaleString(); },
  };

  function effClass(v) {
    if (v == null) return "";
    if (v > 1.05) return "over";
    if (v < 0.25) return "verylow";
    if (v < 0.5) return "low";
    return "";
  }
  function effCell(v) {
    return `<span class="eff ${effClass(v)}">${fmt.pct(v)}</span>`;
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // ---------- form behaviour ----------
  function toLocalInput(d) {
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  function syncForm() {
    f.partition.hidden = f.scope.value !== "partition";
    const custom = f.range.value === "custom";
    form.querySelector(".custom").hidden = !custom;
    if (custom && !f.start.value) {
      const end = new Date(); const start = new Date(end.getTime() - 24 * 3600e3);
      f.start.value = toLocalInput(start); f.end.value = toLocalInput(end);
    }
    form.querySelector(".running").hidden = f.mode.value !== "prorate";
    els.modehelp.textContent = MODE_HELP[f.mode.value];
  }

  function windowFromForm() {
    if (f.range.value === "custom") {
      if (!f.start.value || !f.end.value) throw new Error("Choose both a start and an end for the custom range.");
      const s = new Date(f.start.value), e = new Date(f.end.value);
      if (!(s < e)) throw new Error("The start of the range must be before its end.");
      if ((e - s) / 864e5 > MAX_DAYS) throw new Error(`Ranges longer than ${MAX_DAYS} days are not allowed on this site.`);
      return { start: f.start.value, end: f.end.value };
    }
    const end = new Date(); end.setSeconds(0, 0);
    const start = new Date(end.getTime() - parseInt(f.range.value, 10) * 3600e3);
    return { start: toLocalInput(start), end: toLocalInput(end) };
  }

  function queryString(extra) {
    const w = windowFromForm();
    const q = new URLSearchParams({ start: w.start, end: w.end, ...extra });
    return q.toString();
  }

  async function loadPartitions() {
    try {
      const r = await fetch(BASE + "api/partitions");
      const data = await r.json();
      f.partition.innerHTML = data.partitions.length
        ? data.partitions.map((p) => `<option value="${esc(p)}">${esc(p)}</option>`).join("")
        : `<option value="">no partitions found</option>`;
    } catch (e) {
      f.partition.innerHTML = `<option value="">unavailable</option>`;
    }
  }

  // ---------- status ----------
  function showStatus(text, isError) {
    els.status.hidden = false;
    els.status.textContent = text;
    els.status.classList.toggle("error", !!isError);
  }
  function hideStatus() { els.status.hidden = true; }

  // ---------- rendering ----------
  let charts = {};
  let currentJobs = [];
  let sortKey = "end", sortAsc = false;

  function renderNotes(warnings, diag) {
    if (!warnings.length) { els.notes.innerHTML = ""; return; }
    let sample = "";
    if (diag && diag.sample) {
      sample = `<details class="raw"><summary>Raw accounting fields for job ${esc(diag.job)}</summary><table>
        <thead><tr><th>Step</th><th>TRESUsageInAve</th><th>TRESUsageInMax</th><th>TRESUsageInTot</th></tr></thead>
        <tbody>${diag.sample.map((st) => `<tr><td>${esc(st.step)}</td><td>${esc(st.TRESUsageInAve) || "<i>empty</i>"}</td><td>${esc(st.TRESUsageInMax) || "<i>empty</i>"}</td><td>${esc(st.TRESUsageInTot) || "<i>empty</i>"}</td></tr>`).join("")}</tbody></table>
        <p>Check the same thing yourself: <code>sacct -j ${esc(diag.job)} -o JobID,TRESUsageInAve%120</code></p></details>`;
    }
    els.notes.innerHTML = `<details open><summary>${warnings.length === 1 ? "One thing" : warnings.length + " things"} to know about these numbers</summary><ul>${warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>${sample}</details>`;
  }

  function ledgerRow(label, sub, eff, usedText, allocText, extra) {
    const cls = eff == null ? "none" : (eff < 0.5 ? "low" : "");
    const width = eff == null ? 0 : Math.min(eff, 1) * 100;
    return `<div class="row">
      <div class="label">${label}<small>${sub}</small></div>
      <div><div class="bar ${cls}" role="img" aria-label="${esc(label)} ${fmt.pct(eff)} used"><i style="width:${width}%"></i></div>
        <small style="color:var(--muted)">${usedText}${allocText ? " of " + allocText : ""}</small></div>
      <div class="pct">${fmt.pct(eff)}<small>${extra}</small></div>
    </div>`;
  }

  function renderLedger(data) {
    const s = data.summary, w = data.window;
    const idle = s.cpu_hours_alloc - s.cpu_hours_used;
    const scope = w.scope === "mine" ? `${esc(w.user)}'s` : w.scope === "all" ? "everyone's" : `partition ${esc(w.partition)}'s`;
    const statesLabel = { finished: "finished", completed: "completed", unsuccessful: "unsuccessful" }[w.states];
    const head = `<div class="headline">
      <span><b>${fmt.int(s.jobs)}</b> ${statesLabel} ${scope} jobs${w.mode === "prorate" ? " overlapping" : " ending"} ${fmt.time(w.start)} – ${fmt.time(w.end)}</span>
      <span><b>${fmt.hours(s.cpu_hours_alloc)}</b> CPU-hours allocated</span>
      <span><b>${fmt.hours(s.node_hours)}</b> node-hours</span>
      ${s.gpu_jobs ? `<span><b>${fmt.hours(s.gpu_hours)}</b> GPU-hours</span>` : ""}
      <span><b>${fmt.hours(s.wall_hours)}</b> wall-hours</span>
      <span>${Object.entries(s.states).map(([k, v]) => `<span class="state-${k}">${v} ${k.toLowerCase().replace(/_/g, " ")}</span>`).join(", ")}</span>
    </div>`;

    const rows = [
      ledgerRow("CPU", "time on allocated cores", s.cpu_eff_weighted,
        fmt.hours(s.cpu_hours_used) + " CPU-hours used", fmt.hours(s.cpu_hours_alloc) + " allocated",
        s.jobs ? `${fmt.hours(idle)} CPU-hours idle · mean per job ${fmt.pct(s.cpu_eff_mean)}` : "no jobs"),
      ledgerRow("Memory", "peak use vs allocated", s.mem_eff_weighted,
        fmt.hours(s.mem_gb_hours_used) + " GiB-hours used", fmt.hours(s.mem_gb_hours_req) + " allocated",
        s.mem_jobs_measured
          ? `${s.mem_jobs_measured} of ${s.jobs} jobs measured · mean ${fmt.pct(s.mem_eff_mean)}` + (s.mem_jobs_over ? ` · ${s.mem_jobs_over} over allocation, capped` : "")
          : "no measurements"),
    ];
    if (s.gpu_jobs) {
      const diag = data.gpu_util_diagnosis;
      rows.push(ledgerRow("GPU", "reported device utilization", s.gpu_util_weighted == null ? null : s.gpu_util_weighted / 100,
        s.gpu_util_jobs ? `${s.gpu_util_jobs} of ${s.gpu_jobs} GPU jobs report utilization`
                        : `Slurm recorded no gres/gpuutil for any of these jobs${diag ? " — see the notes above for why" : ""}`,
        "", `${fmt.hours(s.gpu_hours)} GPU-hours across ${s.gpu_jobs} jobs` + gpuTypeText(s.gpu_hours_by_type)));
    }
    rows.push(ledgerRow("Wall time", "run time vs time limit", s.wall_eff_weighted,
      "run time", "requested limit",
      s.wall_eff_weighted != null && s.wall_eff_weighted < 0.5 ? "limits could be shorter → faster scheduling" : "aggregate over jobs with a limit"));
    els.ledger.innerHTML = head + rows.join("");
  }

  function gpuTypeText(byType) {
    const entries = Object.entries(byType || {});
    if (entries.length < 1 || (entries.length === 1 && entries[0][0] === "untyped")) return "";
    return " · " + entries.map(([t, h]) => `${t} ${fmt.hours(h)} h`).join(", ");
  }

  function renderAdvice(cards) {
    if (!cards || !cards.length) { els.advice.innerHTML = ""; return; }
    const html = cards.map((c) => `<article class="${c.id === "ok" ? "ok" : ""}">
      <h3>${esc(c.title)}</h3>
      ${c.impact ? `<div class="impact">${esc(c.impact)}</div>` : ""}
      <p>${esc(c.detail)}</p>
      ${c.action ? `<pre>${esc(c.action)}</pre>` : ""}
      ${c.caveat ? `<div class="caveat">${esc(c.caveat)}</div>` : ""}
    </article>`).join("");
    els.advice.innerHTML = `<h2>What to try</h2><div class="grid">${html}</div>
      <p class="foot">These are rules of thumb computed from Slurm's accounting records, ordered by how much allocated time they could recover. They cannot see inside a program, so treat the numbers as starting points and keep a safety margin, especially on memory and time limits where undershooting kills the job.</p>`;
  }

  const HIST_LABELS = ["0–10", "10–20", "20–30", "30–40", "40–50", "50–60", "60–70", "70–80", "80–90", "90–100"];

  function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

  // ---------- drill-down: click a bar or a breakdown row to list exactly those jobs ----------
  let drill = null;   // { label, test(job), chartId, index }

  function withAlpha(hex, a) {
    const m = /^#([0-9a-f]{6})$/i.exec(hex.trim());
    if (!m) return hex;
    const n = parseInt(m[1], 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
  }

  function binIndex(v) { return v == null ? null : Math.min(Math.floor(Math.max(v, 0) * 10), 9); }

  function setDrill(d) {
    drill = d;
    Object.keys(charts).forEach(paintSelection);
    document.querySelectorAll(".breakdowns tr.selected").forEach((tr) => tr.classList.remove("selected"));
    if (d && d.rowEl) d.rowEl.classList.add("selected");
    if (lastData) renderJobs(lastData);
    if (d) document.querySelector(".jobs").scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
  }

  // Dim every bar except the selected one on the chart the selection came from.
  function paintSelection(id) {
    const chart = charts[id];
    if (!chart || !chart.$baseColors) return;
    chart.data.datasets.forEach((ds, di) => {
      const base = chart.$baseColors[di];
      ds.backgroundColor = base.map((c, i) => (drill && drill.chartId === id && drill.index !== i) ? withAlpha(c, 0.3) : c);
    });
    chart.update("none");
  }

  function drawChart(id, config) {
    const canvas = document.getElementById(id);
    const fig = canvas.closest("figure");
    let fallback = fig.querySelector(".nochart");
    if (charts[id]) { charts[id].destroy(); delete charts[id]; }
    if (typeof Chart === "undefined") {
      canvas.hidden = true;
      if (!fallback) { fallback = document.createElement("p"); fallback.className = "nochart"; fig.appendChild(fallback); }
      // No chart library: render the bins as buttons so drill-down still works.
      fallback.innerHTML = config.fallback.map((f, i) => `<button type="button" data-i="${i}">${esc(f)}</button>`).join(" ");
      fallback.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => config.onPick(parseInt(b.dataset.i, 10))));
      return;
    }
    if (fallback) fallback.remove();
    canvas.hidden = false;
    Chart.defaults.color = cssVar("--muted");
    Chart.defaults.borderColor = cssVar("--line");
    Chart.defaults.font.family = cssVar("--font");
    const opts = config.chart.options;
    opts.onClick = (evt, elements) => { if (elements.length) config.onPick(elements[0].index); };
    opts.onHover = (evt, elements) => { evt.native.target.style.cursor = elements.length ? "pointer" : "default"; };
    const chart = new Chart(canvas, config.chart);
    chart.$baseColors = config.chart.data.datasets.map((ds) => Array.isArray(ds.backgroundColor) ? ds.backgroundColor.slice() : config.chart.data.labels.map(() => ds.backgroundColor));
    charts[id] = chart;
    paintSelection(id);
  }

  // Tooltip footer: the first few job IDs in the hovered bar, and how to see the rest.
  function idsFooter(jobsFor) {
    return (items) => {
      const js = jobsFor(items[0].dataIndex);
      if (!js.length) return "";
      const shown = js.slice(0, 6).map((j) => j.id);
      return [shown.join(", ") + (js.length > shown.length ? `, +${js.length - shown.length} more` : ""), "Click to list these jobs"];
    };
  }

  function histChart(id, bins, label, jobs, key) {
    const colors = bins.map((_, i) => (i < 5 ? cssVar("--waste") : cssVar("--used")));
    const jobsFor = (i) => jobs.filter((j) => binIndex(j[key]) === i);
    const pick = (i) => {
      if (drill && drill.chartId === id && drill.index === i) { setDrill(null); return; }   // click again to clear
      setDrill({ chartId: id, index: i, label: `${label} ${HIST_LABELS[i]}%`, test: (j) => binIndex(j[key]) === i });
    };
    drawChart(id, {
      fallback: bins.map((n, i) => `${HIST_LABELS[i]}%: ${n}`),
      onPick: pick,
      chart: {
        type: "bar",
        data: { labels: HIST_LABELS, datasets: [{ label: "jobs", data: bins, backgroundColor: colors, borderRadius: 2 }] },
        options: {
          plugins: { legend: { display: false }, tooltip: { callbacks: { title: (c) => c[0].label + "% efficient", footer: idsFooter(jobsFor) } } },
          scales: { x: { grid: { display: false }, title: { display: true, text: "efficiency (%) — click a bar to list its jobs" } }, y: { beginAtZero: true, ticks: { precision: 0 }, title: { display: true, text: "jobs" } } },
        },
      },
    });
  }

  function partitionChart(byPartition, jobs) {
    const top = byPartition.slice(0, 12);
    const jobsFor = (i) => jobs.filter((j) => (j.partition || "(none)") === top[i].name);
    const pick = (i) => {
      if (drill && drill.chartId === "chart-part" && drill.index === i) { setDrill(null); return; }
      setDrill({ chartId: "chart-part", index: i, label: `partition ${top[i].name}`, test: (j) => (j.partition || "(none)") === top[i].name });
    };
    drawChart("chart-part", {
      fallback: top.map((p) => `${p.name}: ${fmt.hours(p.cpu_hours_used)} used / ${fmt.hours(p.cpu_hours_alloc)} allocated CPU-hours`),
      onPick: pick,
      chart: {
        type: "bar",
        data: {
          labels: top.map((p) => p.name),
          datasets: [
            { label: "used", data: top.map((p) => p.cpu_hours_used), backgroundColor: cssVar("--used") },
            { label: "idle", data: top.map((p) => Math.max(0, p.cpu_hours_alloc - p.cpu_hours_used)), backgroundColor: cssVar("--idle") },
          ],
        },
        options: {
          indexAxis: "y",
          scales: { x: { stacked: true, title: { display: true, text: "CPU-hours — click a bar to list its jobs" } }, y: { stacked: true, grid: { display: false } } },
          plugins: { legend: { position: "bottom" }, tooltip: { callbacks: { footer: idsFooter(jobsFor) } } },
        },
      },
    });
  }

  const DRILL_FIELD = { "Partition": "partition", "User": "user", "Account": "account", "GPU type": "gpu_type" };

  function breakdownTable(container, title, rows, nameHeader, alwaysShow) {
    if (!rows.length || (rows.length === 1 && nameHeader !== "Partition" && !alwaysShow)) { container.innerHTML = ""; return; }
    const gpuCols = nameHeader === "GPU type";
    const head = gpuCols
      ? `<th>${nameHeader}</th><th class="num">Jobs</th><th class="num">GPU-h</th><th class="num">GPU util</th><th class="num">CPU-h alloc</th><th class="num">CPU eff</th><th class="num">Mem eff</th>`
      : `<th>${nameHeader}</th><th class="num">Jobs</th><th class="num">CPU-h alloc</th><th class="num">CPU eff</th><th class="num">Mem eff</th><th class="num">GPU-h</th><th class="num">Node-h</th>`;
    const row = (r) => gpuCols
      ? `<td>${esc(r.name === "(none)" ? "untyped" : r.name)}</td><td class="num">${fmt.int(r.jobs)}</td><td class="num">${fmt.hours(r.gpu_hours)}</td><td class="num">${r.gpu_util_weighted == null ? "—" : effCell(r.gpu_util_weighted / 100)}</td><td class="num">${fmt.hours(r.cpu_hours_alloc)}</td><td class="num">${effCell(r.cpu_eff_weighted)}</td><td class="num">${effCell(r.mem_eff_weighted)}</td>`
      : `<td>${esc(r.name)}</td><td class="num">${fmt.int(r.jobs)}</td><td class="num">${fmt.hours(r.cpu_hours_alloc)}</td><td class="num">${effCell(r.cpu_eff_weighted)}</td><td class="num">${effCell(r.mem_eff_weighted)}</td><td class="num">${r.gpu_hours ? fmt.hours(r.gpu_hours) : "—"}</td><td class="num">${fmt.hours(r.node_hours)}</td>`;
    container.innerHTML = `<div class="card"><h3>${title}</h3><div style="overflow-x:auto"><table class="drillable">
      <thead><tr>${head}</tr></thead><tbody>${rows.map((r, i) => `<tr data-i="${i}" tabindex="0" title="List these jobs">${row(r)}</tr>`).join("")}</tbody></table></div>
      <p class="rowhint">Click a row to list its jobs.</p></div>`;
    const field = DRILL_FIELD[nameHeader];
    container.querySelectorAll("tbody tr").forEach((tr) => {
      const r = rows[parseInt(tr.dataset.i, 10)];
      const pick = () => {
        if (drill && drill.rowEl === tr) { setDrill(null); return; }
        const value = r.name === "(none)" ? "" : r.name;
        const test = field === "gpu_type"
          ? (j) => j.gpus > 0 && (j.gpu_type || "") === value
          : (j) => (j[field] || "") === value;
        setDrill({ rowEl: tr, label: `${nameHeader.toLowerCase()} ${r.name === "(none)" ? (gpuCols ? "untyped" : "(none)") : r.name}`, test });
      };
      tr.addEventListener("click", pick);
      tr.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); } });
    });
  }

  // ---------- jobs table ----------
  const COLUMNS = [
    { key: "id", label: "Job", cls: "", render: (j) => esc(j.id) + flags(j) },
    { key: "name", label: "Name", cls: "name", render: (j) => `<span title="${esc(j.name)}">${esc(j.name)}</span>` },
    { key: "user", label: "User", cls: "", render: (j) => esc(j.user), scope: "others" },
    { key: "partition", label: "Partition", cls: "", render: (j) => esc(j.partition) },
    { key: "state", label: "State", cls: "", render: (j) => `<span class="state-${esc(j.state)}">${esc(j.state)}</span>` },
    { key: "end", label: "Ended", cls: "", render: (j) => fmt.time(j.end) },
    { key: "elapsed_s", label: "Run time", cls: "num", render: (j) => fmt.dur(j.elapsed_s) },
    { key: "alloc_cpus", label: "CPUs", cls: "num", render: (j) => j.alloc_cpus },
    { key: "cpu_eff", label: "CPU eff", cls: "num", render: (j) => effCell(j.cpu_eff) },
    { key: "mem_eff", label: "Mem eff", cls: "num", render: (j) => `<span title="peak ${fmt.bytes(j.mem_used_b)} of ${fmt.bytes(j.mem_req_b)} allocated${j.mem_source === "maxrss" ? " (peak from MaxRSS)" : ""}${j.mem_alloc_source === "reqmem" ? " (allocation from ReqMem)" : ""}${j.mem_over ? ". Above 100%: summed RSS of forked processes double-counts shared pages, or memory is not enforced" : ""}">${effCell(j.mem_eff)}</span>` },
    { key: "gpus", label: "GPUs", cls: "num", render: (j) => j.gpus || "—", gpu: true },
    { key: "gpu_type", label: "GPU type", cls: "", render: (j) => j.gpus ? esc(j.gpu_type || "untyped") : "—", gpu: true },
    { key: "gpu_util", label: "GPU util", cls: "num", render: (j) => j.gpu_util == null ? "—" : effCell(j.gpu_util / 100), gpu: true },
    { key: "wall_eff", label: "Of limit", cls: "num", render: (j) => j.wall_eff == null ? "—" : fmt.pct(j.wall_eff) },
    { key: "cpu_hours_alloc", label: "CPU-h", cls: "num", render: (j) => fmt.hours(j.cpu_hours_alloc) },
    { key: "hint_text", label: "Hints", cls: "", render: (j) => (j.hints || []).map((h) => `<span class="hint ${/OOM|time limit|cancelled/.test(h.code) ? "bad" : ""}" title="${esc(h.text)}">${esc(h.code)}</span>`).join(""), hints: true },
  ];

  function flags(j) {
    let out = "";
    if (j.multi_day) out += `<span class="flag flag-day" title="ran longer than 24 hours">multi-day</span>`;
    if (j.spans_start) out += `<span class="flag flag-start" title="started before the window">began earlier</span>`;
    if (j.spans_end) out += `<span class="flag flag-end" title="finished after the window, or still running">ends later</span>`;
    if (j.prorated) out += `<span class="flag flag-pro" title="${(j.fraction * 100).toFixed(0)}% of this job's run time is inside the window">prorated ${(j.fraction * 100).toFixed(0)}%</span>`;
    return out;
  }

  function visibleColumns(data) {
    const hasGpu = data.jobs.some((j) => j.gpus > 0);
    const hasHints = data.jobs.some((j) => j.hints && j.hints.length);
    return COLUMNS.filter((c) => (c.scope !== "others" || data.window.scope !== "mine") && (!c.gpu || hasGpu) && (!c.hints || hasHints));
  }

  function renderJobsHeader(cols) {
    els.jobsHeader.innerHTML = cols.map((c) => `<th class="${c.cls} ${c.key === sortKey ? "sorted" + (sortAsc ? " asc" : "") : ""}" data-sort="${c.key}">${c.label}</th>`).join("");
    els.jobsHeader.querySelectorAll("th").forEach((th) => th.addEventListener("click", () => {
      const k = th.dataset.sort;
      if (sortKey === k) sortAsc = !sortAsc; else { sortKey = k; sortAsc = !["id", "name", "user", "partition", "state"].includes(k) ? false : true; }
      renderJobs(lastData);
    }));
  }

  function renderJobs(data) {
    const cols = visibleColumns(data);
    renderJobsHeader(cols);
    const needle = els.filter.value.trim().toLowerCase();
    let rows = data.jobs;
    if (needle) rows = rows.filter((j) => [j.id, j.name, j.user, j.partition, j.state, j.hint_text].join(" ").toLowerCase().includes(needle));
    if (drill) rows = rows.filter(drill.test);
    rows = rows.slice().sort((a, b) => {
      let x = a[sortKey], y = b[sortKey];
      if (x == null) return 1; if (y == null) return -1;
      if (typeof x === "string") { x = x.toLowerCase(); y = String(y).toLowerCase(); }
      return (x < y ? -1 : x > y ? 1 : 0) * (sortAsc ? 1 : -1);
    });
    els.jobsTitle.textContent = `${rows.length.toLocaleString()} job${rows.length === 1 ? "" : "s"}` + (data.table_truncated ? ` (table holds the ${data.jobs.length.toLocaleString()} most recent; charts and totals count all)` : "");
    els.drill.innerHTML = drill
      ? `<span class="chip">Showing: ${esc(drill.label)}<button type="button" id="drill-clear" aria-label="Show all jobs">✕</button></span>`
      : "";
    const clear = document.getElementById("drill-clear");
    if (clear) clear.addEventListener("click", () => setDrill(null));
    els.jobsBody.innerHTML = rows.length
      ? rows.map((j) => `<tr>${cols.map((c) => `<td class="${c.cls}">${c.render(j)}</td>`).join("")}</tr>`).join("")
      : `<tr><td colspan="${cols.length}" class="empty">No jobs match. Widen the time range, change the mode to “ran during”, or clear the filter.</td></tr>`;
  }

  // ---------- cluster capacity ----------
  function renderCluster(data) {
    if (!data || data.error) {
      els.cluster.innerHTML = `<div class="card"><h3>Cluster-wide capacity</h3><p class="note">${esc(data && data.error ? data.error : "Not available.")}</p></div>`;
      return;
    }
    const blocks = data.tres.map((t) => {
      const total = t.reported || 1;
      const seg = (n, cls) => `<i class="seg-${cls}" style="width:${(n / total * 100).toFixed(2)}%" title="${cls}: ${fmt.hours(n / 3600)} ${t.tres}-hours"></i>`;
      const unit = t.tres === "cpu" ? "CPU" : t.tres.startsWith("gres/gpu:") ? t.tres.slice("gres/gpu:".length).toUpperCase() + " GPU" : "GPU";
      return `<div class="tres">
        <div><b>${esc(t.cluster || "cluster")}</b> · ${unit}: <b>${fmt.pct(t.utilization, 1)}</b> of ${fmt.hours(t.reported / 3600)} ${unit}-hours were allocated to jobs</div>
        <div class="stack" role="img" aria-label="${unit} capacity split">${seg(t.allocated, "allocated")}${seg(t.idle, "idle")}${seg(t.planned, "planned")}${seg(t.down + t.planned_down, "down")}</div>
        <div class="key">
          <span><i class="seg-allocated"></i>allocated ${fmt.hours(t.allocated / 3600)}</span>
          <span><i class="seg-idle"></i>idle ${fmt.hours(t.idle / 3600)}</span>
          <span><i class="seg-planned"></i>planned/reserved ${fmt.hours(t.planned / 3600)}</span>
          <span><i class="seg-down"></i>down ${fmt.hours((t.down + t.planned_down) / 3600)}</span>
        </div>
      </div>`;
    });
    els.cluster.innerHTML = `<div class="card"><h3>Cluster-wide capacity</h3>${blocks.join("") || ""}${data.warnings.map((w) => `<p class="note">${esc(w)}</p>`).join("")}</div>`;
  }

  // ---------- main ----------
  let lastData = null;

  async function run() {
    let qs;
    try { qs = queryString({ scope: f.scope.value, partition: f.scope.value === "partition" ? f.partition.value : "", mode: f.mode.value, states: f.states.value, running: f.running.checked ? "1" : "0" }); }
    catch (e) { showStatus(e.message, true); return; }

    els.go.disabled = true;
    showStatus("Asking Slurm's accounting database… long ranges and the everyone view can take a while.", false);
    const clusterPromise = fetch(BASE + "api/cluster?" + queryString({})).then((r) => r.json()).catch((e) => ({ error: e.message }));
    try {
      const r = await fetch(BASE + "api/jobs?" + qs);
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || `Request failed (${r.status}).`);
      data.jobs.forEach((j) => { j.hint_text = (j.hints || []).map((h) => h.code).join(", "); });
      drill = null;
      lastData = data;
      hideStatus();
      els.results.hidden = false;
      renderNotes(data.warnings, data.gpu_util_diagnosis);
      renderLedger(data);
      renderAdvice(data.advice);
      histChart("chart-cpu", data.summary.cpu_eff_hist, "CPU efficiency", data.jobs, "cpu_eff");
      histChart("chart-mem", data.summary.mem_eff_hist, "Memory efficiency", data.jobs, "mem_eff");
      partitionChart(data.by_partition, data.jobs);
      breakdownTable(els.byPartition, "By partition", data.by_partition, "Partition");
      breakdownTable(els.byUser, "By user", data.by_user, "User");
      breakdownTable(els.byAccount, "By account", data.by_account, "Account");
      breakdownTable(els.byGpuType, "GPU jobs by GPU type", data.by_gpu_type, "GPU type", true);
      renderJobs(data);
      history.replaceState(null, "", "?" + qs);
    } catch (e) {
      showStatus(e.message, true);
    } finally {
      els.go.disabled = false;
    }
    renderCluster(await clusterPromise);
  }

  function restoreFromUrl() {
    const q = new URLSearchParams(location.search);
    if (!q.has("scope")) return;
    for (const k of ["scope", "mode", "states"]) if (q.get(k)) f[k].value = q.get(k);
    if (q.get("start") && q.get("end")) { f.range.value = "custom"; f.start.value = q.get("start"); f.end.value = q.get("end"); }
    f.running.checked = q.get("running") === "1";
    if (q.get("partition")) f.partition.dataset.pending = q.get("partition");
  }

  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && drill) setDrill(null); });
  form.addEventListener("change", syncForm);
  form.addEventListener("submit", (e) => { e.preventDefault(); run(); });
  els.filter.addEventListener("input", () => lastData && renderJobs(lastData));

  restoreFromUrl();
  syncForm();
  loadPartitions().then(() => {
    if (f.partition.dataset.pending) f.partition.value = f.partition.dataset.pending;
    run();
  });
})();
