"""Crawl/render requests take exactly one of url/urls.

Both set silently ignored url; fail fast at the model boundary.
"""

import importlib
import inspect
import pkgutil
import typing
from types import ModuleType

import httpx
import pytest
from httpx import AsyncClient
from pydantic import BaseModel, ValidationError

from app.models.crawl_request import CrawlRequest
from app.models.render_request import RenderRequest
from tests._app_factory import service_app


@pytest.mark.parametrize("model", [CrawlRequest, RenderRequest])
def test_url_and_urls_together_rejected(model):
    with pytest.raises(ValidationError):
        model(url="http://example.com/", urls=["http://example.com/"])


@pytest.mark.parametrize("model", [CrawlRequest, RenderRequest])
def test_neither_url_nor_urls_rejected(model):
    with pytest.raises(ValidationError):
        model()
    with pytest.raises(ValidationError):
        model(urls=[])


@pytest.mark.parametrize("model", [CrawlRequest, RenderRequest])
def test_exactly_one_url_source_accepted(model):
    assert model(url="http://example.com/").url == "http://example.com/"
    assert model(urls=["http://example.com/"]).urls == ["http://example.com/"]


@pytest.mark.parametrize("model", [CrawlRequest, RenderRequest])
def test_empty_urls_list_with_url_rejected(model):
    """Urls=[] is not "absent" -- exclude_none dumps it, _payload_urls
    resolves it to zero targets, and url is silently ignored."""
    with pytest.raises(ValidationError):
        model(url="http://example.com/", urls=[])


class TestWebhookUrlValidation:
    async def test_main_invalid_webhook_url(self):
        transport = httpx.ASGITransport(app=service_app())
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/v1/crawl", json={"url": "http://example.com/", "webhook_url": "not_a_url"}
            )
            assert resp.status_code == 422


#: The models FastAPI binds a request body to. Everything reachable from one of these is caller-supplied,
#: which is the property that decides ``extra``.
_BOUND_REQUEST_BODIES = (
    "CrawlRequest",
    "JobCreateRequest",
    "JobControlRequest",
    "MapRequest",
    "RenderRequest",
    "SearchRequest",
    "DiscoveryConfig",
)


#: Derived from the package, so a model is covered the day its file is written.
def _wire_model_modules() -> tuple[ModuleType, ...]:
    package = importlib.import_module("app.models")
    names = (m.name for m in pkgutil.iter_modules(package.__path__))
    return tuple(importlib.import_module(f"app.models.{name}") for name in names)


def _declared_models() -> dict[str, type[BaseModel]]:
    """Every request/response model the wire layer declares, across both homes."""
    return {
        name: obj
        for module in _wire_model_modules()
        for name, obj in vars(module).items()
        if inspect.isclass(obj)
        and issubclass(obj, BaseModel)
        and obj is not BaseModel
        and obj.__module__ == module.__name__
    }


def _caller_supplied() -> set[str]:
    """Transitive closure of the bound request bodies over field annotations.

    Reachability rather than a hand-kept list: an input model ADDED inherits
    pydantic's default ``ignore`` until somebody decides otherwise."""
    models = _declared_models()
    reachable, frontier = set(), set(_BOUND_REQUEST_BODIES)
    while frontier:
        frontier -= reachable
        reachable |= frontier
        nxt: set[str] = set()
        for name in frontier:
            # Inheritance is an edge too.
            for base in models[name].__mro__[1:]:
                if base is not BaseModel and base.__name__ in models:
                    nxt.add(base.__name__)
            for field in models[name].model_fields.values():
                for arg in typing.get_args(field.annotation) or (field.annotation,):
                    for candidate in typing.get_args(arg) or (arg,):
                        if (
                            isinstance(candidate, type)
                            and issubclass(candidate, BaseModel)
                            and candidate.__name__ in models
                        ):
                            nxt.add(candidate.__name__)
        frontier = nxt
    return reachable


class TestExtraKeysAreRefusedOnTheInputSide:
    """An unknown key on an input model is a caller believing they configured
    something they did not; a mistyped ``max_rps`` disables rate limiting.
    Response models are deliberately NOT covered."""

    def test_the_caller_supplied_set_is_not_empty(self):
        """Guards the rest of this class from passing by finding nothing.

        A reachability walk resolving to the empty set makes every assertion
        below vacuously true; this premise is asserted, not assumed."""
        supplied = _caller_supplied()
        assert len(supplied) >= 10, f"reachability walk found too little: {sorted(supplied)}"
        assert "CrawlPolicy" in supplied, "CrawlPolicy is caller-supplied via CrawlRequest.policy"

    def test_every_caller_supplied_model_refuses_unknown_keys(self):
        offenders = sorted(
            name
            for name in _caller_supplied()
            if _declared_models()[name].model_config.get("extra") != "forbid"
        )
        assert not offenders, (
            "caller-supplied models must forbid unknown keys, because an unknown key is a "
            f"caller believing they configured something they did not: {offenders}"
        )

    @pytest.mark.parametrize(
        "model_name",
        [
            "CrawlRequest",
            "CrawlPolicy",
            "CrawlPolicyLimits",
            "Credential",
            "FetchValidators",
            "JobCreateRequest",
            "MapRequest",
            "RenderOptionsRequest",
        ],
    )
    def test_the_error_names_the_offending_field(self, model_name):
        """A rejection a caller cannot act on is a rejection they route around.

        The error must carry a loc pointing at the sent key; a model-level
        message reports ``loc=()`` and the API's code derivation could not map it."""
        model = _declared_models()[model_name]
        with pytest.raises(ValidationError) as exc:
            model(**{**_sample_field(model), "not_a_real_setting": 1})
        locations = {str(part) for error in exc.value.errors() for part in error["loc"]}
        assert any("not_a_real_setting" in location for location in locations), locations

    def test_response_models_are_not_forbidden(self):
        """The other half of the split, as an invariant over the whole output side.

        Asserted as a set so a new response model is covered the day it is
        written; an extra key on a response is not a mistake anyone is told about."""
        models = _declared_models()
        offenders = sorted(
            name
            for name in set(models) - _caller_supplied()
            if models[name].model_config.get("extra") == "forbid"
        )
        assert not offenders, (
            f"response models describe our output; forbidding extras is wrong for: {offenders}"
        )

    def test_response_models_accept_extras(self):
        """Behavioural companion to the config assertion above.

        Restricted to output models needing no required fields; a hand-written
        sample of a 14-field response is a second copy of the schema."""
        models = _declared_models()
        checkable = sorted(
            name
            for name in set(models) - _caller_supplied()
            if not any(field.is_required() for field in models[name].model_fields.values())
        )
        assert checkable, "no output model is constructible with no arguments; the premise is void"
        for name in checkable:
            assert models[name](not_a_real_setting=1) is not None, name


def _sample_field(model: type[BaseModel]) -> dict[str, object]:
    """One minimal valid instance of ``model``, for the extra-key assertions.

    Only the INPUT side needs a sample -- those models must reject, and they are
    small enough that a literal reads cheaper than a generated instance."""
    fixtures: dict[str, dict[str, object]] = {
        "CrawlRequest": {"url": "https://example.com/"},
        "CrawlPolicy": {"limits": {"max_pages": 5}},
        "CrawlPolicyLimits": {"max_pages": 5},
        "Credential": {"kind": "bearer", "token": "t"},
        "FetchValidators": {"if_none_match": "abc"},
        "JobCreateRequest": {"root_url": "https://example.com/"},
        "JobControlRequest": {"action": "pause"},
        "MapRequest": {"url": "https://example.com/"},
        "RenderRequest": {"url": "https://example.com/"},
        "RenderOptionsRequest": {"timeout_seconds": 5},
        "SearchRequest": {"query": "x"},
        "DiscoveryConfig": {},
    }
    return dict(fixtures[model.__name__])


class TestRequestCollectionsAreBounded:
    """A caller-supplied collection with no ceiling is an unbounded fan-out.

    One collection carried ``max_length`` while its siblings were uncapped; the
    ceilings are policy decisions, so they live beside the tests that pin them."""

    #: Longest caller-supplied URL list accepted in one request. A whole-site job is the unbounded path -- it
    #: exists precisely so this is bounded.
    MAX_URLS = 500
    #: Most validators in one request. Each is a conditional-request header pair.
    MAX_VALIDATORS = 64
    #: Most custom headers in one request.
    MAX_HEADERS = 64

    def test_url_lists_are_capped(self):
        from app.models.crawl_request import CrawlRequest

        at_ceiling = {"urls": [f"https://example.com/{index}" for index in range(self.MAX_URLS)]}
        assert len(CrawlRequest(**at_ceiling).urls or []) == self.MAX_URLS
        with pytest.raises(ValidationError):
            CrawlRequest(urls=[f"https://example.com/{i}" for i in range(self.MAX_URLS + 1)])

    def test_the_render_and_map_models_cap_urls_too(self):
        """The cap must travel with the shared url-source base, not just CrawlRequest.

        Three models bind URLs through one base; a ceiling on only the biggest
        leaves the other two uncapped -- the shape this test exists to prevent."""
        from app.models.map_request import MapRequest
        from app.models.render_request import RenderRequest

        too_many = [f"https://example.com/{index}" for index in range(self.MAX_URLS + 1)]
        for model in (MapRequest, RenderRequest):
            with pytest.raises(ValidationError):
                model(urls=too_many)

    def test_validator_maps_are_capped(self):
        from app.models.crawl_request import CrawlRequest

        at_ceiling = {f"https://example.com/{i}": {"if_none_match": "x"} for i in range(self.MAX_VALIDATORS)}
        # A url source is required alongside it -- unrelated to the cap, but the model will not validate a
        # validators map on its own.
        assert (
            len(CrawlRequest(url="https://example.com/0", validators=at_ceiling).validators)
            == self.MAX_VALIDATORS
        )
        over = {f"https://example.com/{i}": {"if_none_match": "x"} for i in range(self.MAX_VALIDATORS + 1)}
        with pytest.raises(ValidationError):
            CrawlRequest(url="https://example.com/0", validators=over)

    def test_header_maps_are_capped(self):
        from app.models.crawl_policy import CrawlPolicy

        at_ceiling = {f"x-custom-{i}": "v" for i in range(self.MAX_HEADERS)}
        assert len(CrawlPolicy(headers=at_ceiling).headers) == self.MAX_HEADERS
        over = {f"x-custom-{i}": "v" for i in range(self.MAX_HEADERS + 1)}
        with pytest.raises(ValidationError):
            CrawlPolicy(headers=over)

    def test_the_credential_ceiling_still_holds(self):
        """Pins the pre-existing cap so the siblings are not capped BELOW it by accident."""
        from app.models.crawl_policy import CrawlPolicy

        at_ceiling = {f"https://h{i}.example": {"kind": "bearer", "token": "t"} for i in range(32)}
        assert len(CrawlPolicy(credentials=at_ceiling).credentials) == 32
        over = {f"https://h{i}.example": {"kind": "bearer", "token": "t"} for i in range(33)}
        with pytest.raises(ValidationError):
            CrawlPolicy(credentials=over)
