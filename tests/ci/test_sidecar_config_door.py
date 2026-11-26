"""One typed sidecar configuration door, with a Compose policy-drift bite."""

from __future__ import annotations

import ipaddress
import logging
import socket
from pathlib import Path

import pytest

from crawlutron.config.settings import CrawlerSettings
from tests.chokepoint_paths import REPO_ROOT

_ANY = ipaddress.IPv4Address(socket.INADDR_ANY)
_POLICY_KEYS = ("CRAWLUTRON_ALLOWED_HOSTS", "CRAWLUTRON_DENIED_HOSTS")
_SERVICES = ("crawlutron", "egress-proxy")


def _settings(allowed: str = "", denied: str = "") -> CrawlerSettings:
    """Build settings through the one shared settings door."""
    return CrawlerSettings.from_mapping(
        {"CRAWLUTRON_ALLOWED_HOSTS": allowed, "CRAWLUTRON_DENIED_HOSTS": denied}
    )


def _compose_documents() -> dict[Path, str]:
    """Every discovered compose layer's text, read through the one FS door."""
    from tests._walk_primitives import discovered_files

    return discovered_files(REPO_ROOT, "docker-compose*.yml", include_content=True)


@pytest.mark.parametrize("bad", ["", "nope", "1 2", "-1", "0", "65536", "1.5", " 8 "])
def test_shared_port_door_refuses_malformed_ports(bad: str) -> None:
    """Kills the bare-int mutant: every hostile port is a clean ValueError."""
    from crawlutron.config.sidecar import SidecarConfig

    with pytest.raises(ValueError, match="EGRESS_PROXY_BIND_PORT"):
        SidecarConfig.for_egress({"EGRESS_PROXY_BIND_PORT": bad}, _settings())


@pytest.mark.parametrize("port", [1, 65535])
def test_shared_port_door_admits_the_range_boundaries(port: int) -> None:
    """Kills an over-tightening mutant: 1 and 65535 stay admitted."""
    from crawlutron.config.sidecar import SidecarConfig

    config = SidecarConfig.for_egress({"EGRESS_PROXY_BIND_PORT": str(port)}, _settings())
    assert config.bind_port == port


def test_ingress_bind_shape_admits_only_the_unspecified_address() -> None:
    """Kills a widened-bind mutant: loopback, public and malformed hosts refuse."""
    from crawlutron.config.sidecar import SidecarConfig

    assert SidecarConfig.for_ingress({"API_INGRESS_BIND_HOST": "0.0.0.0"}).bind_host == _ANY
    for bad in ("127.0.0.1", "8.8.8.8", "::1", "not-an-ip", ""):
        with pytest.raises(ValueError, match="API_INGRESS_BIND_HOST"):
            SidecarConfig.for_ingress({"API_INGRESS_BIND_HOST": bad})


def test_egress_bind_host_admits_explicit_ipv4_and_refuses_garbage() -> None:
    """Kills a dropped-bind-validation mutant: egress keeps its IPv4 door."""
    from crawlutron.config.sidecar import SidecarConfig

    explicit = "127.0.0.1"
    assert SidecarConfig.for_egress({"EGRESS_PROXY_BIND_HOST": explicit}, _settings()).bind_host == (
        ipaddress.IPv4Address(explicit)
    )
    for bad in ("::1", "not-an-ip", "999.1.1.1"):
        with pytest.raises(ValueError, match="EGRESS_PROXY_BIND_HOST"):
            SidecarConfig.for_egress({"EGRESS_PROXY_BIND_HOST": bad}, _settings())


def test_egress_config_policy_is_the_shared_settings_policy() -> None:
    """Kills the removed-shared-policy mutant: policy comes from settings, not a request."""
    from crawlutron.config.sidecar import SidecarConfig
    from safe_url import HostPolicy

    settings = _settings("allowed.example", "denied.example")
    config = SidecarConfig.for_egress({}, settings)
    assert config.policy == HostPolicy.from_settings(settings)


async def test_egress_sidecar_refuses_a_malformed_port_cleanly(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Kills the sidecar-level bare-int mutant: clean logged refusal, exit 1."""
    from scripts import egress_proxy_sidecar

    monkeypatch.setenv("EGRESS_PROXY_BIND_PORT", "not-a-port")
    with caplog.at_level(logging.ERROR, logger="scripts.egress_proxy_sidecar"):
        assert await egress_proxy_sidecar.main() == 1
    assert any("EGRESS_PROXY_BIND_PORT" in record.getMessage() for record in caplog.records)


async def test_ingress_sidecar_refuses_a_malformed_port_cleanly(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Kills the ingress-level bare-int mutant: clean logged refusal, exit 1."""
    from scripts import api_ingress_sidecar

    monkeypatch.setenv("API_INGRESS_BIND_PORT", "not-a-port")
    with caplog.at_level(logging.ERROR, logger="scripts.api_ingress_sidecar"):
        assert await api_ingress_sidecar.main() == 1
    assert any("API_INGRESS_BIND_PORT" in record.getMessage() for record in caplog.records)


async def test_egress_sidecar_serves_the_settings_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills a sidecar that stops consuming the door's policy at the process boundary."""
    from safe_url import HostPolicy
    from scripts import egress_proxy_sidecar

    settings = _settings("allowed.example", "denied.example")
    captured: dict[str, object] = {}

    class _Server:
        port = 8888

        async def serve_forever(self) -> None:
            return None

    async def _serve_proxy(*, policy: object, port: int, sidecar_bind: ipaddress.IPv4Address) -> _Server:
        captured["policy"] = policy
        return _Server()

    monkeypatch.setattr(egress_proxy_sidecar, "get_cached_settings", lambda: settings)
    monkeypatch.setattr(egress_proxy_sidecar, "serve_proxy", _serve_proxy)
    assert await egress_proxy_sidecar.main() == 0
    assert captured["policy"] == HostPolicy.from_settings(settings)


def test_ingress_keeps_its_public_tuple_door() -> None:
    """Kills a door-shape drift: parse_ingress_config still returns the tuple contract."""
    from scripts.api_ingress_sidecar import parse_ingress_config

    config = parse_ingress_config({})
    assert (config[0], config[2], config[3]) == (_ANY, "crawlutron", 8765)


def test_ingress_shares_the_one_port_parser() -> None:
    """Kills a second parser: the ingress refuses the same hostile port set."""
    from crawlutron.config.sidecar import SidecarConfig

    for bad in ("", "nope", "0", "70000", "-1", "1 2"):
        with pytest.raises(ValueError, match="API_INGRESS_BIND_PORT"):
            SidecarConfig.for_ingress({"API_INGRESS_BIND_PORT": bad})
    assert SidecarConfig.for_ingress({"API_INGRESS_BIND_PORT": "65535"}).bind_port == 65535


def test_base_compose_supplies_policy_to_both_containers() -> None:
    """Kills a one-sided shared source: app and sidecar both receive the policy keys."""
    import yaml

    document = yaml.safe_load(_compose_documents()[REPO_ROOT / "docker-compose.yml"])
    envs = {name: document["services"][name].get("environment", {}) or {} for name in _SERVICES}
    for key in _POLICY_KEYS:
        assert all(key in env for env in envs.values()), f"{key} is not shared by app and sidecar"
        assert envs["crawlutron"][key] == envs["egress-proxy"][key]


def _policy_line_numbers(text: str, service: str) -> dict[str, list[int]]:
    """Locate raw policy-key lines inside one compose service block."""
    import re

    lines = text.splitlines()
    headers = [i for i, line in enumerate(lines) if line == f"  {service}:"]
    if not headers:
        return {}
    start = headers[0]
    end = next(
        (i for i in range(start + 1, len(lines)) if re.match(r"^  \S", lines[i])),
        len(lines),
    )
    found: dict[str, list[int]] = {}
    for index in range(start, end):
        match = re.match(r"^\s{6,}(CRAWLUTRON_(?:ALLOWED|DENIED)_HOSTS):", lines[index])
        if match:
            found.setdefault(match.group(1), []).append(index + 1)
    return found


@pytest.mark.parametrize("compose_path", sorted(_compose_documents()), ids=lambda path: path.name)
def test_app_and_sidecar_policy_never_drift(compose_path: Path) -> None:
    """Kills the planted-divergent-env mutant: a mismatch names the file and line."""
    import yaml

    path = compose_path
    text = _compose_documents()[path]
    services = yaml.safe_load(text).get("services", {})
    app_env = services.get("crawlutron", {}).get("environment", {}) or {}
    sidecar_env = services.get("egress-proxy", {}).get("environment", {}) or {}
    raw: dict[str, list[int]] = {}
    for name in _SERVICES:
        for key, numbers in _policy_line_numbers(text, name).items():
            raw.setdefault(key, []).extend(numbers)
    for key in _POLICY_KEYS:
        app_value = app_env.get(key)
        sidecar_value = sidecar_env.get(key)
        if app_value == sidecar_value:
            continue
        where = ", ".join(f"{path}:{number}" for number in raw.get(key, ()))
        pytest.fail(f"{path.name}: {key} app={app_value!r} sidecar={sidecar_value!r} diverges at {where}")
