"""Named contract workspaces — a StorageBackend + prefix holding many contracts.

The global active store is workspace ``default``. Other workspaces are local
folders or MinIO prefixes; nothing lands in ``default`` without an explicit
promote.
"""

from __future__ import annotations

from redibis.workspace.model import WORKSPACE_LAYOUT, WorkspaceRef

__all__ = ["WORKSPACE_LAYOUT", "WorkspaceRef"]
