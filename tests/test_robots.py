"""Tests for robots.py module."""

import unittest
from app.robots import RobotsPolicy

class RobotsPolicyTest(unittest.TestCase):
    def test_allowed_returns_true_for_any_url(self) -> None:
        policy = RobotsPolicy()
        self.assertTrue(policy.allowed("https://example.com/page"))
        self.assertTrue(policy.allowed("http://example.com/other"))
        self.assertTrue(policy.allowed("https://sub.example.com/path"))

    def test_allowed_returns_true_for_empty_string(self) -> None:
        policy = RobotsPolicy()
        self.assertTrue(policy.allowed(""))

    def test_allowed_returns_true_for_invalid_url(self) -> None:
        policy = RobotsPolicy()
        self.assertTrue(policy.allowed("not-a-url"))

if __name__ == "__main__":
    unittest.main()
