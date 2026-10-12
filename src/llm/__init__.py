"""
LLM Abstraction Layer — unified interface powered by LiteLLM.
All LLM calls in the system go through this module.
"""

from src.llm.client import llm_call, llm_call_with_tools, llm_call_structured
from src.llm.routing import get_model_for_task, TASK_MODEL_MAP

__all__ = [
    "llm_call",
    "llm_call_with_tools",
    "llm_call_structured",
    "get_model_for_task",
    "TASK_MODEL_MAP",
]
