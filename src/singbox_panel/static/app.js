(() => {
  const state = {
    range: "month",
    donutMode: "raw",
    dashboard: null,
    pendingRevoke: null,
    qrBundle: null,
    qrActiveTab: 0,
    revealSecret: false,
    pollTimer: null,
  };

  const $ = (id) => document.getElementById(id);

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function formatBytes(bytes, base = 1000) {
    const n = Number(bytes || 0);
    if (!Number.isFinite(n) || n <= 0) return "0 B";
    const units = base === 1024 ? ["B", "KiB", "MiB", "GiB", "TiB"] : ["B", "KB", "MB", "GB", "TB"];
    let idx = 0;
    let val = n;
    while (val >= base && idx < units.length - 1) {
      val /= base;
      idx += 1;
    }
    const digits = idx >= 3 ? 2 : idx === 2 ? 1 : 0;
    return `${val.toFixed(digits)} ${units[idx]}`;
  }

  function formatShortTime(isoStr) {
    if (!isoStr) return "--";
    const dt = new Date(isoStr);
    if (Number.isNaN(dt.getTime())) return isoStr;
    return dt.toLocaleString("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  }

  function rangeLabel(r) {
    return r === "today" ? "今日" : r === "month" ? "本月" : "累计";
  }

  function showToast(message, type = "ok") {
    const host = $("toast-container");
    const el = document.createElement("div");
    el.className = `toast ${type}`;
    el.textContent = message;
    host.appendChild(el);
    setTimeout(() => {
      el.style.opacity = "0";
      el.style.transition = "opacity 0.25s ease";
      setTimeout(() => el.remove(), 260);
    }, 3400);
  }

  async function apiFetch(url, options = {}) {
    const res = await fetch(url, {
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    let payload = null;
    try {
      payload = await res.json();
    } catch {
      payload = null;
    }
    if (!res.ok) {
      const detail = payload?.detail || `HTTP ${res.status}`;
      throw new Error(detail);
    }
    return payload;
  }

  async function copyText(text, label = "内容") {
    try {
      await navigator.clipboard.writeText(text);
      showToast(`已复制${label}`);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
      showToast(`已复制${label}`);
    }
  }

  // --- Rendering ---

  function renderTopbar(data) {
    const meta = data.server_meta;
    const col = data.collector;

    const lb = $("loopback-badge");
    lb.textContent = meta.loopback_only
      ? `${meta.bind_host}:${meta.bind_port} 仅本地回环`
      : `${meta.bind_host}:${meta.bind_port}`;
    lb.className = `pill ${meta.loopback_only ? "pill-secure" : "pill-warn"}`;

    const cb = $("collector-badge");
    const srcMap = {
      v2ray_api: "v2ray_api 精确统计",
      clash_api: "clash_api WebSocket",
      mock: "Demo 模拟数据",
    };
    const srcText = srcMap[col.active_source] || col.active_source;
    cb.textContent = col.connected ? `● ${srcText}` : `○ ${srcText} (未连接)`;
    cb.className = `pill ${col.connected ? "pill-ok" : "pill-warn"}`;

    $("last-flush-text").textContent = col.last_flush_at
      ? `每 ${col.flush_interval_seconds}s 落盘 · 上次 ${formatShortTime(col.last_flush_at)}`
      : `每 ${col.flush_interval_seconds}s 自动落盘`;

    // Diagnostic banner
    const banner = $("diagnostic-banner");
    const messages = [];
    let isDanger = false;
    if (col.last_error) {
      messages.push(`采集器连接异常 (${col.active_source}): ${col.last_error}`);
    }
    if (col.diagnostics?.hint) {
      messages.push(col.diagnostics.hint);
    }
    if (data.singbox_health?.error) {
      messages.push(`sing-box 配置检查: ${data.singbox_health.error}`);
    }
    if (data.oci_quota?.alert_level === "danger") {
      messages.push(
        `OCI 免费额度红线预警：本月估算已达 ${data.oci_quota.percent}%（${formatBytes(data.oci_quota.used_bytes, data.oci_quota.unit_base)} / ${data.oci_quota.monthly_limit_tb} TB）！`
      );
      isDanger = true;
    }

    if (messages.length > 0) {
      banner.textContent = messages.join(" ｜ ");
      banner.className = `alert-banner ${isDanger ? "danger" : ""}`;
    } else {
      banner.className = "alert-banner hidden";
    }
  }

  function renderOciQuota(oci) {
    const base = oci.unit_base || 1000;
    const card = document.querySelector(".oci-card");
    card.classList.remove("level-warning", "level-danger");
    if (oci.alert_level === "warning") card.classList.add("level-warning");
    if (oci.alert_level === "danger") card.classList.add("level-danger");

    $("oci-used").textContent = formatBytes(oci.used_bytes, base);
    $("oci-limit").textContent = `${oci.monthly_limit_tb.toFixed(2)} ${base === 1024 ? "TiB" : "TB"}`;
    $("oci-scale-max").textContent = `${oci.monthly_limit_tb} ${base === 1024 ? "TiB" : "TB"}`;

    const pill = $("oci-percent-pill");
    pill.textContent =
      oci.alert_level === "danger"
        ? `${oci.percent}% · 红线预警`
        : oci.alert_level === "warning"
          ? `${oci.percent}% · 需留意`
          : `${oci.percent}% · 充裕`;
    pill.className = `pill ${
      oci.alert_level === "danger"
        ? "pill-danger"
        : oci.alert_level === "warning"
          ? "pill-warn"
          : "pill-ok"
    }`;

    $("oci-remaining").textContent = formatBytes(oci.remaining_bytes, base);
    $("oci-projected").textContent = `${formatBytes(oci.projected_month_end_bytes, base)} (${oci.projected_percent}%)`;
    $("oci-cycle-days").textContent = `${oci.days_remaining} 天`;

    const startDate = (oci.cycle_start || "").slice(0, 10);
    const endDate = (oci.cycle_end || "").slice(0, 10);
    $("oci-cycle-range").textContent = `账期 ${startDate} ~ ${endDate} · 日均 ${formatBytes(oci.daily_burn_bytes, base)}/天`;

    $("oci-marker-warn").style.left = `${Math.min(98, oci.warn_percent)}%`;
    $("oci-marker-danger").style.left = `${Math.min(98, oci.danger_percent)}%`;

    // Stacked protocol segments inside OCI progress bar
    const segHost = $("oci-bar-segments");
    const limit = Math.max(1, oci.limit_bytes);
    let usedPctAcc = 0;
    segHost.innerHTML = (oci.breakdown || [])
      .map((item) => {
        const rawPct = (item.oci_weighted_bytes / limit) * 100;
        const clamped = Math.max(0, Math.min(100 - usedPctAcc, rawPct));
        usedPctAcc += clamped;
        return `<div class="oci-segment" style="width:${clamped.toFixed(2)}%;background:${escapeHtml(item.color)}" title="${escapeHtml(item.label)}: ${formatBytes(item.oci_weighted_bytes, base)}"></div>`;
      })
      .join("");

    // Transparent multiplier formula chips
    const breakdownHost = $("oci-breakdown");
    breakdownHost.innerHTML = (oci.breakdown || [])
      .map(
        (item) => `
          <div class="oci-chip">
            <span class="dot" style="background:${escapeHtml(item.color)}"></span>
            <span>${escapeHtml(item.label)}</span>
            <span class="mono">${formatBytes(item.total_bytes, base)} × ${item.multiplier} = <strong>${formatBytes(item.oci_weighted_bytes, base)}</strong></span>
          </div>
        `
      )
      .join("");
  }

  function renderKpis(data) {
    const base = data.oci_quota?.unit_base || 1000;
    $("kpi-range-label").textContent = rangeLabel(data.range);
    $("devices-range-pill").textContent = rangeLabel(data.range);

    $("kpi-total-bytes").textContent = formatBytes(data.summary.total_bytes, base);
    $("kpi-up-bytes").textContent = formatBytes(data.summary.upload_bytes, base);
    $("kpi-down-bytes").textContent = formatBytes(data.summary.download_bytes, base);

    const p0 = data.protocols[0];
    const p1 = data.protocols[1];
    if (p0) {
      $("kpi-proto-0-bytes").textContent = formatBytes(p0.total_bytes, base);
      $("kpi-proto-0-sub").textContent = `原始占比 ${p0.share_percent}% · OCI 计入 ${formatBytes(p0.oci_weighted_bytes, base)}`;
    }
    if (p1) {
      $("kpi-proto-1-bytes").textContent = formatBytes(p1.total_bytes, base);
      $("kpi-proto-1-sub").textContent = `原始占比 ${p1.share_percent}% · OCI 计入 ${formatBytes(p1.oci_weighted_bytes, base)}`;
    }

    $("kpi-devices-count").textContent = `${data.summary.active_devices} / ${data.summary.total_devices}`;
  }

  function renderDevicesTable(data) {
    const tbody = $("devices-tbody");
    const base = data.oci_quota?.unit_base || 1000;
    const devices = data.devices || [];

    if (!devices.length) {
      tbody.innerHTML = `<tr><td colspan="5" class="empty-cell">暂无设备数据，点击右上角「新增设备」或从 sing-box 配置同步。</td></tr>`;
      return;
    }

    tbody.innerHTML = devices
      .map((dev) => {
        const statusPill =
          dev.status === "active"
            ? `<span class="pill pill-ok">启用</span>`
            : dev.status === "revoked"
              ? `<span class="pill pill-danger">已吊销</span>`
              : `<span class="pill pill-muted">仅观测</span>`;

        const protoTags = [];
        if (dev.has_vless) protoTags.push(`<span class="pill pill-proto">VLESS</span>`);
        if (dev.has_hysteria2) protoTags.push(`<span class="pill pill-proto pill-proto-hy2">Hy2</span>`);

        let actionsHtml = "";
        if (dev.status === "active") {
          actionsHtml = `
            <button class="btn btn-secondary btn-sm" data-action="qr" data-name="${escapeHtml(dev.name)}">二维码</button>
            <button class="btn btn-danger btn-sm" data-action="revoke" data-name="${escapeHtml(dev.name)}">吊销</button>
          `;
        } else if (dev.status === "revoked") {
          actionsHtml = `
            <button class="btn btn-secondary btn-sm" data-action="restore" data-name="${escapeHtml(dev.name)}">恢复换密</button>
            <button class="btn btn-danger btn-sm" data-action="delete" data-name="${escapeHtml(dev.name)}">删记录</button>
          `;
        } else {
          actionsHtml = `<span class="meta-text">未托管凭据</span>`;
        }

        return `
          <tr>
            <td>
              <div class="dev-name-row">
                <span class="dev-name">${escapeHtml(dev.name)}</span>
                ${statusPill}
                ${protoTags.join("")}
              </div>
              ${dev.note ? `<div class="dev-note">${escapeHtml(dev.note)}</div>` : ""}
            </td>
            <td class="num mono up-color">${formatBytes(dev.upload_bytes, base)}</td>
            <td class="num mono down-color">${formatBytes(dev.download_bytes, base)}</td>
            <td class="num share-cell">
              <div class="mono"><strong>${formatBytes(dev.total_bytes, base)}</strong> <span class="meta-text">(${dev.share_percent}%)</span></div>
              <div class="share-bar-bg"><div class="share-bar-fill" style="width:${Math.min(100, dev.share_percent)}%"></div></div>
            </td>
            <td class="actions-col">
              <div class="row-actions">${actionsHtml}</div>
            </td>
          </tr>
        `;
      })
      .join("");
  }

  function renderProtocolDonut(data) {
    const base = data.oci_quota?.unit_base || 1000;
    const protocols = data.protocols || [];
    const useOci = state.donutMode === "oci";

    const values = protocols.map((p) => (useOci ? p.oci_weighted_bytes : p.total_bytes));
    const total = values.reduce((a, b) => a + b, 0);

    const r = 72;
    const c = 2 * Math.PI * r;
    let offset = 0;

    const circles =
      total > 0
        ? protocols
            .map((p, idx) => {
              const frac = values[idx] / total;
              const dash = frac * c;
              const seg = `<circle cx="98" cy="98" r="${r}" fill="none" stroke="${escapeHtml(p.color)}" stroke-width="22" stroke-dasharray="${dash.toFixed(2)} ${c.toFixed(2)}" stroke-dashoffset="${(-offset).toFixed(2)}" transform="rotate(-90 98 98)" />`;
              offset += dash;
              return seg;
            })
            .join("")
        : `<circle cx="98" cy="98" r="${r}" fill="none" stroke="rgba(148,163,184,0.18)" stroke-width="22" />`;

    $("protocol-donut").innerHTML = `
      <svg viewBox="0 0 196 196" width="196" height="196" role="img" aria-label="协议分布图">
        <circle cx="98" cy="98" r="${r}" fill="none" stroke="rgba(148,163,184,0.12)" stroke-width="22" />
        ${circles}
        <text x="98" y="92" text-anchor="middle" fill="#94a3b8" font-size="11">${useOci ? "OCI 估算" : rangeLabel(data.range) + "原始"}</text>
        <text x="98" y="113" text-anchor="middle" fill="#f1f5f9" font-size="16" font-weight="700" font-family="ui-monospace, monospace">${formatBytes(total, base)}</text>
      </svg>
    `;

    $("protocol-legend").innerHTML = protocols
      .map((p) => {
        const val = useOci ? p.oci_weighted_bytes : p.total_bytes;
        const pct = useOci ? p.oci_share_percent : p.share_percent;
        return `
          <div class="proto-row">
            <div class="proto-left">
              <span class="dot" style="background:${escapeHtml(p.color)}"></span>
              <div>
                <div class="proto-title">${escapeHtml(p.label)}</div>
                <div class="proto-meta mono">↑ ${formatBytes(p.upload_bytes, base)} · ↓ ${formatBytes(p.download_bytes, base)} · ${p.multiplier}×</div>
              </div>
            </div>
            <div class="proto-right">
              <div><strong>${formatBytes(val, base)}</strong></div>
              <div class="meta-text">${pct}%</div>
            </div>
          </div>
        `;
      })
      .join("");
  }

  function renderTrendChart(data) {
    const base = data.oci_quota?.unit_base || 1000;
    const points = data.trend || [];
    $("trend-title").textContent =
      data.range === "today"
        ? "今日分时流量趋势（24 小时）"
        : data.range === "month"
          ? "本月每日流量趋势"
          : "近 30 日流量趋势";

    const host = $("trend-chart");
    const tooltip = $("trend-tooltip");

    if (!points.length) {
      host.innerHTML = `<div class="empty-cell">暂无趋势数据</div>`;
      return;
    }

    const width = 920;
    const height = 220;
    const pad = { top: 16, right: 16, bottom: 32, left: 66 };
    const plotW = width - pad.left - pad.right;
    const plotH = height - pad.top - pad.bottom;

    const maxVal = Math.max(1, ...points.map((p) => p.total_bytes));
    const gridTicks = [0, 0.25, 0.5, 0.75, 1];
    const barSlot = plotW / points.length;
    const barW = Math.max(4, Math.min(26, barSlot * 0.64));

    const gridLines = gridTicks
      .map((t) => {
        const y = pad.top + plotH * (1 - t);
        const val = maxVal * t;
        return `
          <line x1="${pad.left}" y1="${y.toFixed(1)}" x2="${width - pad.right}" y2="${y.toFixed(1)}" stroke="rgba(148,163,184,0.12)" stroke-dasharray="3 4" />
          <text x="${pad.left - 8}" y="${(y + 4).toFixed(1)}" text-anchor="end" fill="#64748b" font-size="10.5" font-family="ui-monospace, monospace">${formatBytes(val, base)}</text>
        `;
      })
      .join("");

    const labelStep = Math.max(1, Math.ceil(points.length / 10));
    const bars = points
      .map((pt, idx) => {
        const xCenter = pad.left + barSlot * idx + barSlot / 2;
        const x = xCenter - barW / 2;
        const totalH = (pt.total_bytes / maxVal) * plotH;
        const downH = (pt.download_bytes / maxVal) * plotH;
        const upH = Math.max(0, totalH - downH);
        const yDown = pad.top + plotH - downH;
        const yUp = yDown - upH;
        const showLabel = idx % labelStep === 0 || idx === points.length - 1;

        return `
          <g class="trend-bar-group" data-idx="${idx}" style="cursor:pointer">
            <rect x="${(pad.left + barSlot * idx).toFixed(1)}" y="${pad.top}" width="${barSlot.toFixed(1)}" height="${plotH}" fill="transparent" />
            <rect x="${x.toFixed(1)}" y="${yDown.toFixed(1)}" width="${barW.toFixed(1)}" height="${Math.max(0, downH).toFixed(1)}" rx="2" fill="#38bdf8" fill-opacity="0.85" />
            <rect x="${x.toFixed(1)}" y="${yUp.toFixed(1)}" width="${barW.toFixed(1)}" height="${Math.max(0, upH).toFixed(1)}" rx="2" fill="#34d399" fill-opacity="0.9" />
            ${showLabel ? `<text x="${xCenter.toFixed(1)}" y="${height - 10}" text-anchor="middle" fill="#64748b" font-size="10.5" font-family="ui-monospace, monospace">${escapeHtml(pt.label)}</text>` : ""}
          </g>
        `;
      })
      .join("");

    host.innerHTML = `
      <svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
        ${gridLines}
        ${bars}
      </svg>
    `;

    host.querySelectorAll(".trend-bar-group").forEach((group) => {
      group.addEventListener("mouseenter", (e) => {
        const idx = Number(group.dataset.idx);
        const pt = points[idx];
        tooltip.innerHTML = `
          <div style="font-weight:700;margin-bottom:4px" class="mono">${escapeHtml(pt.date || pt.label)}</div>
          <div class="mono down-color">下行 ↓ ${formatBytes(pt.download_bytes, base)}</div>
          <div class="mono up-color">上行 ↑ ${formatBytes(pt.upload_bytes, base)}</div>
          <div class="mono" style="margin-top:3px;border-top:1px solid rgba(148,163,184,0.18);padding-top:3px">
            总计 ${formatBytes(pt.total_bytes, base)}
          </div>
          ${pt.oci_weighted_bytes > 0 ? `<div class="mono meta-text">OCI 加权 ${formatBytes(pt.oci_weighted_bytes, base)}</div>` : ""}
        `;
        tooltip.classList.remove("hidden");
        positionTooltip(e);
      });
      group.addEventListener("mousemove", positionTooltip);
      group.addEventListener("mouseleave", () => tooltip.classList.add("hidden"));
    });

    function positionTooltip(e) {
      const rect = host.getBoundingClientRect();
      const left = Math.min(rect.width - 185, Math.max(8, e.clientX - rect.left + 14));
      const top = Math.max(8, e.clientY - rect.top - 24);
      tooltip.style.left = `${left}px`;
      tooltip.style.top = `${top}px`;
    }
  }

  function renderOpsSection(data) {
    $("ssh-tunnel-cmd").textContent = data.server_meta.ssh_tunnel_cmd;
    const h = data.singbox_health || {};
    $("backups-count-pill").textContent = `${h.backups_count || 0} 备份`;

    const matchedText = (h.matched_protocols || [])
      .map((m) => `${m.label}: ${m.matched_inbound_tag ? `✓ ${m.matched_inbound_tag} (:${m.listen_port || "?"})` : "✗ 未匹配"}`)
      .join("<br/>");

    $("singbox-health-grid").innerHTML = `
      <div class="health-item">
        <div class="h-label">sing-box 配置文件</div>
        <div class="h-val mono">${escapeHtml(h.config_path || "--")}</div>
      </div>
      <div class="health-item">
        <div class="h-label">API 探测状态</div>
        <div class="h-val">
          v2ray_api: ${h.v2ray_api_enabled ? `<span class="up-color">已启用</span>` : `<span class="meta-text">未配置</span>`}
          &nbsp;·&nbsp;
          clash_api: ${h.clash_api_enabled ? `<span class="up-color">已启用</span>` : `<span class="meta-text">未配置</span>`}
        </div>
      </div>
      <div class="health-item" style="grid-column: 1 / -1">
        <div class="h-label">已绑定入站协议</div>
        <div class="h-val mono" style="font-size:12px">${matchedText || "--"}</div>
      </div>
    `;
  }

  async function refreshAudit() {
    try {
      const audit = await apiFetch("/api/audit");
      const list = $("audit-list");
      const logs = audit.logs || [];
      $("backups-count-pill").textContent = `${(audit.backups || []).length} 备份`;
      if (!logs.length) {
        list.innerHTML = `<div class="empty-cell">暂无操作记录</div>`;
        return;
      }
      list.innerHTML = logs
        .slice(0, 15)
        .map(
          (item) => `
            <div class="audit-row">
              <div>
                <div>
                  <span class="pill ${item.success ? "pill-ok" : "pill-danger"}">${escapeHtml(item.action)}</span>
                  <strong class="mono" style="margin-left:6px">${escapeHtml(item.target)}</strong>
                </div>
                <div class="audit-detail">${escapeHtml(item.detail)}</div>
              </div>
              <div class="meta-text mono" style="white-space:nowrap">${formatShortTime(item.created_at)}</div>
            </div>
          `
        )
        .join("");
    } catch {
      // ignore passive audit refresh error
    }
  }

  async function refreshDashboard() {
    try {
      const data = await apiFetch(`/api/dashboard?range=${encodeURIComponent(state.range)}`);
      state.dashboard = data;
      renderTopbar(data);
      renderOciQuota(data.oci_quota);
      renderKpis(data);
      renderDevicesTable(data);
      renderProtocolDonut(data);
      renderTrendChart(data);
      renderOpsSection(data);
    } catch (err) {
      showToast(`加载仪表盘失败: ${err.message}`, "error");
    }
  }

  // --- Modals: Add, QR, Revoke ---

  function openModal(id) {
    $(id).classList.remove("hidden");
  }

  function closeModal(id) {
    $(id).classList.add("hidden");
  }

  function populateAddModalProtocols() {
    const host = $("add-protocols-list");
    const protos = state.dashboard?.configured_protocols || [];
    host.innerHTML = protos
      .map(
        (p) => `
          <label class="check-card">
            <input type="checkbox" name="protocols" value="${escapeHtml(p.key)}" checked />
            <span class="dot" style="background:${escapeHtml(p.color)}"></span>
            <div>
              <div style="font-weight:600">${escapeHtml(p.label)}</div>
              <div class="meta-text mono">inbound: ${escapeHtml(p.inbound_tag)} · OCI ${p.oci_multiplier}×</div>
            </div>
          </label>
        `
      )
      .join("");
  }

  function renderQrModal(bundle) {
    state.qrBundle = bundle;
    state.qrActiveTab = 0;
    state.revealSecret = false;

    $("modal-qr-title").textContent = `设备凭据：${bundle.device.name}`;
    $("modal-qr-subtitle").textContent = bundle.device.note
      ? `${bundle.device.note} · 本地离线生成二维码`
      : "本地离线生成二维码，凭据不经过任何第三方服务器";

    const tabs = [
      ...bundle.links.map((item, idx) => ({ idx, label: item.label })),
      { idx: bundle.links.length, label: "sing-box JSON" },
    ];

    const tabsHost = $("qr-tabs");
    tabsHost.innerHTML = tabs
      .map(
        (t, i) =>
          `<button class="qr-tab ${i === 0 ? "active" : ""}" data-qr-tab="${t.idx}">${escapeHtml(t.label)}</button>`
      )
      .join("");

    tabsHost.querySelectorAll(".qr-tab").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.qrActiveTab = Number(btn.dataset.qrTab);
        tabsHost.querySelectorAll(".qr-tab").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        renderQrTabBody();
      });
    });

    renderQrTabBody();
    openModal("modal-qr");
  }

  function renderQrTabBody() {
    const bundle = state.qrBundle;
    const host = $("qr-content");
    if (!bundle) return;

    if (state.qrActiveTab >= bundle.links.length) {
      host.innerHTML = `
        <pre class="json-box">${escapeHtml(bundle.singbox_client_json)}</pre>
        <button id="btn-copy-sb-json" class="btn btn-primary">复制 sing-box 客户端 Outbounds JSON</button>
      `;
      $("btn-copy-sb-json").addEventListener("click", () =>
        copyText(bundle.singbox_client_json, "sing-box JSON")
      );
      return;
    }

    const link = bundle.links[state.qrActiveTab];
    const devName = encodeURIComponent(bundle.device.name);
    const protoKey = encodeURIComponent(link.protocol_key);
    const maskedSecret = state.revealSecret
      ? link.credential_preview
      : `${link.credential_preview.slice(0, 6)}••••••••••••${link.credential_preview.slice(-4)}`;

    const warningsHtml = (link.warnings || [])
      .map((w) => `<div class="alert-banner" style="margin-bottom:10px">${escapeHtml(w)}</div>`)
      .join("");

    host.innerHTML = `
      ${warningsHtml}
      <div class="qr-layout">
        <div class="qr-frame">
          <img src="${link.qr_svg_data_uri}" alt="${escapeHtml(link.label)} 二维码" />
          <div class="qr-dl-row">
            <a class="btn btn-secondary btn-sm" href="/api/devices/${devName}/qrcode?protocol=${protoKey}&format=png" download>下载 PNG</a>
            <a class="btn btn-secondary btn-sm" href="/api/devices/${devName}/qrcode?protocol=${protoKey}&format=svg" download>下载 SVG</a>
          </div>
        </div>

        <div class="qr-meta-list">
          <div style="display:flex;gap:6px;flex-wrap:wrap">
            <span class="pill pill-proto">${escapeHtml(link.security)}</span>
            <span class="pill pill-muted mono">${escapeHtml(link.server)}</span>
            ${link.sni ? `<span class="pill pill-muted mono">SNI: ${escapeHtml(link.sni)}</span>` : ""}
          </div>

          <div>
            <div class="meta-text" style="margin-bottom:4px">
              密钥凭据
              <button id="btn-toggle-secret" class="link-btn" style="margin-left:8px">${state.revealSecret ? "隐藏" : "显示明文"}</button>
            </div>
            <div class="uri-box" style="max-height:none">${escapeHtml(maskedSecret)}</div>
          </div>

          <div>
            <div class="meta-text" style="margin-bottom:4px">通用导入链接（Shadowrocket / v2rayN / Nekobox / sing-box）</div>
            <div class="uri-box">${escapeHtml(link.uri)}</div>
          </div>

          <div style="display:flex;gap:8px;flex-wrap:wrap">
            <button id="btn-copy-uri" class="btn btn-primary">复制分享链接</button>
            <button id="btn-copy-secret" class="btn btn-secondary">复制密钥</button>
          </div>
        </div>
      </div>
    `;

    $("btn-copy-uri").addEventListener("click", () => copyText(link.uri, `${link.label} 分享链接`));
    $("btn-copy-secret").addEventListener("click", () => copyText(link.credential_preview, "设备密钥"));
    $("btn-toggle-secret").addEventListener("click", () => {
      state.revealSecret = !state.revealSecret;
      renderQrTabBody();
    });
  }

  // --- Event Bindings ---

  document.querySelectorAll(".range-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      state.range = tab.dataset.range;
      document.querySelectorAll(".range-tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      refreshDashboard();
    });
  });

  document.querySelectorAll("#donut-mode-toggle .mini-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.donutMode = btn.dataset.mode;
      document.querySelectorAll("#donut-mode-toggle .mini-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      if (state.dashboard) renderProtocolDonut(state.dashboard);
    });
  });

  $("btn-flush").addEventListener("click", async () => {
    const btn = $("btn-flush");
    btn.disabled = true;
    try {
      await apiFetch("/api/collector/flush", { method: "POST" });
      await Promise.all([refreshDashboard(), refreshAudit()]);
      showToast("已刷新最新流量数据");
    } catch (err) {
      showToast(`刷新失败: ${err.message}`, "error");
    } finally {
      btn.disabled = false;
    }
  });

  $("btn-sync-config").addEventListener("click", async () => {
    try {
      const res = await apiFetch("/api/singbox/sync", { method: "POST" });
      await Promise.all([refreshDashboard(), refreshAudit()]);
      showToast(`同步完成：发现 ${res.synced || 0} 台设备`);
    } catch (err) {
      showToast(`同步失败: ${err.message}`, "error");
    }
  });

  $("btn-copy-ssh").addEventListener("click", () => {
    copyText($("ssh-tunnel-cmd").textContent, "SSH 隧道命令");
  });

  $("btn-open-add").addEventListener("click", () => {
    $("form-add-device").reset();
    populateAddModalProtocols();
    openModal("modal-add");
    $("input-device-name").focus();
  });

  $("form-add-device").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    const submitBtn = $("btn-submit-add");
    const checked = Array.from(form.querySelectorAll('input[name="protocols"]:checked')).map(
      (el) => el.value
    );
    if (!checked.length) {
      showToast("请至少选择一个协议", "error");
      return;
    }

    const payload = {
      name: $("input-device-name").value.trim(),
      note: $("input-device-note").value.trim(),
      protocols: checked,
      vless_uuid: $("input-vless-uuid").value.trim() || null,
      hy2_password: $("input-hy2-password").value.trim() || null,
    };

    submitBtn.disabled = true;
    submitBtn.textContent = "校验并写入中...";
    try {
      const res = await apiFetch("/api/devices", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      closeModal("modal-add");
      showToast(`设备 ${res.device.name} 已创建并生效（${res.reload}）`);
      await Promise.all([refreshDashboard(), refreshAudit()]);
      if (res.share) renderQrModal(res.share);
    } catch (err) {
      showToast(`创建失败: ${err.message}`, "error");
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = "创建并生成二维码";
    }
  });

  // Table delegated actions (QR, Revoke, Restore, Delete)
  $("devices-tbody").addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-action]");
    if (!btn) return;
    const action = btn.dataset.action;
    const name = btn.dataset.name;

    if (action === "qr") {
      try {
        const bundle = await apiFetch(`/api/devices/${encodeURIComponent(name)}/share`);
        renderQrModal(bundle);
      } catch (err) {
        showToast(`获取二维码失败: ${err.message}`, "error");
      }
      return;
    }

    if (action === "revoke") {
      state.pendingRevoke = name;
      $("revoke-target-name").textContent = name;
      openModal("modal-revoke");
      return;
    }

    if (action === "restore") {
      btn.disabled = true;
      try {
        const res = await apiFetch(`/api/devices/${encodeURIComponent(name)}/restore`, {
          method: "POST",
          body: JSON.stringify({ rotate_credentials: true }),
        });
        showToast(`已恢复设备 ${name} 并签发新密钥`);
        await Promise.all([refreshDashboard(), refreshAudit()]);
        if (res.share) renderQrModal(res.share);
      } catch (err) {
        showToast(`恢复失败: ${err.message}`, "error");
      } finally {
        btn.disabled = false;
      }
      return;
    }

    if (action === "delete") {
      if (!window.confirm(`删除已吊销设备 ${name} 的记录？（历史流量统计会继续保留）`)) return;
      try {
        await apiFetch(`/api/devices/${encodeURIComponent(name)}`, { method: "DELETE" });
        showToast(`已删除设备记录 ${name}`);
        await Promise.all([refreshDashboard(), refreshAudit()]);
      } catch (err) {
        showToast(`删除失败: ${err.message}`, "error");
      }
    }
  });

  $("btn-confirm-revoke").addEventListener("click", async () => {
    const target = state.pendingRevoke;
    if (!target) return;
    const btn = $("btn-confirm-revoke");
    btn.disabled = true;
    btn.textContent = "吊销中...";
    try {
      const res = await apiFetch(`/api/devices/${encodeURIComponent(target)}/revoke`, {
        method: "POST",
      });
      closeModal("modal-revoke");
      showToast(`已吊销 ${target} 并重载 sing-box（${res.reload}）`);
      await Promise.all([refreshDashboard(), refreshAudit()]);
    } catch (err) {
      showToast(`吊销失败: ${err.message}`, "error");
    } finally {
      btn.disabled = false;
      btn.textContent = "确认吊销并重载";
      state.pendingRevoke = null;
    }
  });

  document.querySelectorAll("[data-close]").forEach((btn) => {
    btn.addEventListener("click", () => closeModal(btn.dataset.close));
  });

  document.querySelectorAll(".modal-backdrop").forEach((backdrop) => {
    backdrop.addEventListener("click", (e) => {
      if (e.target === backdrop) closeModal(backdrop.id);
    });
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      ["modal-add", "modal-qr", "modal-revoke"].forEach(closeModal);
    }
  });

  // Initial load + periodic auto-refresh every 15s
  refreshDashboard();
  refreshAudit();
  state.pollTimer = setInterval(() => {
    if (!document.hidden) refreshDashboard();
  }, 15000);
})();
