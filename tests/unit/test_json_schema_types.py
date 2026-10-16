"""Tests that the JSON Schema types cover the whole specification."""

from collections.abc import Mapping
from typing import Any

from jsonschema_specifications import REGISTRY

from family_assistant.json_schema import JsonSchemaObject

_DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"

# Draft-07's companion to tuple-form ``items``. The 2020-12 metaschema no longer
# names it, but draft-07 schemas (still emitted by common MCP server toolkits) use it.
_DRAFT_07_ONLY = {"additionalItems"}


def _metaschema(uri: str) -> Mapping[str, Any]:
    contents = REGISTRY.contents(uri)
    assert isinstance(contents, Mapping)
    return contents


def _metaschema_keywords() -> set[str]:
    top = _metaschema(_DRAFT_2020_12)
    keywords = set(top["properties"])
    for vocabulary in top["allOf"]:
        keywords |= set(
            _metaschema(f"https://json-schema.org/draft/2020-12/{vocabulary['$ref']}")[
                "properties"
            ]
        )
    return keywords


def test_declares_every_draft_2020_12_keyword() -> None:
    declared = set(JsonSchemaObject.__optional_keys__) - _DRAFT_07_ONLY

    assert declared == _metaschema_keywords()
