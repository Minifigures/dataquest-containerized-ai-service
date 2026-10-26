"""Request and response contracts.

`extra: forbid` is the point of these models. Without it, a caller that sends
`{"promt": "..."}` gets a 200 and a silently empty optimization, because the
typo is accepted and ignored. With it they get a 422 naming the unexpected
field, which is a bug report instead of a mystery.
"""

from pydantic import BaseModel, Field


class PromptRequest(BaseModel):
    model_config = {"extra": "forbid"}

    prompt: str = Field(min_length=1, max_length=4000,
                        description="The prompt to improve.")
    style: str = Field(default="concise",
                       description="concise, detailed, or formal.")


class PromptResponse(BaseModel):
    model_config = {"extra": "forbid"}

    original: str
    optimized: str
    style: str
    model: str
    source: str = Field(
        description="'llm' when a provider answered, 'rules' when the local "
                    "deterministic rewriter did. Callers that must not ship a "
                    "rules-based answer can check this field.")


class HealthResponse(BaseModel):
    model_config = {"extra": "forbid"}

    status: str
    version: str
    database: str
    llm: str
