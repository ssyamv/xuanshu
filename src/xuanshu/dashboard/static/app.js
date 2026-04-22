const money = new Intl.NumberFormat("zh-CN", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});
const quantity = new Intl.NumberFormat("zh-CN", {
  minimumFractionDigits: 0,
  maximumFractionDigits: 8,
});

let chart;
let activeRange = "24h";
let displayMode = "money";
let latestCurve = [];

function fmtMoney(value) {
  const parsed = Number(value || 0);
  return `${parsed >= 0 ? "" : "-"}$${money.format(Math.abs(parsed))}`;
}

function fmtPct(value) {
  return `${(Number(value || 0) * 100).toFixed(2)}%`;
}

function fmtTime(value) {
  if (!value) return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("zh-CN", { hour12: false });
}

function setText(id, text) {
  const node = document.getElementById(id);
  if (node) node.textContent = text;
}

async function getJson(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) throw new Error(`${url} ${response.status}`);
  return response.json();
}

function renderOverview(data) {
  const pnl = Number(data.estimated_pnl || 0);
  setText("modePill", `模式 ${data.mode || "--"}`);
  setText("asOf", `刷新 ${fmtTime(data.as_of)}`);
  setText("pnlValue", displayMode === "money" ? fmtMoney(pnl) : fmtPct(data.estimated_pnl_rate));
  setText("pnlRate", `${fmtPct(data.estimated_pnl_rate)} / ${fmtMoney(pnl)}`);
  setText("equityValue", fmtMoney(data.equity));
  setText("strategyValue", fmtMoney(data.strategy_total_amount));
  setText("baseValue", `初始 ${fmtMoney(data.base_equity)}`);
  setText("marginValue", Number(data.margin_ratio || 0).toFixed(4));
  setText("availableValue", `可用 ${fmtMoney(data.available_balance)}`);
  renderPositions(data.positions || []);
  renderActions(data.actions || []);
}

function renderPositions(positions) {
  setText("positionCount", String(positions.length));
  const list = document.getElementById("positionsList");
  if (!list) return;
  if (!positions.length) {
    list.innerHTML = `<div class="empty">暂无当前持仓</div>`;
    return;
  }
  list.innerHTML = positions.map((item) => {
    const pnl = Number(item.unrealized_pnl || 0);
    return `
      <article class="position-card">
        <header>
          <span class="symbol">${item.symbol}</span>
          <span class="side">${item.side}</span>
        </header>
        <div class="position-row"><span>净持仓</span><strong>${quantity.format(Number(item.net_quantity || 0))}</strong></div>
        <div class="position-row"><span>均价 / 标记价</span><strong>${money.format(Number(item.average_price || 0))} / ${money.format(Number(item.mark_price || 0))}</strong></div>
        <div class="position-row"><span>未实现盈亏</span><strong class="${pnl >= 0 ? "positive" : "negative"}">${fmtMoney(pnl)}</strong></div>
        <div class="position-row"><span>市场状态</span><strong>${item.regime || "unknown"}</strong></div>
        <div class="logic">${item.strategy_id || "未提供"} · ${item.strategy_logic || "未提供"}</div>
      </article>
    `;
  }).join("");
}

function renderActions(actions) {
  const body = document.getElementById("actionsBody");
  if (!body) return;
  if (!actions.length) {
    body.innerHTML = `<tr><td colspan="8" class="empty">暂无最近动作</td></tr>`;
    return;
  }
  body.innerHTML = actions.map((item) => `
    <tr>
      <td>${fmtTime(item.timestamp)}</td>
      <td>${item.label || item.type}</td>
      <td>${item.symbol || "--"}</td>
      <td>${item.side || "--"}</td>
      <td>${quantity.format(Number(item.size || 0))}</td>
      <td>${money.format(Number(item.price || 0))}</td>
      <td class="secondary">${item.strategy_id || "未提供"}</td>
      <td class="secondary">${item.client_order_id || item.order_id || "--"}</td>
    </tr>
  `).join("");
}

function renderCurve(data) {
  latestCurve = data.points || [];
  const labels = latestCurve.map((point) => fmtTime(point.timestamp));
  const values = latestCurve.map((point) => displayMode === "money" ? point.pnl : point.pnl_rate * 100);
  const ctx = document.getElementById("equityChart");
  if (!ctx || !window.Chart) return;
  if (chart) chart.destroy();
  chart = new Chart(ctx, {
    type: "line",
    data: {
      labels,
      datasets: [{
        label: displayMode === "money" ? "收益" : "收益率",
        data: values,
        borderColor: "#2dd4bf",
        backgroundColor: "rgba(45, 212, 191, 0.16)",
        fill: true,
        tension: 0.28,
        pointRadius: 0,
        borderWidth: 2,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { intersect: false, mode: "index" },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (context) => displayMode === "money" ? fmtMoney(context.parsed.y) : `${context.parsed.y.toFixed(2)}%`,
          },
        },
      },
      scales: {
        x: { grid: { color: "rgba(40,64,74,.35)" }, ticks: { color: "#8ea1aa", maxTicksLimit: 8 } },
        y: { grid: { color: "rgba(40,64,74,.5)" }, ticks: { color: "#8ea1aa" } },
      },
    },
  });
}

async function refresh() {
  const [overview, curve] = await Promise.all([
    getJson("/xuanshu/api/overview"),
    getJson(`/xuanshu/api/equity-curve?range=${activeRange}`),
  ]);
  renderOverview(overview);
  renderCurve(curve);
}

document.querySelectorAll("[data-range]").forEach((button) => {
  button.addEventListener("click", async () => {
    activeRange = button.dataset.range;
    document.querySelectorAll("[data-range]").forEach((item) => item.classList.toggle("active", item === button));
    renderCurve(await getJson(`/xuanshu/api/equity-curve?range=${activeRange}`));
  });
});

document.getElementById("displayToggle")?.addEventListener("click", (event) => {
  displayMode = displayMode === "money" ? "rate" : "money";
  event.currentTarget.textContent = displayMode === "money" ? "金额" : "百分比";
  renderCurve({ points: latestCurve });
  refresh().catch(console.error);
});

window.addEventListener("load", () => {
  refresh().catch(console.error);
  setInterval(() => refresh().catch(console.error), 10_000);
});
