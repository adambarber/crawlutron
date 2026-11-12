"""Copy-paste is a defect class here, not a style choice.

Providers and selectors: one contract, one flow."""

import ast
import pathlib

from tests.chokepoint_walks import (
    APP_ROOT,
    class_defs,
    functions,
    nodes_of,
    offender_at,
    resolved_expr_name,
)
from tests.rules import Bites, Registry, Rule

# --------------------------------------------------------------------------- Search-provider result
# normalization ---------------------------------------------------------------------------


def _provider_loop_reinlines(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag the keyed-provider dedupe loop re-inlined outside the collector.

    The copy-paste shape is ``seen: set`` + dict guard + result construction in
    one function; the DuckDuckGo walk has no dict guard, so it never fires.
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
    summary="Use crawlutron.search.providers.collect_results",
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


# The names of the one selector contract, and the single parameter that carries them; see
# ``crawlutron/extraction/content.py`` for why they are one value, not two parameters on three signatures.
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

    The old "take both or neither" counted parameters and let one be honoured;
    the ``Selectors`` carrier inverts the rule, so passing one is inexpressible.
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
    proof_path=APP_ROOT / "crawl" / "_synthetic.py",
)


def _keyed_provider_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """A keyed provider states one request; it does not write one.

    The request/refuse/collect flow lives in the base, so a subclass defining
    the flow reach the network by a route the base never sees.
    """
    offenders: list[str] = []
    for node in class_defs(tree):
        if not any(
            resolved_expr_name(tree, base).rsplit(".", 1)[-1] == "ApiKeyProvider" for base in node.bases
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
        for hook in ("search", "_fetch", "_results", "_items_of"):
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
        " define search()/_fetch()/_results()/_items_of(), so the non-200 check"
        " cannot be re-spelled or dropped"
    ),
    bite=lambda: (
        Bites(
            "from crawlutron.search.providers import ApiKeyProvider\n"
            "class SerperProvider(ApiKeyProvider):\n"
            "    name = 'serper'\n"
            "    _items_path = ('organic',)\n"
            "    def _call(self, query, limit):\n"
            "        return ProviderCall('POST', url)\n"
            "    async def search(self, query, limit, timeout_seconds):\n"
            "        status, body = await self._request(query, limit, timeout_seconds)\n"
            "        return self._results(status, body, lambda p: p.get('organic'),\n"
            "            url_key='link')\n",
            "re-spells the shared provider flow in search()",
        ),
        Bites(
            "from crawlutron.search.providers import ApiKeyProvider\n"
            "class BraveProvider(ApiKeyProvider):\n"
            "    name = 'brave'\n"
            "    _items_path = ('web', 'results')\n"
            "    def _call(self, query, limit):\n"
            "        return ProviderCall('GET', url)\n"
            "    async def _fetch(self, query, limit, timeout_seconds):\n"
            "        return await provider_http('GET', url)\n",
            "re-spells the shared provider flow in _fetch()",
        ),
        Bites(
            "from crawlutron.search.providers import ApiKeyProvider\n"
            "class TavilyProvider(ApiKeyProvider):\n"
            "    name = 'tavily'\n"
            "    _items_path = ('results',)\n",
            "does not state a _call()",
        ),
        Bites(
            "from crawlutron.search.providers import ApiKeyProvider\n"
            "class BingProvider(ApiKeyProvider):\n"
            "    name = 'bing'\n"
            "    def _call(self, query, limit):\n"
            "        return ProviderCall('GET', url)\n",
            "does not declare _items_path",
        ),
    ),
    clean=lambda: (
        "class TavilyProvider(ApiKeyProvider):\n"
        "    name = 'tavily'\n"
        "    _url_key = 'url'\n"
        "    _title_key = 'title'\n"
        "    _description_key = 'content'\n"
        "    _items_path = ('results',)\n"
        "    def _call(self, query, limit):\n"
        "        return ProviderCall('POST', url, json_body={'api_key': self._api_key})\n",
        # A provider that does not inherit the keyed flow (DuckDuckGo) is not this guard's business; the loop
        # guard above owns its shape.
        "class DuckDuckGoProvider:\n"
        "    name = 'duckduckgo'\n"
        "    async def search(self, query, limit, timeout_seconds):\n"
        "        return []\n",
    ),
    proof_path=APP_ROOT / "search" / "_synthetic.py",
)


RULES = Registry(
    (
        PROVIDER_DEDUPE_LOOP_LIVES_IN_THE_COLLECTOR,
        THE_SELECTOR_PAIR_IS_ONE_CONTRACT,
        KEYED_PROVIDERS_INHERIT_THE_SHARED_RESPONSE_FLOW,
    )
)


def test_no_rule_violations():
    RULES.check_all_clean()
