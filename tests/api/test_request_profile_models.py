"""Request profiles stay inside the cookie namespace with typed values."""

import pytest
from pydantic import ValidationError

from tests.chokepoint_walks import APP_ROOT


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

    def test_an_unknown_key_is_refused(self):
        from app.models.crawl_policy import CrawlPolicy

        with pytest.raises(ValidationError, match="request_profile keys not permitted"):
            CrawlPolicy(request_profile={"totally_bogus": 1})

    def test_a_misspelled_key_is_refused_rather_than_ignored(self):
        """The failure this exists for: a typo that reports success and does nothing."""
        from app.models.crawl_policy import CrawlPolicy

        for typo in ("cookie_jarr", "fingerprint_rotatoin", "accept_language"):
            with pytest.raises(ValidationError, match="request_profile keys not permitted"):
                CrawlPolicy(request_profile={typo: "per_host"})

    def test_every_allowed_key_has_a_reader_in_app(self):
        """A key nothing reads is a caller-visible no-op, which is what #61 removed.

        ``state`` sat in the browser profile allowlist for the whole life of the
        closed vocabulary with zero readers. This asserts the property for the
        request profile so the same class cannot reappear.
        """
        from app.models.crawl_policy import CrawlPolicy

        field = CrawlPolicy.model_fields["request_profile"]
        # ``.get`` rather than ``getattr(..., "keys")``: a dict's own ``keys`` attribute would satisfy the
        # getattr and hand back the bound method.
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
        from app.models.crawl_policy import CrawlPolicy

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
    assembled into the same header at ``headers_for_strategy`` is that bypass,
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
        from app.models.crawl_policy import CrawlPolicy

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
        from app.models.crawl_policy import CrawlPolicy

        profile = CrawlPolicy(
            request_profile={"cookie_jar": "per_host", "cookies": {"e.example": {"a": "1"}}}
        )
        assert profile.request_profile["cookies"] == {"e.example": {"a": "1"}}

    def test_a_namespace_is_itself_bounded(self):
        """A namespace is a dict key in a process-wide jar, so it is a size knob too."""
        from app.models.crawl_policy import CrawlPolicy

        with pytest.raises(ValidationError, match="cookie_namespace"):
            CrawlPolicy(request_profile={"cookie_namespace": "t" * 500})


class TestRequestProfileValueTypesMatchTheHeaderMapTheyCorrupt:
    """``headers_for_strategy`` is annotated ``-> dict[str, str]`` and is handed a ``dict[str, Any]``.

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
        from app.models.crawl_policy import CrawlPolicy

        for key in ("user_agents", "accept_languages"):
            for bad in ({"a_string": "Mozilla"}, {"an_entry": [{"x": 1}]}, {"a_none_entry": [None]}):
                with pytest.raises(ValidationError, match=key):
                    CrawlPolicy(request_profile={key: bad})

    def test_a_pool_at_the_ceiling_is_accepted(self):
        from app.models.crawl_policy import CrawlPolicy

        pool = [f"agent-{index}" for index in range(64)]
        assert len(CrawlPolicy(request_profile={"user_agents": pool}).request_profile["user_agents"]) == 64

    def test_an_over_sized_pool_is_refused(self):
        from app.models.crawl_policy import CrawlPolicy

        with pytest.raises(ValidationError, match="at most"):
            CrawlPolicy(request_profile={"user_agents": [f"agent-{i}" for i in range(65)]})

    def test_the_seed_and_family_must_be_strings(self):
        """``fingerprint_seed`` is interpolated into a hash and ``user_agent_family``
        is read with ``str()``, so a dict reaches the hash instead of a string."""
        from app.models.crawl_policy import CrawlPolicy

        for key in ("fingerprint_seed", "user_agent_family", "cookie_jar", "fingerprint_rotation"):
            with pytest.raises(ValidationError, match=key):
                CrawlPolicy(request_profile={key: {"not": "a scalar"}})
