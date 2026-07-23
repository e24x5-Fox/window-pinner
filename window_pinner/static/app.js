(() => {
  const grid = document.getElementById("windows-grid");
  const windowsEmpty = document.getElementById("windows-empty");
  const groupsList = document.getElementById("groups-list");
  const groupsEmpty = document.getElementById("groups-empty");
  const searchInput = document.getElementById("search");
  const refreshBtn = document.getElementById("refresh-btn");
  const demoBtn = document.getElementById("demo-btn");
  const demoFilterBtn = document.getElementById("demo-filter-btn");
  const brandTrigger = document.getElementById("brand-trigger");
  const selectionBar = document.getElementById("selection-bar");
  const selectionCount = document.getElementById("selection-count");
  const linkBtn = document.getElementById("link-btn");
  const clearSelectionBtn = document.getElementById("clear-selection");
  const engineToggle = document.getElementById("engine-toggle");
  const engineLabel = document.getElementById("engine-label");
  const toasts = document.getElementById("toasts");
  const returnSlider = document.getElementById("return-slider");
  const returnValue = document.getElementById("return-value");

  let windowsCache = [];
  let groupsCache = [];
  let demoOnly = false;
  const selected = new Set();

  function isDemoWindow(w) {
    return w.title.startsWith("ДЕМОокно");
  }

  function showToast(message, isError = false) {
    const el = document.createElement("div");
    el.className = "toast" + (isError ? " error" : "");
    el.textContent = message;
    toasts.appendChild(el);
    setTimeout(() => el.remove(), 3200);
  }

  async function api(path, options) {
    const res = await fetch(path, options);
    if (!res.ok) {
      let msg = "Ошибка запроса";
      try {
        const data = await res.json();
        msg = data.error || msg;
      } catch (e) {}
      throw new Error(msg);
    }
    if (res.status === 204) return null;
    return res.json();
  }

  function checkIcon() {
    return '<svg viewBox="0 0 24 24" fill="none"><path d="M4 12.5 9 18l11-13" stroke="#fff" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  }

  function renderWindows() {
    const query = searchInput.value.trim().toLowerCase();
    const filtered = windowsCache
      .filter((w) => w.title.toLowerCase().includes(query))
      .filter((w) => !demoOnly || isDemoWindow(w));

    grid.innerHTML = "";
    windowsEmpty.classList.toggle("hidden", filtered.length > 0);

    for (const w of filtered) {
      const card = document.createElement("div");
      const isGrouped = w.group_id !== null;
      const isSelected = selected.has(w.hwnd);
      card.className = "card" + (isSelected ? " selected" : "") + (isGrouped ? " grouped" : "");
      if (isGrouped) {
        card.style.borderLeftColor = w.group_color;
        card.title = w.group_locked
          ? "Уже в группе — сначала удали группу, чтобы перевязать это окно"
          : "Группа откреплена — окно можно свободно двигать";
        if (!w.group_locked) card.style.borderLeftStyle = "dashed";
      } else {
        card.title = "Нажми, чтобы выбрать для связывания";
      }

      const titleRow = document.createElement("div");
      titleRow.className = "card-title";
      if (isGrouped) {
        const dot = document.createElement("span");
        dot.className = "card-dot";
        dot.style.background = w.group_color;
        titleRow.appendChild(dot);
      }
      titleRow.appendChild(document.createTextNode(w.title));

      const meta = document.createElement("div");
      meta.className = "card-meta";
      meta.textContent = `${w.class} · pid ${w.pid}`;

      const check = document.createElement("div");
      check.className = "card-check";
      check.innerHTML = checkIcon();

      card.appendChild(titleRow);
      card.appendChild(meta);
      card.appendChild(check);

      if (!isGrouped) {
        card.addEventListener("click", () => {
          if (selected.has(w.hwnd)) selected.delete(w.hwnd);
          else selected.add(w.hwnd);
          renderWindows();
          updateSelectionBar();
        });
      }

      grid.appendChild(card);
    }
  }

  function updateSelectionBar() {
    // Drop selections for windows that no longer exist.
    const liveHwnds = new Set(windowsCache.map((w) => w.hwnd));
    for (const hwnd of Array.from(selected)) {
      if (!liveHwnds.has(hwnd)) selected.delete(hwnd);
    }
    const count = selected.size;
    selectionBar.classList.toggle("hidden", count === 0);
    selectionCount.textContent = `Выбрано: ${count}`;
    linkBtn.disabled = count < 2;
  }

  function renderGroups() {
    groupsList.innerHTML = "";
    groupsEmpty.classList.toggle("hidden", groupsCache.length > 0);

    for (const g of groupsCache) {
      const row = document.createElement("div");
      row.className = "group-row" + (g.locked ? "" : " unlocked");

      const dot = document.createElement("span");
      dot.className = "group-chip-dot";
      dot.style.background = g.color;
      row.appendChild(dot);

      const members = document.createElement("div");
      members.className = "group-members";
      g.members.forEach((m, i) => {
        if (i > 0) {
          const arrow = document.createElement("span");
          arrow.className = "group-link-arrow";
          arrow.textContent = "↔";
          members.appendChild(arrow);
        }
        const chip = document.createElement("span");
        chip.className = "group-member";
        chip.textContent = m.title;
        members.appendChild(chip);
      });
      row.appendChild(members);

      const lockBtn = document.createElement("button");
      lockBtn.className = "btn ghost lock-toggle";
      lockBtn.title = g.locked
        ? "Открепить окна группы — их можно будет свободно передвигать"
        : "Закрепить в текущем положении";
      lockBtn.innerHTML = g.locked
        ? '<svg width="15" height="15" viewBox="0 0 24 24" fill="none"><rect x="5" y="11" width="14" height="9" rx="2" stroke="currentColor" stroke-width="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>'
        : '<svg width="15" height="15" viewBox="0 0 24 24" fill="none"><rect x="5" y="11" width="14" height="9" rx="2" stroke="currentColor" stroke-width="2"/><path d="M8 11V7a4 4 0 0 1 7.5-2" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';
      lockBtn.addEventListener("click", async () => {
        try {
          await api(`/api/groups/${g.id}/${g.locked ? "unlock" : "lock"}`, { method: "POST" });
          showToast(g.locked ? "Окна группы откреплены" : "Положение зафиксировано");
          await loadAll();
        } catch (e) {
          showToast(e.message, true);
        }
      });
      row.appendChild(lockBtn);

      const delBtn = document.createElement("button");
      delBtn.className = "btn danger icon";
      delBtn.title = "Удалить группу";
      delBtn.innerHTML = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none"><path d="M4 7h16M9 7V5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2m2 0-1 13a2 2 0 0 1-2 2H10a2 2 0 0 1-2-2L7 7" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
      delBtn.addEventListener("click", async () => {
        try {
          await api(`/api/groups/${g.id}`, { method: "DELETE" });
          showToast("Группа удалена");
          await loadAll();
        } catch (e) {
          showToast(e.message, true);
        }
      });
      row.appendChild(delBtn);

      groupsList.appendChild(row);
    }
  }

  async function loadWindows() {
    windowsCache = await api("/api/windows");
    renderWindows();
    updateSelectionBar();
  }

  async function loadGroups() {
    groupsCache = await api("/api/groups");
    renderGroups();
  }

  async function loadAll() {
    await Promise.all([loadWindows(), loadGroups()]);
  }

  linkBtn.addEventListener("click", async () => {
    const hwnds = Array.from(selected);
    if (hwnds.length < 2) return;
    try {
      await api("/api/groups", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ hwnds }),
      });
      selected.clear();
      showToast("Окна связаны");
      await loadAll();
      updateSelectionBar();
    } catch (e) {
      showToast(e.message, true);
    }
  });

  clearSelectionBtn.addEventListener("click", () => {
    selected.clear();
    renderWindows();
    updateSelectionBar();
  });

  refreshBtn.addEventListener("click", () => loadAll().catch((e) => showToast(e.message, true)));

  // Hidden dev feature: click the logo 5 times in a row (within 2s of each
  // other) to reveal the demo-window button.
  let brandClicks = 0;
  let brandClickTimer = null;
  brandTrigger.addEventListener("click", () => {
    brandClicks += 1;
    clearTimeout(brandClickTimer);
    brandClickTimer = setTimeout(() => { brandClicks = 0; }, 2000);
    if (brandClicks >= 5) {
      brandClicks = 0;
      demoBtn.classList.remove("hidden");
      demoFilterBtn.classList.remove("hidden");
      showToast("Демо-режим включён");
    }
  });

  demoBtn.addEventListener("click", async () => {
    try {
      await api("/api/demo-windows", { method: "POST" });
      showToast("Демо-окно создано");
      setTimeout(() => loadAll().catch(() => {}), 300);
    } catch (e) {
      showToast(e.message, true);
    }
  });

  demoFilterBtn.addEventListener("click", () => {
    demoOnly = !demoOnly;
    demoFilterBtn.classList.toggle("primary", demoOnly);
    demoFilterBtn.classList.toggle("ghost", !demoOnly);
    demoFilterBtn.textContent = demoOnly ? "Показать все окна" : "Только демо-окна";
    renderWindows();
  });
  searchInput.addEventListener("input", renderWindows);

  engineToggle.addEventListener("change", async () => {
    const enabled = engineToggle.checked;
    try {
      await api("/api/engine", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      });
      engineLabel.textContent = enabled ? "Синхронизация включена" : "Синхронизация выключена";
    } catch (e) {
      engineToggle.checked = !enabled;
      showToast(e.message, true);
    }
  });

  function setSliderLabel(el, ms) {
    el.textContent = ms == 0 ? "выкл." : `${ms} мс`;
  }

  returnSlider.addEventListener("input", () => setSliderLabel(returnValue, returnSlider.value));

  async function applySettings() {
    try {
      await api("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          return_ms: Number(returnSlider.value),
        }),
      });
    } catch (e) {
      showToast(e.message, true);
    }
  }

  returnSlider.addEventListener("change", applySettings);

  async function init() {
    try {
      const engine = await api("/api/engine");
      engineToggle.checked = engine.enabled;
      engineLabel.textContent = engine.enabled ? "Синхронизация включена" : "Синхронизация выключена";
    } catch (e) {}
    try {
      const settings = await api("/api/settings");
      returnSlider.value = settings.return_ms;
      setSliderLabel(returnValue, settings.return_ms);
    } catch (e) {}
    await loadAll().catch((e) => showToast(e.message, true));
    setInterval(() => loadAll().catch(() => {}), 2500);
  }

  init();
})();
