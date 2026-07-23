(function () {
  "use strict";

  let currentTaskId = null;
  let voiceStatus = "idle";   // "idle" | "recording" | "processing"
  let taskStatus = "idle";    // "idle" | "running"

  const orb = document.getElementById("orb");
  const statusLabel = document.getElementById("statusLabel");
  const composer = document.getElementById("composer");
  const taskInput = document.getElementById("taskInput");
  const runButton = document.getElementById("runButton");
  const logView = document.getElementById("logView");
  const currentTaskLabel = document.getElementById("currentTaskLabel");
  const historyList = document.getElementById("historyList");

  const STATUS_LABELS = {
    idle_idle: "Idle",
    idle_recording: "Listening…",
    idle_processing: "Transcribing…",
    running_idle: "Running task…",
    running_recording: "Running task…",
    running_processing: "Running task…",
  };

  function updateOrb() {
    orb.className = "orb";
    let label;
    if (taskStatus === "running") {
      orb.classList.add("orb-running");
      label = STATUS_LABELS["running_" + voiceStatus] || "Running task…";
    } else if (voiceStatus === "recording") {
      orb.classList.add("orb-recording");
      label = STATUS_LABELS.idle_recording;
    } else if (voiceStatus === "processing") {
      orb.classList.add("orb-processing");
      label = STATUS_LABELS.idle_processing;
    } else {
      orb.classList.add("orb-idle");
      label = STATUS_LABELS.idle_idle;
    }
    statusLabel.textContent = label;
  }

  function setBusy(busy) {
    taskInput.disabled = busy;
    runButton.disabled = busy;
    runButton.textContent = busy ? "Running…" : "Run";
  }

  function clearEmptyState(container) {
    const placeholder = container.querySelector(".empty-state");
    if (placeholder) placeholder.remove();
  }

  function appendLogLine(line) {
    clearEmptyState(logView);
    const el = document.createElement("div");
    el.className = "log-line";
    el.textContent = line;
    logView.appendChild(el);
    logView.scrollTop = logView.scrollHeight;
  }

  function formatTimeAgo(unixSeconds) {
    const diff = Math.max(0, Date.now() / 1000 - unixSeconds);
    if (diff < 60) return "just now";
    if (diff < 3600) return Math.floor(diff / 60) + "m ago";
    if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
    return Math.floor(diff / 86400) + "d ago";
  }

  function renderHistory(entries) {
    historyList.innerHTML = "";
    if (!entries || entries.length === 0) {
      historyList.innerHTML = '<p class="empty-state">Nothing yet.</p>';
      return;
    }

    entries.forEach(function (entry) {
      const item = document.createElement("div");
      item.className = "history-item" + (entry.success ? "" : " history-item-failed");

      const badge = document.createElement("span");
      badge.className = "history-badge";
      badge.textContent = entry.success ? "\u2713" : "\u2715";

      const body = document.createElement("div");
      body.className = "history-body";

      const text = document.createElement("div");
      text.className = "history-text";
      text.textContent = entry.text;
      body.appendChild(text);

      const meta = document.createElement("div");
      meta.className = "history-meta";
      const sourceLabel = entry.source === "voice" ? "voice" : "text";
      const seconds = (entry.elapsed_ms / 1000).toFixed(1);
      meta.textContent = formatTimeAgo(entry.timestamp) + " \u00b7 " + seconds + "s \u00b7 " + sourceLabel;
      body.appendChild(meta);

      if (entry.result) {
        const resultEl = document.createElement("div");
        resultEl.className = "history-result";
        resultEl.textContent = entry.result;
        body.appendChild(resultEl);
      }

      item.appendChild(badge);
      item.appendChild(body);
      item.addEventListener("click", function () {
        item.classList.toggle("history-item-expanded");
      });

      historyList.appendChild(item);
    });
  }

  function refreshHistory() {
    if (!window.pywebview) return;
    window.pywebview.api.get_history().then(renderHistory).catch(function (err) {
      console.error("get_history failed", err);
    });
  }

  // -- events pushed from Python via window.evaluate_js --
  window.onAgentEvent = function (event) {
    switch (event.type) {
      case "status":
        taskStatus = event.status;
        updateOrb();
        setBusy(event.status === "running");
        break;

      case "voice_status":
        voiceStatus = event.status;
        updateOrb();
        break;

      case "task_start":
        currentTaskId = event.task_id;
        currentTaskLabel.textContent = event.text;
        logView.innerHTML = "";
        break;

      case "log":
        if (event.task_id === currentTaskId) {
          appendLogLine(event.line);
        }
        break;

      case "task_complete":
        refreshHistory();
        break;

      default:
        // Unknown event types are ignored rather than throwing —
        // forward compatibility if a future phase adds new event
        // types this build of the frontend doesn't know about yet.
        break;
    }
  };

  composer.addEventListener("submit", function (e) {
    e.preventDefault();
    const text = taskInput.value.trim();
    if (!text || !window.pywebview) return;

    window.pywebview.api
      .run_task({ text: text })
      .then(function (res) {
        if (!res.accepted) {
          appendLogLine("\u26a0 " + res.reason);
          return;
        }
        taskInput.value = "";
      })
      .catch(function (err) {
        console.error("run_task failed", err);
      });
  });

  window.addEventListener("pywebviewready", function () {
    refreshHistory();
    window.pywebview.api.is_busy().then(function (busy) {
      setBusy(busy);
    });
  });

  updateOrb();
})();