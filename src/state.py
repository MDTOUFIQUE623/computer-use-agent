import logging
from typing import Optional

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Browser State model
# ---------------------------------------------------------------------------
class BrowserState(BaseModel):
    url: str = ""
    title: str = ""
    current_domain: str = ""
    page_type: str = ""
    selected_element: str = ""
    history: list[str] = Field(default_factory=list)

 
 
# ---------------------------------------------------------------------------
# Slot model
# ---------------------------------------------------------------------------
 
class StateSlots(BaseModel):
    """
    Named, typed slots for data produced by one step and consumed by a
    later step. All fields are optional — a slot is empty until some
    tool writes to it.
 
    Add new slots here as new tools/output types are introduced (e.g.
    a future `pdf_text` or `email_thread` slot). Keep each slot single-
    purpose; resist the urge to make one slot do double duty.
    """
 
    # Browser / web research
    browser_text:  Optional[str]       = None   # extracted page text
    browser:       BrowserState        = Field(default_factory=BrowserState)
 
    # Filesystem
    file_list:     Optional[list[str]] = None   # list_files / find_files results
    moved_files:   Optional[dict[str, str]] = None  # organize_files src->dst map
 
    # OCR
    ocr_text:      Optional[str]       = None
 
    # Clipboard
    clipboard_text: Optional[str]      = None
 
    # Vision (last-resort fallback)
    vision_result: Optional[str]       = None   # human-readable description/decision
 
    # Generic alias — always mirrors whichever *_text slot was most
    # recently written. This is what {{extracted_content}} resolves to.
    last_text:     Optional[str]       = None
 
    def set_text(self, slot: str, value: str) -> None:
        """
        Write a text-producing slot and keep `last_text` in sync.
        Use this instead of direct attribute assignment for any slot
        that holds text, so {{extracted_content}} keeps working.
        """
        setattr(self, slot, value)
        self.last_text = value
 
    def get(self, slot: str) -> Optional[object]:
        """Safe getter — returns None for unknown slot names. Supports dot notation."""
        if "." in slot:
            obj, attr = slot.split(".", 1)
            parent = getattr(self, obj, None)
            return getattr(parent, attr, None) if parent else None
        return getattr(self, slot, None)

    @property
    def browser_url(self) -> Optional[str]:
        return self.browser.url or None

    @browser_url.setter
    def browser_url(self, value: Optional[str]) -> None:
        self.browser.url = value or ""

    @property
    def browser_title(self) -> Optional[str]:
        return self.browser.title or None

    @browser_title.setter
    def browser_title(self, value: Optional[str]) -> None:
        self.browser.title = value or ""
 
 
# Map of legacy/alias placeholder names -> real slot name.
# Extend this if older saved Plans reference other now-renamed fields.
_ALIASES: dict[str, str] = {
    "extracted_content": "last_text",
}
 
# Slots that should be treated as "text-like" for {{placeholder}}
# substitution into `target` / `value` strings (lists/dicts get
# stringified by the caller, not substituted directly).
_TEXT_SLOTS = {
    "browser_text",
    "browser_url",
    "browser_title",
    "browser.url",
    "browser.title",
    "browser.current_domain",
    "browser.page_type",
    "browser.selected_element",
    "ocr_text",
    "clipboard_text",
    "vision_result",
    "last_text",
}
 
 
def resolve_slot_name(name: str) -> str:
    """Resolve a placeholder name (possibly an alias) to a real slot name."""
    return _ALIASES.get(name, name)
 
 
def resolve_placeholder(text: Optional[str], slots: StateSlots) -> Optional[str]:
    """
    If `text` is exactly a {{slot_name}} placeholder, return the slot's
    current value (or the original placeholder string if the slot is
    empty/unknown, so callers can detect "nothing to substitute").
 
    Only whole-string placeholders are supported (matches existing
    behavior in graph.py, which checked `step.target == "{{extracted_content}}"`
    rather than doing inline substitution). Kept simple on purpose —
    inline multi-placeholder substitution isn't needed yet.
    """
    if not text or not (text.startswith("{{") and text.endswith("}}")):
        return text
 
    raw_name  = text[2:-2].strip()
    slot_name = resolve_slot_name(raw_name)
 
    if slot_name not in _TEXT_SLOTS:
        log.warning("Unknown or non-text placeholder '%s' — leaving as-is", raw_name)
        return text
 
    value = slots.get(slot_name)
    if value is None:
        log.debug("Placeholder '{{%s}}' resolved to empty slot", raw_name)
        return text
 
    return value
 
 
def describe_slots(slots: StateSlots) -> str:
    """
    Human-readable one-line-per-slot summary for logging/debugging.
    Only shows non-empty slots.
    """
    lines = []
    if slots.browser.url:
        lines.append(f"  browser.url: '{slots.browser.url}'")
        if slots.browser.title:
            lines.append(f"  browser.title: '{slots.browser.title}'")
        if slots.browser.page_type:
            lines.append(f"  browser.page_type: '{slots.browser.page_type}'")

    for field_name in slots.model_fields:
        if field_name == "browser":
            continue
        value = getattr(slots, field_name)
        if value is None:
            continue
        if isinstance(value, str):
            preview = value[:60] + ("..." if len(value) > 60 else "")
            lines.append(f"  {field_name}: '{preview}'")
        elif isinstance(value, list):
            lines.append(f"  {field_name}: [{len(value)} items]")
        elif isinstance(value, dict):
            lines.append(f"  {field_name}: {{{len(value)} entries}}")
        else:
            lines.append(f"  {field_name}: {value!r}")
    return "\n".join(lines) if lines else "  (empty)"
 