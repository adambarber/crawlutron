"""Copy-paste is a defect class here, not a style choice.

The review that produced this file found the same logic re-implemented in a
second place, where it then drifted. Each check names one family of copies
and fails when the second one appears -- in app/ or in the tests themselves."""

from __future__ import annotations

import ast
import contextlib
import io
import pathlib
import tokenize
from collections.abc import Iterable
from pathlib import Path

from tests.chokepoint_walks import (
    REPO_ROOT,
    TESTS_DIR,
    app_python_files,
    call_nodes,
    calls_named,
    class_defs,
    comprehensions,
    functions,
    nodes_of,
    offender_at,
    offenders_over_functions,
    relative_posix,
    resolved_expr_name,
    trees_excluding,
)
from tests.invariants._shared import _enclosing_defs
from tests.rules import Bites, Rule, Silent

# ---------------------------------------------------------------------------
# Search-provider result normalization
# ---------------------------------------------------------------------------


def _provider_loop_reinlines(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag the keyed-provider dedupe loop re-inlined outside the collector.

    The exact copy-paste shape is ``seen: set`` + ``isinstance(item, dict)``
    guard + ``SearchResult`` construction in one function. DuckDuckGo's
    provider legitimately keeps a separate lxml node-walk: it has no dict
    guard, so the sink never fires on it.
    """
    offenders: list[str] = []
    for node in nodes_of(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) or node.name == "_collect_results":
            continue
        subs = list(nodes_of(node))
        assigns_seen = any(
            isinstance(s, ast.AnnAssign) and getattr(s.target, "id", "") == "seen" for s in subs
        )
        builds_result = any(
            isinstance(s, ast.Call) and getattr(s.func, "id", "") == "SearchResult" for s in subs
        )
        guards_dict = any(
            isinstance(s, ast.Call)
            and getattr(s.func, "id", "") == "isinstance"
            and len(s.args) >= 2
            and getattr(s.args[1], "id", "") == "dict"
            for s in subs
        )
        if assigns_seen and builds_result and guards_dict:
            offenders.append(offender_at(path, node, "dedupe loop re-inlined outside _collect_results"))
    return offenders


PROVIDER_DEDUPE_LOOP_LIVES_IN_THE_COLLECTOR = Rule(
    name="provider-dedupe-loop-lives-in-the-collector",
    find=_provider_loop_reinlines,
    summary="Use app.search.providers._collect_results",
    bite=lambda: Bites(
        # The keyed-provider copy: seen:set + isinstance(item, dict) guard +
        # SearchResult construction in one function, re-inlined off the collector.
        "async def search(self, query, limit, timeout_seconds):\n"
        "    results = []\n"
        "    seen: set[str] = set()\n"
        "    for item in payload.get('results') or []:\n"
        "        if not isinstance(item, dict):\n"
        "            continue\n"
        "        results.append(SearchResult(url=item.get('url')))\n"
        "    return results\n",
        "dedupe loop re-inlined outside _collect_results",
    ),
    clean=lambda: (
        # DDG walks lxml nodes instead of provider dicts, so it has no dict guard
        # and the sink never reaches it -- the scope boundary is the whole reason
        # one collector can be mandatory for keyed providers and irrelevant here.
        "async def search(self, query, limit, timeout_seconds):\n"
        "    results = []\n"
        "    seen: set[str] = set()\n"
        "    for node in tree.xpath('//div'):\n"
        "        href = node.get('href')\n"
        "        results.append(SearchResult(url=href))\n"
        "    return results\n",
    ),
)


def test_no_provider_dedupe_loop_reinlines():
    """One normalize/dedupe/clip contract for all keyed providers."""
    PROVIDER_DEDUPE_LOOP_LIVES_IN_THE_COLLECTOR.scan()


def test_provider_loop_detector_bites():
    """The three-provider copy fires; the DDG lxml-walk shape stays clean."""
    PROVIDER_DEDUPE_LOOP_LIVES_IN_THE_COLLECTOR.check_bites()


# The names of the one selector contract, and the single parameter that carries
# them; see ``app/extraction/content.py`` for why they are one value rather than
# two parameters on three signatures.
_SELECTOR_PARAMS = ("css_selector", "xpath")


#: The single parameter that carries the pair, and therefore the shape a
#: narrowing function is expected to have.
_SELECTOR_CARRIER_PARAM = "selectors"


#: The one place the two names are spelled: the ``Selectors`` dataclass. Its
#: generated ``__init__`` is what the sweep sees, so that is the name checked.
_SELECTOR_CARRIER_DECLARANT = "__init__"


#: The content cache key legitimately declares both names: it does not narrow a
#: caller's request, it keys one, and a key built from half the narrowing inputs
#: is a different key. Named rather than inferred from the parameter names,
#: because a rule that infers is a rule that stops applying when the code moves.
_SELECTOR_KEY_BUILDER = "build_content_cache_key"


def _selector_contract_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """A narrowing extractor carries the pair in one value, never as two names.

    The rule used to be "take both names or neither", which forbade the defect
    by counting parameters. It could catch a function declaring one, and it
    could say nothing about a function declaring both and then honouring only
    one. It was also the *reason* the pair was spelled four times -- three
    signatures plus the mapping that fed them -- because satisfying a parameter
    rule is a spelling, and a spelling cannot be shared between entry points.

    :class:`app.extraction.content.Selectors` replaced the spelling, so the rule
    inverts: a function narrows by taking ONE ``selectors`` value, and nothing
    outside the carrier declares the names. That is stronger in the direction
    that matters -- there is no longer any signature in which passing one
    selector is expressible -- and it is what stops a future fourth extractor
    from reintroducing the pair as parameters, which a rule checking only "both
    or neither" would have waved through.
    """
    offenders: list[str] = []
    for node in functions(tree):
        params = node.args
        positional = [arg.arg for arg in (*params.posonlyargs, *params.args)]
        names = [*positional, *(arg.arg for arg in params.kwonlyargs)]
        declared = [name for name in names if name in _SELECTOR_PARAMS]
        if not declared or node.name in (_SELECTOR_CARRIER_DECLARANT, _SELECTOR_KEY_BUILDER):
            continue
        offenders.append(
            offender_at(
                path,
                node,
                f"{node.name} declares {declared} instead of "
                f"carrying the pair in one {_SELECTOR_CARRIER_PARAM!r} value",
            )
        )
    return offenders


THE_SELECTOR_PAIR_IS_ONE_CONTRACT = Rule(
    name="the-selector-pair-is-one-contract",
    find=_selector_contract_offenders,
    summary=(
        "a narrowing function must take one `selectors` value carrying both, and"
        " must not declare css_selector/xpath as parameters of its own; the pair is"
        " one contract and half of it is a request that cannot be honoured"
    ),
    bite=lambda: (
        Bites(
            "def extract_html(html, url, css_selector=None, xpath=None) -> ExtractedContent:\n    ...\n",
            "declares ['css_selector', 'xpath'] instead of carrying",
        ),
        Bites(
            "def _narrow(html, css_selector=None) -> ExtractedContent:\n    ...\n",
            "declares ['css_selector'] instead of carrying",
        ),
    ),
    clean=lambda: (
        # The carrier itself: the one place the two names are spelled.
        "class Selectors:\n    css: str | None = None\n    xpath: str | None = None\n",
        # An extractor carrying the pair as the one value.
        "def extract_html(html, url, selectors=None) -> ExtractedContent:\n    ...\n",
        # A key builder legitimately requires both: it keys a request rather
        # than narrowing one.
        "def build_content_cache_key(url, strategy, headers, extraction, css_selector, xpath):\n    ...\n",
        # A function that takes no selector is somebody else's business.
        "def extract_document(body, url) -> ExtractedContent:\n    ...\n",
    ),
    proof_path=REPO_ROOT / "app" / "crawl" / "_synthetic.py",
)


def test_the_selector_pair_is_one_contract():
    """A narrowing extractor carries the pair in one value, never as two names."""
    THE_SELECTOR_PAIR_IS_ONE_CONTRACT.scan()


def test_selector_contract_detector_bites():
    """A re-spelled pair fires, a lone name fires, the real shape passes."""
    THE_SELECTOR_PAIR_IS_ONE_CONTRACT.check_bites()


def _keyed_provider_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """A keyed provider states one request; it does not write one.

    The normalize/dedupe/clip contract above is only half the shared flow. The
    other half -- issue the request, refuse a non-200, hand the result list to
    the collector -- used to be spelled once per provider, and the per-provider
    request bodies were those spellings: identical but for the
    endpoint and the credential placement, so the only part that genuinely
    varied was the part left as a hook. The flow then moved into the base, and
    what a provider supplies is a ``_call`` describing its request and an
    ``_items_path`` saying where the results sit.

    Requiring the declaration *and* forbidding the flow is what makes a fourth
    provider a subclass with two declarations instead of a fourth copy of the
    non-200 check. ``search``, ``_request``, ``_results`` and ``_items_of`` are
    all the base's now, so a subclass defining any of them is re-spelling part
    of the shared flow and reaching the network by a route the base never sees.
    """
    offenders: list[str] = []
    for node in class_defs(tree):
        if not any(
            resolved_expr_name(tree, base).rsplit(".", 1)[-1] == "_ApiKeyProvider" for base in node.bases
        ):
            continue
        methods = {
            child.name for child in node.body if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        attributes = {
            target.id
            for child in node.body
            if isinstance(child, ast.Assign)
            for target in child.targets
            if isinstance(target, ast.Name)
        }
        if "_call" not in methods:
            offenders.append(offender_at(path, node, f"{node.name} does not state a _call()"))
        if "_items_path" not in attributes:
            offenders.append(offender_at(path, node, f"{node.name} does not declare _items_path"))
        for hook in ("search", "_request", "_results", "_items_of"):
            if hook in methods:
                offenders.append(
                    offender_at(path, node, f"{node.name} re-spells the shared provider flow in {hook}()")
                )
    return offenders


KEYED_PROVIDERS_INHERIT_THE_SHARED_RESPONSE_FLOW = Rule(
    name="keyed-providers-inherit-the-shared-response-flow",
    find=_keyed_provider_offenders,
    summary=(
        "a keyed search provider must state a _call() and an _items_path and not"
        " define search()/_request()/_results()/_items_of(), so the non-200 check"
        " cannot be re-spelled or dropped"
    ),
    bite=lambda: (
        Bites(
            "from app.search.providers import _ApiKeyProvider\n"
            "class SerperProvider(_ApiKeyProvider):\n"
            "    name = 'serper'\n"
            "    _items_path = ('organic',)\n"
            "    def _call(self, query, limit):\n"
            "        return _ProviderCall('POST', url)\n"
            "    async def search(self, query, limit, timeout_seconds):\n"
            "        status, body = await self._request(query, limit, timeout_seconds)\n"
            "        return self._results(status, body, lambda p: p.get('organic'),\n"
            "            url_key='link')\n",
            "re-spells the shared provider flow in search()",
        ),
        Bites(
            "from app.search.providers import _ApiKeyProvider\n"
            "class BraveProvider(_ApiKeyProvider):\n"
            "    name = 'brave'\n"
            "    _items_path = ('web', 'results')\n"
            "    def _call(self, query, limit):\n"
            "        return _ProviderCall('GET', url)\n"
            "    async def _request(self, query, limit, timeout_seconds):\n"
            "        return await _provider_http('GET', url)\n",
            "re-spells the shared provider flow in _request()",
        ),
        Bites(
            "from app.search.providers import _ApiKeyProvider\n"
            "class TavilyProvider(_ApiKeyProvider):\n"
            "    name = 'tavily'\n"
            "    _items_path = ('results',)\n",
            "does not state a _call()",
        ),
        Bites(
            "from app.search.providers import _ApiKeyProvider\n"
            "class BingProvider(_ApiKeyProvider):\n"
            "    name = 'bing'\n"
            "    def _call(self, query, limit):\n"
            "        return _ProviderCall('GET', url)\n",
            "does not declare _items_path",
        ),
    ),
    clean=lambda: (
        "class TavilyProvider(_ApiKeyProvider):\n"
        "    name = 'tavily'\n"
        "    _url_key = 'url'\n"
        "    _title_key = 'title'\n"
        "    _description_key = 'content'\n"
        "    _items_path = ('results',)\n"
        "    def _call(self, query, limit):\n"
        "        return _ProviderCall('POST', url, json_body={'api_key': self._api_key})\n",
        # A provider that does not inherit the keyed flow (DuckDuckGo) is not
        # this guard's business; the loop guard above owns its shape.
        "class DuckDuckGoProvider:\n"
        "    name = 'duckduckgo'\n"
        "    async def search(self, query, limit, timeout_seconds):\n"
        "        return []\n",
    ),
    proof_path=REPO_ROOT / "app" / "search" / "_synthetic.py",
)


def test_keyed_providers_inherit_the_shared_response_flow():
    """One flow for the keyed providers: a request declared, never written."""
    KEYED_PROVIDERS_INHERIT_THE_SHARED_RESPONSE_FLOW.scan()


def test_keyed_provider_flow_detector_bites():
    """The re-spelled flow fires, the incomplete subclass fires, the data passes.

    The first two shapes are what the two POST providers did: the whole flow
    written per provider, differing only in the endpoint and the credential
    placement -- once by overriding ``search``, once by re-opening the request
    itself. The next two are the quieter failures: a provider that never says
    what its request looks like, and one that never says where its results sit,
    each reaching ``NotImplementedError`` or an empty result set on the first
    query instead of at import. The last is the shape the class is for. Each
    source is built to trip exactly one rule, so a rule that started firing for
    the wrong reason shows up as a count change rather than a green run.
    """
    KEYED_PROVIDERS_INHERIT_THE_SHARED_RESPONSE_FLOW.check_bites()


# ---------------------------------------------------------------------------
# robots.txt scanning
# ---------------------------------------------------------------------------

_ROBOTS_SCANNER_SANCTIONED = "iter_robots_fields"


def _robots_scanner_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """One line-walk for every robots consumer.

    Sink: a function doing the ``lstrip(BOM) + splitlines()`` scan. It lived
    three times (crawl-delay, rules, sitemaps) and drifted on the empty-value
    gate; the tokenizer in app/crawl/robots_parse.py is now the only one.
    """
    offenders: list[str] = []
    for node in functions(tree):
        if node.name == _ROBOTS_SCANNER_SANCTIONED:
            continue
        has_bom_strip = any(
            isinstance(s, ast.Call)
            and getattr(s.func, "attr", "") == "lstrip"
            and s.args
            and isinstance(s.args[0], ast.Constant)
            and s.args[0].value in ("﻿", "\ufeff")
            for s in nodes_of(node)
        )
        has_split = any(
            isinstance(s, ast.Call) and getattr(s.func, "attr", "") == "splitlines" for s in nodes_of(node)
        )
        if has_bom_strip and has_split:
            offenders.append(offender_at(path, node, f"{node.name} re-rolls the robots line-scanner"))
    return offenders


ROBOTS_LINE_SCANNER_LIVES_IN_ONE_PLACE = Rule(
    name="robots-line-scanner-lives-in-one-place",
    find=_robots_scanner_offenders,
    summary=(
        "app/crawl/robots_parse.iter_robots_fields is the single scanner for robots"
        " consumers, so the shared line-walk cannot drift on the empty-value gate"
    ),
    bite=lambda: Bites(
        "def parse_robots_rules(text):\n"
        "    for raw in text.lstrip('\\ufeff').splitlines():\n"
        "        yield raw\n",
        "re-rolls the robots line-scanner",
    ),
    clean=lambda: (
        "def iter_robots_fields(text):\n"
        "    for raw in text.lstrip('\\ufeff').splitlines():\n"
        "        yield raw.strip()\n",
    ),
    proof_path=REPO_ROOT / "app" / "crawl" / "_synthetic.py",
)


def test_robots_line_scanner_lives_in_one_place():
    """Every robots consumer walks lines through the shared tokenizer."""
    ROBOTS_LINE_SCANNER_LIVES_IN_ONE_PLACE.scan()


def test_robots_scanner_detector_bites():
    ROBOTS_LINE_SCANNER_LIVES_IN_ONE_PLACE.check_bites()


# ---------------------------------------------------------------------------
# Test-fake construction: sanctioned builders over hand-built fakes
# ---------------------------------------------------------------------------

_FAKE_BUILDER_SANCTIONED = frozenset({"_fetcher_factories.py"})


def _fetcher_factory_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag a test file hand-building a model the fetcher factory already fabricates.

    HttpFetchResult's field set is the crawl contract; a fixture that spells
    it out by hand silently diverges when the contract grows (the exact
    drift this detector exists to catch). _fetcher_factories.make_fetch is the sanctioned builder.
    """
    offenders: list[str] = []
    for node in call_nodes(tree):
        if getattr(node.func, "id", "") == "HttpFetchResult":
            offenders.append(
                offender_at(path, node, "use _fetcher_factories.make_fetch instead of HttpFetchResult(...)")
            )
    return offenders


FETCH_FAKES_COME_FROM_THE_SANCTIONED_BUILDER = Rule(
    name="fetch-fakes-come-from-the-sanctioned-builder",
    find=_fetcher_factory_offenders,
    summary="Use _fetcher_factories.make_fetch()",
    excludes=_FAKE_BUILDER_SANCTIONED,
    bite=lambda: Bites(
        "def f():\n    return HttpFetchResult(url='u')\n",
        "make_fetch",
    ),
    clean=lambda: ("def f():\n    return make_fetch(url='u')\n",),
    scope=lambda *, exclude: trees_excluding(TESTS_DIR, *exclude),
    proof_path=REPO_ROOT / "tests" / "_synthetic.py",
)


def test_tests_do_not_hand_build_http_fetch_results():
    """Every fetch fake comes from the one sanctioned builder."""
    FETCH_FAKES_COME_FROM_THE_SANCTIONED_BUILDER.scan()


def test_fetch_builder_detector_bites():
    FETCH_FAKES_COME_FROM_THE_SANCTIONED_BUILDER.check_bites()


def _call_node_names(node: ast.AST) -> set[str]:
    """Names of every call target under ``node`` (e.g. ``pytest.fail``)."""
    names: set[str] = set()
    for child in nodes_of(node):
        if isinstance(child, ast.Call):
            with contextlib.suppress(Exception):
                names.add(ast.unparse(child.func))
    return names


def _poll_loop_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    def check(node):
        src = ast.dump(node)
        has_while_true = any(
            isinstance(n, ast.While) and ast.unparse(n.test) == "True" for n in nodes_of(node)
        )
        if not (has_while_true and "iscoroutine" in src and "wait_for" in src):
            return []
        # The sanctioned _assertions.poll_until terminates with pytest.fail. A
        # docstring may *claim* a fail; only a real call node counts.
        if any(name == "fail" or name.endswith(".fail") for name in _call_node_names(node)):
            return []
        return [offender_at(path, node, "use _testkit.poll_until instead of a hand-rolled poll loop")]

    return offenders_over_functions(tree, check)


TESTS_USE_THE_SANCTIONED_POLLER = Rule(
    name="tests-use-the-sanctioned-poller",
    find=_poll_loop_offenders,
    summary="Use _testkit.poll_until",
    excludes=_FAKE_BUILDER_SANCTIONED,
    bite=lambda: Bites(
        "async def _poll_until(pred, *, timeout=10.0):\n"
        "    async def _runner():\n"
        "        while True:\n"
        "            v = pred()\n"
        "            if asyncio.iscoroutine(v):\n"
        "                v = await v\n"
        "            if v:\n"
        "                return True\n"
        "    return await asyncio.wait_for(_runner(), timeout=timeout)\n",
        "poll_until",
    ),
    clean=lambda: (
        "async def poll_until(pred, *, timeout=10.0, message=None):\n"
        "    async def _runner():\n"
        "        while True:\n"
        "            if pred():\n"
        "                return True\n"
        "    await asyncio.wait_for(_runner(), timeout=timeout)\n"
        "    pytest.fail(message)\n",
    ),
    scope=lambda *, exclude: trees_excluding(TESTS_DIR, *exclude),
    proof_path=REPO_ROOT / "tests" / "_synthetic.py",
)


def test_tests_do_not_relinline_poll_loop():
    """One sanctioned poller; tests must not re-roll their own."""
    TESTS_USE_THE_SANCTIONED_POLLER.scan()


def test_poll_loop_detector_bites():
    TESTS_USE_THE_SANCTIONED_POLLER.check_bites()


# ---------------------------------------------------------------------------
# dataclass copying: replace, not asdict-and-rebuild
# ---------------------------------------------------------------------------

# ``asdict`` is a *serialization* primitive: it recurses through the field tree
# and hands back plain dicts, so a frozen nested dataclass (a CrawlStrategyAttempt)
# becomes a dict with the same keys. Unpacking that into a constructor call to
# "copy with one field changed" therefore rebuilds an object whose nested fields
# no longer match their declared types. The copy compares equal field-for-field,
# type-checks clean under a lax annotation, and raises AttributeError the first
# time a consumer reaches for an attribute -- which the render adapter does on
# its first read, long after the copy was made.
#
# The distinction the detector draws is not asdict-allowed-or-banned: genuine
# serialization is a legitimate use and job_store.to_json() is one. It is
# *where the result goes*. Unpacked into a constructor call, asdict is always the
# copy mistake; anything else, it is doing its real job. So the sink is the
# constructor, and no file needs an exemption to keep using asdict properly.


def _is_asdict_call(node: ast.expr) -> bool:
    """Whether a node calls asdict, under either import spelling.

    ``from dataclasses import asdict`` and ``import dataclasses`` are both in
    use in this tree, so a detector keyed on one spelling silently stops
    detecting the moment a module picks the other.
    """
    return isinstance(node, ast.Call) and (
        (isinstance(node.func, ast.Name) and node.func.id == "asdict")
        or (isinstance(node.func, ast.Attribute) and node.func.attr == "asdict")
    )


def _asdict_rebuild_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag unpacking ``asdict(...)`` into a constructor call."""
    offenders: list[str] = []
    for call in call_nodes(tree):
        for keyword in call.keywords:
            if keyword.arg is not None or not isinstance(keyword.value, ast.AST):
                continue
            unpacked = [n for n in call_nodes(keyword.value) if _is_asdict_call(n)]
            if unpacked:
                offenders.append(
                    offender_at(
                        path,
                        unpacked[0],
                        "asdict(...) unpacked into a "
                        "constructor call: the nested dataclasses become dicts and "
                        "the rebuilt object violates its declared field types -- use dataclasses.replace()",
                    )
                )
                break
    return offenders


ASDICT_IS_NEVER_UNPACKED_INTO_A_CONSTRUCTOR = Rule(
    name="asdict-is-never-unpacked-into-a-constructor",
    find=_asdict_rebuild_offenders,
    summary="copy dataclasses with dataclasses.replace(), not asdict(**...)",
    bite=lambda: (
        Bites(
            "import dataclasses\ndef f(r):\n    return Thing(**{**dataclasses.asdict(r), 'd': 1})\n",
            "dataclasses.replace",
        ),
        Bites(
            "from dataclasses import asdict\ndef f(r):\n    return Thing(**asdict(r))\n",
            "dataclasses.replace",
        ),
    ),
    clean=lambda: (
        # Serialization keeps asdict: a dict result used as a dict is its real job.
        "import dataclasses\ndef f(s):\n    data = dataclasses.asdict(s)\n    return json.dumps(data)\n",
        "import dataclasses\ndef f(s):\n    return dataclasses.asdict(s)['results']\n",
        "import dataclasses\ndef f(s):\n    data = dataclasses.asdict(s)\n    data['x'] = 1\n    "
        "return data\n",
    ),
    proof_path=REPO_ROOT / "app" / "_synthetic.py",
)


def test_asdict_is_never_unpacked_into_a_constructor():
    """Copying a dataclass with asdict must not survive anywhere in app/."""
    ASDICT_IS_NEVER_UNPACKED_INTO_A_CONSTRUCTOR.scan()


def test_asdict_rebuild_detector_bites():
    """The rebuild shape fires; genuine serialization does not."""
    ASDICT_IS_NEVER_UNPACKED_INTO_A_CONSTRUCTOR.check_bites()


# ---------------------------------------------------------------------------
# JS-endpoint egress door: the filter must not be reinlined
#
# ``filter_js_endpoints`` is the single place a scraped JS endpoint survives or
# is dropped. Both call sites used to carry the inline comprehension
# (``{**endpoint, "url": clean.raw} for ... if (clean := admit_or_none(...))
# is not None``) -- the shape that silently disappears the moment a consumer is
# added in a hurry. This pins the comprehension to the chokepoint itself.
# ---------------------------------------------------------------------------

_JS_ENDPOINTS_FILTER_SANCTIONED = frozenset({"validation.py"})


def _reinline_js_endpoints_filter_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag the inlined endpoint-filter comprehension outside its chokepoint.

    The reinlined shape is a comprehension whose element spreads an existing
    dict and rewrites its ``url`` key (``{**endpoint, "url": clean}``) -- a
    None key in the dict is the ``**`` spread, and the rewritten ``url`` key is
    what makes it an endpoint filter rather than an ordinary copy.
    """
    if path.name == "validation.py":
        return []
    offenders: list[str] = []
    for node in comprehensions(tree):
        elt = node.elt
        if not isinstance(elt, ast.Dict):
            continue
        if any(k is None for k in elt.keys) and any(
            isinstance(k, ast.Constant) and k.value == "url" for k in elt.keys
        ):
            offenders.append(
                offender_at(path, node, "inlined js_endpoints filter: use validation.filter_js_endpoints")
            )
    return offenders


_JS_EGRESS_ELSEWHERE = REPO_ROOT / "app" / "transport" / "content_cache.py"


# The chokepoint's own comprehension really is the matched shape: the file
# exemption is what keeps the real tree clean, not a predicate that misses it.
_JS_ENDPOINTS_VALIDATION_SOURCE = (
    "def filter_js_endpoints(endpoints, *, resolve_against=None, policy=None):\n"
    "    return [\n"
    "        {**endpoint, 'url': clean.raw}\n"
    "        for endpoint in endpoints\n"
        "        if (clean := admit_or_none(endpoint)) is not None\n"
    "    ]\n"
)


JS_ENDPOINTS_FILTER_IS_NOT_REINLINED = Rule(
    name="js-endpoints-filter-is-not-reinlined",
    find=_reinline_js_endpoints_filter_offenders,
    summary="route endpoints through filter_js_endpoints",
    excludes=frozenset({"validation.py"}),
    bite=lambda: (
        Bites(
            "def f(endpoints, base):\n"
            '    return [{**e, "url": c} for e in endpoints\n'
        '           if (c := admit_or_none(e["url"])) is not None]\n',
            "filter_js_endpoints",
        ),
        # The detector genuinely matches the chokepoint's OWN comprehension too:
        # the exemption is the path, not a predicate that happens to miss it.
        Bites(
            _JS_ENDPOINTS_VALIDATION_SOURCE,
            "filter_js_endpoints",
            at=_JS_EGRESS_ELSEWHERE,
        ),
    ),
    clean=lambda: (
        "def f(endpoints, base):\n    return filter_js_endpoints(endpoints, resolve_against=base)\n",
        # The very same chokepoint comprehension stays clean at its own path:
        # validation.py is where the filter legitimately lives.
        Silent(_JS_ENDPOINTS_VALIDATION_SOURCE, at=REPO_ROOT / "app" / "validation.py"),
    ),
)


def test_js_endpoints_filter_is_not_reinlined():
    """Scraped JS endpoints are filtered through ``filter_js_endpoints``."""
    JS_ENDPOINTS_FILTER_IS_NOT_REINLINED.scan()


def test_reinline_js_endpoints_detector_bites():
    """The historical inline shape fires; the chokepoint call stays clean."""
    JS_ENDPOINTS_FILTER_IS_NOT_REINLINED.check_bites()


# --- 4a: the robots matcher compiler is reachable only via its host cache ----

_ROBOTS_COMPILE = "_compile_robots_pattern"


_ROBOTS_COMPILE_HOME = "_RobotsPatternCache"


def _robots_compile_call_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """``_compile_robots_pattern`` may be called only from ``_RobotsPatternCache.get``.

    The pure pattern translation is the cheap half of the robots matcher; the
    safety property is that every caller reaches it through the bounded
    PER-HOST LRU. A second caller reintroduces a process-global matcher cache
    keyed only on the pattern string -- patterns arrive from remotely-fetched
    robots.txt, so a hostile site spraying distinct patterns would evict every
    other site's matchers and control the whole cache. The home method
    is the single sanctioned call site; the module-level wrapper
    (``_robots_pattern_to_regex``) goes through it too.
    """
    offenders: list[str] = []
    for node in calls_named(tree, _ROBOTS_COMPILE):
        scopes = _enclosing_defs(node, tree)
        if _ROBOTS_COMPILE_HOME in scopes and "get" in scopes[: scopes.index(_ROBOTS_COMPILE_HOME) + 1]:
            continue
        offenders.append(
            offender_at(
                path,
                node,
                f"{_ROBOTS_COMPILE} called outside {_ROBOTS_COMPILE_HOME}.get; route through "
                f"the per-host cache so a hostile robots.txt cannot evict another site's matchers",
            )
        )
    return offenders


ROBOTS_PATTERN_COMPILE_LIVES_BEHIND_THE_PER_HOST_CACHE = Rule(
    name="robots-pattern-compile-lives-behind-the-per-host-cache",
    find=_robots_compile_call_offenders,
    summary=(
        "Every caller reaches the pattern compiler through _RobotsPatternCache.get, "
        "so a hostile robots.txt spraying distinct patterns cannot evict another "
        "site's matchers from a global cache"
    ),
    bite=lambda: (
        # A direct caller -- exactly the shape that reintroduces a global,
        # attacker-keyed matcher cache.
        Bites(
            "def f(pattern):\n    return _compile_robots_pattern(pattern)\n",
            "per-host cache",
        ),
        # A same-named method on any OTHER class is not the home.
        Bites(
            "class OtherCache:\n    def get(self, pattern):\n"
            "        return _compile_robots_pattern(pattern)\n",
            "per-host cache",
        ),
    ),
    clean=lambda: (
        # The sanctioned home passes.
        "class _RobotsPatternCache:\n"
        "    def get(self, host, pattern):\n"
        "        return _compile_robots_pattern(pattern)\n",
        # A nested helper inside the home class is still inside the home scope.
        "class _RobotsPatternCache:\n"
        "    def get(self, host, pattern):\n"
        "        def build():\n"
        "            return _compile_robots_pattern(pattern)\n"
        "        return build()\n",
    ),
    proof_path=REPO_ROOT / "app" / "_synthetic.py",
)


def test_robots_pattern_compile_lives_behind_the_per_host_cache():
    ROBOTS_PATTERN_COMPILE_LIVES_BEHIND_THE_PER_HOST_CACHE.scan()


def test_robots_pattern_compile_detector_bites():
    ROBOTS_PATTERN_COMPILE_LIVES_BEHIND_THE_PER_HOST_CACHE.check_bites()


_COMMENT_CLONE_MIN_LINES = 3


# Emitted after every comment-only line, and around indentation: none of them
# says anything about prose, so only a token that carries code ends a block.
_COMMENT_TRANSPARENT = frozenset(
    {tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER, tokenize.ENCODING}
)


def _comment_blocks(source: str) -> list[tuple[int, tuple[str, ...]]]:
    """``(first line, normalized text)`` for each run of consecutive comment lines.

    ``tokenize`` rather than a ``#`` search, because a ``#`` inside a string is a
    character and not a comment. A run is consecutive *lines* rather than merely
    adjacent comment tokens, so a blank line, a statement, or a re-indent between
    two notes ends the block instead of silently welding unrelated prose into one
    clone -- and re-welding it is what the re-indent case has to catch, which is
    why the break is the line gap and not the indentation.
    """
    blocks: list[tuple[int, tuple[str, ...]]] = []
    lines: list[str] = []
    first_line = 0
    previous_end = 0

    def flush() -> None:
        nonlocal lines, first_line
        if lines:
            blocks.append((first_line, tuple(lines)))
            lines = []

    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            text = " ".join(token.string.lstrip("#").split())
            if not text:
                continue
            if lines and token.start[0] != previous_end + 1:
                flush()
            if not lines:
                first_line = token.start[0]
            lines.append(text)
            previous_end = token.end[0]
        elif token.type not in _COMMENT_TRANSPARENT:
            flush()
    flush()
    return blocks


def _duplicated_comment_runs(
    sources: Iterable[tuple[Path, str]], *, minimum: int = _COMMENT_CLONE_MIN_LINES
) -> list[str]:
    """Comment blocks written twice in app/, with every site that repeats them.

    The token floor cannot see this class at all: jscpd matches tokens, so a
    duplicated comment contributes nothing to a clone it reports, and a
    six-line WHY note read as zero clones at 4/25. That is what happened -- the
    same note about the content cache's eviction window sat at both write call
    sites, each copy carrying a claim about locking, and nothing in the suite or
    the gate could tell that a later edit to the locking had updated one and left
    the other asserting something the code no longer did.

    Normalized on whitespace before comparison, so a re-indented copy -- the way
    these forks actually arrive, by pasting between call sites -- still matches.
    A block shorter than ``minimum`` is not a WHY worth centralizing, and a
    two-line threshold would fire on ordinary repeated section rules.
    """
    sites: dict[tuple[str, ...], list[tuple[Path, int]]] = {}
    for path, source in sources:
        for line, block in _comment_blocks(source):
            if len(block) < minimum:
                continue
            sites.setdefault(block, []).append((path, line))
    return [
        " and ".join(f"{relative_posix(path)}:{line}" for path, line in found)
        + f" carry the same {len(block)}-line comment: {block[0]}"
        for block, found in sorted(sites.items())
        if len(found) > 1
    ]


def test_no_comment_block_is_written_twice():
    """app/ states each multi-line WHY once, not once per call site."""
    offenders = _duplicated_comment_runs(
        (path, path.read_text(encoding="utf-8")) for path in app_python_files(exclude=frozenset())
    )
    assert not offenders, "comment blocks written more than once: " + ", ".join(offenders)


def test_comment_clone_detector_bites():
    """A repeated block fires, in one file and across two; distinct blocks do not.

    Hand-rolled rather than :func:`assert_detector`, which parses its sources
    into modules: a comment is not in the ``ast`` at all, so this is the one
    detector in the file whose input is the text.
    """
    path = REPO_ROOT / "app" / "transport" / "content_cache.py"
    repeated = "# first line of the note\n# second line\n# third line\n"
    other = "# a different note entirely\n# with three lines\n# of its own text\n"
    detached = "        # first line of the note\n        # second line\n        # third line\n"

    def detect(source: str) -> list[str]:
        return _duplicated_comment_runs([(path, source)])

    once = detect(f"x = 1\n{repeated}y = 2\n")
    assert not once, once
    twice = detect(f"x = 1\n{repeated}y = 2\n{repeated}")
    expected = "app/transport/content_cache.py:2 and app/transport/content_cache.py:6"
    assert len(twice) == 1 and expected in twice[0], twice
    # Two blocks of the same length but different text are two notes.
    assert not detect(f"x = 1\n{repeated}y = 2\n{other}"), "equal-length distinct blocks are not clones"
    # Two lines is a section rule, not a WHY to centralize.
    two_lines = "# first line of the note\n# second line\n"
    assert not detect(f"x = 1\n{two_lines}y = 2\n{two_lines}"), "a two-line rule is below the floor"
    # A blank line ends a block, so two paragraphs stay two paragraphs instead of
    # one welded run -- which would match nothing, including its own twin.
    assert not detect(f"{repeated}\n{other}"), "a blank line must end a comment block"
    assert [line for line, _ in _comment_blocks(f"{repeated}\n{other}")] == [1, 5]
    # Identical prose is still a duplicate across a blank line: the rule about
    # blocks decides what counts as one note, not whether prose may repeat.
    assert detect(f"{repeated}\n{repeated}"), "identical prose is a duplicate however it is separated"
    # Normalized on whitespace, so a re-indented copy -- how these forks actually
    # arrive -- still matches rather than hiding behind the extra spaces.
    assert detect(f"x = 1\n{repeated}y = 2\n{detached}"), "a re-indented copy is still a clone"
    # A fork usually arrives by pasting between modules, so the same block in two
    # different files is one finding, and both sites have to be named.
    across = _duplicated_comment_runs(
        [
            (REPO_ROOT / "app" / "a.py", f"x = 1\n{repeated}"),
            (REPO_ROOT / "app" / "b.py", f"y = 2\n{repeated}"),
        ]
    )
    assert len(across) == 1 and "app/a.py:2 and app/b.py:2" in across[0], across
