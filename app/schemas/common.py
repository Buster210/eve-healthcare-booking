from typing import Annotated

from fastapi import Query
from pydantic import BaseModel, Field

Int4Id = Annotated[int, Field(gt=0, le=2_147_483_647)]


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int


def pagination_params(
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=2_147_483_647)] = 0,
) -> tuple[int, int]:
    return limit, offset
