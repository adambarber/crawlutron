"""Crawl/render requests take exactly one of url/urls.

Both set silently ignored url; neither failed downstream in _payload_urls.
Fail fast at the model boundary instead.
"""

import inspect
import textwrap
import typing

import httpx
import pytest
from httpx import AsyncClient
from pydantic import BaseModel, ValidationError

from app import api_models, api_responses
from app.api_models import MAX_BROWSER_ACTIONS, CrawlRequest, CrawlStrategy, RenderRequest
from app.constants import (
    ACTIONS_REQUIRING_SELECTOR,
    ALLOWED_BROWSER_ACTION_TYPES,
    BROWSER_ACTION_TYPES,
    BROWSER_PROFILE_KEYS,
)
from tests._app_factory import service_app
from tests.chokepoint_walks import APP_ROOT


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


#: Both wire-model modules. The output half of the split moved to
#: ``app.api_responses``, and a walk that only looks at ``app.api_models`` no
#: longer sees a single response model -- which is what the response-side tests
#: below were written to cover. The set is a list of modules rather than a list
#: of class names so a model is covered the day it is written, and a module
#: added to the split is covered by editing one line.
_WIRE_MODEL_MODULES = (api_models, api_responses)


def _declared_models() -> dict[str, type[BaseModel]]:
    """Every request/response model the wire layer declares, across both modules."""
    return {
        name: obj
        for module in _WIRE_MODEL_MODULES
        for name, obj in vars(module).items()
        if inspect.isclass(obj)
        and issubclass(obj, BaseModel)
        and obj is not BaseModel
        and obj.__module__ == module.__name__
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

    @pytest.mark.parametrize("key", sorted(BROWSER_PROFILE_KEYS))
    def test_every_allowed_profile_key_is_accepted(self, key):
        """Parametrised over the constant, not over a hand-kept list of keys.

        The list this replaced included ``state``, which nothing in ``app/``
        reads -- so the test was asserting that a key the service ignores is
        accepted, which is the inert-input failure the closure exists to
        prevent. Deriving the parameters from the constant means a key added to
        it has to earn a reader, and one removed cannot keep a test alive.
        """
        from app.api_models import CrawlPolicy

        value: object = [] if key == "actions" else "desktop"
        assert CrawlPolicy(browser_profile={key: value}).browser_profile[key] == value

    def test_no_allowed_profile_key_is_inert(self):
        """Every allowed key must have a reader, or allowing it is a lie.

        ``state`` was allowed and read nowhere, so a caller who set it got no
        error and no effect -- indistinguishable from a key that worked. This
        asserts the property directly: each key appears as a literal read
        somewhere in ``app/``.
        """
        import ast
        import pathlib

        app_root = pathlib.Path(__file__).resolve().parents[2] / "app"
        reads: set[str] = set()
        for path in app_root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                # profile.get("k") puts the key first; _profile_int(profile, "k", …)
                # puts the profile first. Both read a literal key, but not from the
                # same argument position.
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name != "get" and not name.startswith("_profile_"):
                    continue
                args = node.args[0:1] if name == "get" else node.args[1:2]
                for arg in args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        reads.add(arg.value)
        inert = sorted(BROWSER_PROFILE_KEYS - reads)
        assert not inert, f"allowed but never read, so inert: {inert}"

    @pytest.mark.parametrize("typo", ["viewports", "resourceMode", "user_agent_family", "exec_js"])
    def test_an_unknown_profile_key_is_refused(self, typo):
        """A misspelled profile key is inert, so it must not pass silently."""
        from app.api_models import CrawlPolicy

        with pytest.raises(ValidationError):
            CrawlPolicy(browser_profile={typo: "x"})


class TestBrowserProfileActionsAreWellFormed:
    """A recognised action name is not the same as a usable action.

    The vocabulary closure proves a name is one the renderer implements. It
    proved nothing about whether the action carries what that name needs: the
    dispatch chain guards its target-taking branches with ``and selector``, so
    a ``click`` arriving without a selector falls past every branch and returns
    having done nothing. The request returns 200 with a page that loaded and no
    interaction performed -- the caller cannot tell that from success.

    Two shapes reach that outcome, and both were accepted:

    - ``{"actions": 5}``. The closure iterated the value directly, so a
      non-iterable raised ``TypeError``. Pydantic wraps ``ValueError`` into a
      422 and does not wrap ``TypeError``, so the exception escaped the request
      handler entirely -- one caller typo became an HTTP 500 on every policy-
      bearing endpoint, and an uncaught traceback over MCP.
    - ``{"actions": [{"type": "click"}]}``, which validated cleanly and then
      did nothing at all.
    """

    def test_actions_must_be_a_list(self):
        """A non-list is a caller mistake to report, not a crash to propagate."""
        from app.api_models import CrawlPolicy

        with pytest.raises(ValidationError):
            CrawlPolicy(browser_profile={"actions": 5})

    def test_a_bare_string_is_not_a_list_of_actions(self):
        from app.api_models import CrawlPolicy

        with pytest.raises(ValidationError):
            CrawlPolicy(browser_profile={"actions": "click"})

    @pytest.mark.parametrize("action_type", sorted(ACTIONS_REQUIRING_SELECTOR))
    def test_an_action_that_names_a_target_must_carry_one(self, action_type):
        from app.api_models import CrawlPolicy

        with pytest.raises(ValidationError):
            CrawlPolicy(browser_profile={"actions": [{"type": action_type}]})

    @pytest.mark.parametrize("action_type", sorted(ALLOWED_BROWSER_ACTION_TYPES - ACTIONS_REQUIRING_SELECTOR))
    def test_an_action_needing_no_target_still_validates(self, action_type):
        from app.api_models import CrawlPolicy

        assert CrawlPolicy(browser_profile={"actions": [{"type": action_type}]})

    def test_actions_is_bounded(self):
        """The key allowlist bounds key COUNT; the values were unbounded.

        Execution is already bounded by the render budget, so this is a parse
        and memory cost rather than unbounded work -- but a caller can still
        hand the model a 200,000-element action list, which is the same shape as
        the unbounded ``urls`` collection the ceiling above already refuses.
        """
        from app.api_models import CrawlPolicy

        one = {"type": "click", "selector": "#a"}
        assert CrawlPolicy(browser_profile={"actions": [one] * MAX_BROWSER_ACTIONS})
        with pytest.raises(ValidationError):
            CrawlPolicy(browser_profile={"actions": [one] * (MAX_BROWSER_ACTIONS + 1)})

    async def test_a_broken_actions_value_is_a_422_on_the_wire(self, async_client: httpx.AsyncClient):
        """The exception has to be one the handler converts, not one it re-raises.

        Asserted on the wire rather than only against the model because the two
        failure shapes are indistinguishable there: a ``TypeError`` out of a
        validator surfaces as a 500, a ``ValueError`` as a 422, and only the
        second is the behaviour this field promises.
        """
        response = await async_client.post(
            "/v1/crawl",
            json={"url": "https://example.com/", "policy": {"browser_profile": {"actions": 5}}},
        )
        assert response.status_code == 422, response.text


class TestTheRendererAgreesWithTheVocabulary:
    """The vocabulary table is only the source of truth if the renderer reads it.

    The closure refuses an action type the renderer does not implement, so the
    two are one decision that happens to be written twice: once as a table in
    ``app.constants`` and once as an if/elif chain in the renderer. The previous
    guard compared the table to a tuple written in this test file, so it was a
    third copy that could not see the chain at all -- adding a member to the
    table without adding an ``elif`` reproduced the exact defect the closure
    exists to prevent, and the test stayed green.

    These read the chain's own AST, so the guard fails when the renderer changes
    rather than when this file is edited.
    """

    @staticmethod
    def _dispatch_chain() -> tuple[set[str], set[str]]:
        """Return ``(types the chain recognises, of which the ones needing a selector)``.

        Parsed rather than executed: the branches are the contract, and running
        them would need a live page. ``and selector`` in a branch test is how the
        chain expresses "this action needs a target", so a branch carrying it is
        exactly a member of the requiring set.
        """
        import ast
        import inspect

        from app.crawl.strategies.browser_actions import BrowserActionsMixin

        source = inspect.getsource(BrowserActionsMixin._dispatch_action)
        tree = ast.parse(textwrap.dedent(source))
        recognised: set[str] = set()
        requiring: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            test = node.test
            # A guarded branch reads ``action_type == "click" and selector``, so its
            # test is a BoolOp whose first value is the comparison; an unguarded one
            # reads ``action_type == "wait_seconds"`` and the test IS the comparison.
            guarded = isinstance(test, ast.BoolOp)
            comparison = test.values[0] if guarded else test
            if not (
                isinstance(comparison, ast.Compare)
                and len(comparison.comparators) == 1
                and isinstance(comparison.comparators[0], ast.Constant)
                and isinstance(comparison.comparators[0].value, str)
                and isinstance(comparison.left, ast.Name)
                and comparison.left.id == "action_type"
            ):
                continue
            name = comparison.comparators[0].value
            recognised.add(name)
            if guarded:
                requiring.add(name)
        return recognised, requiring

    def test_every_recognised_action_type_is_in_the_table(self):
        recognised, _ = self._dispatch_chain()
        assert recognised == set(BROWSER_ACTION_TYPES), (
            f"the chain and the table disagree: table-only={set(BROWSER_ACTION_TYPES) - recognised}, "
            f"chain-only={recognised - set(BROWSER_ACTION_TYPES)}"
        )

    def test_the_requiring_set_is_the_chain_own_guard(self):
        """``ACTIONS_REQUIRING_SELECTOR`` must be what the chain skips without."""
        _, requiring = self._dispatch_chain()
        declared = set(ACTIONS_REQUIRING_SELECTOR)
        assert requiring == declared, (
            "the chain's ``and selector`` guards and the model's requirement set disagree: "
            f"model-only={declared - requiring}, chain-only={requiring - declared}"
        )


class TestRequestProfileVocabularyIsClosed:
    """``request_profile`` is ``dict[str, Any]``, the same shape ``browser_profile`` had.

    Every other caller-supplied mapping on this model is closed: ``headers``
    refuses a banned name, ``credentials`` refuses a colliding origin, and
    ``browser_profile`` refuses a key that is not in the table. This one was
    left open, so a misspelling is not a refusal but a silent no-op:

    - ``{"cookie_jarr": "per_host"}`` disables the per-host jar and reports 200.
    - ``{"fingerprint_rotatoin": True}`` disables fingerprint rotation and
      reports 200.

    The consumer reads eight keys. The field description advertised a ninth,
    ``accept_language``, which is not one of them -- the code reads the plural
    ``accept_languages`` -- so the prose was advertising a key that did nothing,
    the same failure as the inert ``state`` key removed in #61.
    """

    #: The keys the profile consumer actually reads.
    READ_KEYS = frozenset(
        {
            "accept_languages",
            "cookie_jar",
            "cookie_namespace",
            "cookies",
            "fingerprint_rotation",
            "fingerprint_seed",
            "user_agent_family",
            "user_agents",
        }
    )

    def test_an_unknown_key_is_refused(self):
        from app.api_models import CrawlPolicy

        with pytest.raises(ValidationError, match="request_profile keys not permitted"):
            CrawlPolicy(request_profile={"totally_bogus": 1})

    def test_a_misspelled_key_is_refused_rather_than_ignored(self):
        """The failure this exists for: a typo that reports success and does nothing."""
        from app.api_models import CrawlPolicy

        for typo in ("cookie_jarr", "fingerprint_rotatoin", "accept_language"):
            with pytest.raises(ValidationError, match="request_profile keys not permitted"):
                CrawlPolicy(request_profile={typo: "per_host"})

    def test_every_allowed_key_has_a_reader_in_app(self):
        """A key nothing reads is a caller-visible no-op, which is what #61 removed.

        ``state`` sat in the browser profile allowlist for the whole life of the
        closed vocabulary with zero readers. This asserts the property for the
        request profile so the same class cannot reappear.
        """
        from app.api_models import CrawlPolicy

        field = CrawlPolicy.model_fields["request_profile"]
        # ``.get`` rather than ``getattr(..., "keys")``: a dict's own ``keys``
        # attribute would satisfy the getattr and hand back the bound method.
        declared = (field.json_schema_extra or {}).get("keys")
        assert declared, "request_profile does not declare its key set"

        app = "\n".join(path.read_text() for path in sorted(APP_ROOT.rglob("*.py")))
        inert = sorted(key for key in declared if f'"{key}"' not in app and f"'{key}'" not in app)
        assert not inert, f"request_profile allows keys nothing reads: {inert}"

    def test_the_description_names_exactly_the_allowed_keys(self):
        """Prose advertising a key that does not exist is the ``state`` bug in prose.

        This is what let ``accept_language`` survive: nothing compared the
        description against the table, so a key could be advertised and inert at
        the same time.
        """
        from app.api_models import CrawlPolicy

        field = CrawlPolicy.model_fields["request_profile"]
        description = field.description or ""
        named = {
            key
            for key in (
                "accept_languages",
                "cookie_jar",
                "cookie_namespace",
                "cookies",
                "fingerprint_rotation",
                "fingerprint_seed",
                "user_agent_family",
                "user_agents",
            )
            if key in description
        }
        assert named, f"the description names none of the allowed keys: {description!r}"


class TestRequestProfileCookiesCannotBypassTheNamespaceChokepoint:
    """``headers`` refuses a raw ``Cookie``. ``request_profile.cookies`` builds one anyway.

    The ban's stated reason is that "the cookie jar owns Cookie; a raw header
    bypasses the cookie-namespace chokepoint". A caller-supplied nested dict
    assembled into the same header at ``_headers_for_strategy`` is that bypass,
    reached by a different door -- and the ban cannot see it, because it only
    inspects ``headers``.

    Unbounded, it is worse than a large header. ``content_cache_key`` serialises
    the whole header map into the cache key, so every distinct cookie string is a
    distinct cache slot holding a full response body. A 500-host cookie map
    produced a 14,088-character ``Cookie`` header, and 200,000 host entries
    parsed into one.
    """

    #: Most hosts a static cookie map may name.
    MAX_COOKIE_HOSTS = 32
    #: Most cookies in one host's map.
    MAX_COOKIE_PAIRS = 32

    def _profile(self, cookies: dict):
        from app.api_models import CrawlPolicy

        return CrawlPolicy(
            request_profile={"cookie_jar": "per_host", "cookie_namespace": "t1", "cookies": cookies}
        )

    def test_a_cookIe_map_at_the_ceiling_is_accepted(self):
        at_ceiling = {f"h{index}.example": {"session": "v"} for index in range(self.MAX_COOKIE_HOSTS)}
        assert len(self._profile(at_ceiling).request_profile["cookies"]) == self.MAX_COOKIE_HOSTS

    def test_a_cookie_map_over_the_host_ceiling_is_refused(self):
        over = {f"h{index}.example": {"session": "v"} for index in range(self.MAX_COOKIE_HOSTS + 1)}
        with pytest.raises(ValidationError, match="at most"):
            self._profile(over)

    def test_a_host_carrying_too_many_cookies_is_refused(self):
        """The ceiling is per host, not only in total: one host is the common case."""
        over = {"h.example": {f"k{i}": "v" for i in range(self.MAX_COOKIE_PAIRS + 1)}}
        with pytest.raises(ValidationError, match="at most"):
            self._profile(over)

    def test_a_non_mapping_cookie_map_is_refused(self):
        """Refused as a refusal. A ``TypeError`` here is a 500, not a 422."""
        for bad in ([1, 2], "session=1", 5):
            with pytest.raises(ValidationError):
                self._profile(bad)  # type: ignore[arg-type]

    def test_a_host_entry_that_is_not_a_mapping_is_refused(self):
        with pytest.raises(ValidationError):
            self._profile({"h.example": "session=1"})  # type: ignore[dict-item]

    def test_the_un_namespaced_single_tenant_path_still_works(self):
        """One caller, one session, no namespace -- the path ``cookie_jar`` promises to keep.

        An earlier version of this validator refused a cookie map with no
        ``cookie_namespace``, on the theory that an unnamed namespace is the
        shared jar and so mixes tenants. That refused the documented
        single-tenant path, and the theory does not hold: a namespace is a string
        the caller picks, and this service has no caller authentication, so it is
        a tenant key rather than a boundary. There is no value to refusing here
        that a caller could act on.
        """
        from app.api_models import CrawlPolicy

        profile = CrawlPolicy(
            request_profile={"cookie_jar": "per_host", "cookies": {"e.example": {"a": "1"}}}
        )
        assert profile.request_profile["cookies"] == {"e.example": {"a": "1"}}

    def test_a_namespace_is_itself_bounded(self):
        """A namespace is a dict key in a process-wide jar, so it is a size knob too."""
        from app.api_models import CrawlPolicy

        with pytest.raises(ValidationError, match="cookie_namespace"):
            CrawlPolicy(request_profile={"cookie_namespace": "t" * 500})


class TestRequestProfileValueTypesMatchTheHeaderMapTheyCorrupt:
    """``_headers_for_strategy`` is annotated ``-> dict[str, str]`` and is handed a ``dict[str, Any]``.

    Nothing checked the value types, so a pool entry that is not a string lands
    in the header map the HTTP client is about to serialise:

    - ``{"user_agents": [{"x": 1}]}`` puts a ``dict`` in ``User-Agent``.
    - ``{"accept_languages": [[1, 2]]}`` puts a ``list`` in ``Accept-Language``.
    - ``{"user_agents": [None]}`` raises ``TypeError`` from inside the header
      builder -- the same shape as #58, one module over.
    """

    def test_a_pool_must_be_a_list_of_strings(self):
        """One test over the two shapes, not one per shape.

        ``user_agents`` and ``accept_languages`` are the same reader reached
        through two keys, so a case that failed for one failed for the other;
        splitting them produced two tests with a duplicated body and no way to
        add a third key without a third copy.
        """
        from app.api_models import CrawlPolicy

        for key in ("user_agents", "accept_languages"):
            for bad in ({"a_string": "Mozilla"}, {"an_entry": [{"x": 1}]}, {"a_none_entry": [None]}):
                with pytest.raises(ValidationError, match=key):
                    CrawlPolicy(request_profile={key: bad})

    def test_a_pool_at_the_ceiling_is_accepted(self):
        from app.api_models import CrawlPolicy

        pool = [f"agent-{index}" for index in range(64)]
        assert len(CrawlPolicy(request_profile={"user_agents": pool}).request_profile["user_agents"]) == 64

    def test_an_over_sized_pool_is_refused(self):
        from app.api_models import CrawlPolicy

        with pytest.raises(ValidationError, match="at most"):
            CrawlPolicy(request_profile={"user_agents": [f"agent-{i}" for i in range(65)]})

    def test_the_seed_and_family_must_be_strings(self):
        """``fingerprint_seed`` is interpolated into a hash and ``user_agent_family``
        is read with ``str()``, so a dict reaches the hash instead of a string."""
        from app.api_models import CrawlPolicy

        for key in ("fingerprint_seed", "user_agent_family", "cookie_jar", "fingerprint_rotation"):
            with pytest.raises(ValidationError, match=key):
                CrawlPolicy(request_profile={key: {"not": "a scalar"}})


class TestBrowserActionKeysAreClosed:
    """The action *type* vocabulary is closed. The keys inside an action are not.

    ``_close_browser_profile_vocabulary`` refuses a top-level key that is not in
    the table and an action ``type`` that is not in the vocabulary, then hands
    each action dict to the chain, which reads four keys out of it. Nothing
    checks those. So::

        {"type": "fill", "selector": "#q", "val": "hi"}

    is accepted, and the chain fills an empty string -- a 200 on a page that
    loaded with no interaction performed, which is exactly the silent no-op the
    ``type`` check exists to prevent, one level down.

    This is the ``state`` key again (#61) and the ``accept_language``
    description (#64): the same failure, at the third place it can hide.
    """

    #: The keys the action chain reads, per ``browser_actions.py``.
    READ_KEYS = frozenset({"selector", "timeout", "seconds", "value"})

    def test_an_unknown_action_key_is_refused(self):
        from app.api_models import CrawlPolicy

        with pytest.raises(ValidationError, match="action keys not permitted"):
            CrawlPolicy(
                browser_profile={"actions": [{"type": "click", "selector": "#x", "url": "http://e/"}]}
            )

    def test_a_misspelled_action_key_is_refused_rather_than_ignored(self):
        """``val`` for ``value`` is the reported case: the action then does nothing."""
        from app.api_models import CrawlPolicy

        with pytest.raises(ValidationError, match="action keys not permitted"):
            CrawlPolicy(browser_profile={"actions": [{"type": "fill", "selector": "#q", "val": "hi"}]})

    def test_a_permitted_action_still_validates(self):
        from app.api_models import CrawlPolicy

        for action in (
            {"type": "click", "selector": "#x"},
            {"type": "fill", "selector": "#q", "value": "hi"},
            {"type": "wait", "selector": "#x", "timeout": 1000},
            {"type": "wait_seconds", "seconds": 2},
            {"type": "scroll_bottom"},
        ):
            policy = CrawlPolicy(browser_profile={"actions": [action]})
            assert policy.browser_profile["actions"][0]["type"] == action["type"]

    def test_the_chain_reads_exactly_the_declared_keys(self):
        """The drift guard: a reader added without a declaration fails here.

        Read off the chain's own ``action.get(...)`` calls rather than restated,
        because the whole failure of this issue is a key the consumer reads and
        the producer never named. Restating the list here would just be a second
        place to forget.
        """
        import ast

        from app.constants import BROWSER_ACTION_KEYS
        from tests.chokepoint_walks import STRATEGIES_DIR, attribute_calls, string_argument

        source = (STRATEGIES_DIR / "browser_actions.py").read_text()
        read: set[str] = set()
        for node, func in attribute_calls(ast.parse(source)):
            if func.attr == "get" and isinstance(func.value, ast.Name) and func.value.id == "action":
                read.add(string_argument(node))
        read.discard(None)

        assert read == set(BROWSER_ACTION_KEYS), (
            f"the chain and the table disagree: chain-only={read - set(BROWSER_ACTION_KEYS)}, "
            f"table-only={set(BROWSER_ACTION_KEYS) - read}"
        )


class TestCallerKnobsBehindClosedKeysAreCeilinged:
    """A closed key set answers *which* keys exist. It is silent about how large their values may be.

    The browser profile's vocabulary was closed in aa46380 and the ceiling
    question was not asked, so every knob behind the closed keys took whatever
    the caller sent. Four of them, each with a place in the code that already
    said the value should be bounded:

    - ``_profile_int`` and ``_profile_float`` return ``max(0, value)`` with no
      upper clamp, so ``lazy_scroll_steps: 10**12`` is accepted.
    - ``_lazy_load_scroll`` is called with no budget, immediately below
      ``_run_action_chain``, which is passed ``budget_seconds=navigation_timeout``
      and whose docstring says production must pass a budget. The one call site
      documented as needing a ceiling is the one without one.
    - ``cookie_harvest_timeout_seconds`` is the ``wait_for`` timeout, and the
      comment at that call site says the harvest must not hold the pooled
      renderer for the per-URL budget.
    - ``screenshot_selectors`` has no ``max_length``; each entry appends a
      base64 PNG to the result metadata, so it is a response-size knob too.

    All four returned 200. The 900 s chain budget bounds wall clock and none of
    these.
    """

    #: Longest scroll a lazy-load pass will take, in steps. Deep pages still load
    #: in a few dozen; this is past the point of usefulness, not the point of cost.
    MAX_LAZY_SCROLL_STEPS = 50
    #: Longest per-step scroll pause, in seconds.
    MAX_LAZY_SCROLL_DELAY_SECONDS = 5.0
    #: Longest cookie-harvest wait, in seconds. The harvest runs in a finally
    #: block on a pooled renderer, so this is a hold on a shared resource.
    MAX_COOKIE_HARVEST_TIMEOUT_SECONDS = 5.0
    #: Most element screenshots in one render. Each is a full base64 PNG in the
    #: result metadata, so this bounds the response as well as the render.
    MAX_SCREENSHOT_SELECTORS = 16

    def test_scroll_steps_over_the_ceiling_are_refused(self):
        from app.api_models import CrawlPolicy

        with pytest.raises(ValidationError, match="lazy_scroll_steps"):
            CrawlPolicy(browser_profile={"lazy_scroll_steps": 10**12})

    def test_a_scroll_delay_over_the_ceiling_is_refused(self):
        from app.api_models import CrawlPolicy

        with pytest.raises(ValidationError, match="lazy_scroll_delay_seconds"):
            CrawlPolicy(browser_profile={"lazy_scroll_delay_seconds": 3600.0})

    def test_a_cookie_harvest_timeout_over_the_ceiling_is_refused(self):
        from app.api_models import CrawlPolicy

        with pytest.raises(ValidationError, match="cookie_harvest_timeout_seconds"):
            CrawlPolicy(browser_profile={"cookie_harvest_timeout_seconds": 10**9})

    def test_too_many_screenshot_selectors_are_refused(self):
        from app.api_models import CrawlPolicy

        over = [f"#{index}" for index in range(self.MAX_SCREENSHOT_SELECTORS + 1)]
        with pytest.raises(ValidationError, match="screenshot_selectors"):
            CrawlPolicy(browser_profile={"screenshot_selectors": over})

    def test_a_value_at_each_ceiling_is_accepted(self):
        """The ceilings are the boundary, not a lower bound the caller cannot reach."""
        from app.api_models import CrawlPolicy

        policy = CrawlPolicy(
            browser_profile={
                "lazy_scroll_steps": self.MAX_LAZY_SCROLL_STEPS,
                "lazy_scroll_delay_seconds": self.MAX_LAZY_SCROLL_DELAY_SECONDS,
                "cookie_harvest_timeout_seconds": self.MAX_COOKIE_HARVEST_TIMEOUT_SECONDS,
                "screenshot_selectors": [f"#{i}" for i in range(self.MAX_SCREENSHOT_SELECTORS)],
            }
        )
        assert policy.browser_profile["lazy_scroll_steps"] == self.MAX_LAZY_SCROLL_STEPS

    def test_the_readers_clamp_too_even_when_the_model_is_bypassed(self):
        """Defence in depth, asserted at the call path rather than at a helper.

        The model is a boundary, not the only door: by the time a profile reaches
        a renderer it is a dict, so the validator is not in the path any more.
        The readers are what actually bound the work, so they are what is
        asserted -- through ``_scroll_config_for`` and ``_harvest_timeout_for``,
        which is how the renderer calls them, rather than through
        ``_profile_int`` with no ceiling, whose contract is deliberately to pass a
        value through.
        """
        from app.constants import (
            MAX_COOKIE_HARVEST_TIMEOUT_SECONDS,
            MAX_LAZY_SCROLL_DELAY_SECONDS,
            MAX_LAZY_SCROLL_STEPS,
        )
        from app.crawl.strategies.browser_actions import BrowserActionsMixin

        class _Renderer(BrowserActionsMixin):
            _lazy_scroll_steps = 3
            _lazy_scroll_delay_seconds = 0.5
            _harvest_timeout_seconds = 2.0

        renderer = _Renderer()

        steps, delay = renderer._scroll_config_for(
            {"lazy_scroll_steps": 10**12, "lazy_scroll_delay_seconds": 10**9}
        )
        assert (steps, delay) == (MAX_LAZY_SCROLL_STEPS, MAX_LAZY_SCROLL_DELAY_SECONDS)
        assert renderer._harvest_timeout_for({"cookie_harvest_timeout_seconds": 10**9}) == (
            MAX_COOKIE_HARVEST_TIMEOUT_SECONDS
        )

        # The knob still moves inside the ceiling: a clamp that pinned every
        # value to the ceiling would pass the same assertions.
        assert renderer._scroll_config_for({"lazy_scroll_steps": 7}) == (7, 0.5)
        assert renderer._harvest_timeout_for({}) == 2.0

        # Garbage still falls back to the default rather than raising, as before.
        assert renderer._scroll_config_for({"lazy_scroll_steps": "not a number"}) == (3, 0.5)
        assert renderer._scroll_config_for({"lazy_scroll_steps": -5}) == (0, 0.5)


class TestStrategyLadderIsBounded:
    """``strategies`` is the outbound fan-out per URL, and it was the one collection left uncapped.

    ``urls`` has carried ``max_length=500`` and ``headers`` ``max_length=64`` all
    along. ``strategies`` had ``min_length=1`` and nothing above it, which reads
    as an oversight rather than a decision: it is a list of outbound network
    attempts, and the ladder runs one per entry.

    The ceiling is a ceiling and not a dedupe, deliberately. A duplicate is
    usually pure waste -- if a strategy comes back blocked, the same strategy
    blocks again, and a usable one returns immediately -- but not always: a
    browser render mints a session cookie that a later ``HTTP_CFFI_IMPERSONATE``
    entry replays, so a non-blocked-but-unusable first attempt can leave the jar
    in a state that makes its own repetition succeed. Dedupe would have thrown
    that path away. A ceiling bounds the repetition instead of declaring all of
    it worthless.
    """

    #: Most ladder entries in one request. The vocabulary is three strategies and
    #: cookie replay needs a repeat or two after a browser render, so this is
    #: several times more than any useful ladder.
    MAX_STRATEGIES = 16

    def test_a_ladder_at_the_ceiling_is_accepted(self):
        from app.api_models import CrawlPolicy

        ladder = [CrawlStrategy.HTTP_DECLARED_CRAWLER] * self.MAX_STRATEGIES
        assert len(CrawlPolicy(strategies=ladder).strategies) == self.MAX_STRATEGIES

    def test_a_ladder_over_the_ceiling_is_refused(self):
        from app.api_models import CrawlPolicy

        over = [CrawlStrategy.HTTP_DECLARED_CRAWLER] * (self.MAX_STRATEGIES + 1)
        with pytest.raises(ValidationError, match="at most"):
            CrawlPolicy(strategies=over)

    def test_an_empty_ladder_is_still_refused(self):
        """The lower bound is unchanged: an empty ladder has no strategy to run."""
        from app.api_models import CrawlPolicy

        with pytest.raises(ValidationError):
            CrawlPolicy(strategies=[])

    def test_the_ceiling_is_a_ceiling_on_work_not_on_distinct_strategies(self):
        """A repetition the caller means is allowed, up to the ceiling.

        Pins the distinction the ceiling rests on, so a later reader does not
        "fix" this by deduping and break cookie replay.
        """
        from app.api_models import CrawlPolicy

        repeated = [
            CrawlStrategy.BROWSER_HEADLESS,
            CrawlStrategy.HTTP_CFFI_IMPERSONATE,
            CrawlStrategy.HTTP_CFFI_IMPERSONATE,
        ]
        assert len(CrawlPolicy(strategies=repeated).strategies) == 3
