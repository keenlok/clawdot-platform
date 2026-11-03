"""Deep web research for chatcore."""

from chatcore_research.jobs import MemoryResearchJobs, PgResearchJobs
from chatcore_research.plugin import ResearchPlugin
from chatcore_research.quota import AllowAllResearch, ResearchAccess
from chatcore_research.settings import ResearchSettings

__all__ = [
    "AllowAllResearch",
    "MemoryResearchJobs",
    "PgResearchJobs",
    "ResearchAccess",
    "ResearchPlugin",
    "ResearchSettings",
]
