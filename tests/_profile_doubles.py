"""A request-profile double whose value set flips after its first resolution."""

from collections.abc import Mapping


class StatefulRequestProfile(Mapping[str, object]):
    """Answers with the first resolution's values through the first full pass.

    Pydantic probes declared keys with ``.get`` before iterating, then reads the keys it iterated; both
    passes are the one resolution, so the second state only starts after the first is complete.
    """

    def __init__(self) -> None:
        self.snapshot = 0

    def __iter__(self):
        self.snapshot += 1
        yield "user_agent_family"
        yield "cookie_jar"
        yield "cookies"

    def __len__(self) -> int:
        return 3

    def __getitem__(self, key: str) -> object:
        if self.snapshot <= 1:
            values: dict[str, object] = {
                "user_agent_family": "desktop_chrome",
                "cookie_jar": "none",
                "cookies": {},
            }
        else:
            values = {
                "user_agent_family": "generic_crawler",
                "cookie_jar": "per_host",
                "cookies": {"profile.example": {"session": "second"}},
            }
        return values[key]
