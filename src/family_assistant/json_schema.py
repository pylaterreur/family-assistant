"""Static types for JSON Schema documents.

Tool parameter schemas arrive from local tool definitions and from MCP servers,
and use whatever JSON Schema the author wrote: integer enums, schema-valued
``additionalProperties``, ``anyOf``, draft-07 ``definitions``, vendor keywords.
These types describe that whole language rather than the part our own tools
happen to use, so a schema from anywhere fits them unchanged.

``JsonSchemaObject`` declares every keyword of JSON Schema Draft 2020-12 (and the
draft-07 spellings still common in the wild) with its type from the
specification. Any other key is allowed with any value, so unrecognised
keywords are preserved rather than rejected or dropped.
``tests/unit/test_json_schema_types.py`` checks the keyword list against the
official metaschemas, so it cannot drift into a subset.

These are static types only. Nothing validates a schema against them at
runtime; a schema read from outside is cast at the boundary.

One project-specific value is in use: ``"type": "attachment"`` marks a parameter
that carries an attachment id (see ``tools/attachment_utils.py``), which is why
``type`` is a plain string rather than the seven standard type names.
"""

from __future__ import annotations

from typing_extensions import TypedDict

type JsonValue = (
    str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
)

# The functional form is required because several keywords (``$ref``, ``if``,
# ``else``, ``not``) are not valid Python identifiers.
JsonSchemaObject = TypedDict(
    "JsonSchemaObject",
    {
        # Core vocabulary
        "$schema": str,
        "$id": str,
        "$ref": str,
        "$anchor": str,
        "$dynamicRef": str,
        "$dynamicAnchor": str,
        "$vocabulary": dict[str, bool],
        "$comment": str,
        "$defs": "dict[str, JsonSchema]",
        # Applicator vocabulary
        "prefixItems": "list[JsonSchema]",
        # A list is the draft-07 tuple form, which 2020-12 spells ``prefixItems``.
        "items": "JsonSchema | list[JsonSchema]",
        "contains": "JsonSchema",
        "additionalProperties": "JsonSchema",
        "properties": "dict[str, JsonSchema]",
        "patternProperties": "dict[str, JsonSchema]",
        "dependentSchemas": "dict[str, JsonSchema]",
        "propertyNames": "JsonSchema",
        "if": "JsonSchema",
        "then": "JsonSchema",
        "else": "JsonSchema",
        "allOf": "list[JsonSchema]",
        "anyOf": "list[JsonSchema]",
        "oneOf": "list[JsonSchema]",
        "not": "JsonSchema",
        # Unevaluated vocabulary
        "unevaluatedItems": "JsonSchema",
        "unevaluatedProperties": "JsonSchema",
        # Validation vocabulary
        "type": str | list[str],
        "const": JsonValue,
        "enum": list[JsonValue],
        "multipleOf": int | float,
        "maximum": int | float,
        "exclusiveMaximum": int | float,
        "minimum": int | float,
        "exclusiveMinimum": int | float,
        "maxLength": int,
        "minLength": int,
        "pattern": str,
        "maxItems": int,
        "minItems": int,
        "uniqueItems": bool,
        "maxContains": int,
        "minContains": int,
        "maxProperties": int,
        "minProperties": int,
        "required": list[str],
        "dependentRequired": dict[str, list[str]],
        # Meta-data vocabulary
        "title": str,
        "description": str,
        "default": JsonValue,
        "deprecated": bool,
        "readOnly": bool,
        "writeOnly": bool,
        "examples": list[JsonValue],
        # Format vocabulary
        "format": str,
        # Content vocabulary
        "contentEncoding": str,
        "contentMediaType": str,
        "contentSchema": "JsonSchema",
        # Draft-07 and 2019-09 keywords that the 2020-12 metaschema still accepts
        "definitions": "dict[str, JsonSchema]",
        "dependencies": "dict[str, JsonSchema | list[str]]",
        "$recursiveAnchor": bool,
        "$recursiveRef": str,
        "additionalItems": "JsonSchema",
    },
    total=False,
    # ``object`` rather than ``JsonValue``: with a recursive extra-items type,
    # basedpyright's assignability checks between this and ``dict`` explode.
    extra_items=object,
)

type JsonSchema = JsonSchemaObject | bool
"""A JSON Schema: an object, or ``true``/``false`` (accept or reject everything)."""
