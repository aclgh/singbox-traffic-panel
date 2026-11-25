(() => {
  const state = {
    range: "month",
    donutMode: "raw",
    dashboard: null,
    sortKey: "total",
    sortDir: "descending",
    pendingRevoke: null,
    pendingDelete: null,
    qrBundle: null,
    pollTimer: null,
    dashboardAbort: null,
  };

  const $ = (id) => document.getElementById(id);
  const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

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

  // --- Screen reader announcement helper (ariaNotify + pre-existing live region fallback) ---

  let lastPoliteAnnouncement = "";

  function announcePolite(message) {
    if (!message) return;
    if (typeof document.ariaNotify === "function") {
      try {
        document.ariaNotify(message);
        return;
      } catch {
        // Fallback to live region below
      }
    }
    const region = $("sr-live-announcer");
    if (!region) return;
    let text = message;
    if (text === lastPoliteAnnouncement) text += "\u00A0";
    lastPoliteAnnouncement = text;
    region.textContent = "";
    requestAnimationFrame(() => {
      region.textContent = text;
    });
  }

  // --- Two-State Dark/Light Theme Toggle (modern-web-guidance/dark-mode-toggle) ---

  const themeMeta = document.querySelector('meta[name="color-scheme"]');
  const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");

  function getStoredTheme() {
    try {
      const val = localStorage.getItem("color-scheme");
      return val === "light" || val === "dark" ? val : null;
    } catch {
      return null;
    }
  }

  function applyThemeMeta(stored) {
    if (!themeMeta) return;
    themeMeta.content = stored ?? "light dark";
  }

  $("btn-theme-toggle")?.addEventListener("click", () => {
    const systemScheme = darkQuery.matches ? "dark" : "light";
    const currentRendered = getStoredTheme() ?? systemScheme;
    const target = currentRendered === "dark" ? "light" : "dark";

    if (target === systemScheme) {
      // Returning to match system preference: remove stored override so OS tracking resumes
      try {
        localStorage.removeItem("color-scheme");
      } catch {}
      applyThemeMeta(null);
    } else {
      try {
        localStorage.setItem("color-scheme", target);
      } catch {}
      applyThemeMeta(target);
    }
  });

  window.addEventListener("storage", (e) => {
    if (e.key === "color-scheme") {
      applyThemeMeta(e.newValue === "light" || e.newValue === "dark" ? e.newValue : null);
    }
  });

  // --- Top-Layer Animation & Exit Fallback (modern-web-guidance/animate-to-from-top-layer) ---

  let supportsDisplayTransitionCache;

  function canTransitionDisplay() {
    if (supportsDisplayTransitionCache !== undefined) return supportsDisplayTransitionCache;
    if (!window.CSS?.supports?.("transition-behavior", "allow-discrete") || !document.body) {
      return false;
    }
    const probe = document.createElement("div");
    probe.style.cssText = "transition: display 1s allow-discrete !important; display: block;";
    document.body.appendChild(probe);
    getComputedStyle(probe).display;
    probe.style.display = "none";
    supportsDisplayTransitionCache = getComputedStyle(probe).display === "block";
    probe.remove();
    return supportsDisplayTransitionCache;
  }

  function supportsTopLayerExit() {
    return Boolean(window.CSS?.supports?.("overlay", "auto") && canTransitionDisplay());
  }

  async function closeTopLayerDialog(dialog) {
    if (!dialog || !dialog.open) return;
    if (!supportsTopLayerExit()) {
      dialog.setAttribute("data-closing", "");
      const animations = dialog.getAnimations({ subtree: true });
      if (animations.length > 0) {
        await Promise.race([
          Promise.allSettled(animations.map((a) => a.finished)),
          new Promise((r) => setTimeout(r, 400)),
        ]);
      }
      if (!dialog.hasAttribute("data-closing")) return;
      dialog.removeAttribute("data-closing");
    }
    dialog.close();
  }

  function hasOpenModalDialog() {
    return Boolean(document.querySelector("dialog[open]"));
  }

  function setupDialogs() {
    const dialogs = document.querySelectorAll("dialog.panel-dialog");

    // Reset returnValue on close & route cancel through exit animation fallback when needed
    for (const dialog of dialogs) {
      dialog.addEventListener("close", () => {
        dialog.removeAttribute("data-closing");
        dialog.returnValue = "";
        flushQueuedToasts();
      });

      if (!supportsTopLayerExit()) {
        dialog.addEventListener("cancel", (event) => {
          event.preventDefault();
          closeTopLayerDialog(dialog);
        });
      }
    }

    // Light-dismiss fallback for browsers without native closedby="any" (e.g. Safari)
    if (!("closedBy" in HTMLDialogElement.prototype)) {
      const outside = (e, dialog) => {
        const r = dialog.getBoundingClientRect();
        return e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
      };
      for (const dialog of document.querySelectorAll('dialog[closedby="any"]')) {
        let pressedOutside = false;
        dialog.addEventListener("pointerdown", (e) => {
          pressedOutside = outside(e, dialog);
        });
        dialog.addEventListener("pointerup", (e) => {
          if (pressedOutside && outside(e, dialog)) closeTopLayerDialog(dialog);
          pressedOutside = false;
        });
      }
    }

    // Prepare Add Device form before opening via native Invoker Commands (command="show-modal")
    const addDialog = $("modal-add");
    addDialog?.addEventListener("command", (event) => {
      if (event.command === "show-modal") {
        prepareAddDeviceModal();
      } else if (event.command === "close" && !supportsTopLayerExit()) {
        event.preventDefault();
        closeTopLayerDialog(addDialog);
      }
    });

    for (const dialog of dialogs) {
      if (dialog === addDialog) continue;
      dialog.addEventListener("command", (event) => {
        if (event.command === "close" && !supportsTopLayerExit()) {
          event.preventDefault();
          closeTopLayerDialog(dialog);
        }
      });
    }

    // Fallback for browsers without Invoker Commands (command="show-modal" / "close")
    if (!("commandForElement" in HTMLButtonElement.prototype)) {
      document.addEventListener("click", (event) => {
        if (event.defaultPrevented) return;
        const btn = event.target.closest('button[command="show-modal"], button[command="close"]');
        if (!btn) return;
        const target = document.getElementById(btn.getAttribute("commandfor") || "");
        if (!(target instanceof HTMLDialogElement)) return;
        if (btn.getAttribute("command") === "show-modal") {
          if (target.id === "modal-add") prepareAddDeviceModal();
          if (!target.open) target.showModal();
        } else {
          closeTopLayerDialog(target);
        }
      });
    }
  }

  // --- Manual Popover Toasts (modern-web-guidance/persistent-toast-notifications) ---

  const supportsPopover = Object.hasOwn(HTMLElement.prototype, "popover");
  const activeToasts = [];
  const queuedToasts = [];
  let toastSeq = 0;

  function layoutToasts() {
    let offset = 0;
    for (let i = activeToasts.length - 1; i >= 0; i -= 1) {
      const entry = activeToasts[i];
      entry.el.style.setProperty("--toast-offset", `${offset}px`);
      const h = entry.el.getBoundingClientRect().height || 46;
      offset += h + 10;
    }
  }

  async function dismissToast(entry, { restoreFocus = false } = {}) {
    const idx = activeToasts.indexOf(entry);
    if (idx === -1) return;
    clearTimeout(entry.timer);
    activeToasts.splice(idx, 1);

    const hadFocus = entry.el.contains(document.activeElement);
    if (!supportsTopLayerExit() && supportsPopover) {
      entry.el.setAttribute("data-closing", "");
      const anims = entry.el.getAnimations({ subtree: true });
      if (anims.length) {
        await Promise.race([
          Promise.allSettled(anims.map((a) => a.finished)),
          new Promise((r) => setTimeout(r, 300)),
        ]);
      }
      entry.el.removeAttribute("data-closing");
    }

    if (supportsPopover) {
      try {
        entry.el.hidePopover();
      } catch {}
    } else {
      entry.el.classList.remove(":popover-open");
    }
    entry.el.remove();
    layoutToasts();

    if (restoreFocus && hadFocus) {
      const next = activeToasts[Math.min(idx, activeToasts.length - 1)];
      if (next) {
        next.closeBtn.focus();
      } else if (entry.prevFocus && document.contains(entry.prevFocus)) {
        entry.prevFocus.focus({ preventScroll: true });
      }
    }
  }

  function scheduleToastAutoDismiss(entry, delayMs = 3800) {
    clearTimeout(entry.timer);
    entry.timer = setTimeout(() => {
      if (entry.el.matches(":focus-within") || entry.el.matches(":hover")) {
        scheduleToastAutoDismiss(entry, 1500);
        return;
      }
      dismissToast(entry);
    }, delayMs);
  }

  function showToast(message, type = "ok") {
    // Do not surface background toasts while a modal dialog is open; queue until dismissed
    if (hasOpenModalDialog()) {
      queuedToasts.push({ message, type });
      return;
    }

    const region = $("toast-region");
    if (!region) return;

    toastSeq += 1;
    const id = `panel-toast-${toastSeq}`;
    const el = document.createElement("div");
    el.id = id;
    el.className = `toast-item ${type}`;
    el.setAttribute("popover", "manual");

    const textSpan = document.createElement("span");
    textSpan.textContent = message;

    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "toast-close";
    closeBtn.setAttribute("popovertarget", id);
    closeBtn.setAttribute("popovertargetaction", "hide");
    closeBtn.setAttribute("aria-label", "关闭通知");
    closeBtn.textContent = "×";

    el.append(textSpan, closeBtn);
    region.append(el);

    const entry = {
      el,
      closeBtn,
      timer: null,
      prevFocus: document.activeElement instanceof HTMLElement ? document.activeElement : null,
    };

    closeBtn.addEventListener("click", (e) => {
      e.preventDefault();
      dismissToast(entry, { restoreFocus: true });
    });

    activeToasts.push(entry);
    if (supportsPopover) {
      el.showPopover();
    } else {
      el.classList.add(":popover-open");
    }
    layoutToasts();
    announcePolite(message);
    scheduleToastAutoDismiss(entry, type === "error" ? 5500 : 3600);
  }

  function flushQueuedToasts() {
    if (hasOpenModalDialog() || !queuedToasts.length) return;
    while (queuedToasts.length) {
      const next = queuedToasts.shift();
      showToast(next.message, next.type);
    }
  }

  // --- Declarative Copy Buttons (modern-web-guidance/copy-button) ---

  const copyTimers = new WeakMap();
  let lastCopyAnnouncement = "";

  function legacyCopy(text) {
    const previousFocus = document.activeElement;
    const field = Object.assign(document.createElement("textarea"), { value: text });
    field.setAttribute("readonly", "");
    field.style.cssText = "position:fixed;inset-block-start:0;opacity:0";
    document.body.append(field);
    field.select();
    try {
      return document.execCommand("copy");
    } finally {
      field.remove();
      if (previousFocus instanceof HTMLElement) {
        previousFocus.focus({ preventScroll: true });
      }
    }
  }

  function reportCopy(button, ok) {
    const tip = button.nextElementSibling;
    clearTimeout(copyTimers.get(button));
    button.removeAttribute("data-copied");
    button.removeAttribute("data-failed");

    let message = ok ? "已复制" : "复制失败";
    if (message === lastCopyAnnouncement) message += "\u00A0";
    lastCopyAnnouncement = message;

    if (tip) tip.textContent = "";
    void button.offsetWidth;
    button.setAttribute(ok ? "data-copied" : "data-failed", "");
    if (tip) {
      requestAnimationFrame(() => {
        tip.textContent = message;
      });
    }

    copyTimers.set(
      button,
      setTimeout(() => {
        button.removeAttribute("data-copied");
        button.removeAttribute("data-failed");
        if (tip) tip.textContent = "";
      }, 2000)
    );
  }

  async function copyFromTarget(button, target) {
    const raw =
      target.dataset.copyValue !== undefined
        ? target.dataset.copyValue
        : target.matches("input, textarea")
          ? target.value
          : target.innerText.trim();
    if (!raw) return;
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(raw);
      } else if (!legacyCopy(raw)) {
        throw new Error("copy failed");
      }
      reportCopy(button, true);
    } catch {
      try {
        if (!legacyCopy(raw)) throw new Error("copy failed");
        reportCopy(button, true);
      } catch {
        reportCopy(button, false);
      }
    }
  }

  // Register only ONE path (CommandEvent OR fallback click) so copy never fires twice
  if ("CommandEvent" in window) {
    document.addEventListener(
      "command",
      (event) => {
        if (event.command === "--copy" && event.source && event.target) {
          copyFromTarget(event.source, event.target);
        }
      },
      true
    );
  } else {
    document.addEventListener("click", (event) => {
      if (event.defaultPrevented) return;
      const button = event.target.closest('button[command="--copy"]');
      if (!button) return;
      const target = document.getElementById(button.getAttribute("commandfor") || "");
      if (target) copyFromTarget(button, target);
    });
  }

  // --- Tabs Helper (focusgroup + offline keyboard roving-tabindex fallback) ---

  const supportsFocusgroup = Object.hasOwn(HTMLElement.prototype, "focusGroup");

  function syncTabSelection(tablist, selectedTab) {
    const tabs = Array.from(tablist.querySelectorAll('[role="tab"]'));
    for (const tab of tabs) {
      const isSelected = tab === selectedTab;
      tab.setAttribute("aria-selected", String(isSelected));
      tab.toggleAttribute("focusgroupstart", isSelected);
      if (!supportsFocusgroup) {
        tab.tabIndex = isSelected ? 0 : -1;
      }
    }
    try {
      selectedTab.scrollIntoView({ block: "nearest", inline: "nearest", container: "nearest" });
    } catch {
      selectedTab.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
  }

  function wireTablistKeyboard(tablist, onSelect, { followFocus = true } = {}) {
    const tabs = () => Array.from(tablist.querySelectorAll('[role="tab"]'));
    const initial = tabs().find((t) => t.getAttribute("aria-selected") === "true") || tabs()[0];
    if (initial) syncTabSelection(tablist, initial);

    tablist.addEventListener("click", (e) => {
      const tab = e.target.closest('[role="tab"]');
      if (!tab) return;
      syncTabSelection(tablist, tab);
      onSelect(tab);
    });

    tablist.addEventListener("keydown", (e) => {
      if (supportsFocusgroup) return;
      const list = tabs();
      if (!list.length) return;
      const idx = list.indexOf(document.activeElement);
      if (idx === -1) return;

      let nextIdx = idx;
      if (e.key === "ArrowRight") nextIdx = (idx + 1) % list.length;
      else if (e.key === "ArrowLeft") nextIdx = (idx - 1 + list.length) % list.length;
      else if (e.key === "Home") nextIdx = 0;
      else if (e.key === "End") nextIdx = list.length - 1;
      else return;

      e.preventDefault();
      list[nextIdx].focus();
    });

    if (followFocus) {
      tablist.addEventListener("focusin", (e) => {
        const tab = e.target.closest('[role="tab"]');
        if (!tab || tab.getAttribute("aria-selected") === "true") return;
        syncTabSelection(tablist, tab);
        onSelect(tab);
      });
    }
  }

  // --- Deferred Form Validation & ARIA Sync (modern-web-guidance/validate-input-after-interaction) ---

  function setupFormValidationSync(form) {
    if (!form) return;
    const supportsUserInvalid = CSS.supports("selector(:user-invalid)");
    const dirtyState = new WeakMap();
    const flagged = new WeakSet();
    const announced = new WeakSet();
    let announceTimer = null;

    const linkError = (el, shown) => {
      const errorId = el.dataset.error;
      if (!errorId) return;
      const ids = (el.getAttribute("aria-describedby") || "")
        .split(/\s+/)
        .filter((id) => id && id !== errorId);
      if (shown) ids.push(errorId);
      if (ids.length) el.setAttribute("aria-describedby", ids.join(" "));
      else el.removeAttribute("aria-describedby");
    };

    const announceFieldError = (el) => {
      const errEl = document.getElementById(el.dataset.error || "");
      if (!errEl) return;
      clearTimeout(announceTimer);
      announceTimer = setTimeout(() => {
        announcePolite(errEl.textContent.replace(/\s+/g, " ").trim());
      }, 450);
    };

    const syncAria = (el, shouldAnnounce = false) => {
      if (!el.matches?.("input, select, textarea")) return;
      const invalid = supportsUserInvalid
        ? el.matches(":user-invalid")
        : Boolean(dirtyState.get(el)?.showError && !el.validity.valid);

      if (!supportsUserInvalid && dirtyState.get(el)?.showError) {
        el.classList.toggle("user-invalid-fallback", !el.validity.valid);
        el.classList.toggle("user-valid-fallback", el.validity.valid);
      }

      if (invalid) {
        if (shouldAnnounce && !announced.has(el)) {
          announceFieldError(el);
          announced.add(el);
        }
        el.setAttribute("aria-invalid", "true");
        linkError(el, true);
        flagged.add(el);
      } else if (flagged.has(el) || el.getAttribute("aria-invalid") !== "true") {
        el.setAttribute("aria-invalid", "false");
        linkError(el, false);
        flagged.delete(el);
        announced.delete(el);
      }
    };

    form.querySelectorAll("input, select, textarea").forEach((el) => syncAria(el));

    form.addEventListener(
      "blur",
      (e) => {
        const el = e.target;
        if (!supportsUserInvalid && el.matches?.("input, select, textarea")) {
          const st = dirtyState.get(el) || { interacted: false, showError: false };
          if (st.interacted) st.showError = true;
          dirtyState.set(el, st);
        }
        syncAria(el, true);
      },
      true
    );

    form.addEventListener("input", (e) => {
      const el = e.target;
      if (!supportsUserInvalid && el.matches?.("input, select, textarea")) {
        const st = dirtyState.get(el) || { interacted: false, showError: false };
        st.interacted = true;
        dirtyState.set(el, st);
      }
      if (el.hasAttribute("aria-invalid")) syncAria(el);
    });

    form.addEventListener(
      "invalid",
      (e) => {
        const el = e.target;
        if (!supportsUserInvalid) {
          dirtyState.set(el, { interacted: true, showError: true });
        }
        announced.add(el);
        syncAria(el);
      },
      true
    );

    form.addEventListener("reset", () => {
      setTimeout(() => {
        for (const el of form.elements) {
          dirtyState.delete(el);
          el.classList.remove("user-invalid-fallback", "user-valid-fallback");
          syncAria(el);
        }
      });
    });
  }

  // --- Sortable Devices Table (modern-web-guidance/sortable-table + dynamic-sort) ---

  const devicesTable = $("devices-table");
  const devicesTbody = $("devices-tbody");

  function reorderNodes(container, orderedNodes) {
    const move = Object.hasOwn(Element.prototype, "moveBefore")
      ? (node) => container.moveBefore(node, null)
      : (node) => container.append(node);
    for (const node of orderedNodes) move(node);
  }

  function sortRowsInPlace(rows, cellIndex, type, direction) {
    const factor = direction === "ascending" ? 1 : -1;
    const rawValue = (row) => {
      const cell = row.cells[cellIndex];
      if (!cell) return "";
      return cell.dataset.value ?? cell.textContent.trim();
    };
    return rows.sort((a, b) => {
      const va = rawValue(a);
      const vb = rawValue(b);
      if (type === "number") return factor * (Number(va) - Number(vb));
      return factor * collator.compare(String(va), String(vb));
    });
  }

  function applyTableSort(button, { animate = true, announce = true } = {}) {
    const th = button.closest("th");
    if (!th || !devicesTable || !devicesTbody) return;

    const rows = Array.from(devicesTbody.rows).filter((r) => r.dataset.deviceName);
    if (!rows.length) return;

    const direction =
      animate
        ? th.getAttribute("aria-sort") === "ascending"
          ? "descending"
          : "ascending"
        : state.sortDir;

    state.sortKey = th.dataset.sortKey || "total";
    state.sortDir = direction;

    for (const header of devicesTable.tHead.rows[0].cells) {
      header.removeAttribute("aria-sort");
    }
    th.setAttribute("aria-sort", direction);
    syncMobileSortPills();

    const ordered = sortRowsInPlace(rows, th.cellIndex, th.dataset.type || "text", direction);
    const update = () => reorderNodes(devicesTbody, ordered);

    if (animate) {
      if (typeof devicesTable.startViewTransition === "function") {
        devicesTable.startViewTransition(update);
      } else if (typeof document.startViewTransition === "function") {
        document.startViewTransition(update);
      } else {
        update();
      }
    } else {
      update();
    }

    if (announce) {
      const dirLabel = direction === "ascending" ? "升序" : "降序";
      const colLabel = button.querySelector("span")?.textContent.trim() || button.textContent.trim();
      const status = $("table-sort-status");
      if (status) {
        status.textContent = `已按${colLabel}${dirLabel}排列，共 ${ordered.length} 台设备`;
      }
    }
  }

  const mobileSortLabels = {
    total: "总计",
    download: "下行",
    upload: "上行",
    name: "设备名",
  };

  function syncMobileSortPills() {
    for (const pill of document.querySelectorAll(".mobile-sort-pill")) {
      const key = pill.dataset.mobileSort;
      const isActive = key === state.sortKey;
      pill.setAttribute("aria-pressed", String(isActive));
      const base = mobileSortLabels[key] || key;
      pill.textContent = isActive ? `${base} ${state.sortDir === "ascending" ? "↑" : "↓"}` : base;
    }
  }

  document.querySelector(".mobile-sort-bar")?.addEventListener("click", (e) => {
    const pill = e.target.closest("button[data-mobile-sort]");
    if (!pill || !devicesTable) return;
    const key = pill.dataset.mobileSort;
    const th = devicesTable.querySelector(`th[data-sort-key="${CSS.escape(key)}"]`);
    const btn = th?.querySelector("button");
    if (btn) applyTableSort(btn, { animate: true, announce: true });
  });

  if (devicesTable) {
    if ("commandForElement" in HTMLButtonElement.prototype) {
      devicesTable.addEventListener("command", (event) => {
        if (event.command === "--sort" && event.source) {
          applyTableSort(event.source, { animate: true, announce: true });
        }
      });
    } else {
      for (const btn of devicesTable.tHead.querySelectorAll('button[command="--sort"]')) {
        btn.addEventListener("click", () => applyTableSort(btn, { animate: true, announce: true }));
      }
    }
  }

  // --- API Fetch Helper ---

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

  // --- Rendering ---

  function renderTopbar(data) {
    const col = data.collector || {};
    const demoBadge = $("demo-badge");
    if (demoBadge) {
      demoBadge.hidden = col.active_source !== "mock";
    }

    const banner = $("diagnostic-banner");
    const messages = [];
    let isDanger = false;
    if (col.last_error) {
      messages.push("流量数据暂时无法同步，请稍后刷新重试。");
    } else if (col.active_source === "mock" && col.diagnostics?.hint) {
      messages.push(col.diagnostics.hint);
    }
    if (data.oci_quota?.alert_level === "danger") {
      messages.push(
        `本月流量已接近上限：${data.oci_quota.percent}%（${formatBytes(
          data.oci_quota.used_bytes,
          data.oci_quota.unit_base
        )} / ${data.oci_quota.monthly_limit_tb} TB）`
      );
      isDanger = true;
    }

    if (messages.length > 0) {
      banner.textContent = messages.join(" ｜ ");
      banner.className = `alert-banner${isDanger ? " danger" : ""}`;
      banner.hidden = false;
    } else {
      banner.hidden = true;
      banner.textContent = "";
    }
  }

  function renderOciQuota(oci) {
    const base = oci.unit_base || 1000;
    const card = document.querySelector(".oci-card");
    card.classList.remove("level-warning", "level-danger");
    if (oci.alert_level === "warning") card.classList.add("level-warning");
    if (oci.alert_level === "danger") card.classList.add("level-danger");

    const usedText = formatBytes(oci.used_bytes, base);
    const unitName = base === 1024 ? "TiB" : "TB";
    $("oci-used").textContent = usedText;
    $("oci-limit").textContent = `${oci.monthly_limit_tb.toFixed(2)} ${unitName}`;
    $("oci-scale-max").textContent = `${oci.monthly_limit_tb} ${unitName}`;

    const pill = $("oci-percent-pill");
    pill.textContent =
      oci.alert_level === "danger"
        ? `⚠ ${oci.percent}% · 红线预警`
        : oci.alert_level === "warning"
          ? `△ ${oci.percent}% · 需留意`
          : `✓ ${oci.percent}% · 充裕`;
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
    $("oci-cycle-range").innerHTML = `账期 <span class="nowrap">${escapeHtml(startDate)} ~ ${escapeHtml(endDate)}</span> · <span class="nowrap">日均 ${escapeHtml(formatBytes(oci.daily_burn_bytes, base))}/天</span>`;

    $("oci-marker-warn").style.insetInlineStart = `${Math.min(98, oci.warn_percent)}%`;
    $("oci-marker-danger").style.insetInlineStart = `${Math.min(98, oci.danger_percent)}%`;

    const track = $("oci-progress-track");
    track.setAttribute("aria-valuenow", String(Math.min(100, oci.percent)));
    track.setAttribute("aria-valuetext", `${usedText} / ${oci.monthly_limit_tb} ${unitName} (${oci.percent}%)`);

    const segHost = $("oci-bar-segments");
    const limit = Math.max(1, oci.limit_bytes);
    let usedPctAcc = 0;
    segHost.innerHTML = (oci.breakdown || [])
      .map((item) => {
        const rawPct = (item.oci_weighted_bytes / limit) * 100;
        const clamped = Math.max(0, Math.min(100 - usedPctAcc, rawPct));
        usedPctAcc += clamped;
        return `<div class="oci-segment" style="inline-size:${clamped.toFixed(2)}%;background:${escapeHtml(item.color)}" title="${escapeHtml(item.label)}: ${formatBytes(item.oci_weighted_bytes, base)}"></div>`;
      })
      .join("");

    $("oci-breakdown").innerHTML = (oci.breakdown || [])
      .map(
        (item) => `
          <div class="oci-chip">
            <span class="oci-chip-head">
              <span class="dot" style="background:${escapeHtml(item.color)}" aria-hidden="true"></span>
              <span>${escapeHtml(item.label)}</span>
            </span>
            <span class="mono oci-chip-val">${formatBytes(item.total_bytes, base)} × ${item.multiplier} = <strong>${formatBytes(item.oci_weighted_bytes, base)}</strong></span>
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
      $("kpi-proto-0-label").textContent = `${p0.label}（${p0.multiplier}×）`;
      $("kpi-proto-0-bytes").textContent = formatBytes(p0.total_bytes, base);
      $("kpi-proto-0-sub").textContent = `原始占比 ${p0.share_percent}% · OCI 计入 ${formatBytes(p0.oci_weighted_bytes, base)}`;
    }
    if (p1) {
      $("kpi-proto-1-label").textContent = `${p1.label}（${p1.multiplier}×）`;
      $("kpi-proto-1-bytes").textContent = formatBytes(p1.total_bytes, base);
      $("kpi-proto-1-sub").textContent = `原始占比 ${p1.share_percent}% · OCI 计入 ${formatBytes(p1.oci_weighted_bytes, base)}`;
    }

    $("kpi-devices-count").textContent = `${data.summary.active_devices} / ${data.summary.total_devices}`;
  }

  function buildDeviceRowMarkup(dev, base) {
    const statusPill =
      dev.status === "active"
        ? `<span class="pill pill-ok">● 启用</span>`
        : dev.status === "revoked"
          ? `<span class="pill pill-danger">✕ 已吊销</span>`
          : `<span class="pill pill-muted">○ 仅观测</span>`;

    const protoTags = [];
    if (dev.has_vless) protoTags.push(`<span class="pill pill-proto">VLESS</span>`);
    if (dev.has_hysteria2) protoTags.push(`<span class="pill pill-proto pill-proto-hy2">Hy2</span>`);

    let actionsHtml = "";
    if (dev.status === "active") {
      actionsHtml = `
        <button type="button" class="btn btn-secondary btn-sm" data-action="qr" data-name="${escapeHtml(dev.name)}">二维码</button>
        <button type="button" class="btn btn-danger btn-sm" data-action="revoke" data-name="${escapeHtml(dev.name)}">吊销</button>
      `;
    } else if (dev.status === "revoked") {
      actionsHtml = `
        <button type="button" class="btn btn-secondary btn-sm" data-action="restore" data-name="${escapeHtml(dev.name)}">恢复换密</button>
        <button type="button" class="btn btn-danger btn-sm" data-action="delete" data-name="${escapeHtml(dev.name)}">删记录</button>
      `;
    } else {
      actionsHtml = `<span class="meta-text">未托管凭据</span>`;
    }

    const shareVal = Math.min(100, Math.max(0, Number(dev.share_percent || 0)));

    return `
      <th scope="row" data-value="${escapeHtml(dev.name)}">
        <div class="dev-name-row">
          <span class="dev-name">${escapeHtml(dev.name)}</span>
          ${statusPill}
          ${protoTags.join("")}
        </div>
        ${dev.note ? `<div class="dev-note">${escapeHtml(dev.note)}</div>` : ""}
      </th>
      <td class="num mono up-color" data-value="${Number(dev.upload_bytes || 0)}">${formatBytes(dev.upload_bytes, base)}</td>
      <td class="num mono down-color" data-value="${Number(dev.download_bytes || 0)}">${formatBytes(dev.download_bytes, base)}</td>
      <td class="num share-cell" data-value="${Number(dev.total_bytes || 0)}">
        <div class="mono"><strong>${formatBytes(dev.total_bytes, base)}</strong> <span class="meta-text">(${dev.share_percent}%)</span></div>
        <progress class="share-progress" max="100" value="${shareVal}" aria-label="${escapeHtml(dev.name)} 流量占比 ${dev.share_percent}%"></progress>
      </td>
      <td class="actions-col">
        <div class="row-actions">${actionsHtml}</div>
      </td>
    `;
  }

  function renderDevicesTable(data) {
    const base = data.oci_quota?.unit_base || 1000;
    const devices = data.devices || [];

    if (!devices.length) {
      devicesTbody.innerHTML = `<tr><td colspan="5" class="empty-cell">暂无设备，点击右上角「新增设备」添加。</td></tr>`;
      return;
    }

    // Reuse existing <tr> nodes by device name so sort view-transitions & identity persist
    const existingByName = new Map();
    for (const row of Array.from(devicesTbody.rows)) {
      if (row.dataset.deviceName) {
        existingByName.set(row.dataset.deviceName, row);
      } else {
        row.remove();
      }
    }

    const activeNames = new Set();
    for (const dev of devices) {
      activeNames.add(dev.name);
      let row = existingByName.get(dev.name);
      if (!row) {
        row = document.createElement("tr");
        row.dataset.deviceName = dev.name;
        devicesTbody.append(row);
      }
      row.innerHTML = buildDeviceRowMarkup(dev, base);
    }

    for (const [name, row] of existingByName) {
      if (!activeNames.has(name)) row.remove();
    }

    // Re-apply active sort without animation on background poll
    const activeTh = devicesTable.querySelector(`th[data-sort-key="${CSS.escape(state.sortKey)}"]`);
    const activeBtn = activeTh?.querySelector("button");
    if (activeBtn) {
      applyTableSort(activeBtn, { animate: false, announce: false });
    }
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
        : `<circle cx="98" cy="98" r="${r}" fill="none" stroke="var(--border-strong)" stroke-width="22" />`;

    $("protocol-donut").innerHTML = `
      <svg viewBox="0 0 196 196" width="196" height="196" role="img" aria-label="协议分布图">
        <circle cx="98" cy="98" r="${r}" fill="none" stroke="var(--surface-elevated)" stroke-width="22" />
        ${circles}
        <text x="98" y="92" text-anchor="middle" fill="var(--text-muted)" font-size="11">${useOci ? "OCI 估算" : rangeLabel(data.range) + "原始"}</text>
        <text x="98" y="113" text-anchor="middle" fill="var(--text)" font-size="16" font-weight="700" font-family="var(--font-mono)">${formatBytes(total, base)}</text>
      </svg>
    `;

    $("protocol-legend").innerHTML = protocols
      .map((p) => {
        const val = useOci ? p.oci_weighted_bytes : p.total_bytes;
        const pct = useOci ? p.oci_share_percent : p.share_percent;
        return `
          <div class="proto-row">
            <div class="proto-left">
              <span class="dot" style="background:${escapeHtml(p.color)}" aria-hidden="true"></span>
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

    const wrap = host.closest(".trend-chart-wrap") || host;
    const width = Math.max(260, Math.floor(wrap.clientWidth || host.clientWidth || 720));
    host.dataset.renderedWidth = String(width);
    const isNarrow = width < 480;
    const height = isNarrow ? 200 : 224;
    const pad = { top: 14, right: 10, bottom: 30, left: isNarrow ? 54 : 68 };
    const plotW = Math.max(120, width - pad.left - pad.right);
    const plotH = height - pad.top - pad.bottom;

    const maxVal = Math.max(1, ...points.map((p) => p.total_bytes));
    const gridTicks = isNarrow ? [0, 0.5, 1] : [0, 0.25, 0.5, 0.75, 1];
    const barSlot = plotW / points.length;
    const barW = Math.max(3, Math.min(26, barSlot * 0.64));

    const gridLines = gridTicks
      .map((t) => {
        const y = pad.top + plotH * (1 - t);
        const val = maxVal * t;
        return `
          <line x1="${pad.left}" y1="${y.toFixed(1)}" x2="${width - pad.right}" y2="${y.toFixed(1)}" stroke="var(--border)" stroke-dasharray="3 4" />
          <text x="${pad.left - 6}" y="${(y + 4).toFixed(1)}" text-anchor="end" fill="var(--text-dim)" font-size="${isNarrow ? 9.5 : 10.5}" font-family="var(--font-mono)">${formatBytes(val, base)}</text>
        `;
      })
      .join("");

    const maxLabels = isNarrow ? 5 : 10;
    const labelStep = Math.max(1, Math.ceil(points.length / maxLabels));
    const bars = points
      .map((pt, idx) => {
        const xCenter = pad.left + barSlot * idx + barSlot / 2;
        const x = xCenter - barW / 2;
        const totalH = (pt.total_bytes / maxVal) * plotH;
        const downH = (pt.download_bytes / maxVal) * plotH;
        const upH = Math.max(0, totalH - downH);
        const yDown = pad.top + plotH - downH;
        const yUp = yDown - upH;
        const showLabel = idx % labelStep === 0 || (idx === points.length - 1 && points.length - 1 - Math.floor((points.length - 1) / labelStep) * labelStep >= Math.ceil(labelStep / 2));

        return `
          <g class="trend-bar-group" data-idx="${idx}" style="cursor:pointer">
            <rect x="${(pad.left + barSlot * idx).toFixed(1)}" y="${pad.top}" width="${barSlot.toFixed(1)}" height="${plotH}" fill="transparent" />
            <rect x="${x.toFixed(1)}" y="${yDown.toFixed(1)}" width="${barW.toFixed(1)}" height="${Math.max(0, downH).toFixed(1)}" rx="2" fill="var(--primary)" fill-opacity="0.88" />
            <rect x="${x.toFixed(1)}" y="${yUp.toFixed(1)}" width="${barW.toFixed(1)}" height="${Math.max(0, upH).toFixed(1)}" rx="2" fill="var(--ok)" fill-opacity="0.92" />
            ${showLabel ? `<text x="${xCenter.toFixed(1)}" y="${height - 10}" text-anchor="middle" fill="var(--text-dim)" font-size="${isNarrow ? 9.5 : 10.5}" font-family="var(--font-mono)">${escapeHtml(pt.label)}</text>` : ""}
          </g>
        `;
      })
      .join("");

    host.innerHTML = `
      <svg viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" role="img" aria-label="流量趋势柱状图">
        ${gridLines}
        ${bars}
      </svg>
    `;

    function showTooltipFor(idx, clientX, clientY) {
      const pt = points[idx];
      if (!pt) return;
      tooltip.innerHTML = `
        <div style="font-weight:700;margin-block-end:4px" class="mono">${escapeHtml(pt.date || pt.label)}</div>
        <div class="mono down-color">下行 ↓ ${formatBytes(pt.download_bytes, base)}</div>
        <div class="mono up-color">上行 ↑ ${formatBytes(pt.upload_bytes, base)}</div>
        <div class="mono" style="margin-block-start:3px;border-block-start:1px solid var(--border);padding-block-start:3px">
          总计 ${formatBytes(pt.total_bytes, base)}
        </div>
        ${pt.oci_weighted_bytes > 0 ? `<div class="mono meta-text">OCI 加权 ${formatBytes(pt.oci_weighted_bytes, base)}</div>` : ""}
      `;
      tooltip.hidden = false;
      const rect = host.getBoundingClientRect();
      const tipW = tooltip.offsetWidth || 170;
      const left = Math.min(Math.max(4, rect.width - tipW - 4), Math.max(4, clientX - rect.left - tipW / 2));
      const top = Math.max(4, Math.min(rect.height - 96, clientY - rect.top - 96));
      tooltip.style.insetInlineStart = `${left}px`;
      tooltip.style.insetBlockStart = `${top}px`;
    }

    host.querySelectorAll(".trend-bar-group").forEach((group) => {
      const idx = Number(group.dataset.idx);
      group.addEventListener("mouseenter", (e) => showTooltipFor(idx, e.clientX, e.clientY));
      group.addEventListener("mousemove", (e) => showTooltipFor(idx, e.clientX, e.clientY));
      group.addEventListener("click", (e) => {
        e.stopPropagation();
        showTooltipFor(idx, e.clientX, e.clientY);
      });
      group.addEventListener("mouseleave", () => {
        if (window.matchMedia("(hover: hover)").matches) tooltip.hidden = true;
      });
    });
  }

  const AUDIT_ACTION_LABELS = {
    "device.create": "新增设备",
    "device.revoke": "吊销设备",
    "device.restore": "恢复设备",
    "device.delete": "删除记录",
    "singbox.sync": "同步设备",
  };

  async function refreshAudit() {
    try {
      const audit = await apiFetch("/api/audit");
      const list = $("audit-list");
      const logs = audit.logs || [];
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
                  <span class="pill ${item.success ? "pill-ok" : "pill-danger"}">${escapeHtml(AUDIT_ACTION_LABELS[item.action] || item.action)}</span>
                  <strong class="mono" style="margin-inline-start:6px">${escapeHtml(item.target)}</strong>
                </div>
                <div class="audit-detail">${escapeHtml(item.detail)}</div>
              </div>
              <div class="meta-text mono" style="white-space:nowrap">${formatShortTime(item.created_at)}</div>
            </div>
          `
        )
        .join("");
    } catch {
      // Passive audit refresh error ignored
    }
  }

  async function refreshDashboard() {
    state.dashboardAbort?.abort();
    const controller = new AbortController();
    state.dashboardAbort = controller;

    try {
      const data = await apiFetch(`/api/dashboard?range=${encodeURIComponent(state.range)}`, {
        signal: controller.signal,
      });
      state.dashboard = data;
      renderTopbar(data);
      renderOciQuota(data.oci_quota);
      renderKpis(data);
      renderDevicesTable(data);
      renderProtocolDonut(data);
      renderTrendChart(data);
    } catch (err) {
      if (err.name === "AbortError") return;
      showToast(`加载失败: ${err.message}`, "error");
    }
  }

  // --- Add Device Modal ---

  function prepareAddDeviceModal() {
    const form = $("form-add-device");
    form.reset();
    const inlineErr = $("add-form-error");
    inlineErr.hidden = true;
    inlineErr.textContent = "";

    const host = $("add-protocols-list");
    const protos = state.dashboard?.configured_protocols || [];
    host.innerHTML = protos
      .map(
        (p) => `
          <label class="check-card">
            <input type="checkbox" name="protocols" value="${escapeHtml(p.key)}" checked />
            <span class="dot" style="background:${escapeHtml(p.color)}" aria-hidden="true"></span>
            <div>
              <div style="font-weight:600">${escapeHtml(p.label)}</div>
              <div class="meta-text mono">OCI 计费 ${p.oci_multiplier}×</div>
            </div>
          </label>
        `
      )
      .join("");
  }

  $("form-add-device").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    const inlineErr = $("add-form-error");
    inlineErr.hidden = true;

    if (!form.checkValidity()) {
      const firstInvalid = form.querySelector(":invalid");
      firstInvalid?.focus();
      return;
    }

    const checked = Array.from(form.querySelectorAll('input[name="protocols"]:checked')).map(
      (el) => el.value
    );
    if (!checked.length) {
      inlineErr.textContent = "请至少选择一个开通协议。";
      inlineErr.hidden = false;
      return;
    }

    const payload = {
      name: $("input-device-name").value.trim(),
      note: $("input-device-note").value.trim(),
      protocols: checked,
      vless_uuid: $("input-vless-uuid").value.trim() || null,
      hy2_password: $("input-hy2-password").value.trim() || null,
    };

    const submitBtn = $("btn-submit-add");
    submitBtn.disabled = true;
    submitBtn.textContent = "创建中...";
    try {
      const res = await apiFetch("/api/devices", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      await closeTopLayerDialog($("modal-add"));
      await Promise.all([refreshDashboard(), refreshAudit()]);
      if (res.share) {
        openQrModal(res.share);
      } else {
        showToast(`设备 ${res.device.name} 已创建`);
      }
    } catch (err) {
      inlineErr.textContent = `创建失败: ${err.message}`;
      inlineErr.hidden = false;
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = "创建并生成二维码";
    }
  });

  // --- QR Code & Share Links Modal (with accessible tabs + hidden="until-found") ---

  function openQrModal(bundle) {
    state.qrBundle = bundle;
    $("modal-qr-title").textContent = `设备连接：${bundle.device.name}`;
    $("modal-qr-subtitle").textContent = bundle.device.note || "扫码或复制链接导入客户端";

    const tabsHost = $("qr-tabs");
    const panelsHost = $("qr-panels");
    const devEncoded = encodeURIComponent(bundle.device.name);

    const tabsHtml = [
      ...bundle.links.map(
        (item, idx) => `
          <button
            type="button"
            id="qr-tab-${idx}"
            class="qr-tab"
            role="tab"
            data-panel-idx="${idx}"
            aria-selected="${idx === 0 ? "true" : "false"}"
            ${idx === 0 ? "focusgroupstart" : ""}
          >${escapeHtml(item.label)}</button>
        `
      ),
      `
        <button
          type="button"
          id="qr-tab-json"
          class="qr-tab"
          role="tab"
          data-panel-idx="${bundle.links.length}"
          aria-selected="${bundle.links.length === 0 ? "true" : "false"}"
          ${bundle.links.length === 0 ? "focusgroupstart" : ""}
        >sing-box JSON</button>
      `,
    ].join("");

    const panelsHtml = [
      ...bundle.links.map((link, idx) => {
        const protoEncoded = encodeURIComponent(link.protocol_key);
        const secretId = `qr-secret-${idx}`;
        const uriId = `qr-uri-${idx}`;
        const masked = `${link.credential_preview.slice(0, 6)}••••••••••••${link.credential_preview.slice(-4)}`;
        const warningsHtml = (link.warnings || [])
          .map((w) => `<div class="alert-banner" style="margin-block-end:10px">${escapeHtml(w)}</div>`)
          .join("");

        return `
          <div
            class="qr-tabpanel"
            role="tabpanel"
            aria-labelledby="qr-tab-${idx}"
            data-panel-idx="${idx}"
            ${idx === 0 ? "" : 'hidden="until-found"'}
          >
            ${warningsHtml}
            <div class="qr-layout">
              <div class="qr-col">
                <div class="qr-frame">
                  <img src="${link.qr_svg_data_uri}" alt="${escapeHtml(link.label)} 客户端配置二维码" />
                </div>
                <div class="qr-dl-row">
                  <a class="btn btn-secondary btn-sm" href="/api/devices/${devEncoded}/qrcode?protocol=${protoEncoded}&format=png" download>下载 PNG</a>
                  <a class="btn btn-secondary btn-sm" href="/api/devices/${devEncoded}/qrcode?protocol=${protoEncoded}&format=svg" download>下载 SVG</a>
                </div>
              </div>

              <div class="qr-meta-list">
                <div style="display:flex;gap:6px;flex-wrap:wrap">
                  <span class="pill pill-proto">${escapeHtml(link.security)}</span>
                  <span class="pill pill-muted mono">${escapeHtml(link.server)}</span>
                  ${link.sni ? `<span class="pill pill-muted mono">SNI: ${escapeHtml(link.sni)}</span>` : ""}
                </div>

                <div>
                  <div class="meta-text" style="margin-block-end:4px">
                    密钥凭据
                    <button
                      type="button"
                      class="link-btn"
                      style="margin-inline-start:8px"
                      data-toggle-secret="${secretId}"
                      aria-expanded="false"
                    >显示明文</button>
                  </div>
                  <div
                    id="${secretId}"
                    class="uri-box"
                    style="max-block-size:none"
                    data-masked="${escapeHtml(masked)}"
                    data-copy-value="${escapeHtml(link.credential_preview)}"
                  >${escapeHtml(masked)}</div>
                </div>

                <div>
                  <div class="meta-text" style="margin-block-end:4px">通用导入链接（Shadowrocket / v2rayN / Nekobox / sing-box）</div>
                  <div id="${uriId}" class="uri-box">${escapeHtml(link.uri)}</div>
                </div>

                <div class="qr-action-row">
                  <span class="copy-control">
                    <button
                      type="button"
                      class="btn btn-primary copy-button"
                      command="--copy"
                      commandfor="${uriId}"
                    >
                      <span class="copy-icon" aria-hidden="true"></span>
                      <span>复制分享链接</span>
                    </button>
                    <span class="copy-tip" role="status"></span>
                  </span>
                  <span class="copy-control">
                    <button
                      type="button"
                      class="btn btn-secondary copy-button"
                      command="--copy"
                      commandfor="${secretId}"
                    >
                      <span class="copy-icon" aria-hidden="true"></span>
                      <span>复制密钥</span>
                    </button>
                    <span class="copy-tip" role="status"></span>
                  </span>
                </div>
              </div>
            </div>
          </div>
        `;
      }),
      `
        <div
          class="qr-tabpanel"
          role="tabpanel"
          aria-labelledby="qr-tab-json"
          data-panel-idx="${bundle.links.length}"
          ${bundle.links.length === 0 ? "" : 'hidden="until-found"'}
        >
          <pre id="qr-sb-json" class="json-box">${escapeHtml(bundle.singbox_client_json)}</pre>
          <span class="copy-control">
            <button type="button" class="btn btn-primary copy-button" command="--copy" commandfor="qr-sb-json">
              <span class="copy-icon" aria-hidden="true"></span>
              <span>复制 JSON 配置</span>
            </button>
            <span class="copy-tip" role="status"></span>
          </span>
        </div>
      `,
    ].join("");

    tabsHost.innerHTML = tabsHtml;
    panelsHost.innerHTML = panelsHtml;

    const selectQrPanel = (idx) => {
      for (const panel of panelsHost.querySelectorAll('[role="tabpanel"]')) {
        const match = Number(panel.dataset.panelIdx) === idx;
        panel.hidden = match ? false : "until-found";
      }
    };

    const firstTab = tabsHost.querySelector('[role="tab"]');
    if (firstTab) syncTabSelection(tabsHost, firstTab);

    // Sync find-in-page reveal via beforematch
    panelsHost.querySelectorAll('[role="tabpanel"]').forEach((panel) => {
      panel.addEventListener("beforematch", () => {
        const idx = Number(panel.dataset.panelIdx);
        const tab = tabsHost.querySelector(`[data-panel-idx="${idx}"]`);
        if (tab) {
          syncTabSelection(tabsHost, tab);
          selectQrPanel(idx);
        }
      });
    });

    const qrDialog = $("modal-qr");
    if (!qrDialog.open) qrDialog.showModal();
  }

  // Delegated QR tabs & secret visibility toggle
  wireTablistKeyboard($("qr-tabs"), (tab) => {
    const idx = Number(tab.dataset.panelIdx);
    for (const panel of $("qr-panels").querySelectorAll('[role="tabpanel"]')) {
      panel.hidden = Number(panel.dataset.panelIdx) === idx ? false : "until-found";
    }
  });

  $("qr-panels").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-toggle-secret]");
    if (!btn) return;
    const box = document.getElementById(btn.dataset.toggleSecret);
    if (!box) return;
    const expanded = btn.getAttribute("aria-expanded") === "true";
    btn.setAttribute("aria-expanded", String(!expanded));
    btn.textContent = expanded ? "显示明文" : "隐藏明文";
    box.textContent = expanded ? box.dataset.masked : box.dataset.copyValue;
  });

  // --- Global Controls & Table Row Actions ---

  // Range tabs fetch on demand -> selection by activation (click/Enter/Space) to avoid redundant fetches on arrowing
  wireTablistKeyboard(
    $("range-tablist"),
    (tab) => {
      const nextRange = tab.dataset.range;
      if (!nextRange || nextRange === state.range) return;
      state.range = nextRange;
      refreshDashboard();
    },
    { followFocus: false }
  );

  // Donut mode toggle is instant -> selection follows focus
  wireTablistKeyboard(
    $("donut-mode-toggle"),
    (tab) => {
      state.donutMode = tab.dataset.mode;
      if (state.dashboard) renderProtocolDonut(state.dashboard);
    },
    { followFocus: true }
  );

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

  devicesTbody.addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-action]");
    if (!btn) return;
    const action = btn.dataset.action;
    const name = btn.dataset.name;

    if (action === "qr") {
      try {
        const bundle = await apiFetch(`/api/devices/${encodeURIComponent(name)}/share`);
        openQrModal(bundle);
      } catch (err) {
        showToast(`获取二维码失败: ${err.message}`, "error");
      }
      return;
    }

    if (action === "revoke") {
      state.pendingRevoke = name;
      $("revoke-target-name").textContent = name;
      $("revoke-error").hidden = true;
      $("modal-revoke").showModal();
      return;
    }

    if (action === "restore") {
      btn.disabled = true;
      try {
        const res = await apiFetch(`/api/devices/${encodeURIComponent(name)}/restore`, {
          method: "POST",
          body: JSON.stringify({ rotate_credentials: true }),
        });
        await Promise.all([refreshDashboard(), refreshAudit()]);
        if (res.share) {
          openQrModal(res.share);
        } else {
          showToast(`已恢复设备 ${name} 并签发新密钥`);
        }
      } catch (err) {
        showToast(`恢复失败: ${err.message}`, "error");
      } finally {
        btn.disabled = false;
      }
      return;
    }

    if (action === "delete") {
      state.pendingDelete = name;
      $("delete-target-name").textContent = name;
      $("delete-error").hidden = true;
      $("modal-delete").showModal();
    }
  });

  $("btn-confirm-revoke").addEventListener("click", async () => {
    const target = state.pendingRevoke;
    if (!target) return;
    const btn = $("btn-confirm-revoke");
    const errBox = $("revoke-error");
    errBox.hidden = true;
    btn.disabled = true;
    btn.textContent = "吊销中...";
    try {
      await apiFetch(`/api/devices/${encodeURIComponent(target)}/revoke`, {
        method: "POST",
      });
      await closeTopLayerDialog($("modal-revoke"));
      showToast(`已吊销 ${target}`);
      await Promise.all([refreshDashboard(), refreshAudit()]);
    } catch (err) {
      errBox.textContent = `吊销失败: ${err.message}`;
      errBox.hidden = false;
    } finally {
      btn.disabled = false;
      btn.textContent = "确认吊销";
      state.pendingRevoke = null;
    }
  });

  $("btn-confirm-delete").addEventListener("click", async () => {
    const target = state.pendingDelete;
    if (!target) return;
    const btn = $("btn-confirm-delete");
    const errBox = $("delete-error");
    errBox.hidden = true;
    btn.disabled = true;
    try {
      await apiFetch(`/api/devices/${encodeURIComponent(target)}`, { method: "DELETE" });
      await closeTopLayerDialog($("modal-delete"));
      showToast(`已删除设备记录 ${target}`);
      await Promise.all([refreshDashboard(), refreshAudit()]);
    } catch (err) {
      errBox.textContent = `删除失败: ${err.message}`;
      errBox.hidden = false;
    } finally {
      btn.disabled = false;
      state.pendingDelete = null;
    }
  });

  // --- Bootstrap ---

  // Dismiss touch trend tooltip on outside tap & re-render crisp SVG on resize/orientation change
  document.addEventListener("click", () => {
    const tip = $("trend-tooltip");
    if (tip) tip.hidden = true;
  });

  let trendResizeRaf = 0;
  function scheduleTrendResize() {
    if (trendResizeRaf) return;
    trendResizeRaf = requestAnimationFrame(() => {
      trendResizeRaf = 0;
      const host = $("trend-chart");
      if (!host || !state.dashboard) return;
      const wrap = host.closest(".trend-chart-wrap") || host;
      const w = Math.max(260, Math.floor(wrap.clientWidth || host.clientWidth || 0));
      const rendered = Number(host.dataset.renderedWidth || 0);
      if (w > 0 && Math.abs(w - rendered) > 6) {
        const tip = $("trend-tooltip");
        if (tip) tip.hidden = true;
        renderTrendChart(state.dashboard);
      }
    });
  }

  if (typeof ResizeObserver === "function") {
    const trendWrap = document.querySelector(".trend-chart-wrap") || $("trend-chart");
    if (trendWrap) {
      const ro = new ResizeObserver(scheduleTrendResize);
      ro.observe(trendWrap);
    }
  }
  window.addEventListener("resize", scheduleTrendResize, { passive: true });

  setupDialogs();
  setupFormValidationSync($("form-add-device"));
  refreshDashboard();
  refreshAudit();

  state.pollTimer = setInterval(() => {
    if (!document.hidden) refreshDashboard();
  }, 15000);
})();
