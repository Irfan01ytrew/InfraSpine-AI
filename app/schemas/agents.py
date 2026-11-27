"""Validated requests and responses for the persisted, scoped agent catalog."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.rag import MarketResearchReport, RagChatResponse, TrainingPlan

AgentRunResult = RagChatResponse | TrainingPlan | MarketResearchReport

AgentType = Literal["knowledge_research", "market_research", "company_analysis", "employee_training"]
AgentTool = Literal["knowledge_search", "public_web_research", "company_data_analysis", "training_plan"]
KnowledgeScope = Literal["requester_acl", "department_only"]


class AgentProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(..., min_length=1, max_length=120)
    department_group_id: uuid.UUID
    agent_type: AgentType
    knowledge_scope: KnowledgeScope = "requester_acl"
    tool_policy: list[AgentTool] = Field(..., min_length=1, max_length=4)


class AgentProfileVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_version: int = Field(..., ge=1)
    name: str = Field(..., min_length=1, max_length=120)
    agent_type: AgentType
    knowledge_scope: KnowledgeScope
    tool_policy: list[AgentTool] = Field(..., min_length=1, max_length=4)


class AgentProfileOut(BaseModel):
    id: uuid.UUID
    agent_key: uuid.UUID
    version: int
    name: str
    agent_type: AgentType
    department_group_id: uuid.UUID
    department_name: str
    knowledge_scope: KnowledgeScope
    tool_policy: list[AgentTool]
    enabled: bool
    created_at: datetime
    can_manage: bool


class AgentProfileListResponse(BaseModel):
    agents: list[AgentProfileOut]


class AgentDepartmentOut(BaseModel):
    id: uuid.UUID
    name: str


class AgentDepartmentListResponse(BaseModel):
    departments: list[AgentDepartmentOut]


class AgentRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    capability: AgentTool
    task: str = Field(..., min_length=1, max_length=4000)
    document_ids: list[uuid.UUID] | None = Field(default=None, max_length=50)
    confirm_external_search: bool = False


class AgentRunResponse(BaseModel):
    run_id: uuid.UUID
    profile_id: uuid.UUID
    profile_version: int
    capability: AgentTool
    result: AgentRunResult


class AgentRunRecordOut(BaseModel):
    run_id: uuid.UUID
    profile_id: uuid.UUID
    profile_version: int
    capability: AgentTool
    status: Literal["queued", "running", "completed", "failed"]
    attempts: int
    failure_code: str | None = None
    retry_available: bool
    requested_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    content_purged_at: datetime | None = None
    result: AgentRunResult | None = None


class AgentRunHistoryOut(BaseModel):
    runs: list[AgentRunRecordOut]
