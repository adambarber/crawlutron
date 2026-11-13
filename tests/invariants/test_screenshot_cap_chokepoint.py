"""Every screenshot payload is base64-encoded in one place: the shared capped helper."""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import call_nodes, offender_at
from tests.rules import Bites, Rule


def _is_screenshot_call(call: ast.Call) -> bool:
    """``<receiver>.screenshot(...)`` -- the page/``element`` capture verbs."""
    return isinstance(call.func, ast.Attribute) and call.func.attr == "screenshot"


def _contains_screenshot_call(node: ast.AST) -> bool:
    """True if `node`'s subtree reaches a ``.screenshot(...)`` call."""
    return any(isinstance(c, ast.Call) and _is_screenshot_call(c) for c in ast.walk(node))


def _screenshot_flow_names(tree: ast.AST) -> frozenset[str]:
    """Plain names assigned from an expression containing a ``.screenshot(...)`` call."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and _contains_screenshot_call(node.value):
                    names.add(target.id)
    return frozenset(names)


def _uncapped_screenshot_b64_offenders(
    tree: ast.AST,
    path: pathlib.Path,
    **_: object,
) -> list[str]:
    """Every ``base64.b64encode`` fed by a screenshot, outside the single capped helper."""
    flow_names = _screenshot_flow_names(tree)
    offenders: list[str] = []
    for call in call_nodes(tree):
        if not call.args:
            continue
        leaf = call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", "")
        if leaf != "b64encode":
            continue
        argument = call.args[0]
        if isinstance(argument, ast.Name) and argument.id in flow_names:
            offenders.append(
                offender_at(path, call, "b64encode of a screenshot outside the shared screenshot_payload cap")
            )
        elif _contains_screenshot_call(argument):
            offenders.append(
                offender_at(path, call, "b64encode of a screenshot outside the shared screenshot_payload cap")
            )
    return offenders


SCREENSHOT_PAYLOAD_ENCODED_ONCE = Rule(
    name="screenshot-payload-encoded-once",
    find=_uncapped_screenshot_b64_offenders,
    summary=(
        "a browser screenshot is base64-encoded only in screenshot_diagnostics.screenshot_payload, "
        "the one place its bytes are capped (#742)"
    ),
    bite=lambda: (
        Bites(
            "import base64\n"
            "async def capture(page):\n"
            "    element = await page.query_selector('#a')\n"
            "    return base64.b64encode(await element.screenshot()).decode('ascii')\n",
            "b64encode",
        ),
        # The full-page site bound its capture to a local first; both shapes bleed.
        Bites(
            "import base64\n"
            "async def capture(page):\n"
            "    shot = await page.screenshot(full_page=True)\n"
            "    return base64.b64encode(shot).decode('ascii')\n",
            "b64encode",
        ),
    ),
    clean=lambda: (
        # The sanctioned helper base64-encodes a plain parameter, never screenshot bytes.
        "import base64\n"
        "def screenshot_payload(raw: bytes) -> str:\n"
        "    if len(raw) <= 1:\n"
        "        return base64.b64encode(raw).decode('ascii')\n"
        "    return 'refused'\n",
        # base64 of a non-screenshot value (wire body, auth header) is not this law.
        "import base64\ndef f(body):\n    return base64.b64encode(body[:10]).decode('ascii')\n",
    ),
)


def test_screenshot_base64_is_capped_in_one_place() -> None:
    SCREENSHOT_PAYLOAD_ENCODED_ONCE.scan()
