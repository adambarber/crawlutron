"""Bite proofs for the hermeticity guard."""

from __future__ import annotations

import socket

import pytest

from tests.network_guard import is_installed


def _public_ip_refused(dial) -> None:
    """A TCP probe dialing a public IP must be refused by name; UDP uses its own bite."""
    assert is_installed()
    probe = socket.socket()
    try:
        with pytest.raises(pytest.fail.Exception, match=r"93\.184\.216\.34"):
            dial(probe)
    finally:
        probe.close()


def test_guard_is_armed_for_every_test():
    assert is_installed(), "hermeticity guard is not armed: tests could reach real network"


def test_connect_to_a_public_host_is_refused_naming_the_destination():
    assert is_installed()
    with pytest.raises(pytest.fail.Exception, match=r"example\.com"):
        socket.create_connection(("example.com", 80), timeout=1)


def test_connect_via_socket_is_refused_naming_the_destination():
    _public_ip_refused(lambda probe: probe.connect(("93.184.216.34", 80)))


def test_getaddrinfo_of_a_dns_name_is_refused_naming_the_destination():
    assert is_installed()
    with pytest.raises(pytest.fail.Exception, match="crawlutron-guard-bite"):
        socket.getaddrinfo("crawlutron-guard-bite.invalid", None)


def test_dot_local_mdns_name_is_refused_by_the_guard():
    assert is_installed()
    with pytest.raises(pytest.fail.Exception, match="x.local"):
        socket.getaddrinfo("x.local", None)


def test_ip_literal_getaddrinfo_passes_parsed_not_resolved():
    assert is_installed()
    answers = socket.getaddrinfo("127.0.0.1", 0, socket.AF_INET)
    assert answers, "IP literal getaddrinfo must pass the guard"


def test_af_unix_connect_passes_the_guard(tmp_path):
    assert is_installed()
    probe = socket.socket(socket.AF_UNIX)
    try:
        # AF_UNIX is out of the guard's scope: the refusal must NOT fire, the
        # real connect must fail with the OS's own absent-file error instead.
        with pytest.raises(OSError) as excinfo:
            probe.connect(str(tmp_path / "absent.sock"))
        assert not isinstance(excinfo.value, pytest.fail.Exception)
    finally:
        probe.close()


def test_connect_ex_to_a_public_ip_is_refused_naming_the_destination():
    _public_ip_refused(lambda probe: probe.connect_ex(("93.184.216.34", 80)))


def test_udp_sendto_a_public_ip_is_refused_naming_the_destination():
    assert is_installed()
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        with pytest.raises(pytest.fail.Exception, match=r"93\.184\.216\.34"):
            probe.sendto(b"x", ("93.184.216.34", 53))
    finally:
        probe.close()


def test_connected_udp_send_to_a_public_ip_is_refused_at_the_connect():
    assert is_installed()
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # A connected-UDP send dials the connect-time peer: the guard refuses
        # at connect, so send never happens on a non-loopback peer.
        with pytest.raises(pytest.fail.Exception, match=r"93\.184\.216\.34"):
            probe.connect(("93.184.216.34", 53))
    finally:
        probe.close()


def test_gethostbyname_of_a_dns_name_is_refused_naming_the_destination():
    with pytest.raises(pytest.fail.Exception, match="crawlutron-guard-bite"):
        socket.gethostbyname("crawlutron-guard-bite.invalid")


def test_gethostbyname_ex_of_a_dns_name_is_refused_naming_the_destination():
    with pytest.raises(pytest.fail.Exception, match="crawlutron-guard-bite"):
        socket.gethostbyname_ex("crawlutron-guard-bite.invalid")
