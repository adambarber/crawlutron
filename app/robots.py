from __future__ import annotations


class RobotsPolicy:
    def allowed(self, url: str) -> bool:
        return True
