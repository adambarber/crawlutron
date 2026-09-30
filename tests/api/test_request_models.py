"""Crawl/render requests take exactly one of url/urls.

Both set silently ignored url; neither failed downstream in _payload_urls.
Fail fast at the model boundary instead.
"""

import inspect
import typing

import httpx
import pytest
from httpx import AsyncClient
from pydantic import BaseModel, ValidationError

from app import api_models
from app.api_models import CrawlRequest, RenderRequest
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


#: The models FastAPI binds a request body to. Everything reachable from one of
#: these is caller-supplied, which is the property that decides ``extra``.
#: Derived by NAME on purpose: these are the shapes a route hands to the
#: framework, and they have to be listed here because a route's binding is a
#: runtime fact the model layer cannot see.
_BOUND_REQUEST_BODIES = (
    "CrawlRequest",
    "JobCreateRequest",
    "JobControlRequest",
    "MapRequest",
    "RenderRequest",
    "SearchRequest",
    "DiscoveryConfig",
)


def _declared_models() -> dict[str, type[BaseModel]]:
    """Every request/response model the wire layer declares."""
    return {
        name: obj
        for name, obj in vars(api_models).items()
        if inspect.isclass(obj)
        and issubclass(obj, BaseModel)
        and obj is not BaseModel
        and obj.__module__ == api_models.__name__
    }


def _caller_supplied() -> set[str]:
    """Transitive closure of the bound request bodies over field annotations.

    Reachability rather than a hand-kept list, because the failure this guards
    against is a model being ADDED to the input side and inheriting pydantic's
    default ``ignore`` with nobody deciding otherwise. A list written by hand
    goes stale silently and reproduces the exact bug it was meant to prevent.
    """
    models = _declared_models()
    reachable, frontier = set(), set(_BOUND_REQUEST_BODIES)
    while frontier:
        frontier -= reachable
        reachable |= frontier
        nxt: set[str] = set()
        for name in frontier:
            # Inheritance is an edge too. ``_SingleUrlSourceRequest`` is never a
            # FIELD of a bound request body -- it is their BASE CLASS -- so a
            # field-only walk classifies it as output-side and exempts it, which
            # is precisely the wrong answer: a caller constructs it directly, and
            # it already carries a validator that can raise.
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
    something they did not.

    The dangerous cases are all silent today: a mistyped ``max_rps`` disables
    rate limiting against a third-party target, a mistyped ``strategies`` runs
    the default chain (which includes a HEADED browser), a mistyped
    ``respect_robots`` changes nothing while appearing in the caller's payload.

    Response models are deliberately NOT covered: they describe what this service
    produced, and a caller sending extras to one is not a configuration mistake.
    """

    def test_the_caller_supplied_set_is_not_empty(self):
        """Guards the rest of this class from passing by finding nothing.

        A reachability walk that silently resolves to the empty set would make
        every assertion below vacuously true -- a gate that reports success
        because it examined no models. This is the premise the other tests rest
        on, so it is asserted rather than assumed.
        """
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

        The error must carry a loc pointing at the key they sent. A model-level
        message would report loc=() and the API's code derivation could not map
        it, so a caller whose only mistake was a typo would be told a code that
        names no field.
        """
        model = _declared_models()[model_name]
        with pytest.raises(ValidationError) as exc:
            model(**{**_sample_field(model), "not_a_real_setting": 1})
        locations = {str(part) for error in exc.value.errors() for part in error["loc"]}
        assert any("not_a_real_setting" in location for location in locations), locations

    def test_response_models_are_not_forbidden(self):
        """The other half of the split, as an invariant over the whole output side.

        Asserted as a set rather than per-model, so a new response model is
        covered the day it is written. A blanket ``forbid`` everywhere would break
        every response the service emits, and would be the wrong correction: the
        caller is not configuring our output, so an extra key on a response is
        not a mistake anyone needs to be told about.
        """
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

        Restricted to output models that need no required fields, so a valid
        instance is ``{}``. A hand-written sample for the 14-field
        ``CapabilitiesResponse`` would be a second copy of the response schema
        living in the test suite -- exactly the drift-prone hand-maintained
        declaration this codebase already rejects elsewhere.
        """
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

    Only the INPUT side needs a sample: those models are the ones that must
    reject, and they are small enough that a literal is cheaper to read than a
    generated instance. See ``test_response_models_accept_extras`` for why the
    output side deliberately avoids this.
    """
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

    ``credentials`` has carried ``max_length=32`` all along, while its siblings
    in the same model were left uncapped. That asymmetry reads as an oversight
    rather than a decision, and each capped collection is a place where a
    malicious or merely careless payload turns into work the service agreed to:
    a policy naming a thousand origins, a crawl naming a million URLs.

    The ceilings are policy decisions, not derivable facts, so they live here
    beside the tests that pin them rather than being spread across the model.
    """

    #: Longest caller-supplied URL list accepted in one request. A whole-site
    #: job is the unbounded path -- it exists precisely so this is bounded.
    MAX_URLS = 500
    #: Most validators in one request. Each is a conditional-request header pair.
    MAX_VALIDATORS = 64
    #: Most custom headers in one request.
    MAX_HEADERS = 64

    def test_url_lists_are_capped(self):
        from app.api_models import CrawlRequest

        at_ceiling = {"urls": [f"https://example.com/{index}" for index in range(self.MAX_URLS)]}
        assert len(CrawlRequest(**at_ceiling).urls or []) == self.MAX_URLS
        with pytest.raises(ValidationError):
            CrawlRequest(urls=[f"https://example.com/{i}" for i in range(self.MAX_URLS + 1)])

    def test_the_render_and_map_models_cap_urls_too(self):
        """The cap must travel with the shared url-source base, not just CrawlRequest.

        Three models bind URLs and they share one base class; a ceiling declared
        on only the biggest one leaves the other two uncapped, which is the
        shape this test exists to prevent.
        """
        from app.api_models import MapRequest, RenderRequest

        too_many = [f"https://example.com/{index}" for index in range(self.MAX_URLS + 1)]
        for model in (MapRequest, RenderRequest):
            with pytest.raises(ValidationError):
                model(urls=too_many)

    def test_validator_maps_are_capped(self):
        from app.api_models import CrawlRequest

        at_ceiling = {f"https://example.com/{i}": {"if_none_match": "x"} for i in range(self.MAX_VALIDATORS)}
        # A url source is required alongside it -- unrelated to the cap, but the
        # model will not validate a validators map on its own.
        assert (
            len(CrawlRequest(url="https://example.com/0", validators=at_ceiling).validators)
            == self.MAX_VALIDATORS
        )
        over = {f"https://example.com/{i}": {"if_none_match": "x"} for i in range(self.MAX_VALIDATORS + 1)}
        with pytest.raises(ValidationError):
            CrawlRequest(url="https://example.com/0", validators=over)

    def test_header_maps_are_capped(self):
        from app.api_models import CrawlPolicy

        at_ceiling = {f"x-custom-{i}": "v" for i in range(self.MAX_HEADERS)}
        assert len(CrawlPolicy(headers=at_ceiling).headers) == self.MAX_HEADERS
        over = {f"x-custom-{i}": "v" for i in range(self.MAX_HEADERS + 1)}
        with pytest.raises(ValidationError):
            CrawlPolicy(headers=over)

    def test_the_credential_ceiling_still_holds(self):
        """Pins the pre-existing cap so the siblings are not capped BELOW it by accident."""
        from app.api_models import CrawlPolicy

        at_ceiling = {f"https://h{i}.example": {"kind": "bearer", "token": "t"} for i in range(32)}
        assert len(CrawlPolicy(credentials=at_ceiling).credentials) == 32
        over = {f"https://h{i}.example": {"kind": "bearer", "token": "t"} for i in range(33)}
        with pytest.raises(ValidationError):
            CrawlPolicy(credentials=over)


class TestOutputFormatIsTheDocumentedSet:
    """``output_format`` describes itself as an enum, so it is one.

    The field is annotated ``str | None`` and its description reads "One of:
    json (default), jsonl, ndjson, markdown, csv". Nothing bound the value to
    that set, so a typo fell back to JSON and the caller received valid JSON
    having asked for CSV, with nothing to indicate the substitution.

    A census of every model for a field whose description enumerates values
    while its annotation does not enforce them returns exactly this one field,
    so the fix is deliberately not generalised into a sweep.
    """

    DOCUMENTED = ("json", "jsonl", "ndjson", "markdown", "csv")

    def test_the_documented_set_is_what_the_type_accepts(self):
        """The description and the annotation must state the same set.

        This is the drift guard for the field: the description is what a caller
        reads, the Literal is what is enforced, and the failure this issue
        describes is exactly the two disagreeing while both look correct.
        """
        from typing import get_args

        from app.api_models import CrawlRequest

        args = get_args(CrawlRequest.model_fields["output_format"].annotation)
        enforced = {value for arg in args for value in get_args(arg) if isinstance(value, str)}
        assert enforced == set(self.DOCUMENTED), f"documented and enforced sets disagree: {enforced}"

    @pytest.mark.parametrize("value", DOCUMENTED)
    def test_each_documented_value_validates(self, value):
        from app.api_models import CrawlRequest

        assert CrawlRequest(url="https://example.com/", output_format=value).output_format == value

    def test_absent_still_means_the_default(self):
        from app.api_models import CrawlRequest

        assert CrawlRequest(url="https://example.com/").output_format is None
        assert CrawlRequest(url="https://example.com/", output_format=None).output_format is None

    @pytest.mark.parametrize("typo", ["jsonlines", "JSON", "csv2", "md", "yaml", ""])
    def test_a_value_outside_the_set_is_refused(self, typo):
        from app.api_models import CrawlRequest

        with pytest.raises(ValidationError):
            CrawlRequest(url="https://example.com/", output_format=typo)

    def test_the_refusal_names_the_field(self):
        """A caller whose only mistake was a typo must be told WHICH field.

        Asserted here because a model-level rejection reports loc=() and the
        API derives its error code from where pydantic points -- so a bare
        "invalid request" would name nothing the caller can act on.
        """
        from app.api_models import CrawlRequest

        with pytest.raises(ValidationError) as exc:
            CrawlRequest(url="https://example.com/", output_format="jsonlines")
        assert any("output_format" in str(error["loc"]) for error in exc.value.errors())


class TestBrowserProfileVocabularyIsClosed:
    """An open profile means every future dangerous key is a separate audit.

    ``browser_profile`` is ``dict[str, Any]`` and exactly one key was checked
    (``execute_js``). Every other key a caller may set is prose in a field
    description, and a key that is inert reads identically to one that worked --
    so a caller who misspelled an action type got a page that loaded with
    nothing done and nothing said.

    The vocabulary is asserted against the renderer's own if/elif chain, so
    closing the model side cannot drift from the consumer that acts on it.
    """

    ALLOWED = ("click", "fill", "hover", "scroll", "scroll_bottom", "wait", "wait_seconds")

    def test_the_allowed_set_is_what_the_renderer_recognises(self):
        """The model must not permit an action the renderer cannot perform.

        A type in the model but not the renderer is the worst pairing: accepted
        at the boundary, then silently skipped at the point of use.
        """
        from app.constants import ALLOWED_BROWSER_ACTION_TYPES

        assert set(self.ALLOWED) == set(ALLOWED_BROWSER_ACTION_TYPES)

    @pytest.mark.parametrize("action_type", ALLOWED)
    def test_each_allowed_action_validates(self, action_type):
        from app.api_models import CrawlPolicy

        profile = CrawlPolicy(browser_profile={"actions": [{"type": action_type, "selector": "#x"}]})
        assert profile.browser_profile["actions"][0]["type"] == action_type

    def test_execute_js_stays_refused_with_its_reasoning_intact(self):
        """The RCE refusal must survive, and must still be about execute_js."""
        from app.api_models import CrawlPolicy

        with pytest.raises(ValidationError, match="execute_js"):
            CrawlPolicy(browser_profile={"actions": [{"type": "execute_js", "value": "fetch('x')"}]})

    @pytest.mark.parametrize("typo", ["clik", "Execute_JS", "fill_text", "", "CLICK_ALL"])
    def test_an_unrecognised_action_type_is_refused(self, typo):
        from app.api_models import CrawlPolicy

        with pytest.raises(ValidationError) as exc:
            CrawlPolicy(browser_profile={"actions": [{"type": typo, "selector": "#x"}]})
        message = str(exc.value)
        assert "actions" in message, message

    def test_the_refusal_names_the_allowed_vocabulary(self):
        """A refusal that does not say what IS allowed leaves the caller guessing.

        The message must list the accepted types, so a caller can correct the
        call without reading this service's source.
        """
        from app.api_models import CrawlPolicy

        with pytest.raises(ValidationError) as exc:
            CrawlPolicy(browser_profile={"actions": [{"type": "clik"}]})
        message = str(exc.value)
        for action_type in self.ALLOWED:
            assert action_type in message, f"{action_type} missing from refusal: {message}"

    def test_an_action_type_is_case_insensitive_because_the_renderer_is(self):
        """The renderer lowercases, so a mixed-case type WOULD have worked.

        Rejecting ``CLICK`` while ``click`` passes would refuse something the
        consumer handles. The rule is the vocabulary, not the spelling.
        """
        from app.api_models import CrawlPolicy

        assert CrawlPolicy(browser_profile={"actions": [{"type": "CLICK", "selector": "#x"}]})

    @pytest.mark.parametrize("key", ["state", "viewport", "resource_mode", "actions"])
    def test_documented_profile_keys_are_accepted(self, key):
        from app.api_models import CrawlPolicy

        value = [] if key == "actions" else "desktop"
        assert CrawlPolicy(browser_profile={key: value}).browser_profile[key] == value

    @pytest.mark.parametrize("typo", ["viewports", "resourceMode", "user_agent_family", "exec_js"])
    def test_an_unknown_profile_key_is_refused(self, typo):
        """A misspelled profile key is inert, so it must not pass silently."""
        from app.api_models import CrawlPolicy

        with pytest.raises(ValidationError):
            CrawlPolicy(browser_profile={typo: "x"})
