"""GEAP Enterprise Ops Agent package.

ADK discovers the agent through ``agent_app.agent.root_agent`` (and the
``app`` object, which wires compaction, resumability, and plugins).
"""

from . import agent  # noqa: F401
