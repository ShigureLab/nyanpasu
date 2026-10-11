from __future__ import annotations

from nyanpasu.memory_consolidation import MemoryPolicy

PR_MAKER_MEMORY_POLICY = MemoryPolicy(
    extraction="""Retain human requirements and acceptance conditions, implemented behavior and
key design choices with their reasons, verified failures and fixes, and why a plan changed.
Distinguish implemented and verified behavior from intended future work. Retain attributed
human PR feedback and its scope/source when it changes requirements or design.
Discard branch creation, commits/pushes, PR publication, and other routine execution records.""",
    summary="""Identify the substantive change and its key domain terms, then retain important
requirements, design decisions/reasons, and verified technical limitations with detail links.
Do not turn plans into implemented behavior or describe routine Git/PR operations.""",
)
