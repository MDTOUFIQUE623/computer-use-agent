import time
import logging
from dataclasses import dataclass
from typing import Optional

import pytesseract
import pyautogui
from PIL import Image

import uiautomation as auto

from src.models import ToolResult
from src.config import (
    TESSERACT_PATH,
    OCR_CONFIDENCE_MIN,
    OCR_LANGUAGE,
    OCR_SCREENSHOT_WIDTH,
)

log = logging.getLogger(__name__)

# Set tesseract executable path
pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH

# Internal data structure

@dataclass
class WordData:
    """
    One word found by OCR with its position and confidence.
    center_x, center_y are absolute screen coordinates — ready to click.
    """
    text:       str
    confidence: float
    left:       int    # bounding box — absolute screen coords
    top:        int
    width:      int
    height:     int
    center_x:   int    # precomputed click target
    center_y:   int

# Main class

class OCRTools:
    """
    Screen text reading via Tesseract OCR.

    Primary use case — read text from a specific app window:
        ocr = OCRTools()
        result = ocr.read_window_text("WhatsApp")

    Find and get coordinates for clicking:
        result = ocr.find_text_on_screen("Send")
        if result.success:
            x = result.data["center_x"]
            y = result.data["center_y"]
            pyautogui.click(x, y)

    Usage note:
        OCR is the THIRD fallback in the tool priority:
          1. uiautomation (instant, no image needed)
          2. Playwright DOM (instant, no image needed)
          3. OCR (fast, needs screenshot of region)
          4. Gemini vision (slow, expensive — true last resort)
    """

    # Window-based OCR  (primary use case)

    def read_window_text(
        self,
        app_name: str,
        region: Optional[str] = None,
    ) -> ToolResult:
        """
        Read all visible text from a specific app's window.

        Crops the screenshot to exactly the window boundaries — no noise
        from other apps on screen.

        app_name: partial window title match e.g. "WhatsApp", "Spotify"
        region:   optional sub-region — "top_half", "bottom_half",
                  "left_half", "right_half", "center"
                  Use this to focus on specific parts of a large window.

        Example:
            # Read messages in WhatsApp (bottom portion has the chat)
            result = ocr.read_window_text("WhatsApp", region="bottom_half")
            print(result.data["text"])
        """
        start = time.monotonic()
        try:
            # Get window bounds via uiautomation
            bounds = _get_window_bounds(app_name)
            if bounds is None:
                return ToolResult(
                    success=False,
                    message=f"Window '{app_name}' not found on screen",
                    error="WindowNotFound",
                    duration_ms=_ms(start)
                )

            left, top, right, bottom = bounds
            width  = right  - left
            height = bottom - top

            # Apply sub-region crop if requested
            crop_box = _apply_region(left, top, width, height, region)

            # Take screenshot of just that region
            image = _screenshot_region(*crop_box)

            # Run OCR
            words = _run_ocr(image, offset_x=crop_box[0], offset_y=crop_box[1])

            full_text = _words_to_text(words)
            word_count = len(full_text.split()) if full_text else 0

            return ToolResult(
                success=True,
                message=(
                    f"Read {word_count} word(s) from '{app_name}'"
                    + (f" ({region})" if region else "")
                ),
                data={
                    "text":       full_text,
                    "word_count": word_count,
                    "words":      [_word_to_dict(w) for w in words],
                    "app_name":   app_name,
                    "region":     region,
                },
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error("read_window_text failed for '%s': %s", app_name, e)
            return ToolResult(
                success=False,
                message=f"OCR failed for '{app_name}'",
                error=str(e),
                duration_ms=_ms(start)
            )

    def find_text_in_window(
        self,
        app_name: str,
        target_text: str,
        region: Optional[str] = None,
    ) -> ToolResult:
        """
        Find specific text inside an app window and return click coordinates.

        Returns the screen coordinates of the center of the found text.
        These coordinates can be used directly with pyautogui.click().

        app_name:    window to search in
        target_text: text to find (case-insensitive, partial match)
        region:      optional sub-region to narrow the search

        Example:
            result = ocr.find_text_in_window("WhatsApp", "John Smith")
            if result.success:
                pyautogui.click(result.data["center_x"], result.data["center_y"])
        """
        start = time.monotonic()
        try:
            # First read all text from the window
            read_result = self.read_window_text(app_name, region)
            if not read_result.success:
                return read_result

            words  = read_result.data["words"]
            target = target_text.lower()

            # Search word by word
            # Also search in pairs for multi-word targets
            found = _find_in_words(words, target)

            if found is None:
                return ToolResult(
                    success=False,
                    message=(
                        f"Text '{target_text}' not found in '{app_name}'"
                    ),
                    error="TextNotFound",
                    data={
                        "searched_text": target_text,
                        "app_name":      app_name,
                    },
                    duration_ms=_ms(start)
                )

            return ToolResult(
                success=True,
                message=(
                    f"Found '{target_text}' in '{app_name}' "
                    f"at ({found.center_x}, {found.center_y})"
                ),
                data={
                    "word":       found.text,
                    "center_x":  found.center_x,
                    "center_y":  found.center_y,
                    "confidence": found.confidence,
                    "region": {
                        "left":   found.left,
                        "top":    found.top,
                        "width":  found.width,
                        "height": found.height,
                    },
                },
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error(
                "find_text_in_window failed for '%s' in '%s': %s",
                target_text, app_name, e
            )
            return ToolResult(
                success=False,
                message=f"OCR search failed",
                error=str(e),
                duration_ms=_ms(start)
            )

    # -----------------------------------------------------------------------
    # Full screen OCR  (use sparingly)
    # -----------------------------------------------------------------------

    def scan_full_screen(self) -> ToolResult:
        """
        OCR the entire screen.

        Use this only when you don't know which window to target.
        Slower than window-based OCR because the image is larger.
        Consider using read_window_text() instead whenever possible.
        """
        start = time.monotonic()
        try:
            image  = _screenshot_full()
            words  = _run_ocr(image, offset_x=0, offset_y=0)
            text   = _words_to_text(words)

            return ToolResult(
                success=True,
                message=f"Full screen OCR: {len(words)} word(s) found",
                data={
                    "full_text":  text,
                    "word_count": len(text.split()) if text else 0,
                    "words":      [_word_to_dict(w) for w in words],
                },
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error("scan_full_screen failed: %s", e)
            return ToolResult(
                success=False,
                message="Full screen OCR failed",
                error=str(e),
                duration_ms=_ms(start)
            )

    def find_text_on_screen(
        self,
        target_text: str,
    ) -> ToolResult:
        """
        Find text anywhere on the full screen.
        Use find_text_in_window() first if you know which app has the text.
        This is slower but works when you don't know the window.
        """
        start = time.monotonic()
        try:
            image  = _screenshot_full()
            words  = _run_ocr(image, offset_x=0, offset_y=0)
            target = target_text.lower()
            found  = _find_in_words(words, target)

            if found is None:
                return ToolResult(
                    success=False,
                    message=f"'{target_text}' not found on screen",
                    error="TextNotFound",
                    duration_ms=_ms(start)
                )

            return ToolResult(
                success=True,
                message=(
                    f"Found '{target_text}' at "
                    f"({found.center_x}, {found.center_y})"
                ),
                data={
                    "word":       found.text,
                    "center_x":  found.center_x,
                    "center_y":  found.center_y,
                    "confidence": found.confidence,
                    "region": {
                        "left":   found.left,
                        "top":    found.top,
                        "width":  found.width,
                        "height": found.height,
                    },
                },
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error("find_text_on_screen failed: %s", e)
            return ToolResult(
                success=False,
                message="Screen OCR search failed",
                error=str(e),
                duration_ms=_ms(start)
            )

    def scan_region(
        self,
        left: int,
        top: int,
        width: int,
        height: int,
    ) -> ToolResult:
        """
        OCR a specific screen region defined by pixel coordinates.

        Use this when you know exactly where to look —
        faster and more accurate than full screen OCR.

        All coordinates are absolute screen pixels.
        """
        start = time.monotonic()
        try:
            image = _screenshot_region(left, top, width, height)
            words = _run_ocr(image, offset_x=left, offset_y=top)
            text  = _words_to_text(words)

            return ToolResult(
                success=True,
                message=f"Region OCR: {len(words)} word(s) found",
                data={
                    "full_text":  text,
                    "word_count": len(text.split()) if text else 0,
                    "words":      [_word_to_dict(w) for w in words],
                    "region": {
                        "left": left, "top": top,
                        "width": width, "height": height,
                    },
                },
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error("scan_region failed: %s", e)
            return ToolResult(
                success=False,
                message="Region OCR failed",
                error=str(e),
                duration_ms=_ms(start)
            )

    # -----------------------------------------------------------------------
    # WhatsApp specific helpers
    # (uses window OCR — no CDP, no browser needed)
    # -----------------------------------------------------------------------

    def read_whatsapp_messages(self) -> ToolResult:
        """
        Read visible messages from the WhatsApp desktop app.

        Uses bottom 70% of the window — that's where the chat content is.
        The top 30% is the contact list sidebar.

        Returns the raw text found. Brain/Gemini can parse
        the actual messages from this text.
        """
        return self.read_window_text("WhatsApp", region="right_bottom")

    def find_whatsapp_contact(self, contact_name: str) -> ToolResult:
        """
        Find a contact name in the WhatsApp contact list (left sidebar).
        Returns click coordinates to open that conversation.
        """
        return self.find_text_in_window(
            "WhatsApp", contact_name, region="left_half"
        )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _get_window_bounds(
    app_name: str,
) -> Optional[tuple[int, int, int, int]]:
    """
    Get (left, top, right, bottom) pixel coordinates of a window.
    Returns None if window not found.
    """
    name_lower = app_name.lower()
    desktop    = auto.GetRootControl()

    for window in desktop.GetChildren():
        title = (window.Name or "").lower()
        if name_lower in title:
            rect = window.BoundingRectangle
            return (rect.left, rect.top, rect.right, rect.bottom)

    return None


def _apply_region(
    left: int,
    top: int,
    width: int,
    height: int,
    region: Optional[str],
) -> tuple[int, int, int, int]:
    """
    Apply a named sub-region crop to window bounds.
    Returns (left, top, width, height) of the cropped area.

    region values:
      None         — full window
      "top_half"   — top 50%
      "bottom_half"— bottom 50%
      "left_half"  — left 50%
      "right_half" — right 50%
      "center"     — middle 60% x 60%
      "right_bottom" — bottom-right quadrant (WhatsApp chat area)
    """
    if region is None:
        return (left, top, width, height)

    if region == "top_half":
        return (left, top, width, height // 2)

    if region == "bottom_half":
        half_h = height // 2
        return (left, top + half_h, width, half_h)

    if region == "left_half":
        return (left, top, width // 2, height)

    if region == "right_half":
        half_w = width // 2
        return (left + half_w, top, half_w, height)

    if region == "center":
        pad_x = int(width  * 0.2)
        pad_y = int(height * 0.2)
        return (
            left + pad_x,
            top  + pad_y,
            width  - pad_x * 2,
            height - pad_y * 2,
        )

    if region == "right_bottom":
        # Bottom-right 70%w x 70%h — WhatsApp chat area
        x_start = left + int(width  * 0.30)
        y_start = top  + int(height * 0.30)
        return (
            x_start,
            y_start,
            width  - int(width  * 0.30),
            height - int(height * 0.30),
        )

    # Unknown region name — return full window
    log.warning("Unknown region '%s' — using full window", region)
    return (left, top, width, height)


def _screenshot_region(
    left: int,
    top: int,
    width: int,
    height: int,
) -> Image.Image:
    """
    Capture a specific screen region and return as PIL Image.
    Downscales if wider than OCR_SCREENSHOT_WIDTH for speed.
    """
    screenshot = pyautogui.screenshot(
        region=(left, top, width, height)
    )

    # Downscale for faster OCR if needed
    if screenshot.width > OCR_SCREENSHOT_WIDTH:
        ratio      = OCR_SCREENSHOT_WIDTH / screenshot.width
        new_height = int(screenshot.height * ratio)
        screenshot = screenshot.resize(
            (OCR_SCREENSHOT_WIDTH, new_height),
            Image.LANCZOS
        )

    return screenshot


def _screenshot_full() -> Image.Image:
    """Capture the full screen."""
    screenshot = pyautogui.screenshot()

    if screenshot.width > OCR_SCREENSHOT_WIDTH:
        ratio      = OCR_SCREENSHOT_WIDTH / screenshot.width
        new_height = int(screenshot.height * ratio)
        screenshot = screenshot.resize(
            (OCR_SCREENSHOT_WIDTH, new_height),
            Image.LANCZOS
        )

    return screenshot


def _run_ocr(
    image: Image.Image,
    offset_x: int,
    offset_y: int,
) -> list[WordData]:
    """
    Run Tesseract on an image and return WordData list.

    offset_x, offset_y: the screen coordinates of the image's
    top-left corner. Used to convert bounding box coords from
    image-relative to absolute screen coordinates.

    Filters out:
      - Words below OCR_CONFIDENCE_MIN confidence
      - Empty strings
      - Single characters that are likely noise
    """
    # image_to_data returns a TSV-like dict with per-word data
    data = pytesseract.image_to_data(
        image,
        lang=OCR_LANGUAGE,
        output_type=pytesseract.Output.DICT,
        config="--psm 11"  # psm 11 = sparse text, good for UI screens
    )

    words: list[WordData] = []
    n = len(data["text"])

    # Calculate scale factor if image was downscaled
    orig_width  = pyautogui.size().width
    scale = orig_width / OCR_SCREENSHOT_WIDTH if image.width <= OCR_SCREENSHOT_WIDTH else 1.0

    for i in range(n):
        text = str(data["text"][i]).strip()
        conf = float(data["conf"][i])

        # Skip low confidence, empty, or noise
        if conf < OCR_CONFIDENCE_MIN:
            continue
        if not text or len(text) < 2:
            continue

        # Convert image-relative coords to screen coords
        img_left   = int(data["left"][i])
        img_top    = int(data["top"][i])
        img_width  = int(data["width"][i])
        img_height = int(data["height"][i])

        # Scale back up if image was downscaled
        screen_left   = offset_x + int(img_left   * scale)
        screen_top    = offset_y + int(img_top    * scale)
        screen_width  = int(img_width  * scale)
        screen_height = int(img_height * scale)

        words.append(WordData(
            text       = text,
            confidence = conf,
            left       = screen_left,
            top        = screen_top,
            width      = screen_width,
            height     = screen_height,
            center_x   = screen_left + screen_width  // 2,
            center_y   = screen_top  + screen_height // 2,
        ))

    return words


def _words_to_text(words: list[WordData]) -> str:
    """
    Convert WordData list back to readable text.
    Groups words by approximate line (similar top coordinate).
    """
    if not words:
        return ""

    # Group into lines by top coordinate proximity
    lines:  list[list[WordData]] = []
    current_line: list[WordData] = []
    current_top = words[0].top

    for word in words:
        # If this word is on a new line (>10px vertical gap)
        if abs(word.top - current_top) > 10:
            if current_line:
                lines.append(current_line)
            current_line = [word]
            current_top  = word.top
        else:
            current_line.append(word)

    if current_line:
        lines.append(current_line)

    # Sort each line by x position, join words with spaces
    result_lines = []
    for line in lines:
        line.sort(key=lambda w: w.left)
        result_lines.append(" ".join(w.text for w in line))

    return "\n".join(result_lines)


def _find_in_words(
    words: list[dict],
    target: str,
) -> Optional[WordData]:
    """
    Find target text in a list of word dicts (from OCR result data).
    Supports both single words and multi-word phrases.
    Returns the WordData of the best match or None.

    For multi-word targets, returns the WordData of the first word
    in the match (so clicking it gets close to the text).
    """
    target_lower  = target.lower()
    target_words  = target_lower.split()

    # Rebuild WordData objects from dicts
    word_objects: list[WordData] = []
    for w in words:
        word_objects.append(WordData(
            text       = w["text"],
            confidence = w["confidence"],
            left       = w["left"],
            top        = w["top"],
            width      = w["width"],
            height     = w["height"],
            center_x   = w["center_x"],
            center_y   = w["center_y"],
        ))

    # Single word search
    if len(target_words) == 1:
        best      = None
        best_conf = -1.0
        for w in word_objects:
            if target_lower in w.text.lower() and w.confidence > best_conf:
                best      = w
                best_conf = w.confidence
        return best

    # Multi-word search — look for consecutive words matching the phrase
    for i in range(len(word_objects) - len(target_words) + 1):
        chunk = word_objects[i : i + len(target_words)]
        chunk_text = " ".join(w.text.lower() for w in chunk)
        if target_lower in chunk_text:
            return chunk[0]  # Return first word of the match

    return None


def _word_to_dict(word: WordData) -> dict:
    """Convert WordData to a plain dict for ToolResult.data."""
    return {
        "text":       word.text,
        "confidence": word.confidence,
        "left":       word.left,
        "top":        word.top,
        "width":      word.width,
        "height":     word.height,
        "center_x":   word.center_x,
        "center_y":   word.center_y,
    }


def _ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)
