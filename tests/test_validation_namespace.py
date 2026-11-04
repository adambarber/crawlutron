from __future__ import annotations

import crawlutron.safety.validation as validation


def test_the_module_namespace_declares_its_public_surface():
    # HostPolicy and NormalizedUrl are annotation-only under the deferred-annotation
    # law: they must NOT sit in the runtime namespace, and __all__ stays minimal.
    assert "HostPolicy" not in vars(validation)
    assert "NormalizedUrl" not in vars(validation)
    assert set(validation.__all__) == {"filter_js_endpoints", "is_public_ip_value"}
    # admit_or_none is a genuine runtime dependency of filter_js_endpoints, imported
    # from its owner; it is not a re-export and stays off __all__.
    assert "admit_or_none" in vars(validation)
