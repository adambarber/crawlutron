"""POST /v1/admit: the pre-flight admission contract, red-first for #654.

The base check (check_robots=false) is pure: zero I/O, zero quota, zero store
writes; the only I/O path is the opt-in robots check through the shared cache.
"""

from __future__ import annotations

import ast
import inspect
import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from crawlutron.config.settings import CrawlerSettings

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@asynccontextmanager
async def serve_admit(app: FastAPI) -> AsyncGenerator[AsyncClient, None]:
    from tests._async_harnesses import serve

    async with serve(app) as client:
        yield client


def _admit_app(settings: CrawlerSettings | None = None) -> FastAPI:
    from crawlutron.api.admit_router import router as admit_router
    from crawlutron.api.dependencies import get_settings

    app = FastAPI()
    app.include_router(admit_router)
    app.dependency_overrides[get_settings] = lambda: settings or CrawlerSettings.from_mapping({})
    return app


@pytest.fixture
def admit_settings():
    return CrawlerSettings.from_mapping({})


async def _post_admit(client: AsyncClient, payload: dict[str, object]) -> dict[str, object]:
    return json.loads((await client.post("/v1/admit", json=payload)).text)


def _check(body: dict[str, object], key: str) -> object:
    checks = body["checks"]
    assert isinstance(checks, dict)
    return checks[key]


async def test_admitted_url_returns_canonical_url_and_clean_checks(admit_settings):
    async with serve_admit(_admit_app(admit_settings)) as client:
        body = await _post_admit(client, {"url": "https://Example.com:443/a?b=1"})
    assert body["admitted"] is True
    assert body["classification"] is None
    assert body["canonical_url"] == "https://example.com/a?b=1"
    assert body["checks"] == {"url": None, "host_policy": None, "egress": None, "robots": None}


class TestRefusedClassifications:
    """Each refusal surfaces as the existing classification vocabulary, never a 4xx."""

    async def test_invalid_url(self, admit_settings):
        async with serve_admit(_admit_app(admit_settings)) as client:
            body = await _post_admit(client, {"url": "https:///no-host"})
        assert body["admitted"] is False
        assert body["classification"] == "invalid_url"
        assert _check(body, "url") == "invalid_url"
        assert body["canonical_url"] is None

    async def test_unsupported_scheme(self, admit_settings):
        async with serve_admit(_admit_app(admit_settings)) as client:
            body = await _post_admit(client, {"url": "ftp://example.com/x"})
        assert body["admitted"] is False
        assert body["classification"] == "unsupported_scheme"
        assert _check(body, "url") == "unsupported_scheme"

    async def test_host_denied(self):
        settings = CrawlerSettings.from_mapping({"CRAWLUTRON_DENIED_HOSTS": "blocked.test"})
        async with serve_admit(_admit_app(settings)) as client:
            body = await _post_admit(client, {"url": "https://blocked.test/secret"})
        assert body["admitted"] is False
        assert body["classification"] == "host_denied"
        assert _check(body, "host_policy") == "host_denied"
        assert _check(body, "url") is None

    async def test_host_not_allowed(self):
        settings = CrawlerSettings.from_mapping({"CRAWLUTRON_ALLOWED_HOSTS": "allowed.test"})
        async with serve_admit(_admit_app(settings)) as client:
            body = await _post_admit(client, {"url": "https://elsewhere.test/"})
        assert body["admitted"] is False
        assert body["classification"] == "host_not_allowed"
        assert _check(body, "host_policy") == "host_not_allowed"

    async def test_private_network_target(self, admit_settings, monkeypatch):
        monkeypatch.delenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", raising=False)
        from crawlutron.models.host_policy import reset_private_target_memo

        reset_private_target_memo()
        async with serve_admit(_admit_app(admit_settings)) as client:
            body = await _post_admit(client, {"url": "http://127.0.0.1:9/x"})
        assert body["admitted"] is False
        assert body["classification"] == "private_network_target"
        assert _check(body, "egress") == "private_network_target"


class TestPurity:
    """check_robots=false is 100% pure: no wire, no robots loader, no quota seam."""

    async def test_base_check_does_zero_io(self, monkeypatch, admit_settings):
        def _boom(*args, **kwargs):
            raise AssertionError("the pure admission check performed I/O")

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", _boom)
        monkeypatch.setattr("crawlutron.api.admit_endpoint.load_robots_rules", _boom)
        async with serve_admit(_admit_app(admit_settings)) as client:
            response = await client.post("/v1/admit", json={"url": "https://example.com/"})
        assert response.json()["admitted"] is True

    def test_endpoint_module_never_touches_the_quota_seam(self):
        from crawlutron.api import admit_endpoint

        source = inspect.getsource(admit_endpoint)
        assert "caller_auth" not in source
        assert "concurrency_tracker" not in source
        assert "Tracking" not in source


class TestRobotsOptIn:
    """check_robots=true is the only I/O path, through the shared robots cache."""

    @staticmethod
    def _loader_for(allowed: bool):
        from crawlutron.transport.robots import RobotsRules
        from crawlutron.vocabulary.robots import RobotsSource

        rules = [] if allowed else [(False, "/")]

        async def _fake_loader(target, fetcher, timeout_seconds, *, fetch_and_decode=None, policy=None):
            return RobotsRules(rules, [], None, RobotsSource.policy)

        return _fake_loader

    async def _admit_with_robots(self, monkeypatch, admit_settings, *, allowed: bool) -> dict[str, object]:
        """One shared harness: opt-in check with a scripted robots policy."""
        monkeypatch.setattr(
            "crawlutron.api.admit_endpoint.load_robots_rules", self._loader_for(allowed=allowed)
        )
        async with serve_admit(_admit_app(admit_settings)) as client:
            return await _post_admit(client, {"url": "https://example.com/x", "check_robots": True})

    async def test_disallowed_url(self, monkeypatch, admit_settings):
        body = await self._admit_with_robots(monkeypatch, admit_settings, allowed=False)
        assert body["admitted"] is False
        assert body["classification"] == "robots_disallowed"
        assert _check(body, "robots") == "robots_disallowed"
        assert body["canonical_url"] == "https://example.com/x"

    async def test_allowed_url_with_robots_check(self, monkeypatch, admit_settings):
        body = await self._admit_with_robots(monkeypatch, admit_settings, allowed=True)
        assert body["admitted"] is True
        assert _check(body, "robots") is None

    async def test_robots_check_is_opt_in(self, monkeypatch, admit_settings):
        calls: list[object] = []

        async def _loader(*args, **kwargs):
            calls.append(args)
            raise AssertionError("robots check ran without opt-in")

        monkeypatch.setattr("crawlutron.api.admit_endpoint.load_robots_rules", _loader)
        async with serve_admit(_admit_app(admit_settings)) as client:
            body = await _post_admit(client, {"url": "https://example.com/x"})
        assert body["admitted"] is True
        assert calls == []

    def test_robots_loader_is_the_shared_one(self):
        """The cache lives inside load_robots_rules; sharing it is sharing the function."""
        from crawlutron.api import admit_endpoint
        from crawlutron.transport.robots import load_robots_rules

        assert admit_endpoint.load_robots_rules is load_robots_rules


class TestMcpParity:
    """The MCP admit tool mirrors the REST handler one-for-one."""

    async def test_tool_list_advertises_admit(self):
        from crawlutron.mcp.server import tools_list_result

        tools = tools_list_result()["tools"]
        assert isinstance(tools, list)
        names = [tool["name"] for tool in tools if isinstance(tool, dict)]
        assert "admit" in names

    async def _tool_verdict(self, admit_settings, url: str) -> object:
        from crawlutron.mcp.handlers import dispatch as tool_dispatch
        from crawlutron.mcp.registry import TOOLS_BY_NAME
        from crawlutron.models.admit_request import AdmitRequest

        result = await tool_dispatch(
            TOOLS_BY_NAME["admit"],
            AdmitRequest(url=url),
            settings=admit_settings,
            shutdown_event=None,
            concurrency_tracker=None,
            job_store=None,
            caller=None,
        )
        structured = result.structured
        assert isinstance(structured, dict)
        return structured

    async def test_tool_mirrors_refused_rest_response(self, admit_settings):
        structured = await self._tool_verdict(admit_settings, "ftp://example.com/x")
        assert isinstance(structured, dict)
        assert structured["admitted"] is False
        assert structured["classification"] == "unsupported_scheme"

    async def test_tool_mirrors_admitted_rest_response(self, admit_settings):
        structured = await self._tool_verdict(admit_settings, "https://example.com/")
        assert isinstance(structured, dict)
        assert structured["admitted"] is True
        assert structured["canonical_url"] == "https://example.com/"


class TestDiscovery:
    """Discovery surfaces name admission in the same commit as the door."""

    def test_well_known_links_name_admit(self):
        from crawlutron.discovery.well_known import discovery_payload

        assert discovery_payload(CrawlerSettings.from_mapping({}))["links"]["admit"] == "/v1/admit"

    def test_recommended_endpoints_name_admit_before_crawl(self):
        from crawlutron.discovery.well_known import discovery_payload

        recommended = discovery_payload(CrawlerSettings.from_mapping({}))["recommended_endpoints"]
        assert "POST /v1/admit" in recommended
        assert recommended.index("POST /v1/admit") < recommended.index("POST /v1/crawl")

    def test_capabilities_advertises_pre_flight_admission(self):
        from crawlutron.discovery.capabilities import capabilities_payload

        features = capabilities_payload(CrawlerSettings.from_mapping({}))["features"]
        assert features["pre_flight_admission"] is True

    def test_agent_hints_recommend_admission_as_preflight(self):
        from crawlutron.discovery.agent_hints import agent_hints_payload

        guidance = " ".join(agent_hints_payload()["guidance"])
        assert "/v1/admit" in guidance
        assert "before the first request" in guidance


class TestStructuralRefusals:
    """The constructor's structural refusals surface as url-check data, never a 500."""

    @pytest.mark.parametrize(
        ("url", "reason"),
        [
            ("http://user:pass@example.com/", "userinfo_refused"),
            ("http://[v7.future]/x", "ipvfuture_refused"),
            ("http://example.com:abc/", "invalid_port"),
            ("http://example.com:99999/", "invalid_port"),
            ("http://" + "a" * 64 + ".example/", "invalid_idn_host"),
            ("http://[::1", "invalid_url"),
            ("http://exa mple.com/", "invalid_url"),  # space in the host
            ("http://exa\tmple.com/", "invalid_url"),  # tab folds in urlparse
            ("http://exa\nmple.com/", "invalid_url"),  # newline folds in urlparse
            ("http:// example.com/", "invalid_url"),  # leading space
            ("http://example.com /", "invalid_url"),  # trailing space
            ("http://exa mple.com:8080/", "invalid_url"),  # space with a port
        ],
    )
    async def test_structural_refusal_is_wire_data(self, admit_settings, url, reason):
        async with serve_admit(_admit_app(admit_settings)) as client:
            body = await _post_admit(client, {"url": url})
        assert body["admitted"] is False
        assert body["classification"] == reason
        assert _check(body, "url") == reason
        assert body["canonical_url"] is None


def _str_constants(node: ast.AST) -> list[tuple[int, str]]:
    """Every string literal in a subtree, with its line number."""
    return [
        (sub.lineno, sub.value)
        for sub in ast.walk(node)
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
    ]


def _raise_reasons(path: Path) -> list[tuple[int, str]]:
    """Literal ValueError refusal reasons raised in one file."""
    return [
        (node.lineno, sub.value)
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and isinstance(node.exc.func, ast.Name)
        and node.exc.func.id == "ValueError"
        for sub in node.exc.args
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
    ]


def _reason_return_literals(path: Path) -> list[tuple[int, str]]:
    """Reason literals returned by host_*_reason verdict functions."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name.startswith("host_") and node.name.endswith("_reason")
        ):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return):
                    found.extend(_str_constants(sub))
    return found


def _producer_files(normalized_url_path: Path) -> list[Path]:
    """DERIVED producer set: normalized_url.py plus its crawlutron.models imports;
    a new helper raising refusals joins the scan by being imported."""
    models_root = normalized_url_path.parent
    seeds = {normalized_url_path}
    for node in ast.walk(ast.parse(normalized_url_path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("crawlutron.models."):
            candidate = models_root / f"{node.module.rpartition('.')[2]}.py"
            if candidate.exists():
                seeds.add(candidate)
    return sorted(seeds)


def _producer_set(normalized_url_path: Path) -> list[tuple[Path, Callable[[Path], list[tuple[int, str]]]]]:
    """(file, collector) for the derived producer set plus the verdict functions."""
    producers: list[tuple[Path, Callable[[Path], list[tuple[int, str]]]]] = [
        (path, _raise_reasons) for path in _producer_files(normalized_url_path)
    ]
    producers.append((normalized_url_path.parent / "host_policy.py", _reason_return_literals))
    return producers


def _raisable_producer_reasons(normalized_url_path: Path) -> set[str]:
    """Every refusal literal the derived producer set can raise or return."""
    reasons: set[str] = set()
    for path, collect in _producer_set(normalized_url_path):
        reasons.update(reason for _, reason in collect(path))
    return reasons


def _producers_missing_rows(endpoint_path: Path, known: frozenset[str]) -> list[str]:
    """Raisable producer reasons with no table row, as file:line findings."""
    normalized_url_path = endpoint_path.parents[1] / "models" / "normalized_url.py"
    return [
        f"{path}:{lineno}: {reason!r} is raisable but has no _REASON_TABLE row"
        for path, collect in _producer_set(normalized_url_path)
        for lineno, reason in collect(path)
        if reason not in known
    ]


class TestProducerVocabularyLoop:
    """Every ValueError reason the admission producers can raise has a table row.

    Derives the raisable vocabulary from the live raise/return sites by AST, so a
    new producer refusal without a _REASON_TABLE row reds here naming file+line.

    Boundary: only ValueError raise sites with a LITERAL first arg are seen.
    Non-literal messages (urllib's port parse, "Invalid IPv6 URL") are NOT a
    live blind shape anymore -- the constructor boundary in NormalizedUrl.__init__
    maps any foreign ValueError explicitly to a named refusal -- but the wrap is
    the closure, not the scan: the derivation still cannot SEE a non-literal
    raise, it only cannot LEAK one. The producer closure is ONE hop deep
    (normalized_url + its models imports); a two-hop chain raising its own
    refusals is unscanned. host_policy verdicts are found by NAMING CONVENTION
    (``host_*_reason`` return literals); a variable-form raise outside that
    pattern goes blind silently.
    """

    def test_every_producer_reason_has_a_table_row(self):
        import crawlutron.api.admit_endpoint as endpoint_module

        missing = _producers_missing_rows(Path(endpoint_module.__file__), endpoint_module.known_reasons())
        assert missing == [], "producer->table loop broken:\n" + "\n".join(missing)

    def test_the_boundary_refusal_set_matches_the_constructor_vocabulary(self):
        """One vocabulary in both directions: every raisable constructor literal is
        in the boundary pass-through set (else it is remapped to invalid_url, and
        its table row is dead), and every set entry is table-mapped (else the
        boundary passes a value the wire cannot name)."""
        import crawlutron.api.admit_endpoint as endpoint_module
        from crawlutron.models.normalized_url import refusal_literals

        endpoint_path = Path(endpoint_module.__file__)
        models_dir = endpoint_path.parents[1] / "models"
        raisable = _raisable_producer_reasons(models_dir / "normalized_url.py")
        boundary_gaps = raisable - refusal_literals()
        table_gaps = refusal_literals() - set(endpoint_module.known_reasons())
        assert not boundary_gaps, f"constructor raisables outside the boundary set: {sorted(boundary_gaps)}"
        assert not table_gaps, f"boundary set entries with no table row: {sorted(table_gaps)}"


class TestHostileInputSweep:
    """No raise escapes admit_url: every hostile shape is wire data or a 401/422."""

    HOSTILE_URLS = (
        "http://[::1",  # malformed bracket: urllib's non-literal ValueError
        "http://[::1]:99999/",  # bracket + out-of-range port
        "http://example.com:/",  # empty port
        "http:example.com",  # scheme-relative, no authority
        "//example.com",  # network-path reference, no scheme
        "http://example.com/#a#b",  # fragment tricks
        "http://exa%zzmple.com/",  # invalid percent-escape in host
        "http://example.com/%zz",  # invalid percent-escape in path
        "http://[fe80::1%25eth0]/",  # IPv6 with zone id
        "http://user@/path",  # userinfo, empty host
        "http://:@example.com/",  # empty userinfo pair
        "http://.example.com/",  # dot host
        "http://example.com..../",  # trailing dots
        "http://example.com:0/",  # port zero
        "https://[v7.x]/",  # IPvFuture
    )

    @pytest.mark.parametrize("url", HOSTILE_URLS)
    async def test_no_raise_escapes_the_door(self, admit_settings, url):
        async with serve_admit(_admit_app(admit_settings)) as client:
            response = await client.post("/v1/admit", json={"url": url})
        assert response.status_code in {200, 401, 422}, response.text
        body = json.loads(response.text)
        if response.status_code == 200:
            assert body["classification"] is not None or body["admitted"] is True
