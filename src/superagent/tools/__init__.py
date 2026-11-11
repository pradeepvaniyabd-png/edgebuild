from . import data_tools, media_tools, memory_tools, system_tools, web_tools
from .registry import ApprovalNeed, ToolContext, ToolRegistry, ToolSpec


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for module in (system_tools, web_tools, data_tools, media_tools, memory_tools):
        module.register(registry)
    return registry


__all__ = ["ApprovalNeed", "ToolContext", "ToolRegistry", "ToolSpec", "build_registry"]
