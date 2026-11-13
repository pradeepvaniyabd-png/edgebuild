from .base import AgentContext, AgentRegistry, AgentResult, BaseAgent, action
from .coder import CoderAgent, DebuggerAgent
from .data import AnalystAgent, FileAgent
from .general import BrowserAgent, MasterAgent, ResearchAgent
from .government import GovernmentAgent
from .system import SystemAgent
from .trading import TradingAgent


def build_agents(extra: list[BaseAgent] | None = None) -> AgentRegistry:
    registry = AgentRegistry()
    for agent in (
        MasterAgent(), ResearchAgent(), BrowserAgent(), CoderAgent(), DebuggerAgent(), FileAgent(), AnalystAgent(),
        GovernmentAgent(), TradingAgent(), SystemAgent(),
        *(extra or []),
    ):  # fmt: skip
        registry.register(agent)
    return registry


__all__ = ["AgentContext", "AgentRegistry", "AgentResult", "BaseAgent", "action", "build_agents"]
