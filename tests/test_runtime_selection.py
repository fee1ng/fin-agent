import os
import unittest
from unittest.mock import patch

from fin_agent.agent.runtime import create_agent


class RuntimeSelectionTests(unittest.TestCase):
    def test_existing_default_uses_legacy_agent(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch("fin_agent.agent.core.FinAgent") as legacy:
            agent = create_agent(max_steps=3)
        legacy.assert_called_once_with(max_steps=3)
        self.assertIs(agent, legacy.return_value)

    def test_unknown_engine_fails_without_silent_fallback(self):
        with patch("fin_agent.agent.core.FinAgent") as legacy:
            with self.assertRaisesRegex(ValueError, "Unsupported agent engine"):
                create_agent(engine="typo")
        legacy.assert_not_called()

    def test_explicit_selection_overrides_environment(self):
        with patch.dict(os.environ, {"FIN_AGENT_ENGINE": "harness"}), \
             patch("fin_agent.agent.core.FinAgent") as legacy:
            self.assertIs(create_agent(engine="langgraph"), legacy.return_value)
