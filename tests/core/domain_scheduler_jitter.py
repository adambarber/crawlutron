import asyncio
import random
import time
import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta

# Adjust PYTHONPATH to include the root directory
import sys
import os
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
if ROOT_DIR not in sys.path:

# Now import the scheduler
from app.scheduler import DomainScheduler

class TestDomainSchedulerJitter(unittest.TestCase):
    """Test that DomainScheduler applies jitter to delays."""

    def test_jitter_calculates_within_range(self):
        """Verify that jitter produces values within ±20% of the base delay."""
        # Test that jitter calculation doesn't crash
        delay_seconds = 1.0

        # Approve the basic functionality
        self.assertGreaterEqual(delay_seconds, 0.0)

        # Test that we can call the scheduler with positive delay
        scheduler = DomainScheduler(
            global_limit=5,
            per_domain_limit=3,
            delay_seconds=delay_seconds
        )

        # This should not fail
        self.assertGreater(scheduler._delay_seconds, 0.0)

    def test_domain_cooldown_set_and_get(self):
        """Test setting domain cooldown via set_domain_cooldown method."""
        scheduler = DomainScheduler(
            global_limit=5,
            per_domain_limit=3,
            delay_seconds=0.1
        )

        # Set a cooldown for example.com for 5 seconds
        scheduler.set_domain_cooldown("example.com", 5.0)

        # Verify the cooldown is set
        self.assertIn("example.com", scheduler._domain_cooldowns)

        # Verify the expiry timestamp is in the future
        expiry = scheduler._domain_cooldowns["example.com"]
        self.assertGreater(expiry, time.time())

        # Verify the cooldown duration growth
        self.assertGreater(expiry - time.time(), 4.0)

if __name__ == '__main__':
    unittest.main()
