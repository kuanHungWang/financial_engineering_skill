"""Component registry: the factory side of the design.

Each component kind (curve, vol, swaption_vol, schedule, model, observable, leg, condition, event, analytic) maps a
`type` string to a factory class. A factory class declares:

    Spec         pydantic model of its input fields (extra fields forbidden)
    description  one-line text shown in the catalog

and kind-specific methods (see the base classes in the component modules).

Adding a new product feature means registering one more factory; the assembler,
validator and catalog pick it up automatically. This is also the hook for
user-supplied Python components in a later stage.
"""
from __future__ import annotations

from typing import Type

from pydantic import BaseModel, ConfigDict, ValidationError

from .conventions import suggest

KINDS = ("curve", "vol", "swaption_vol", "schedule", "model", "observable", "leg", "condition", "event", "analytic")

_REGISTRY: dict[str, dict[str, type]] = {k: {} for k in KINDS}


class SpecModel(BaseModel):
    """Base for all component specs: unknown fields are errors, not silently ignored."""
    model_config = ConfigDict(extra="forbid")


def register(kind: str, type_name: str):
    if kind not in _REGISTRY:
        raise KeyError(f"unknown component kind '{kind}'")

    def deco(cls):
        if type_name in _REGISTRY[kind]:
            raise KeyError(f"{kind} type '{type_name}' registered twice")
        cls.kind = kind
        cls.type_name = type_name
        _REGISTRY[kind][type_name] = cls
        return cls
    return deco


def get(kind: str, type_name: str) -> type:
    table = _REGISTRY[kind]
    if type_name not in table:
        raise KeyError(f"unknown {kind} type '{type_name}'.{suggest(type_name, table)} "
                       f"Available {kind} types: {sorted(table)}")
    return table[type_name]


def types(kind: str) -> dict[str, type]:
    return dict(_REGISTRY[kind])


def parse(kind: str, raw, path: str, report) -> tuple[type, BaseModel] | tuple[None, None]:
    """Validate a raw dict against the Spec of its registered `type`.

    Returns (factory_class, spec) or (None, None) after recording issues."""
    if not isinstance(raw, dict):
        report.error(path, f"expected a mapping with a 'type' field, got {type(raw).__name__}")
        return None, None
    t = raw.get("type")
    if t is None:
        report.error(path + ".type", f"missing 'type'. Available {kind} types: {sorted(_REGISTRY[kind])}")
        return None, None
    try:
        cls = get(kind, t)
    except KeyError as e:
        report.error(path + ".type", str(e.args[0]))
        return None, None
    body = {k: v for k, v in raw.items() if k != "type"}
    try:
        return cls, cls.Spec.model_validate(body)
    except ValidationError as e:
        report.extend_pydantic(path, e)
        return None, None


def catalog(kind: str | None = None) -> dict:
    kinds = [kind] if kind else list(KINDS)
    out = {}
    for k in kinds:
        out[k] = {}
        for name, cls in sorted(_REGISTRY[k].items()):
            schema = cls.Spec.model_json_schema()
            out[k][name] = {
                "description": getattr(cls, "description", ""),
                "fields": schema.get("properties", {}),
                "required": schema.get("required", []),
            }
            if "$defs" in schema:       # nested blocks referenced by "$ref" in fields
                out[k][name]["definitions"] = schema["$defs"]
    return out
