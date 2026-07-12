from orchestrator.agents.specialists.base import SpecialistAgent, SpecialistError
from orchestrator.llm.clients import LLMClient
from orchestrator.tools.registry import ToolRegistry


class ResearchSpecialist(SpecialistAgent):
    name = "research"
    ROLE = "You research topics on the web and gather sourced facts."


class AnalysisSpecialist(SpecialistAgent):
    name = "analysis"
    ROLE = "You extract and analyze data using SQL queries and Python code."


class WritingSpecialist(SpecialistAgent):
    name = "writing"
    ROLE = "You write clear drafts, summaries, and memos, saving deliverables to files."


class CodeSpecialist(SpecialistAgent):
    name = "code"
    ROLE = "You write and execute Python code in a sandbox to produce results."


def make_specialists(llm: LLMClient, registry: ToolRegistry) -> dict[str, SpecialistAgent]:
    return {
        cls.name: cls(llm, registry)
        for cls in (ResearchSpecialist, AnalysisSpecialist, WritingSpecialist, CodeSpecialist)
    }


__all__ = [
    "AnalysisSpecialist",
    "CodeSpecialist",
    "ResearchSpecialist",
    "SpecialistAgent",
    "SpecialistError",
    "WritingSpecialist",
    "make_specialists",
]
