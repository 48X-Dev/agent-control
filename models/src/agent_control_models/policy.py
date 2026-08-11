from .base import BaseModel
from .controls import ControlDefinition, UnrenderedTemplateControl


class Control(BaseModel):
    """A control with identity and configuration."""

    id: int
    name: str
    control: ControlDefinition | UnrenderedTemplateControl


class Policy(BaseModel):
    """A policy with its associated controls."""

    id: int
    name: str
    controls: list[Control]
