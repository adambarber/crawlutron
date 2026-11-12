"""Output format and browser profiles speak the documented vocabulary."""

import httpx
import pytest
from pydantic import ValidationError

from crawlutron.models.browser_profile_record import BrowserProfileRecord
from crawlutron.models.crawl_policy_vocabulary import MAX_BROWSER_ACTIONS
from crawlutron.models.crawl_request import CrawlRequest
from crawlutron.vocabulary.browser import (
    ACTIONS_REQUIRING_SELECTOR,
    ALLOWED_BROWSER_ACTION_TYPES,
    BROWSER_ACTION_TYPES,
    BROWSER_PROFILE_KEYS,
)


class TestOutputFormatIsTheDocumentedSet:
    """``output_format`` describes itself as an enum, so it is one.

    ``str | None`` with an enumerating description let typos fall back to JSON.
    The census found one such field; the fix is deliberately not a sweep."""

    DOCUMENTED = ("json", "jsonl", "ndjson", "markdown", "csv")

    def test_the_documented_set_is_what_the_type_accepts(self):
        """The description and the annotation must state the same set.

        The drift guard for the field: description is what a caller reads,
        Literal is what is enforced, and the failure is the two disagreeing."""
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

        A model-level rejection reports ``loc=()`` and the API derives its
        error code from where pydantic points -- a bare message names nothing."""

        with pytest.raises(ValidationError) as exc:
            CrawlRequest(url="https://example.com/", output_format="jsonlines")
        assert any("output_format" in str(error["loc"]) for error in exc.value.errors())


class TestBrowserProfileVocabularyIsClosed:
    """An open profile means every future dangerous key is a separate audit.

    ``browser_profile`` was ``dict[str, Any]`` with one key checked; the
    vocabulary is asserted against the renderer's chain, not a third copy."""

    ALLOWED = ("click", "fill", "hover", "scroll", "scroll_bottom", "wait", "wait_seconds")

    def test_the_allowed_set_is_what_the_renderer_recognises(self):
        """The model must not permit an action the renderer cannot perform.

        A type in the model but not the renderer is the worst pairing: accepted
        at the boundary, then silently skipped at the point of use.
        """
        from crawlutron.vocabulary.browser import ALLOWED_BROWSER_ACTION_TYPES

        assert set(self.ALLOWED) == set(ALLOWED_BROWSER_ACTION_TYPES)

    @pytest.mark.parametrize("action_type", ALLOWED)
    def test_each_allowed_action_validates(self, action_type):
        from crawlutron.models.crawl_policy import CrawlPolicy

        profile = CrawlPolicy(browser_profile={"actions": [{"type": action_type, "selector": "#x"}]})
        actions = profile.browser_profile["actions"]
        assert isinstance(actions, list)
        first = actions[0]
        assert isinstance(first, dict)
        assert first["type"] == action_type

    def test_execute_js_stays_refused_with_its_reasoning_intact(self):
        """The RCE refusal must survive, and must still be about execute_js."""
        from crawlutron.models.crawl_policy import CrawlPolicy

        with pytest.raises(ValidationError, match="execute_js"):
            CrawlPolicy(browser_profile={"actions": [{"type": "execute_js", "value": "fetch('x')"}]})

    @pytest.mark.parametrize("typo", ["clik", "Execute_JS", "fill_text", "", "CLICK_ALL"])
    def test_an_unrecognised_action_type_is_refused(self, typo):
        from crawlutron.models.crawl_policy import CrawlPolicy

        with pytest.raises(ValidationError) as exc:
            CrawlPolicy(browser_profile={"actions": [{"type": typo, "selector": "#x"}]})
        message = str(exc.value)
        assert "actions" in message, message

    def test_the_refusal_names_the_allowed_vocabulary(self):
        """A refusal that does not say what IS allowed leaves the caller guessing.

        The message must list the accepted types, so a caller can correct the
        call without reading this service's source.
        """
        from crawlutron.models.crawl_policy import CrawlPolicy

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
        from crawlutron.models.crawl_policy import CrawlPolicy

        assert CrawlPolicy(browser_profile={"actions": [{"type": "CLICK", "selector": "#x"}]})

    @pytest.mark.parametrize("key", sorted(BROWSER_PROFILE_KEYS))
    def test_every_allowed_profile_key_is_accepted(self, key):
        """Parametrised over the constant, not over a hand-kept list of keys: the replaced list included
        ``state``, which nothing in ``app/`` reads, so a key added to the constant must earn a reader.
        Values are typed (BrowserProfileRecord), so each key carries a value its declared type admits.
        """
        from crawlutron.models.crawl_policy import CrawlPolicy

        typed_values: BrowserProfileRecord = {
            "actions": [],
            "auto_accept_cookie_consent": True,
            "cookie_harvest_timeout_seconds": 5,
            "include_html": True,
            "include_screenshot": True,
            "lazy_scroll_delay_seconds": 1,
            "lazy_scroll_steps": 3,
            "resource_mode": "reduced",
            "screenshot_selectors": ["#x"],
            "storage_state": "s.json",
            "viewport": "mobile",
        }
        profile = CrawlPolicy(browser_profile=typed_values)
        written = dict(profile.browser_profile)
        expected = dict(typed_values)
        assert written[key] == expected[key]

    def test_no_allowed_profile_key_is_inert(self):
        """Every allowed key must have a reader, or allowing it is a lie.

        ``state`` was allowed and read nowhere; each key must appear as a
        literal read somewhere in ``crawlutron/``, asserted directly."""
        import ast
        import pathlib

        app_root = pathlib.Path(__file__).resolve().parents[2] / "crawlutron"
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
        from crawlutron.models.crawl_policy import CrawlPolicy

        with pytest.raises(ValidationError):
            CrawlPolicy(browser_profile={typo: "x"})


class TestBrowserProfileActionsAreWellFormed:
    """A recognised action name is not the same as a usable action.

    ``click`` without a selector returned 200, having done nothing; a
    non-iterable ``actions`` value escaped the handler as a 500."""

    def test_actions_must_be_a_list(self):
        """A non-list is a caller mistake to report, not a crash to propagate."""
        from crawlutron.models.crawl_policy import CrawlPolicy

        with pytest.raises(ValidationError):
            CrawlPolicy(browser_profile={"actions": 5})

    def test_a_bare_string_is_not_a_list_of_actions(self):
        from crawlutron.models.crawl_policy import CrawlPolicy

        with pytest.raises(ValidationError):
            CrawlPolicy(browser_profile={"actions": "click"})

    @pytest.mark.parametrize("action_type", sorted(ACTIONS_REQUIRING_SELECTOR))
    def test_an_action_that_names_a_target_must_carry_one(self, action_type):
        from crawlutron.models.crawl_policy import CrawlPolicy

        with pytest.raises(ValidationError):
            CrawlPolicy(browser_profile={"actions": [{"type": action_type}]})

    @pytest.mark.parametrize("action_type", sorted(ALLOWED_BROWSER_ACTION_TYPES - ACTIONS_REQUIRING_SELECTOR))
    def test_an_action_needing_no_target_still_validates(self, action_type):
        from crawlutron.models.crawl_policy import CrawlPolicy

        assert CrawlPolicy(browser_profile={"actions": [{"type": action_type}]})

    def test_actions_is_bounded(self):
        """The key allowlist bounds key COUNT; the values were unbounded.

        Execution is render-budget-bounded, so this is a parse and memory cost;
        a caller could still hand the model a 200,000-element action list."""
        from crawlutron.models.crawl_policy import CrawlPolicy

        one = {"type": "click", "selector": "#a"}
        assert CrawlPolicy(browser_profile={"actions": [one] * MAX_BROWSER_ACTIONS})
        with pytest.raises(ValidationError):
            CrawlPolicy(browser_profile={"actions": [one] * (MAX_BROWSER_ACTIONS + 1)})

    async def test_a_broken_actions_value_is_a_422_on_the_wire(self, async_client: httpx.AsyncClient):
        """The exception has to be one the handler converts, not re-raises.

        A ``TypeError`` out of a validator surfaces as a 500, a ``ValueError``
        as a 422, and only the second is this field's promised behaviour."""
        response = await async_client.post(
            "/v1/crawl",
            json={"url": "https://example.com/", "policy": {"browser_profile": {"actions": 5}}},
        )
        assert response.status_code == 422, response.text


class TestTheRendererAgreesWithTheVocabulary:
    """The vocabulary table is only the source of truth if the renderer reads it.

    The closure refuses an action the renderer does not implement -- one
    decision written twice; these read the chain's own AST, not a third copy."""

    @staticmethod
    def _chain() -> tuple[set[str], set[str]]:
        """The dispatch table's recognised and selector-requiring sets."""
        from crawlutron.transport.browser_driver import ACTION_STEPS

        recognised = set(ACTION_STEPS)
        requiring = {name for name, step in ACTION_STEPS.items() if step.needs_selector}
        return recognised, requiring

    def test_every_recognised_action_type_is_in_the_table(self):
        recognised, _ = self._chain()
        assert recognised == set(BROWSER_ACTION_TYPES), (
            f"the chain and the table disagree: table-only={set(BROWSER_ACTION_TYPES) - recognised}, "
            f"chain-only={recognised - set(BROWSER_ACTION_TYPES)}"
        )

    def test_the_requiring_set_is_the_chain_own_guard(self):
        """``ACTIONS_REQUIRING_SELECTOR`` must be what the chain skips without."""
        _, requiring = self._chain()
        declared = set(ACTIONS_REQUIRING_SELECTOR)
        assert requiring == declared, (
            "the chain's guards and the model's requirement set disagree: "
            f"model-only={declared - requiring}, chain-only={requiring - declared}"
        )

    def test_the_dispatch_handlers_are_typed_against_the_closed_vocabulary(self):
        """An action reaches a handler as the declared ``BrowserAction``, not.
        ``dict[str, Any]``: the closed vocabulary must be the type, so a caller
        that hands the chain an undeclared action key is a type error."""
        import ast

        from tests.chokepoint_walks import APP_ROOT

        tree = ast.parse((APP_ROOT / "transport" / "browser_driver.py").read_text(encoding="utf-8"))
        action_class = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "_ActionStep")
        annotation = next(
            stmt.annotation
            for stmt in ast.walk(action_class)
            if isinstance(stmt, ast.AnnAssign) and getattr(stmt.target, "id", None) == "handler"
        )
        source = ast.unparse(annotation)
        assert "BrowserAction" in source and "dict[str, Any]" not in source, (
            f"the dispatch handler must be typed on the closed vocabulary, got: {source}"
        )
