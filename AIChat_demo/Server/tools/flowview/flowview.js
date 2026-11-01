(() => {
  "use strict";

  const MAX_EVENTS = 5000;
  const MAX_ROWS = 3000;
  const POLL_MS = 800;

  const state = {
    mode: "terminal",
    events: [],
    seen: new Set(),
    lastId: null,
    paused: false,
    follow: true,
    pending: [],
    selectedTrace: null,
    polling: false,
  };

  const el = {
    terminalMode: document.getElementById("terminalMode"),
    flowMode: document.getElementById("flowMode"),
    terminalToolbar: document.getElementById("terminalToolbar"),
    flowToolbar: document.getElementById("flowToolbar"),
    terminalView: document.getElementById("terminalView"),
    flowView: document.getElementById("flowView"),
    logViewport: document.getElementById("logViewport"),
    flowLogViewport: document.getElementById("flowLogViewport"),
    terminalEmpty: document.getElementById("terminalEmpty"),
    flowEmpty: document.getElementById("flowEmpty"),
    searchInput: document.getElementById("searchInput"),
    sourceFilter: document.getElementById("sourceFilter"),
    statusFilter: document.getElementById("statusFilter"),
    traceFilter: document.getElementById("traceFilter"),
    notifyFilter: document.getElementById("notifyFilter"),
    followButton: document.getElementById("followButton"),
    pauseButton: document.getElementById("pauseButton"),
    traceSelect: document.getElementById("traceSelect"),
    latestTraceButton: document.getElementById("latestTraceButton"),
    flowMeta: document.getElementById("flowMeta"),
    flowKind: document.getElementById("flowKind"),
    flowTraceId: document.getElementById("flowTraceId"),
    flowDuration: document.getElementById("flowDuration"),
    flowEventCount: document.getElementById("flowEventCount"),
    flowStatus: document.getElementById("flowStatus"),
    stageRail: document.getElementById("stageRail"),
    clearTraceButton: document.getElementById("clearTraceButton"),
    connectionDot: document.getElementById("connectionDot"),
    connectionText: document.getElementById("connectionText"),
    lastUpdate: document.getElementById("lastUpdate"),
    eventCount: document.getElementById("eventCount"),
    bufferStatus: document.getElementById("bufferStatus"),
    viewerHealth: document.getElementById("viewerHealth"),
  };

  const escapeHtml = (value) => String(value ?? "").replace(
    /[&<>"']/g,
    (character) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[character]),
  );

  const levelOf = (status) => {
    const value = String(status || "").toLowerCase();
    if (value === "error" || value === "failed") return "error";
    if (value === "warn" || value === "timeout") return "warn";
    if (["ok", "complete", "played", "done"].includes(value)) return "ok";
    return "info";
  };

  const levelText = (status) => {
    const level = levelOf(status);
    return {
      error: "ERROR",
      warn: "WARN",
      ok: "OK",
      info: "INFO",
    }[level];
  };

  const formatClock = (ts) => {
    if (!ts) return "--:--:--.---";
    const match = String(ts).match(/T(\d{2}:\d{2}:\d{2}\.\d{3})/);
    return match ? match[1] : String(ts);
  };

  const formatDuration = (milliseconds) => {
    if (milliseconds === null || milliseconds === undefined) return "-";
    const value = Number(milliseconds);
    if (!Number.isFinite(value)) return "-";
    if (value < 1000) return `${Math.round(value)} ms`;
    if (value < 60_000) return `${(value / 1000).toFixed(1)} s`;
    return `${(value / 60_000).toFixed(1)} min`;
  };

  const timestampMs = (event) => {
    const value = Date.parse(event?.ts || "");
    return Number.isFinite(value) ? value : 0;
  };

  const eventMessage = (event) => {
    if (event.message) return String(event.message);
    if (event.duration_ms !== null && event.duration_ms !== undefined) {
      return formatDuration(event.duration_ms);
    }
    return "";
  };

  const eventState = (event) => {
    if (event.component !== "client" || event.step !== "state") return "";
    const match = String(event.message || "").match(/state=([a-z_]+)/i);
    return match ? match[1].toLowerCase() : "";
  };

  const traceKind = (traceId, events) => {
    if (String(traceId).startsWith("notify-")) return "notify";
    if (events.some(
      (event) => event.notify_id !== null && event.notify_id !== undefined,
    )) return "notify";
    return "voice";
  };

  const traceLabel = (traceId, kind) => {
    const prefix = kind === "notify" ? "提醒" : "语音";
    return `${prefix}  ${traceId}`;
  };

  const matchesFilters = (event) => {
    const source = el.sourceFilter.value;
    const status = el.statusFilter.value;
    const trace = el.traceFilter.value.trim().toLowerCase();
    const notify = el.notifyFilter.value.trim();
    const search = el.searchInput.value.trim().toLowerCase();

    if (source && event.component !== source) return false;
    if (status && levelOf(event.status) !== status) return false;
    if (trace && !String(event.trace_id || "").toLowerCase().includes(trace)) {
      return false;
    }
    if (notify && String(event.notify_id ?? "") !== notify) return false;
    if (search) {
      const haystack = [
        event.component,
        event.step,
        event.status,
        event.message,
        event.trace_id,
        event.notify_id,
      ].join(" ").toLowerCase();
      if (!haystack.includes(search)) return false;
    }
    return true;
  };

  const isNearBottom = (viewport) => (
    viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight < 36
  );

  const updateFollowState = (enabled) => {
    state.follow = enabled;
    el.followButton.classList.toggle("is-active", enabled);
    el.followButton.setAttribute("aria-pressed", String(enabled));
    if (enabled) {
      el.logViewport.scrollTop = el.logViewport.scrollHeight;
    }
  };

  const setConnection = (mode, text) => {
    el.connectionDot.classList.toggle("is-live", mode === "live");
    el.connectionDot.classList.toggle("is-error", mode === "error");
    el.connectionText.textContent = text;
  };

  const setLastUpdate = () => {
    const now = new Date();
    el.lastUpdate.textContent = now.toLocaleTimeString("zh-CN", {
      hour12: false,
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  };

  const createLogRow = (event) => {
    const row = document.createElement("div");
    const level = levelOf(event.status);
    const traceId = event.trace_id ? String(event.trace_id) : "";
    row.className = "log-row";
    row.tabIndex = 0;
    row.dataset.eventId = event.id || "";
    row.innerHTML = `
      <span class="log-time" title="${escapeHtml(event.ts || "")}">${escapeHtml(formatClock(event.ts))}</span>
      <span class="log-level is-${level}">${levelText(event.status)}</span>
      <span class="log-source">${escapeHtml(event.component || "-")}</span>
      <span class="log-step">${escapeHtml(event.step || "-")}</span>
      <span class="log-message">${escapeHtml(eventMessage(event) || "-")}</span>
      <span class="log-trace">${traceId ? `<button class="trace-link" type="button" title="${escapeHtml(traceId)}" data-trace="${escapeHtml(traceId)}">${escapeHtml(traceId)}</button>` : ""}</span>
      <pre class="log-detail" hidden>${escapeHtml(JSON.stringify(event, null, 2))}</pre>
    `;

    const toggleDetail = () => {
      const detail = row.querySelector(".log-detail");
      detail.hidden = !detail.hidden;
    };
    row.addEventListener("click", (eventClick) => {
      if (eventClick.target.closest(".trace-link")) return;
      toggleDetail();
    });
    row.addEventListener("keydown", (keyboardEvent) => {
      if (keyboardEvent.key === "Enter" || keyboardEvent.key === " ") {
        keyboardEvent.preventDefault();
        toggleDetail();
      }
    });
    row.querySelector(".trace-link")?.addEventListener("click", (traceClick) => {
      traceClick.stopPropagation();
      selectTrace(traceId);
      setMode("flow");
    });
    return row;
  };

  const renderRows = (viewport, events, emptyElement, emptyText) => {
    viewport.querySelectorAll(".log-row").forEach((row) => row.remove());
    if (!events.length) {
      emptyElement.hidden = false;
      emptyElement.textContent = emptyText;
      return;
    }
    emptyElement.hidden = true;
    const fragment = document.createDocumentFragment();
    events.slice(-MAX_ROWS).forEach((event) => fragment.append(createLogRow(event)));
    viewport.append(fragment);
  };

  const appendRows = (events) => {
    if (!events.length) return;
    el.terminalEmpty.hidden = true;
    const shouldFollow = state.follow && isNearBottom(el.logViewport);
    const fragment = document.createDocumentFragment();
    events.forEach((event) => fragment.append(createLogRow(event)));
    el.logViewport.append(fragment);
    const rows = el.logViewport.querySelectorAll(".log-row");
    for (let index = 0; index < rows.length - MAX_ROWS; index += 1) {
      rows[index].remove();
    }
    if (shouldFollow) {
      el.logViewport.scrollTop = el.logViewport.scrollHeight;
    }
  };

  const renderTerminal = () => {
    const filtered = state.events.filter(matchesFilters);
    renderRows(el.logViewport, filtered, el.terminalEmpty, "没有匹配的日志");
    if (state.follow) el.logViewport.scrollTop = el.logViewport.scrollHeight;
    updateCounters();
  };

  const updateCounters = () => {
    el.eventCount.textContent = `${state.events.length} 条`;
    const pending = state.pending.length;
    el.bufferStatus.textContent = pending ? `${pending} 条待显示` : "";
    el.pauseButton.textContent = state.paused
      ? `继续${pending ? ` (${pending})` : ""}`
      : "暂停";
    el.pauseButton.classList.toggle("is-active", state.paused);
    el.pauseButton.setAttribute("aria-pressed", String(state.paused));
  };

  const mergeEvents = (incoming) => {
    const added = [];
    incoming.forEach((event) => {
      if (!event?.id || state.seen.has(event.id)) return;
      state.seen.add(event.id);
      state.events.push(event);
      added.push(event);
      state.lastId = event.id;
    });
    if (state.events.length > MAX_EVENTS) {
      const removed = state.events.splice(0, state.events.length - MAX_EVENTS);
      removed.forEach((event) => state.seen.delete(event.id));
    }
    return added;
  };

  const traceGroups = () => {
    const groups = new Map();
    state.events.forEach((event) => {
      if (!event.trace_id) return;
      const traceId = String(event.trace_id);
      if (!groups.has(traceId)) groups.set(traceId, []);
      groups.get(traceId).push(event);
    });
    return [...groups.entries()]
      .map(([traceId, events]) => {
        const sorted = [...events].sort((a, b) => timestampMs(a) - timestampMs(b));
        return {
          traceId,
          events: sorted,
          kind: traceKind(traceId, sorted),
          latest: timestampMs(sorted[sorted.length - 1]),
        };
      })
      .sort((a, b) => b.latest - a.latest);
  };

  const renderTraceOptions = (groups) => {
    const current = state.selectedTrace;
    el.traceSelect.innerHTML = "";
    groups.forEach((group) => {
      const option = document.createElement("option");
      option.value = group.traceId;
      option.textContent = traceLabel(group.traceId, group.kind);
      el.traceSelect.append(option);
    });
    if (!groups.length) {
      state.selectedTrace = null;
      return;
    }
    if (current && groups.some((group) => group.traceId === current)) {
      el.traceSelect.value = current;
    } else {
      state.selectedTrace = groups[0].traceId;
      el.traceSelect.value = state.selectedTrace;
    }
  };

  const selectTrace = (traceId) => {
    state.selectedTrace = traceId;
    const exists = [...el.traceSelect.options].some(
      (option) => option.value === traceId,
    );
    if (exists) el.traceSelect.value = traceId;
  };

  const voiceStages = (events) => {
    const clientEvents = events.filter(
      (event) => event.component === "client" && event.step === "state",
    );
    const speakingEvents = clientEvents.filter(
      (event) => eventState(event) === "speaking",
    );
    const listeningEvents = clientEvents.filter(
      (event) => eventState(event) === "listening",
    );
    const thinkingEvents = clientEvents.filter(
      (event) => eventState(event) === "thinking",
    );
    const idleEvents = clientEvents.filter(
      (event) => eventState(event) === "idle",
    );
    const ttsStartIndex = events.findIndex(
      (event) => event.component === "tts" && event.step === "start",
    );
    const playbackEvents = speakingEvents.filter(
      (event) => ttsStartIndex >= 0 && events.indexOf(event) > ttsStartIndex,
    );
    return [
      { name: "唤醒", events: speakingEvents.slice(0, 1) },
      { name: "录音", events: listeningEvents.slice(0, 1) },
      { name: "识别", events: events.filter((event) => event.component === "asr") },
      { name: "思考", events: thinkingEvents.slice(0, 1) },
      {
        name: "生成",
        events: events.filter(
          (event) => event.component === "llm"
            && ["start", "reply_ready", "turn_complete"].includes(event.step),
        ),
      },
      { name: "合成", events: events.filter((event) => event.component === "tts") },
      { name: "播放", events: playbackEvents.slice(0, 1) },
      { name: "完成", events: idleEvents.slice(-1) },
    ];
  };

  const notifyStages = (events) => [
    {
      name: "调度",
      events: events.filter(
        (event) => event.component === "notify"
          && ["claimed", "sent"].includes(event.step),
      ),
    },
    {
      name: "合成",
      events: events.filter((event) => event.component === "tts"),
    },
    {
      name: "发送",
      events: events.filter(
        (event) => event.component === "notify" && event.step === "playback_sent",
      ),
    },
    {
      name: "确认",
      events: events.filter((event) => event.component === "ack"),
    },
  ];

  const stageStatus = (events) => {
    if (!events.length) return "pending";
    if (events.some((event) => levelOf(event.status) === "error")) return "error";
    if (events.some((event) => levelOf(event.status) === "warn")) return "warn";
    if (events.some((event) => levelOf(event.status) === "ok")) return "ok";
    return "info";
  };

  const stageDuration = (events) => {
    if (!events.length) return null;
    const explicit = events.find(
      (event) => event.duration_ms !== null && event.duration_ms !== undefined,
    );
    if (explicit) return Number(explicit.duration_ms);
    const start = timestampMs(events[0]);
    const end = timestampMs(events[events.length - 1]);
    return end > start ? end - start : null;
  };

  const traceStatus = (events) => {
    if (events.some((event) => levelOf(event.status) === "error")) return "error";
    if (events.some((event) => levelOf(event.status) === "warn")) return "warn";
    const last = events[events.length - 1];
    if (last?.component === "ack" && levelOf(last.status) === "ok") return "ok";
    if (last?.component === "client" && eventState(last) === "idle") return "ok";
    if (events.some(
      (event) => event.component === "notify" && event.step === "playback_sent",
    )) return "ok";
    return "info";
  };

  const renderStages = (stages) => {
    el.stageRail.innerHTML = stages.map((stage) => {
      const status = stageStatus(stage.events);
      const duration = stageDuration(stage.events);
      const note = stage.events.length ? `${stage.events.length} 条` : "未收到";
      return `
        <div class="stage is-${status}">
          <span class="stage-name">${escapeHtml(stage.name)}</span>
          <span class="stage-value">${duration === null ? "-" : escapeHtml(formatDuration(duration))}</span>
          <span class="stage-note">${escapeHtml(note)}</span>
        </div>
      `;
    }).join("");
  };

  const renderFlow = () => {
    const groups = traceGroups();
    renderTraceOptions(groups);
    const group = groups.find((item) => item.traceId === state.selectedTrace);
    if (!group) {
      el.flowKind.textContent = "流程";
      el.flowTraceId.textContent = "未选择";
      el.flowDuration.textContent = "-";
      el.flowEventCount.textContent = "0 条事件";
      el.flowStatus.textContent = "等待";
      el.flowStatus.className = "summary-status";
      el.stageRail.innerHTML = "";
      el.flowMeta.textContent = "";
      renderRows(el.flowLogViewport, [], el.flowEmpty, "暂无可聚合流程");
      return;
    }

    const events = group.events;
    const start = timestampMs(events[0]);
    const end = timestampMs(events[events.length - 1]);
    const duration = end > start ? end - start : null;
    const status = traceStatus(events);
    const stages = group.kind === "notify"
      ? notifyStages(events)
      : voiceStages(events);

    el.flowKind.textContent = group.kind === "notify" ? "提醒流程" : "语音流程";
    el.flowTraceId.textContent = group.traceId;
    el.flowDuration.textContent = duration === null ? "-" : formatDuration(duration);
    el.flowEventCount.textContent = `${events.length} 条事件`;
    el.flowStatus.textContent = status === "ok"
      ? "已完成"
      : status === "error"
        ? "失败"
        : status === "warn"
          ? "需检查"
          : "进行中";
    el.flowStatus.className = `summary-status is-${status}`;
    el.flowMeta.textContent = formatClock(events[events.length - 1]?.ts);
    renderStages(stages);
    renderRows(el.flowLogViewport, events, el.flowEmpty, "该流程暂无事件");
    updateCounters();
  };

  const setMode = (mode) => {
    state.mode = mode;
    const terminal = mode === "terminal";
    el.terminalMode.classList.toggle("is-active", terminal);
    el.flowMode.classList.toggle("is-active", !terminal);
    el.terminalMode.setAttribute("aria-selected", String(terminal));
    el.flowMode.setAttribute("aria-selected", String(!terminal));
    el.terminalToolbar.hidden = !terminal;
    el.flowToolbar.hidden = terminal;
    el.terminalView.hidden = !terminal;
    el.flowView.hidden = terminal;
    if (terminal) {
      renderTerminal();
    } else {
      renderFlow();
    }
  };

  const fetchJson = async (url) => {
    const response = await fetch(url, {
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  };

  const refreshHealth = async () => {
    try {
      const health = await fetchJson("/healthz");
      el.viewerHealth.textContent = health.ok ? "日志在线" : "日志不可写";
      el.viewerHealth.classList.toggle("is-error-text", !health.ok);
    } catch (error) {
      el.viewerHealth.textContent = "查看器断开";
      el.viewerHealth.classList.add("is-error-text");
    }
  };

  const acceptEvents = (events) => {
    const added = mergeEvents(events);
    if (!added.length) return;
    if (state.mode === "flow") {
      renderFlow();
      return;
    }
    if (state.paused) {
      state.pending.push(...added);
      updateCounters();
      return;
    }
    appendRows(added.filter(matchesFilters));
    updateCounters();
  };

  const poll = async () => {
    if (state.polling) return;
    state.polling = true;
    try {
      let url = "/api/events?limit=1200";
      if (state.lastId) {
        url = `/api/events?limit=1000&after_id=${encodeURIComponent(state.lastId)}`;
      }
      const payload = await fetchJson(url);
      setConnection("live", "实时");
      setLastUpdate();
      acceptEvents(Array.isArray(payload.events) ? payload.events : []);
    } catch (error) {
      setConnection("error", "连接中断");
    } finally {
      state.polling = false;
    }
  };

  const schedulePoll = async () => {
    await poll();
    window.setTimeout(schedulePoll, POLL_MS);
  };

  let filterTimer = null;
  const scheduleFilterRender = () => {
    window.clearTimeout(filterTimer);
    filterTimer = window.setTimeout(() => {
      if (state.mode === "terminal") renderTerminal();
    }, 120);
  };

  el.terminalMode.addEventListener("click", () => setMode("terminal"));
  el.flowMode.addEventListener("click", () => setMode("flow"));
  el.searchInput.addEventListener("input", scheduleFilterRender);
  el.sourceFilter.addEventListener("change", scheduleFilterRender);
  el.statusFilter.addEventListener("change", scheduleFilterRender);
  el.traceFilter.addEventListener("input", scheduleFilterRender);
  el.notifyFilter.addEventListener("input", scheduleFilterRender);

  el.followButton.addEventListener("click", () => updateFollowState(!state.follow));
  el.logViewport.addEventListener("scroll", () => {
    if (!state.follow) return;
    const nearBottom = isNearBottom(el.logViewport);
    if (!nearBottom) updateFollowState(false);
  });
  el.pauseButton.addEventListener("click", () => {
    state.paused = !state.paused;
    if (!state.paused && state.pending.length) {
      const pending = state.pending.splice(0);
      appendRows(pending.filter(matchesFilters));
      if (state.follow) el.logViewport.scrollTop = el.logViewport.scrollHeight;
    }
    updateCounters();
  });

  el.traceSelect.addEventListener("change", () => {
    state.selectedTrace = el.traceSelect.value;
    renderFlow();
  });
  el.latestTraceButton.addEventListener("click", () => {
    const groups = traceGroups();
    if (groups.length) {
      selectTrace(groups[0].traceId);
      renderFlow();
    }
  });
  el.clearTraceButton.addEventListener("click", () => {
    const groups = traceGroups();
    if (groups.length) {
      selectTrace(groups[0].traceId);
      renderFlow();
    }
  });

  updateFollowState(true);
  renderTerminal();
  refreshHealth();
  window.setInterval(refreshHealth, 10_000);
  schedulePoll();
})();
