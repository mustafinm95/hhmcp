from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class FieldState(BaseModel):
    value: Any = None
    state: Literal["value", "absent", "error"] = "absent"
    error: str | None = None


class Salary(BaseModel):
    lower: float | None = None
    upper: float | None = None
    currency: str | None = None
    period: str | None = None
    gross: bool | None = None


class SearchSpec(BaseModel):
    url: str | None = None
    text: str | None = None
    exclude: list[str] = Field(default_factory=list)
    area: list[str] = Field(default_factory=list)
    salary: float | None = None
    experience: str | None = None
    employment: list[str] = Field(default_factory=list)
    schedule: list[str] = Field(default_factory=list)
    working_hours: list[str] = Field(default_factory=list)
    work_format: list[str] = Field(default_factory=list)
    period: int | None = None
    order_by: str | None = None
    unchecked_parameters: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def source_exclusive(self):
        structured = any(
            [
                self.text,
                self.exclude,
                self.area,
                self.salary is not None,
                self.experience,
                self.employment,
                self.schedule,
                self.working_hours,
                self.work_format,
                self.period,
                self.order_by,
            ]
        )
        if self.url and structured:
            raise ValueError("url and structured filters are mutually exclusive")
        if not self.url and not structured:
            raise ValueError("provide url or at least one structured filter")
        return self


class Employer(BaseModel):
    hh_id: str
    name: str | None = None
    description: str | None = None
    site_url: str | None = None
    public_features: dict[str, Any] = Field(default_factory=dict)
    fetched_at: datetime | None = None


class Vacancy(BaseModel):
    hh_id: str
    url: str
    title: str | None = None
    description: str | None = None
    skills: list[str] = Field(default_factory=list)
    conditions: dict[str, Any] = Field(default_factory=dict)
    salary: Salary | None = None
    address: str | None = None
    metro: list[str] = Field(default_factory=list)
    published_at: datetime | None = None
    employer_id: str | None = None
    employer_name: str | None = None
    department_name: str | None = None
    contacts: dict[str, Any] = Field(default_factory=dict)
    archived: bool = False
    unavailable: bool = False
    parser_version: str = "1"
    field_states: dict[str, FieldState] = Field(default_factory=dict)
    discovered_at: datetime | None = None
    fetched_at: datetime | None = None
    cache_age_seconds: int | None = None


class RunState(StrEnum):
    queued = "queued"
    running = "running"
    paused = "paused"
    interrupted = "interrupted"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class Run(BaseModel):
    id: str
    state: RunState
    created_at: datetime
    updated_at: datetime
    stop_reason: str | None = None
    complete: bool = False
    limit: int = 1000
    discovered: int = 0
    accepted: int = 0
    loaded: int = 0
    cached: int = 0
    errors: int = 0
    search_specs: list[SearchSpec] = Field(default_factory=list)


class Criterion(BaseModel):
    field: str
    operator: Literal["eq", "contains", "gte", "in", "salary_minimum"]
    value: Any
    weight: float = Field(default=1, gt=0)
    required: bool = False
    currency: str | None = None
    period: str | None = None
    gross: bool | None = None


class CandidateProfile(BaseModel):
    id: str
    name: str
    criteria: list[Criterion]


class CriterionResult(BaseModel):
    criterion: Criterion
    result: Literal["match", "miss", "unknown"]
    explanation: str


class RankingResult(BaseModel):
    vacancy_id: str
    eligibility: Literal["подходит", "не подходит", "недостаточно данных"]
    score: float
    completeness: float
    criteria: list[CriterionResult]
