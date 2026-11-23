"""Forms and frames collection: required, textarea, select options, frameset,
sandbox attributes. srcdoc recursion is delegated to the traversal core.
"""

from __future__ import annotations

from crawlutron_extract.limits import ExtractLimits


class FormFrameCollector:
    """Collects forms and frames during one parse pass.

    srcdoc inline documents are parsed by the traversal core, which calls
    back into this collector for any nested frames/forms found inside.
    """

    def __init__(self, limits: ExtractLimits) -> None:
        self._limits = limits
        self._forms: list[dict] = []
        self._frames: list[dict] = []
        self._srcdoc_refused = False
        self._in_select = False
        self._select_name = ""
        self._textarea_field: dict | None = None
        self._srcdoc_depth = 0
        self._srcdoc_cap = 5

    def _ensure_form(self) -> bool:
        if not self._forms:
            self._forms.append({"action": "", "method": "GET", "fields": []})
        return len(self._forms[-1]["fields"]) < self._limits.max_links

    def handle_starttag(self, tag: str, attrs: dict[str, str]) -> None:
        if tag == "form":
            self._handle_form_start(attrs)
        elif tag == "select":
            self._in_select = True
            self._select_name = attrs.get("name", "")
        elif tag == "option" and self._in_select:
            if not self._ensure_form():
                return
            self._forms[-1]["fields"].append(
                {
                    "type": "option",
                    "name": self._select_name,
                    "value": attrs.get("value", ""),
                    "selected": "true" if attrs.get("selected") is not None else "false",
                }
            )
        elif tag in ("input", "button", "textarea"):
            self._handle_form_field(tag, attrs)
        elif tag in ("iframe", "frame"):
            self._handle_frame_start(attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag == "textarea":
            self._textarea_field = None
        elif tag == "select":
            self._in_select = False
            self._select_name = ""

    def handle_data(self, data: str) -> None:
        if self._textarea_field is not None:
            self._textarea_field["value"] += data

    def _handle_form_start(self, attrs: dict[str, str]) -> None:
        if len(self._forms) >= self._limits.max_forms:
            return
        self._forms.append(
            {
                "action": attrs.get("action", ""),
                "method": attrs.get("method", "GET").upper(),
                "fields": [],
            }
        )

    def _handle_form_field(self, tag: str, attrs: dict[str, str]) -> None:
        if not self._ensure_form():
            return
        field: dict = {
            "type": attrs.get("type", ""),
            "name": attrs.get("name", ""),
            "value": attrs.get("value", ""),
            "required": "true" if attrs.get("required") is not None else "false",
        }
        self._forms[-1]["fields"].append(field)
        if tag == "textarea":
            self._textarea_field = field

    def _handle_frame_start(self, attrs: dict[str, str]) -> None:
        if len(self._frames) >= self._limits.max_frames:
            return
        frame_entry: dict = {
            "src": attrs.get("src", ""),
            "srcdoc": attrs.get("srcdoc", ""),
            "name": attrs.get("name", ""),
            "sandbox": attrs.get("sandbox", ""),
        }
        self._frames.append(frame_entry)

    def result(self) -> tuple[list[dict], list[dict], int]:
        flags: int = 0
        if self._srcdoc_refused:
            flags |= 2
        return self._forms, self._frames, flags

    def add_frame(self, frame: dict) -> None:
        self._frames.append(frame)

    def add_frames(self, frames: list[dict]) -> None:
        self._frames.extend(frames)
