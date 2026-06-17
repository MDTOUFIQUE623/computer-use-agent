import logging
from dataclasses import dataclass
from typing import Callable, Optional, TYPE_CHECKING
 
from src.models import ToolType, Step, ToolResult
 
if TYPE_CHECKING:
    # Avoid circular imports at module load time; these are only used
    # for type hints, not at runtime.
    from src.brain import Brain
    from src.state import StateSlots
 
log = logging.getLogger(__name__)
 
 
# ---------------------------------------------------------------------------
# Execution context — uniform extras passed to every executor
# ---------------------------------------------------------------------------
 
@dataclass
class ExecutionContext:
    """
    Bundles the optional extras a tool executor might need, so every
    registered executor has the same call signature regardless of
    whether it uses these fields.
 
    slots: typed cross-step state from src.state.StateSlots (Phase 1).
           Browser tool uses this to resolve {{browser_url}} etc. in
           step.target. Most tools ignore it.
    brain: the active Brain instance. Only vision currently uses this
           (for prior-attempt context), but kept generic in case future
           tools need planner-level context too.
    """
    slots: Optional["StateSlots"] = None
    brain: Optional["Brain"]      = None
 
 
# Type alias for clarity at call sites
ToolExecutor = Callable[[Step, ExecutionContext], ToolResult]
 
 
# ---------------------------------------------------------------------------
# ToolSpec — what each tool module registers
# ---------------------------------------------------------------------------
 
@dataclass
class ToolSpec:
    """
    What a tool module hands back from build_executor().
 
    tool_type: which ToolType this spec handles — must be unique across
               all registered specs, enforced at registration time.
    executor:  the single callable that handles every action for this
               tool. Internal action-name -> method dispatch stays
               inside the tool module (e.g. src/tools/files.py owns its
               own ActionType -> FileTools method mapping); the registry
               doesn't need to know about individual actions.
    """
    tool_type: ToolType
    executor:  ToolExecutor
 
 
# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
 
class ToolRegistry:
    """
    Holds one ToolSpec per ToolType. Built once at import time from the
    modules listed in _TOOL_MODULES, then used by graph.py for every
    step execution via get_executor().
    """
 
    def __init__(self) -> None:
        self._specs: dict[ToolType, ToolSpec] = {}
 
    def register(self, spec: ToolSpec) -> None:
        if spec.tool_type in self._specs:
            raise ValueError(
                f"Tool type '{spec.tool_type.value}' is already registered "
                f"— each ToolType may only have one executor. Check for "
                f"duplicate entries in _TOOL_MODULES."
            )
        self._specs[spec.tool_type] = spec
        log.debug("Registered executor for tool: %s", spec.tool_type.value)
 
    def get_executor(self, tool_type: ToolType) -> Optional[ToolExecutor]:
        spec = self._specs.get(tool_type)
        return spec.executor if spec else None
 
    def is_registered(self, tool_type: ToolType) -> bool:
        return tool_type in self._specs
 
    def registered_tools(self) -> list[ToolType]:
        return list(self._specs.keys())
 
 
# ---------------------------------------------------------------------------
# Module list — the only place that needs editing to add/remove a tool
# ---------------------------------------------------------------------------
 
# Each entry is a dotted module path that must expose build_executor().
# Order doesn't matter; registration is keyed by ToolType, not position.
_TOOL_MODULES: list[str] = [
    "src.tools.files",
    "src.tools.windows_ui",
    "src.tools.browser",
    "src.tools.apps",
    "src.tools.ocr",
    "src.tools.vision",
]
 
 
def build_registry() -> ToolRegistry:
    """
    Import each module in _TOOL_MODULES and register its executor.
    Called once, lazily, the first time the registry is needed (see
    get_registry() below) — not at module import time, so that tools
    with heavy/platform-specific imports (pyautogui, uiautomation, etc.)
    don't get pulled in just by importing src.registry.
    """
    import importlib
 
    registry = ToolRegistry()
 
    for module_path in _TOOL_MODULES:
        try:
            module = importlib.import_module(module_path)
        except Exception as e:
            log.error("Failed to import tool module '%s': %s", module_path, e)
            continue
 
        build_fn = getattr(module, "build_executor", None)
        if build_fn is None:
            log.error(
                "Tool module '%s' has no build_executor() function — skipping",
                module_path,
            )
            continue
 
        try:
            spec = build_fn()
        except Exception as e:
            log.error(
                "build_executor() failed for '%s': %s", module_path, e
            )
            continue
 
        if not isinstance(spec, ToolSpec):
            log.error(
                "build_executor() in '%s' did not return a ToolSpec — skipping",
                module_path,
            )
            continue
 
        registry.register(spec)
 
    return registry
 
 
# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------
 
_registry_instance: Optional[ToolRegistry] = None
 
 
def get_registry() -> ToolRegistry:
    """
    Return the process-wide ToolRegistry, building it on first call.
    graph.py calls this once per process (or per _execute_step call —
    the build is cheap after the first time since the module-level
    cache below avoids re-importing tool modules).
    """
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = build_registry()
    return _registry_instance
 
 
def reset_registry() -> None:
    """
    Clear the cached registry. Mainly useful for tests that want to
    verify build_registry() behavior in isolation, or after a hot-reload
    of a tool module during development.
    """
    global _registry_instance
    _registry_instance = None