"""通用接口契约；模块字段由领域服务持有，不再为每个模块复制一份模型类。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    size: int = 20


class ActionResult(BaseModel):
    ok: bool
    message: str
    entry: dict[str, Any] | None = None


class EntryPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None


class ActionPayload(BaseModel):
    # 允许迁移/补报类动作携带 fence、幂等键、时间窗等额外参数。
    model_config = ConfigDict(extra="allow")

    action: str
    remark: str | None = None
    site: str | None = None
    amount: int | str | None = None
    window_start: str | None = None
    report_key: str | None = None
    expected_fence: int | None = None
    conflict_drill: bool | None = None
