"""C1: the classification taxonomy has exactly one source of truth.

``app.constants`` used to hardcode the blocked/failed groupings as string
literals while ``app.api_models`` re-listed the same strings as
``CrawlClassification``. Adding a classification therefore meant editing two
files whose agreement nobody checked. The groupings are now derived from the
single enum, and these tests pin the derivation in both directions.
"""

from app.api_models import CrawlClassification
from app.constants import (
    BROWSER_BLOCKED_CLASSIFICATIONS,
    CLASSIFICATION_STATUS_BY_LABEL,
    FAILED_CLASSIFICATIONS,
    HTTP_BLOCKED_CLASSIFICATIONS,
)

_GROUPINGS = (
    HTTP_BLOCKED_CLASSIFICATIONS,
    BROWSER_BLOCKED_CLASSIFICATIONS,
    FAILED_CLASSIFICATIONS,
)


def test_every_grouping_member_is_an_enum_member():
    """No grouping may name a string the taxonomy does not know about."""
    for grouping in _GROUPINGS:
        for label in grouping:
            assert label in CrawlClassification.__members__, (
                f"{label!r} is listed in a classification grouping but is not a "
                f"CrawlClassification member"
            )


def test_groupings_and_the_status_table_cover_the_documented_taxonomy():
    """Members the status table deliberately leaves to their documented default
    are exactly the older fetch-level failure codes; everything else the enum
    advertises must be claimed by a grouping or by the table."""
    documented_default = {
        "robots_disallowed",
        "navigation_timeout",
        "browser_crash",
        "network_error",
        "connection_error",
        "http_status_error",
        "value_error",
        "tls_error",
        "dns_error",
        "invalid_url",
        "unsupported_scheme",
        "private_network_target",
        "unknown_error",
    }
    covered = set(CLASSIFICATION_STATUS_BY_LABEL)
    for grouping in _GROUPINGS:
        covered |= set(grouping)
    accounted = covered | documented_default
    entire = {member.value for member in CrawlClassification}
    assert accounted == entire, (
        f"taxonomy/accounting drift: "
        f"unaccounted={sorted(entire - accounted)} "
        f"secretly-defaulted={sorted(accounted - entire - documented_default)}"
    )


def test_capabilities_advertises_exactly_the_taxonomy():
    """The /v1/capabilities wire list is generated from the single enum, not a
    second hand-copied list."""
    from app.discovery.capabilities import capabilities_payload
    from app.settings import CrawlerSettings

    advertised = capabilities_payload(CrawlerSettings.from_mapping())["supported_classifications"]
    assert advertised == [member.value for member in CrawlClassification]


def test_groupings_are_derived_from_the_enum_not_relisted():
    """Each grouping is built from enum member references, not a hand-copied set
    of string literals -- the AST has no string constant inside a grouping."""
    import ast
    import inspect

    from app import constants

    tree = ast.parse(inspect.getsource(constants))
    assignments = {}
    for node in ast.walk(tree):
        target = None
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        if target in (
            "HTTP_BLOCKED_CLASSIFICATIONS",
            "BROWSER_BLOCKED_CLASSIFICATIONS",
            "FAILED_CLASSIFICATIONS",
        ):
            assignments[target] = value
    for name in (
        "HTTP_BLOCKED_CLASSIFICATIONS",
        "BROWSER_BLOCKED_CLASSIFICATIONS",
        "FAILED_CLASSIFICATIONS",
    ):
        value = assignments[name]
        literals = [
            node.value
            for node in ast.walk(value)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        assert not literals, f"{name} embeds string literals: {literals}"

        grouped = [
            node.attr
            for node in ast.walk(value)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "CrawlClassification"
        ]
        assert grouped, f"{name} groups no enum members"
        unknown = [attr for attr in grouped if attr not in CrawlClassification.__members__]
        assert not unknown, f"{name} references non-members: {unknown}"
