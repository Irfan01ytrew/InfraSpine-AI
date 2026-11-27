"""Admin-only preview and confirmation contracts for agent-run content retention."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AgentRunRetentionPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retention_days: int = Field(..., ge=30, le=3650)


class AgentRunRetentionPurgeRequest(AgentRunRetentionPreviewRequest):
    batch_size: int = Field(default=100, ge=1, le=1000)
    confirm: Literal[True]


class AgentRunRetentionPreviewResponse(BaseModel):
    dry_run: Literal[True] = True
    retention_days: int
    cutoff_at: datetime
    eligible_runs: int
    preserved_fields: list[str]


class AgentRunRetentionPurgeResponse(BaseModel):
    result: Literal["purged"] = "purged"
    retention_days: int
    cutoff_at: datetime
    purged_runs: int
    remaining_eligible_runs: int
    batch_size: int
    audit_event: Literal["agent.profile_run_content_purged"]
