"""
Phase 10 — Native Desktop App.

A real installed-feeling Windows window (pywebview, rendered via
Windows' built-in Edge WebView2), not a browser tab and not a separate
local website — the UI and the agent run in the same Python process,
talking to each other through pywebview's direct JS<->Python bridge.
No HTTP server anywhere, local or otherwise.

Architecture:
  AgentRunner   — owns task execution, live progress streaming, and a
                  small persisted history. Knows nothing about
                  pywebview specifically (it only needs a push_update
                  callback) so it's testable without a real window.
  _LiveLineForwarder — captures a running task's print() output and
                  forwards it line-by-line in real time, by plugging
                  into graph.py's EXISTING thread-aware stdout
                  machinery (built for Phase 6's parallel supervisor)
                  rather than building a second interception mechanism.
  Api           — thin pywebview-facing wrapper exposing methods to
                  JS as window.pywebview.api.<name>(...).
  run_desktop_app() — the entry point main.py calls when
                  GUI_ENABLED=true. Also wires up Phase 8/9's
                  VoiceController (hotkey + wake word) in the
                  background, since the requirement is "say hey jarvis
                  any time, regardless of whether the window has
                  focus" — see start_background_listening() in
                  src/voice.py.

TESTING LIMITATIONS: pywebview fundamentally needs a real Windows
display + WebView2 runtime to render anything. This was built and
reviewed in a sandboxed Linux container with neither. The pywebview API
itself (create_window, js_api, evaluate_js, the window.pywebviewready
JS event) was verified against current documentation, and everything
below it (AgentRunner, the stdout-forwarding hook, history persistence)
was tested directly with fakes standing in for the window — but the
actual rendered window, and any Windows-Chromium-specific rendering
quirks, need your machine to confirm.
"""
import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from src.config import (
    GUI_WINDOW_WIDTH,
    GUI_WINDOW_HEIGHT,
    GUI_HISTORY_PATH,
    GUI_HISTORY_MAX_ENTRIES,
    VOICE_ENABLED,
)

log = logging.getLogger(__name__)


class _LiveLineForwarder:
    """
    A stdout-like object (just needs .write()/.flush()) that captures a
    thread's print() output and forwards each COMPLETE line to a
    callback as soon as it's available.

    This is deliberately different from graph.py's Phase 6
    _ThreadAwareStdout buffering strategy: Phase 6 buffers a whole
    parallel-worker's output and flushes it as ONE block at the very
    end, specifically to avoid interleaving multiple concurrent
    subtasks' output in a single shared terminal. The desktop app has
    no such interleaving risk — each task gets its own dedicated log
    panel in the UI — so real-time, line-by-line streaming is both safe
    and actually what you want for a live progress view.

    Installed into graph.py's EXISTING _stdout_buffer_local (Phase 6
    infrastructure) rather than building a second, separate
    interception mechanism — graph.py's _ThreadAwareStdout already
    routes a thread's print() calls to whatever object sits in that
    thread-local slot, so a compatible .write()/.flush() here is all
    that's needed. Zero changes to graph.py itself.
    """

    def __init__(self, on_line: Callable[[str], None]):
        self._on_line = on_line
        self._buffer = ""

    def write(self, s: str) -> int:
        self._buffer += s
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._on_line(line)
        return len(s)

    def flush(self) -> None:
        pass


class AgentRunner:
    """
    Owns task execution, live progress streaming, and a small persisted
    history. Deliberately knows nothing about pywebview — it only needs
    a push_update(event: dict) callback — so it can be unit-tested
    without any real GUI window, and so the UI layer could be swapped
    later (a different desktop toolkit, a CLI test harness) without
    touching this class.

    Event shapes pushed via push_update:
      {"type": "task_start",    "task_id": str, "text": str, "source": str}
      {"type": "log",           "task_id": str, "line": str}
      {"type": "task_complete", "task_id": str, "text": str, "source": str,
                                 "success": bool, "result": str,
                                 "elapsed_ms": int, "timestamp": float}
      {"type": "status",        "status": "idle" | "running"}
    """

    def __init__(self, push_update: Callable[[dict], None]):
        self._push_update = push_update
        self._lock = threading.Lock()
        self._task_running = False
        self._history: list[dict] = self._load_history()

    @property
    def is_busy(self) -> bool:
        with self._lock:
            return self._task_running

    def run_task(self, text: str, source: str = "text") -> dict:
        """
        Starts a task in a background thread and returns immediately —
        {"accepted": bool, "task_id": str|None, "reason": str|None}.
        The actual outcome streams back via push_update events, not
        this return value, since a task can run far longer than is
        comfortable for a synchronous JS->Python call to wait on.
        """
        text = (text or "").strip()
        if not text:
            return {"accepted": False, "reason": "Empty task", "task_id": None}

        with self._lock:
            if self._task_running:
                return {
                    "accepted": False,
                    "reason": "A task is already running",
                    "task_id": None,
                }
            self._task_running = True

        task_id = uuid.uuid4().hex[:8]
        threading.Thread(
            target=self._run_task_thread, args=(task_id, text, source), daemon=True
        ).start()
        return {"accepted": True, "task_id": task_id, "reason": None}

    def _run_task_thread(self, task_id: str, text: str, source: str) -> None:
        from src.graph import _stdout_buffer_local, run_task_sync

        self._push_update({
            "type": "task_start", "task_id": task_id, "text": text, "source": source,
        })
        self._push_update({"type": "status", "status": "running"})

        forwarder = _LiveLineForwarder(
            on_line=lambda line: self._push_update(
                {"type": "log", "task_id": task_id, "line": line}
            )
        )
        _stdout_buffer_local.buffer = forwarder

        start = time.monotonic()
        try:
            final_state = run_task_sync(text)
        except Exception as e:
            # run_task_sync already catches broadly and returns a
            # failure dict rather than raising — this is an extra
            # backstop in case something upstream of that still slips
            # through, so the UI never gets stuck showing "running"
            # forever with the app otherwise looking fine.
            log.error("AgentRunner task crashed outside run_task_sync: %s", e, exc_info=True)
            final_state = {"is_done": True, "is_failed": True, "last_error": str(e)}
        finally:
            _stdout_buffer_local.buffer = None

        elapsed_ms = int((time.monotonic() - start) * 1000)
        success = bool(final_state.get("is_done")) and not final_state.get("is_failed")

        result_preview = ""
        slots = final_state.get("slots")
        if slots is not None and getattr(slots, "last_text", None):
            result_preview = slots.last_text[:800]
        elif final_state.get("last_error"):
            result_preview = final_state["last_error"]
        elif final_state.get("ask_user_message"):
            result_preview = final_state["ask_user_message"]

        entry = {
            "task_id":    task_id,
            "text":       text,
            "source":     source,
            "success":    success,
            "result":     result_preview,
            "elapsed_ms": elapsed_ms,
            "timestamp":  time.time(),
        }
        self._history.insert(0, entry)
        self._history = self._history[:GUI_HISTORY_MAX_ENTRIES]
        self._save_history()

        self._push_update({"type": "task_complete", **entry})
        self._push_update({"type": "status", "status": "idle"})

        with self._lock:
            self._task_running = False

    def get_history(self) -> list:
        return self._history

    def _load_history(self) -> list:
        try:
            if GUI_HISTORY_PATH.exists():
                return json.loads(GUI_HISTORY_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("Could not load task history (starting empty): %s", e)
        return []

    def _save_history(self) -> None:
        try:
            GUI_HISTORY_PATH.write_text(
                json.dumps(self._history, indent=2), encoding="utf-8"
            )
        except Exception as e:
            log.warning("Could not save task history: %s", e)


class Api:
    """
    Thin pywebview-facing wrapper. Methods here become callable from JS
    as window.pywebview.api.<name>(params) — per pywebview's own
    convention, each exposed method takes exactly one parameter, so
    multiple values are packed into a dict from the JS side rather than
    passed as separate positional arguments.
    """

    def __init__(self, runner: AgentRunner):
        self._runner = runner

    def run_task(self, params: dict) -> dict:
        text = (params or {}).get("text", "")
        return self._runner.run_task(text, source="text")

    def get_history(self, params: dict = None) -> list:
        return self._runner.get_history()

    def is_busy(self, params: dict = None) -> bool:
        return self._runner.is_busy


def run_desktop_app() -> None:
    """
    Phase 10 entry point, called from main.py when GUI_ENABLED=true.
    Blocking — owns the calling thread (pywebview's event loop, same
    main-thread requirement pystray had in Phase 8/9).
    """
    import webview

    window_holder: dict = {}

    def push_update(event: dict) -> None:
        """
        Forwards a backend event to the JS frontend via
        window.evaluate_js(...). This is the officially-documented
        pattern for background-thread-to-UI updates in pywebview (see
        its own evaluate_js examples, which run background work via
        webview.start(background_func, window) and call
        window.evaluate_js(...) from inside that background thread) —
        not something invented for this project.
        """
        window = window_holder.get("window")
        if window is None:
            return
        try:
            payload = json.dumps(event)
            window.evaluate_js(f"window.onAgentEvent({payload})")
        except Exception as e:
            log.debug("push_update failed (non-fatal, window may be closing): %s", e)

    runner = AgentRunner(push_update)
    api = Api(runner)

    # -- voice/wake-word: same VoiceController as Phase 8/9, but only
    #    the background-listening half — no tray icon here, the app
    #    window is the visible presence now. Works regardless of
    #    window focus per the requirement that "hey jarvis" (or the
    #    hotkey) should work any time, not just while the window is
    #    active — both pynput's hotkey listener and sounddevice's wake-
    #    word stream run their own background threads independent of
    #    window focus. --
    voice_controller = None
    if VOICE_ENABLED:
        from src.voice import VoiceController, VoiceDependencyError

        def on_voice_status(status: str) -> None:
            push_update({"type": "voice_status", "status": status})

        def on_voice_task(text: str) -> None:
            runner.run_task(text, source="voice")

        try:
            voice_controller = VoiceController(
                on_task=on_voice_task, on_status_change=on_voice_status
            )
            voice_controller.start_background_listening()
        except VoiceDependencyError as e:
            log.error(
                "Voice dependencies not ready — the app will run with "
                "text input only: %s", e,
            )

    html_path = Path(__file__).parent / "gui_assets" / "index.html"
    window = webview.create_window(
        "Computer-Use Agent",
        url=str(html_path),
        js_api=api,
        width=GUI_WINDOW_WIDTH,
        height=GUI_WINDOW_HEIGHT,
        min_size=(640, 480),
        background_color="#12141C",
    )
    window_holder["window"] = window

    try:
        webview.start()
    finally:
        if voice_controller is not None:
            voice_controller.stop_background_listening()