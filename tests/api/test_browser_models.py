"""Output format and browser profiles speak the documented vocabulary."""

import inspect
import textwrap

import httpx
import pytest
from pydantic import ValidationError

from app.models.crawl_policy import MAX_BROWSER_ACTIONS
from app.models.crawl_request import CrawlRequest
from app.vocabulary.browser import (
    ACTIONS_REQUIRING_SELECTOR,
    ALLOWED_BROWSER_ACTION_TYPES,
    BROWSER_ACTION_TYPES,
    BROWSER_PROFILE_KEYS,
)


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

        args = get_args(CrawlRequest.model_fields["output_format"].annotation)
        enforced = {value for arg in args for value in get_args(arg) if isinstance(value, str)}
        assert enforced == set(self.DOCUMENTED), f"documented and enforced sets disagree: {enforced}"

    @pytest.mark.parametrize("value", DOCUMENTED)
    def test_each_documented_value_validates(self, value):

        assert CrawlRequest(url="https://example.com/", output_format=value).output_format == value

    def test_absent_still_means_the_default(self):

        assert CrawlRequest(url="https://example.com/").output_format is None
        assert CrawlRequest(url="https://example.com/", output_format=None).output_format is None

    @pytest.mark.parametrize("typo", ["jsonlines", "JSON", "csv2", "md", "yaml", ""])
    def test_a_value_outside_the_set_is_refused(self, typo):

        with pytest.raises(ValidationError):
            CrawlRequest(url="https://example.com/", output_format=typo)

    def test_the_refusal_names_the_field(self):
        """A caller whose only mistake was a typo must be told WHICH field.

        Asserted here because a model-level rejection reports loc=() and the
        API derives its error code from where pydantic points -- so a bare
        "invalid request" would name nothing the caller can act on.
        """

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
        from app.vocabulary.browser import ALLOWED_BROWSER_ACTION_TYPES

        assert set(self.ALLOWED) == set(ALLOWED_BROWSER_ACTION_TYPES)

    @pytest.mark.parametrize("action_type", ALLOWED)
    def test_each_allowed_action_validates(self, action_type):
        from app.models.crawl_policy import CrawlPolicy

        profile = CrawlPolicy(browser_profile={"actions": [{"type": action_type, "selector": "#x"}]})
        assert profile.browser_profile["actions"][0]["type"] == action_type

    def test_execute_js_stays_refused_with_its_reasoning_intact(self):
        """The RCE refusal must survive, and must still be about execute_js."""
        from app.models.crawl_policy import CrawlPolicy

        with pytest.raises(ValidationError, match="execute_js"):
            CrawlPolicy(browser_profile={"actions": [{"type": "execute_js", "value": "fetch('x')"}]})

    @pytest.mark.parametrize("typo", ["clik", "Execute_JS", "fill_text", "", "CLICK_ALL"])
    def test_an_unrecognised_action_type_is_refused(self, typo):
        from app.models.crawl_policy import CrawlPolicy

        with pytest.raises(ValidationError) as exc:
            CrawlPolicy(browser_profile={"actions": [{"type": typo, "selector": "#x"}]})
        message = str(exc.value)
        assert "actions" in message, message

    def test_the_refusal_names_the_allowed_vocabulary(self):
        """A refusal that does not say what IS allowed leaves the caller guessing.

        The message must list the accepted types, so a caller can correct the
        call without reading this service's source.
        """
        from app.models.crawl_policy import CrawlPolicy

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
        from app.models.crawl_policy import CrawlPolicy

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
        from app.models.crawl_policy import CrawlPolicy

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
                # profile.get("k") puts the key first; _profile_int(profile, "k", …) puts the profile first.
                # Both read a literal key, but not from the same argument position.
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
        from app.models.crawl_policy import CrawlPolicy

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
        from app.models.crawl_policy import CrawlPolicy

        with pytest.raises(ValidationError):
            CrawlPolicy(browser_profile={"actions": 5})

    def test_a_bare_string_is_not_a_list_of_actions(self):
        from app.models.crawl_policy import CrawlPolicy

        with pytest.raises(ValidationError):
            CrawlPolicy(browser_profile={"actions": "click"})

    @pytest.mark.parametrize("action_type", sorted(ACTIONS_REQUIRING_SELECTOR))
    def test_an_action_that_names_a_target_must_carry_one(self, action_type):
        from app.models.crawl_policy import CrawlPolicy

        with pytest.raises(ValidationError):
            CrawlPolicy(browser_profile={"actions": [{"type": action_type}]})

    @pytest.mark.parametrize("action_type", sorted(ALLOWED_BROWSER_ACTION_TYPES - ACTIONS_REQUIRING_SELECTOR))
    def test_an_action_needing_no_target_still_validates(self, action_type):
        from app.models.crawl_policy import CrawlPolicy

        assert CrawlPolicy(browser_profile={"actions": [{"type": action_type}]})

    def test_actions_is_bounded(self):
        """The key allowlist bounds key COUNT; the values were unbounded.

        Execution is already bounded by the render budget, so this is a parse
        and memory cost rather than unbounded work -- but a caller can still
        hand the model a 200,000-element action list, which is the same shape as
        the unbounded ``urls`` collection the ceiling above already refuses.
        """
        from app.models.crawl_policy import CrawlPolicy

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
    ``app.vocabulary.browser`` and once as an if/elif chain in the renderer. The previous
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

        from app.transport.browser_driver import BrowserDriver

        source = inspect.getsource(BrowserDriver._dispatch_action)
        tree = ast.parse(textwrap.dedent(source))
        recognised: set[str] = set()
        requiring: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            test = node.test
            # A guarded branch reads ``action_type == "click" and selector``, so its test is a BoolOp whose
            # first value
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
