"""Every tool the agents can use (app/ai/tools/registry.py describes the rules)."""

from __future__ import annotations

from app.ai.tools import business, investments, workspace
from app.ai.tools.registry import Registry


def default_registry() -> Registry:
    return Registry([*investments.TOOLS, *business.TOOLS, *workspace.TOOLS])


__all__ = ["default_registry"]
