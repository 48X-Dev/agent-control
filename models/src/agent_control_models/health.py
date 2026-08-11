"""Health check models."""

from .base import BaseModel


class HealthResponse(BaseModel):
    """Health check response model."""

    status: str
    version: str
