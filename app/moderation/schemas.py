from typing import Literal, Optional
from pydantic import BaseModel, Field


Label = Literal["SCAM", "OK"]
Category = Literal["job_scam", "crypto", "investment", "phishing", "other", "none"]


class LlmModerationResult(BaseModel):
    label: Label
    category: Category
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    raw_response: Optional[dict] = None
