"""Browser doubles: fake drivers, routes, renderers, and pinned requests."""

from tests._browser_doubles import (
    FakeBrowserDriver,
    FakeRequest,
    FakeResponse,
    FakeRoute,
    assert_continued,
    assert_route_decision,
    build_pinned_renderer,
    pinned_page_renderer,
    render_bare,
    render_pinned,
    response_handler_double,
    run_route_with_pinner,
    scripted_renderer,
    stub_pinned_request,
)

__all__ = [
    "FakeBrowserDriver",
    "FakeRequest",
    "FakeResponse",
    "FakeRoute",
    "assert_continued",
    "assert_route_decision",
    "build_pinned_renderer",
    "pinned_page_renderer",
    "render_bare",
    "render_pinned",
    "response_handler_double",
    "run_route_with_pinner",
    "scripted_renderer",
    "stub_pinned_request",
]
