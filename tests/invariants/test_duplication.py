"""Copy-paste is a defect class here, not a style choice.

Providers and selectors: one contract, one flow."""

import ast
import pathlib

from tests.chokepoint_walks import REPO_ROOT, class_defs, functions, nodes_of, offender_at, resolved_expr_name
from tests.rules import Bites, Rule

# --------------------------------------------------------------------------- Search-provider result
# normalization ---------------------------------------------------------------------------


def _provider_loop_reinlines(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag the keyed-provider dedupe loop re-inlined outside the collector.

    The exact copy-paste shape is ``seen: set`` + ``isinstance(item, dict)``
    guard + ``SearchResult`` construction in one function. DuckDuckGo's
    provider legitimately keeps a separate lxml node-walk: it has no dict
    guard, so the sink never fires on it.
    """
    offenders: list[str] = []
    for node in nodes_of(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) or node.name == "collect_results":
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
            offenders.append(offender_at(path, node, "dedupe loop re-inlined outside collect_results"))
    return offenders


PROVIDER_DEDUPE_LOOP_LIVES_IN_THE_COLLECTOR = Rule(
    name="provider-dedupe-loop-lives-in-the-collector",
    find=_provider_loop_reinlines,
    summary="Use app.search.providers.collect_results",
    bite=lambda: Bites(
        # The keyed-provider copy: seen:set + isinstance(item, dict) guard + SearchResult construction in one
        # function, re-inlined off the collector.
        "async def search(self, query, limit, timeout_seconds):\n"
        "    results = []\n"
        "    seen: set[str] = set()\n"
        "    for item in payload.get('results') or []:\n"
        "        if not isinstance(item, dict):\n"
        "            continue\n"
        "        results.append(SearchResult(url=item.get('url')))\n"
        "    return results\n",
        "dedupe loop re-inlined outside collect_results",
    ),
    clean=lambda: (
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


# The names of the one selector contract, and the single parameter that carries them; see
# ``app/extraction/content.py`` for why they are one value rather than two parameters on three signatures.
_SELECTOR_PARAMS = ("css_selector", "xpath")


#: The single parameter that carries the pair, and therefore the shape a narrowing function is expected to
#: have.
_SELECTOR_CARRIER_PARAM = "selectors"


#: The one place the two names are spelled: the ``Selectors`` dataclass. Its generated ``__init__`` is what
#: the sweep sees, so that is the name checked.
_SELECTOR_CARRIER_DECLARANT = "__init__"


#: The content cache key legitimately declares both names: it does not narrow a caller's request, it keys one,
#: and a key built from half the narrowing inputs is a different key.
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
        # A key builder legitimately requires both: it keys a request rather than narrowing one.
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
            "        return await provider_http('GET', url)\n",
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
        # A provider that does not inherit the keyed flow (DuckDuckGo) is not this guard's business; the loop
        # guard above owns its shape.
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
