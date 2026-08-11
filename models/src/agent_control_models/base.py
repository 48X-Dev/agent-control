"""Base models and utilities for Agent Control."""

from typing import Any

from pydantic import BaseModel as PydanticBaseModel
from pydantic import ConfigDict


class BaseModel(PydanticBaseModel):
    """Base model for all Agent Control models."""

    model_config = ConfigDict(
        # Allow both snake_case and camelCase in JSON
        populate_by_name=True,
        # Use enum values in JSON output
        use_enum_values=True,
        # Validate on assignment
        validate_assignment=True,
        # Allow extra fields to be ignored (forward compatibility)
        extra="ignore",
    )

    def to_dict(self) -> dict[str, Any]:
        """Convert model to dictionary."""
        return self.model_dump(mode="python", exclude_none=True)

    def to_json(self) -> str:
        """Convert model to JSON string."""
        return self.model_dump_json(exclude_none=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BaseModel":
        """Create model instance from dictionary."""
        return cls.model_validate(data)

    @classmethod
    def from_json(cls, json_str: str) -> "BaseModel":
        """Create model instance from JSON string."""
        return cls.model_validate_json(json_str)
