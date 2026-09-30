/* mac-monitor 前端：实时卡片 + Canvas 曲线图（SSE 推送，零依赖） */
"use strict";

const IS_DESKTOP_HOST = new URLSearchParams(location.search).get("desktop") === "1";
const IS_NATIVE_HOST = new URLSearchParams(location.search).get("native") === "1";
if (IS_DESKTOP_HOST) {
  document.documentElement.classList.add("desktop-host-root");
  document.body.classList.add("desktop-host", "compact-mode");
  if (IS_NATIVE_HOST) document.body.classList.add("native-controls");
}

/* Apple circular Gauge：开放圆弧、进度描边与当前位置标记。 */
const GAUGE_PATH = "M 24.3 80.6 A 40 40 0 1 1 75.7 80.6";
document.querySelectorAll(".metric-ring").forEach((ring) => {
  const label = ring.querySelector(".ring-core > span")?.textContent?.trim() || "系统指标";
  ring.setAttribute("role", "meter");
  ring.setAttribute("aria-label", label);
  ring.setAttribute("aria-valuemin", "0");
  ring.setAttribute("aria-valuemax", "100");
  ring.insertAdjacentHTML("afterbegin", `
    <svg class="gauge-svg" viewBox="0 0 100 100" aria-hidden="true" focusable="false">
      <path class="gauge-track" d="${GAUGE_PATH}" pathLength="100"></path>
      <path class="gauge-value" d="${GAUGE_PATH}" pathLength="100"></path>
      <circle class="gauge-marker" cx="24.3" cy="80.6" r="4.25"></circle>
    </svg>
  `);
});

/* ---------------- 主题（自动跟随系统，可手动切换） ---------------- */
const THEME_KEY = "mm-theme";
const THEME_SEQ = [
  ["auto", "自动"],
  ["light", "浅色"],
  ["dark", "深色"],
];

function currentTheme() {
  const t = localStorage.getItem(THEME_KEY);
  return THEME_SEQ.some(([k]) => k === t) ? t : "auto";
}

function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  const label = document.getElementById("themeLabel");
  const entry = THEME_SEQ.find(([k]) => k === t) || THEME_SEQ[0];
  label.textContent = entry[1];
}

document.getElementById("themeBtn").addEventListener("click", () => {
  const idx = THEME_SEQ.findIndex(([k]) => k === currentTheme());
  const next = THEME_SEQ[(idx + 1) % THEME_SEQ.length][0];
  localStorage.setItem(THEME_KEY, next);
  applyTheme(next);
  charts.forEach((c) => c.draw());
});
applyTheme(currentTheme());

const darkMQ = window.matchMedia("(prefers-color-scheme: dark)");
darkMQ.addEventListener("change", () => charts.forEach((c) => c.draw()));

/* ---------------- 工具 ---------------- */
function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function fmtTime(ts, withSec) {
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return withSec
    ? `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
    : `${p(d.getHours())}:${p(d.getMinutes())}`;
}

function niceDomain(min, max, want = 4) {
  if (!(max > min)) max = min + 1;
  const raw = (max - min) / want;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const n = raw / mag;
  const step = (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * mag;
  const lo = Math.floor(min / step) * step;
  const hi = Math.ceil(max / step) * step;
  const ticks = [];
  for (let v = lo; v <= hi + step / 2; v += step) ticks.push(+v.toFixed(6));
  return { lo, hi, ticks };
}

/* ---------------- 曲线图 ---------------- */
const WINDOW_SEC = 300; // 显示最近 5 分钟

class Chart {
  /**
   * cfg: { series: fn(rows)->[{label,color,get}] | 数组, yMin, yMax, unit, yFmt(v) }
   */
  constructor(canvasId, tipId, legendId, cfg) {
    this.canvas = document.getElementById(canvasId);
    this.tip = document.getElementById(tipId);
    this.legendEl = document.getElementById(legendId);
    this.cfg = cfg;
    this.rows = [];
    this.series = [];
    this.hoverT = null;
    this._geom = null;

    this.ctx = this.canvas.getContext("2d");
    new ResizeObserver(() => {
      this.fit();
      this.draw();
    }).observe(this.canvas.parentElement);

    this.canvas.addEventListener("mousemove", (e) => this._onMove(e));
    this.canvas.addEventListener("mouseleave", () => {
      this.hoverT = null;
      this.tip.style.display = "none";
      this.draw();
    });
    this.fit();
  }

  fit() {
    const dpr = window.devicePixelRatio || 1;
    const w = this.canvas.clientWidth;
    const h = this.canvas.clientHeight;
    if (!w || !h) return;
    this.canvas.width = Math.round(w * dpr);
    this.canvas.height = Math.round(h * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.w = w;
    this.h = h;
  }

  setData(rows) {
    this.rows = rows;
    const raw = typeof this.cfg.series === "function"
      ? this.cfg.series(rows)
      : this.cfg.series;
    this.series = raw;
    this.draw();
    this._renderLegend();
  }

  _renderLegend() {
    const last = this.rows[this.rows.length - 1];
    const parts = this.series.map((s) => {
      const v = last ? s.get(last) : null;
      const val = v == null ? "–" : (this.cfg.yFmt ? this.cfg.yFmt(v) : Math.round(v));
      return `<span class="lg" style="--c:${s.color}"><i></i>${s.label} <b>${val}</b></span>`;
    });
    this.legendEl.innerHTML = parts.join("");
  }

  _domain() {
    const t1 = Math.max(Date.now() / 1000, this.rows.length ? this.rows[this.rows.length - 1].ts : 0);
    const t0 = t1 - WINDOW_SEC;
    const visible = this.rows.filter((r) => r.ts >= t0);
    let lo = this.cfg.yMin, hi = this.cfg.yMax;
    const vals = [];
    for (const s of this.series) {
      for (const r of visible) {
        const v = s.get(r);
        if (v != null && isFinite(v)) vals.push(v);
      }
    }
    let ticks;
    if (lo == null || hi == null) {
      if (!vals.length) return { t0, t1, visible, y: null };
      const d = niceDomain(Math.min(...vals), Math.max(...vals));
      lo = d.lo; hi = d.hi; ticks = d.ticks;
    } else {
      ticks = niceDomain(lo, hi).ticks;
    }
    return { t0, t1, visible, y: { lo, hi, ticks }, vals };
  }

  draw() {
    if (!this.w || !this.h) this.fit();
    const ctx = this.ctx;
    const { w, h } = this;
    if (!w || !h) return;
    const denseDesktop = IS_DESKTOP_HOST && !document.body.classList.contains("compact-mode");
    const M = denseDesktop
      ? { l: 30, r: 5, t: 3, b: 17 }
      : { l: 46, r: 12, t: 10, b: 24 };
    const pw = w - M.l - M.r;
    const ph = h - M.t - M.b;
    ctx.clearRect(0, 0, w, h);

    const grid = cssVar("--grid") || "rgba(120,132,160,.16)";
    const sub = cssVar("--sub") || "#888";
    const dom = this._domain();

    ctx.font = `${denseDesktop ? 8 : 11}px -apple-system, "PingFang SC", sans-serif`;

    if (!dom.y || !this.series.length || !dom.vals?.length) {
      ctx.fillStyle = sub;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText("暂无数据", w / 2, h / 2);
      this._geom = null;
      return;
    }

    const xOf = (t) => M.l + ((t - dom.t0) / (dom.t1 - dom.t0)) * pw;
    const yOf = (v) => M.t + (1 - (v - dom.y.lo) / (dom.y.hi - dom.y.lo)) * ph;

    /* 网格 + Y 轴 */
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (const tick of dom.y.ticks) {
      const y = yOf(tick);
      ctx.strokeStyle = grid;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(M.l, y + 0.5);
      ctx.lineTo(w - M.r, y + 0.5);
      ctx.stroke();
      ctx.fillStyle = sub;
      ctx.fillText(
        this.cfg.yFmt ? this.cfg.yFmt(tick) : String(Math.round(tick)),
        M.l - 7, y
      );
    }

    /* X 轴：整分钟刻度 */
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    for (let t = Math.ceil(dom.t0 / 60) * 60; t <= dom.t1; t += 60) {
      const x = xOf(t);
      ctx.strokeStyle = grid;
      ctx.beginPath();
      ctx.moveTo(x + 0.5, M.t);
      ctx.lineTo(x + 0.5, M.t + ph);
      ctx.stroke();
      ctx.fillStyle = sub;
      if (x > M.l + 18 && x < w - M.r - 18) {
        ctx.fillText(fmtTime(t, false), x, h - M.b + 7);
      }
    }

    /* 悬停十字线 */
    let hoverRow = null;
    if (this.hoverT != null) {
      let best = null, bd = Infinity;
      for (const r of dom.visible) {
        const d = Math.abs(r.ts - this.hoverT);
        if (d < bd) { bd = d; best = r; }
      }
      if (best && bd < WINDOW_SEC) {
        hoverRow = best;
        const x = xOf(best.ts);
        ctx.strokeStyle = sub;
        ctx.globalAlpha = 0.45;
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        ctx.moveTo(x + 0.5, M.t);
        ctx.lineTo(x + 0.5, M.t + ph);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.globalAlpha = 1;
      }
    }

    /* 曲线 */
    for (const s of this.series) {
      const pts = [];
      for (const r of dom.visible) {
        const v = s.get(r);
        if (v != null && isFinite(v)) pts.push([xOf(r.ts), yOf(Math.max(dom.y.lo, Math.min(dom.y.hi, v)))]);
      }
      if (pts.length < 2) continue;

      /* 渐变填充（该系列自身的淡淡底色） */
      const grad = ctx.createLinearGradient(0, M.t, 0, M.t + ph);
      grad.addColorStop(0, hexA(s.color, 0.16));
      grad.addColorStop(1, hexA(s.color, 0));
      ctx.beginPath();
      this._path(ctx, pts);
      ctx.lineTo(pts[pts.length - 1][0], M.t + ph);
      ctx.lineTo(pts[0][0], M.t + ph);
      ctx.closePath();
      ctx.fillStyle = grad;
      ctx.fill();

      /* 线 */
      ctx.beginPath();
      this._path(ctx, pts);
      ctx.strokeStyle = s.color;
      ctx.lineWidth = 2;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      ctx.stroke();

      /* 悬停点 */
      if (hoverRow) {
        const v = s.get(hoverRow);
        if (v != null && isFinite(v)) {
          ctx.beginPath();
          ctx.arc(xOf(hoverRow.ts), yOf(Math.max(dom.y.lo, Math.min(dom.y.hi, v))), 3.5, 0, Math.PI * 2);
          ctx.fillStyle = s.color;
          ctx.fill();
          ctx.strokeStyle = cssVar("--card") || "#fff";
          ctx.lineWidth = 2;
          ctx.stroke();
        }
      }
    }

    this._geom = { dom, xOf, M, pw };

    /* 悬停提示框 */
    if (hoverRow) {
      const rows = this.series.map((s) => {
        const v = s.get(hoverRow);
        const txt = v == null ? "–" : (this.cfg.yFmt ? this.cfg.yFmt(v) : Math.round(v));
        return `<div class="t-row" style="--c:${s.color}"><i></i><span>${s.label}</span><b>${txt}</b></div>`;
      }).join("");
      this.tip.innerHTML = `<div class="t-time">${fmtTime(hoverRow.ts, true)}</div>${rows}`;
      this.tip.style.display = "block";
      const x = xOf(hoverRow.ts);
      const tw = this.tip.offsetWidth;
      const left = x + 14 + tw > this.w ? x - tw - 14 : x + 14;
      this.tip.style.left = Math.max(4, left) + "px";
      this.tip.style.top = "12px";
    }
  }

  /* 中点二次曲线，平滑且不越过数据点 */
  _path(ctx, pts) {
    ctx.moveTo(pts[0][0], pts[0][1]);
    if (pts.length === 1) return;
    for (let i = 1; i < pts.length - 1; i++) {
      const xc = (pts[i][0] + pts[i + 1][0]) / 2;
      const yc = (pts[i][1] + pts[i + 1][1]) / 2;
      ctx.quadraticCurveTo(pts[i][0], pts[i][1], xc, yc);
    }
    ctx.quadraticCurveTo(
      pts[pts.length - 2][0], pts[pts.length - 2][1],
      pts[pts.length - 1][0], pts[pts.length - 1][1]
    );
  }

  _onMove(e) {
    if (!this._geom) return;
    const rect = this.canvas.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const { dom, M, pw } = this._geom;
    const frac = Math.min(1, Math.max(0, (x - M.l) / pw));
    this.hoverT = dom.t0 + frac * (dom.t1 - dom.t0);
    this.draw();
  }
}

/* #rrggbb → rgba() */
function hexA(hex, a) {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return `rgba(128,128,128,${a})`;
  const n = parseInt(m[1], 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

/* ---------------- 图表实例 ---------------- */
const FAN_COLORS = () => [cssVar("--c-f0"), cssVar("--c-f1"), cssVar("--c-f2"), cssVar("--c-f3")];

const charts = [
  new Chart("chart1", "tip1", "lg1", {
    series: [
      { label: "CPU", color: "#6366f1", get: (r) => r.cpu },
      { label: "GPU", color: "#f59e0b", get: (r) => r.gpu },
    ],
    yMin: 0, yMax: 100,
    yFmt: (v) => `${Math.round(v)}%`,
  }),
  new Chart("chart2", "tip2", "lg2", {
    series: [
      { label: "1 分钟", color: "#10b981", get: (r) => r.load && r.load[0] },
      { label: "5 分钟", color: "#3b82f6", get: (r) => r.load && r.load[1] },
      { label: "15 分钟", color: "#8b5cf6", get: (r) => r.load && r.load[2] },
    ],
    yMin: 0,
    yFmt: (v) => (v >= 10 ? Math.round(v) : v.toFixed(1)),
  }),
  new Chart("chart3", "tip3", "lg3", {
    series: (rows) => {
      const n = rows.reduce((m, r) => Math.max(m, r.fans ? r.fans.length : 0), 0);
      const colors = FAN_COLORS();
      return Array.from({ length: Math.min(n, 4) }, (_, i) => ({
        label: `风扇 ${i + 1}`,
        color: colors[i],
        get: (r) => (r.fans && r.fans[i] ? r.fans[i].rpm : null),
      }));
    },
    yMin: 0,
    yFmt: (v) => Math.round(v).toLocaleString("en-US"),
  }),
];

/* ---------------- 状态与卡片 ---------------- */
let history = [];
let meta = {};

function setStatus(on) {
  const el = document.getElementById("status");
  el.classList.toggle("on", on);
  el.querySelector("em").textContent = on ? "实时" : "重连中";
  document.getElementById("miniStatusDot").classList.toggle("on", on);
}

function updateMeta() {
  document.getElementById("subTitle").textContent =
    `${meta.hostname || ""} · ${meta.cpu_model || ""} · ${meta.os || ""}`;
}

function updateCards() {
  const s = history[history.length - 1];
  if (!s) return;
  const set = (id, v) => (document.getElementById(id).textContent = v);
  const setBar = (id, value) => {
    const el = document.getElementById(id);
    const pct = value == null || !isFinite(value) ? 0 : Math.max(0, Math.min(100, value));
    el.style.width = `${pct}%`;
  };
  const setRing = (id, value, warnAt = 65, hotAt = 85) => {
    const el = document.getElementById(id);
    const available = value != null && isFinite(value);
    const pct = available ? Math.max(0, Math.min(100, value)) : 0;
    el.style.setProperty("--value", pct);
    el.classList.remove("level-normal", "level-warn", "level-hot", "level-empty");
    const stateClass = !available
      ? "level-empty"
      : pct >= hotAt ? "level-hot" : pct >= warnAt ? "level-warn" : "level-normal";
    el.classList.add(stateClass);

    const path = el.querySelector(".gauge-value");
    const marker = el.querySelector(".gauge-marker");
    if (path && marker) {
      path.style.strokeDasharray = `${pct} ${100 - pct}`;
      const point = path.getPointAtLength(path.getTotalLength() * pct / 100);
      marker.setAttribute("cx", point.x.toFixed(2));
      marker.setAttribute("cy", point.y.toFixed(2));
    }

    const label = el.getAttribute("aria-label") || "系统指标";
    const state = !available ? "不可用" : pct >= hotAt ? "高" : pct >= warnAt ? "注意" : "正常";
    if (available) el.setAttribute("aria-valuenow", pct.toFixed(1));
    else el.removeAttribute("aria-valuenow");
    el.setAttribute("aria-valuetext", available ? `${label} ${pct.toFixed(1)}%，${state}` : `${label}不可用`);
  };

  /* CPU */
  set("cpuVal", s.cpu == null ? "–" : s.cpu.toFixed(1));
  set("miniCpuVal", s.cpu == null ? "–" : Math.round(s.cpu));
  set("deskCpuVal", s.cpu == null ? "–" : Math.round(s.cpu));
  set("cpuSub", meta.cores ? `${meta.cores} 核 · ${meta.cpu_model || ""}` : " ");
  setBar("cpuBar", s.cpu);
  setRing("miniCpuRing", s.cpu);
  setRing("deskCpuRing", s.cpu);

  /* GPU */
  set("gpuVal", s.gpu == null ? "–" : String(s.gpu));
  set("miniGpuVal", s.gpu == null ? "–" : String(Math.round(s.gpu)));
  set("deskGpuVal", s.gpu == null ? "–" : String(Math.round(s.gpu)));
  set("gpuSub", s.gpu == null
    ? (meta.gpu_available === false ? "此机型不可读取" : "采样中")
    : "设备利用率");
  setBar("gpuBar", s.gpu);
  setRing("miniGpuRing", s.gpu);
  setRing("deskGpuRing", s.gpu);

  /* 平均负载 */
  if (s.load) {
    set("loadVal", s.load[0].toFixed(2));
    set("miniLoadVal", s.load[0].toFixed(1));
    set("deskLoadVal", s.load[0].toFixed(1));
    set("loadSub", `5 分钟 ${s.load[1].toFixed(2)} · 15 分钟 ${s.load[2].toFixed(2)}`);
    setBar("loadBar", meta.cores ? s.load[0] / meta.cores * 100 : 0);
    setRing("miniLoadRing", meta.cores ? s.load[0] / meta.cores * 100 : 0);
    setRing("deskLoadRing", meta.cores ? s.load[0] / meta.cores * 100 : 0);
  }

  /* 风扇 */
  if (s.fans && s.fans.length) {
    const avg = s.fans.reduce((a, f) => a + f.rpm, 0) / s.fans.length;
    const load = s.fans.reduce((a, f) => a + f.load_pct, 0) / s.fans.length;
    set("fanVal", String(Math.round(avg)));
    set("miniFanVal", String(Math.round(avg)));
    set("deskFanVal", String(Math.round(avg)));
    set("fanSub",
      s.fans.map((f, i) => `F${i + 1} ${f.rpm}`).join(" · ") +
      ` · 负载 ${load.toFixed(0)}%`);
    setBar("fanBar", load);
    setRing("miniFanRing", load, 70, 90);
    setRing("deskFanRing", load, 70, 90);
  } else {
    set("fanVal", "–");
    set("miniFanVal", "–");
    set("deskFanVal", "–");
    set("fanSub", s.fan_note || meta.fan_note || "不可用");
    setBar("fanBar", 0);
    setRing("miniFanRing", null);
    setRing("deskFanRing", null);
  }

  /* 内存 */
  if (s.mem) {
    set("memVal", s.mem.pct.toFixed(1));
    set("miniMemVal", String(Math.round(s.mem.pct)));
    set("deskMemVal", String(Math.round(s.mem.pct)));
    set("memSub", `${s.mem.used_gb} / ${s.mem.total_gb} GB`);
    setBar("memBar", s.mem.pct);
    setRing("miniMemRing", s.mem.pct);
    setRing("deskMemRing", s.mem.pct);
  }

  set("historyCount", history.length.toLocaleString("zh-CN"));
}

function render() {
  updateCards();
  charts.forEach((c) => c.setData(history));
}

/* ---------------- 实时连接（SSE） ---------------- */
function connect() {
  const es = new EventSource("/api/stream");
  es.onopen = () => setStatus(true);
  es.onmessage = (e) => {
    try {
      const s = JSON.parse(e.data);
      if (!s) return;
      history.push(s);
      if (history.length > 1800) history.splice(0, history.length - 1800);
      setStatus(true);
      render();
    } catch (err) { console.warn("渲染出错:", err); }
  };
  es.onerror = () => setStatus(false);
  /* EventSource 断线后会自动重连 */
}

/* ---------------- 启动 ---------------- */
async function boot() {
  try {
    const [m, hist] = await Promise.all([
      fetch("/api/meta").then((r) => r.json()),
      fetch("/api/history?n=300").then((r) => r.json()),
    ]);
    meta = m || {};
    history = hist || [];
    updateMeta();
    render();
  } catch (_) {
    /* 下面的 SSE 仍会尝试连接 */
  }
  connect();
}

function updateClock() {
  const el = document.getElementById("clock");
  if (el) el.textContent = fmtTime(Date.now() / 1000, true);
}
updateClock();
setInterval(updateClock, 1000);

function sendWindowCommand(command) {
  const handler = window.webkit?.messageHandlers?.windowControl;
  if (handler) handler.postMessage(command);
}

function setCompactMode(compact) {
  if (!IS_DESKTOP_HOST) return;
  document.body.classList.toggle("compact-mode", compact);
  sendWindowCommand(compact ? "compact" : "expand");
  if (!compact) {
    requestAnimationFrame(() => charts.forEach((chart) => {
      chart.fit();
      chart.draw();
    }));
  }
}

document.getElementById("expandBtn").addEventListener("click", () => setCompactMode(false));
document.getElementById("compactBtn").addEventListener("click", () => setCompactMode(true));
document.getElementById("closeBtn").addEventListener("click", () => {
  if (IS_DESKTOP_HOST) sendWindowCommand("close");
  else window.close();
});
document.querySelector(".compact-shell").addEventListener("click", (event) => {
  if (!event.target.closest("button, a")) setCompactMode(false);
});
document.querySelector(".dashboard").addEventListener("click", (event) => {
  if (IS_DESKTOP_HOST && !document.body.classList.contains("compact-mode") &&
      !event.target.closest("button, a")) {
    setCompactMode(true);
  }
});
document.addEventListener("keydown", (event) => {
  if (IS_DESKTOP_HOST && event.key === "Escape") setCompactMode(true);
});

const branchButtons = [...document.querySelectorAll(".chart-branches button")];
const chartPanels = [...document.querySelectorAll(".charts .panel")];
function activateChartBranch(index) {
  branchButtons.forEach((button, i) => button.classList.toggle("active", i === index));
  chartPanels.forEach((panel, i) => panel.classList.toggle("active-chart", i === index));
  requestAnimationFrame(() => {
    charts[index].fit();
    charts[index].draw();
  });
}
branchButtons.forEach((button, index) => {
  button.addEventListener("click", () => activateChartBranch(index));
});

boot();
