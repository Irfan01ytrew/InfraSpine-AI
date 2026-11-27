"""Contracts for the independent Agent Studio API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class AgentStudioDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    instructions: str = Field(min_length=1, max_length=6000)
    tool_permissions: list[str] = Field(default_factory=lambda: ["web_search", "browse_url", "file_list", "file_read", "create_file", "generate_image", "remember", "recall"], max_length=12)
    approval_tools: list[str] = Field(default_factory=lambda: ["create_file", "generate_image"], max_length=8)
    delegate_agent_ids: list[uuid.UUID] = Field(default_factory=list, max_length=8)
    rag_enabled: bool = False
    max_steps: int = Field(default=6, ge=1, le=8)
    provider_id: str | None = Field(default=None, min_length=1, max_length=64)
    model: str | None = Field(default=None, min_length=1, max_length=160)

    @model_validator(mode="after")
    def provider_pair(self):
        if (self.provider_id is None) != (self.model is None):
            raise ValueError("Choose both a provider and a model, or leave both on the active default.")
        return self


class AgentStudioCreate(AgentStudioDefinition):
    pass


class AgentStudioUpdate(AgentStudioDefinition):
    expected_version: int = Field(ge=1)


class AgentStudioAgentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    version: int
    name: str
    description: str
    instructions: str
    tool_permissions: list[str]
    approval_tools: list[str]
    delegate_agent_ids: list[uuid.UUID]
    rag_enabled: bool
    max_steps: int
    provider_id: str | None
    model: str | None
    enabled: bool
    paused: bool
    created_at: datetime
    updated_at: datetime


class AgentStudioAgentList(BaseModel):
    agents: list[AgentStudioAgentOut]


class AgentStudioThreadOut(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime
    last_run_status: str | None = None
    last_run_at: datetime | None = None


class AgentStudioThreadList(BaseModel):
    threads: list[AgentStudioThreadOut]


class AgentStudioThreadCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(default="New agent run", min_length=1, max_length=180)


class AgentStudioRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    prompt: str = Field(min_length=1, max_length=6000)
    scheduled_at: AwareDatetime | None = None


class AgentStudioDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decision: Literal["approve", "reject"]
    note: str = Field(default="", max_length=500)


class AgentStudioPendingApproval(BaseModel):
    id: uuid.UUID
    tool_name: str
    summary: str


class AgentStudioRunOut(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    thread_id: uuid.UUID
    status: str
    prompt: str
    answer: str | None = None
    steps: list[dict] = Field(default_factory=list)
    pending_approval: AgentStudioPendingApproval | None = None
    failure_code: str | None = None
    attempt_count: int = 0
    next_attempt_at: datetime
    provider: str | None = None
    model: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_calls: int = 0
    estimated_cost_usd: float | None = None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class AgentStudioRunList(BaseModel):
    runs: list[AgentStudioRunOut]


class AgentStudioToolOut(BaseModel):
    name: str
    description: str
    requires_approval_by_default: bool = False
    available: bool = True
    availability_note: str | None = None


class AgentStudioTools(BaseModel):
    tools: list[AgentStudioToolOut]


class AgentStudioMemoryOut(BaseModel):
    id: uuid.UUID
    memory_key: str
    value: str
    updated_at: datetime


class AgentStudioMemoryList(BaseModel):
    memories: list[AgentStudioMemoryOut]


class AgentStudioMemoryWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    memory_key: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=1200)
