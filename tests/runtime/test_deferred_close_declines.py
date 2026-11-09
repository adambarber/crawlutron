"""The decline record: a declined deferred close is a handled state, not noise."""

from crawlutron.runtime.resource_lifecycle import DeferredClose, declined_closes


class _CloseMe:
    """A resource whose aclose records completion."""

    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def test_a_schedule_with_no_running_loop_records_the_decline_and_prints_nothing():
    """#643 warning-zero: the no-loop decline is counted in the record, not logged."""
    before = declined_closes()
    resource = _CloseMe()
    owner = DeferredClose[_CloseMe](aclose=_CloseMe.aclose)
    owner.schedule(resource, skip_if_closed=False)
    after = declined_closes()
    assert any(reason == "no_loop" and kind == "_CloseMe" for reason, kind in after), (
        "the no-loop decline was not recorded"
    )
    assert not resource.closed, "a decline must not close"
    assert after != before, "the record did not change"


def test_the_decline_record_has_a_reset_door():
    """The reset family's law: mutated module state empties through one door."""
    from crawlutron.runtime.resource_lifecycle import clear_declined_closes

    owner = DeferredClose[_CloseMe](aclose=_CloseMe.aclose)
    owner.schedule(_CloseMe(), skip_if_closed=False)
    assert declined_closes(), "the precondition failed: nothing recorded"
    clear_declined_closes()
    assert declined_closes() == {}, "the reset door left declines held"
