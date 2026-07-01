"""Backward-compatibility shim.

Company event memory moved into the multi-tier `bharat_terminal.kb.memory`
package. It is now retrieved from the derived, embedded `memory_items` index
(relevance + recency + importance) instead of re-scanning `impact_reports` on
every read. Existing importers keep working via these re-exports.
"""
from bharat_terminal.kb.memory.company import (  # noqa: F401
    build_company_memory,
    format_memory_block,
)
