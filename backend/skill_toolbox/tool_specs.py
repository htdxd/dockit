from __future__ import annotations

from typing import Any


def _function(
    name: str, description: str, properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


TOOL_SPECS = [
    _function(
        "read",
        "Read a text file or image from the task workspace.",
        {
            "path": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 2000},
        },
        ["path"],
    ),
    _function(
        "write",
        "Create or completely rewrite a UTF-8 text file in the task workspace.",
        {"path": {"type": "string"}, "content": {"type": "string"}},
        ["path", "content"],
    ),
    _function(
        "edit",
        "Replace exact text in an existing UTF-8 text file.",
        {
            "path": {"type": "string"},
            "old_text": {"type": "string"},
            "new_text": {"type": "string"},
            "replace_all": {"type": "boolean", "default": False},
        },
        ["path", "old_text", "new_text"],
    ),
    _function(
        "exec_cmd",
        "Run an official action allowlisted by the active skill.",
        {"action": {"type": "string"}, "args": {"type": "object"}},
        ["action", "args"],
    ),
    _function(
        "ask_user_questions",
        "Pause the task and ask all necessary structured questions together.",
        {
            "questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "label": {"type": "string"},
                        "type": {
                            "type": "string",
                            "enum": ["text", "textarea", "select"],
                        },
                        "required": {"type": "boolean"},
                        "options": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["id", "label", "type"],
                    "additionalProperties": False,
                },
            }
        },
        ["questions"],
    ),
    _function(
        "finish_task",
        "Finish the task after verifying every artifact exists.",
        {"artifacts": {"type": "array", "items": {"type": "string"}, "minItems": 1}},
        ["artifacts"],
    ),
]
