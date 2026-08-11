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
        "Read a text or image file from the task workspace or the active skill's "
        "read-only directory. Path must be relative (absolute paths are rejected). "
        "Text formats (.txt/.md/.json/.yaml/.csv/.py/.svg/.xml/.html etc.) return "
        "content with optional offset/limit pagination; images (.png/.jpg/.jpeg/.gif/"
        ".webp) return a base64 view. .md also returns a 'media' list of images it "
        "references (materialized under work/_media/). PDF is preprocessed once by "
        "the shared material layer and must be read through [MATERIALS] IR paths.",
        {
            "path": {"type": "string", "description": "Relative path from workspace root"},
            "offset": {"type": "integer", "minimum": 0, "description": "Line offset for text (default 0)"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "description": "Max lines for text (default 500)"},
        },
        ["path"],
    ),
    _function(
        "ingest",
        "Legacy compatibility extraction for docx / md / txt only. Task uploads "
        "are already parsed by the shared material layer; read its DocumentIR "
        "instead of calling ingest. This compatibility action writes a markdown "
        "projection under work/materials/: a markdown file "
        "(images as ![alt](path), tables as HTML, formulas as $...$, code as "
        "fenced blocks), a _media/ folder with materialized images, and a "
        "manifest.json index. Returns the manifest entry for the file with "
        "media metadata (width/height/aspect/portrait_likely). Idempotent: "
        "already-ingested files return the cached entry. Path must be relative "
        "and inside the workspace.",
        {
            "path": {"type": "string", "description": "Relative workspace path of the material to ingest"},
        },
        ["path"],
    ),
    _function(
        "write",
        "Create or completely rewrite a UTF-8 text file in the task workspace. "
        "Path must be relative. Supports text formats including .svg/.xml/.json/"
        ".md/.py/.html. Binary formats (.pptx/.docx/.pdf) are NOT supported — "
        "produce them via exec_cmd scripts instead.",
        {
            "path": {"type": "string", "description": "Relative path from workspace root"},
            "content": {"type": "string", "description": "Full file content (UTF-8 text)"},
        },
        ["path", "content"],
    ),
    _function(
        "edit",
        "Replace an exact text snippet in an existing workspace text file. "
        "old_text must be unique unless replace_all=true. Path must be relative.",
        {
            "path": {"type": "string", "description": "Relative path from workspace root"},
            "old_text": {"type": "string", "description": "Exact text to find"},
            "new_text": {"type": "string", "description": "Replacement text"},
            "replace_all": {"type": "boolean", "default": False, "description": "Replace all occurrences"},
        },
        ["path", "old_text", "new_text"],
    ),
    _function(
        "exec_cmd",
        "Run a skill-declared script action. The 'action' name and its argument "
        "keys come from the active skill's manifest.json 'scripts' table. "
        "Returns JSON with exit_code, stdout (last 8000 chars), and stderr.",
        {
            "action": {"type": "string", "description": "Action name from manifest.json scripts"},
            "args": {
                "type": "object",
                "description": "Argument dict; keys match manifest argv placeholders. "
                "An optional 'env' sub-dict is appended to the command line as "
                "KEY=VALUE pairs for scripts that want to read os.environ.",
            },
        },
        ["action", "args"],
    ),
    _function(
        "ask_user_questions",
        "Pause the task and ask the user structured questions. This is the ONLY "
        "way to get user input — there is no chat channel. Ask all needed questions "
        "in one call. The task resumes when the user submits answers.",
        {
            "questions": {
                "type": "array",
                "description": "All questions to ask the user now",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Stable identifier for the answer"},
                        "label": {"type": "string", "description": "User-facing question text"},
                        "type": {
                            "type": "string",
                            "enum": ["text", "textarea", "select"],
                            "description": "Input field type",
                        },
                        "required": {"type": "boolean", "description": "Whether the user must answer"},
                        "options": {"type": "array", "items": {"type": "string"}, "description": "Choices for select type"},
                    },
                    "required": ["id", "label", "type"],
                    "additionalProperties": False,
                },
            }
        },
        ["questions"],
    ),
    _function(
        "task_failed",
        "Abort the task with an explicit failure reason. Use when a required "
        "external step (e.g. MinerU conversion or rendering) failed and cannot "
        "be recovered — never call finish_task with a fake or missing artifact. "
        "This must be the ONLY tool call in its turn.",
        {"error": {"type": "string", "description": "User-facing failure reason"}},
        ["error"],
    ),
    _function(
        "finish_task",
        "Finish the task. Pass relative paths of verified artifact files (must "
        "exist in the workspace). They will be copied to the output directory. "
        "This must be the ONLY tool call in its turn.",
        {"artifacts": {"type": "array", "items": {"type": "string"}, "minItems": 1, "description": "Relative workspace paths of final artifacts"}},
        ["artifacts"],
    ),
]
