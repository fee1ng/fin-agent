"""Select exactly one agent runtime without changing the public chat interface."""
import os


def create_agent(*, engine=None, **options):
    selected = engine if engine is not None else os.getenv("FIN_AGENT_ENGINE", "langgraph")
    if selected == "langgraph":
        from fin_agent.agent.core import FinAgent
        agent = FinAgent(**options)
        agent.engine = selected
        return agent
    if selected == "harness":
        from fin_agent.agent.harness import HarnessAgent
        return HarnessAgent(**options)
    raise ValueError(f"Unsupported agent engine: {selected!r}")
