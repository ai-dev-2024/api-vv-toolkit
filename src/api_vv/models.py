"""Strict data contracts shared by generation, execution and reporting."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Schema = dict[str, Any] | bool
Priority = Literal["must", "should", "could"]
GateStatus = Literal["failed", "not covered", "partially covered"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Parameter(Model):
    name: str
    location: Literal["path", "query", "header", "cookie"]
    required: bool = False
    schema_: Schema = Field(default_factory=dict, alias="schema")
    example: Any = None


class Header(Model):
    required: bool = False
    schema_: Schema = Field(default_factory=dict, alias="schema")


class Response(Model):
    content: dict[str, Schema] = Field(default_factory=dict)
    headers: dict[str, Header] = Field(default_factory=dict)


class SecurityScheme(Model):
    type: Literal["apiKey", "http", "oauth2", "openIdConnect"]
    name: str | None = None
    location: Literal["header", "query", "cookie"] | None = None
    scheme: str | None = None


class Operation(Model):
    id: str
    method: str
    path: str
    parameters: list[Parameter] = Field(default_factory=list)
    request_schema: Schema | None = None
    request_required: bool = False
    request_media_type: str = "application/json"
    responses: dict[str, Response]
    security: list[dict[str, list[str]]] = Field(default_factory=list)

    @property
    def auth_required(self) -> bool:
        return bool(self.security) and all(self.security)


class Spec(Model):
    openapi: str
    title: str
    operations: dict[str, Operation]
    security_schemes: dict[str, SecurityScheme] = Field(default_factory=dict)


class Coverage(Model):
    operation_id: str
    responses: list[str] = Field(min_length=1)
    checks: list[str] = Field(default_factory=list)


class Requirement(Model):
    id: str = Field(pattern=r"^REQ-\d{3,}$")
    text: str = Field(min_length=1)
    priority: Priority
    verification_method: Literal["test", "demonstration", "inspection", "analysis"]
    covers: list[Coverage] = Field(min_length=1)
    latency_ms: float | None = Field(default=None, gt=0, allow_inf_nan=False)


class Requirements(Model):
    requirements: list[Requirement] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids(self) -> Requirements:
        ids = [item.id for item in self.requirements]
        if len(ids) != len(set(ids)):
            raise ValueError("requirement ids must be unique")
        return self


class Case(Model):
    id: str
    name: str
    operation_id: str
    parameters: dict[str, dict[str, Any]] = Field(default_factory=dict)
    body: Any = None
    has_body: bool = False
    expected_status: str = Field(pattern=r"^(?:[1-5][0-9]{2}|[1-5]XX|default)$")
    negative: bool = False
    auth: Literal["configured", "omit"] = "configured"
    enabled: bool = True
    review_reason: str | None = None
    generators: list[str]
    checks: list[str]
    requirement_ids: list[str] = Field(default_factory=list)


class ProposalReview(Model):
    index: int
    accepted: bool
    reason: str
    case_id: str | None = None


class Audit(Model):
    requirement_id: str
    provider: str
    prompt: str
    raw_response: str
    reviews: list[ProposalReview]


class Plan(Model):
    version: Literal["1.0"] = "1.0"
    spec: Spec
    requirements: Requirements
    cases: list[Case]
    llm_audit: list[Audit] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_ids(self) -> Plan:
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("case ids must be unique")
        return self


class Result(Model):
    case_id: str
    verdict: Literal["passed", "failed", "skipped"]
    actual_status: int | None = None
    latency_ms: float | None = None
    errors: list[str] = Field(default_factory=list)
    attempts: int = 0


class Run(Model):
    plan_digest: str
    results: list[Result]


class LLMConfig(Model):
    provider: Literal["mock", "openai", "anthropic"] = "mock"
    model: str = "mock-v1"
    base_url_env: str = "API_VV_LLM_BASE_URL"
    api_key_env: str = "API_VV_LLM_API_KEY"
    fixtures: str | None = None
    timeout_seconds: float = Field(default=30, gt=0, allow_inf_nan=False)


class Config(Model):
    concurrency: int = Field(default=5, ge=1, le=100)
    timeout_seconds: float = Field(default=10, gt=0, allow_inf_nan=False)
    retries: int = Field(default=0, ge=0, le=5)
    credentials_env: dict[str, str] = Field(default_factory=dict)
    # Pydantic copies mutable defaults per instance.
    gate_priorities: list[Priority] = ["must"]
    gate_statuses: list[GateStatus] = ["failed", "not covered", "partially covered"]
    llm: LLMConfig = Field(default_factory=LLMConfig)
