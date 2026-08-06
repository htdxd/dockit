from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from skill_toolbox.actions.docx import build_simple_docx
from skill_toolbox.models import ImageContent, ToolCall, ToolResult
from skill_toolbox.policy import PolicyViolation, WorkspacePolicy

MAX_TEXT_BYTES = 1_000_000
MAX_IMAGE_BYTES = 12_000_000
TEXT_SUFFIXES = {
    ".txt",
    ".md",
    ".json",
    ".yaml",
    ".yml",
    ".csv",
    ".py",
    ".html",
    ".htm",
    ".css",
    ".js",
    ".ts",
    ".svg",
    ".xml",
    ".rs",
    ".toml",
    ".ini",
    ".cfg",
    ".log",
}


class ToolRegistry:
    def __init__(
        self,
        policy: WorkspacePolicy,
        allowed_actions: frozenset[str],
        skill_dir: Path = Path("."),
        scripts: dict[str, tuple[str, tuple[str, ...]]] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.policy = policy
        self.allowed_actions = allowed_actions
        self.skill_dir = skill_dir
        self.scripts = scripts or {}
        # Extra env vars merged into exec_cmd subprocesses (e.g. MINERU_TOKEN).
        self.extra_env = env or {}
        self.actions: dict[str, Callable[[dict[str, Any]], None]] = {
            "build_simple_docx": self._build_simple_docx,
        }

    async def execute(self, call: ToolCall) -> ToolResult:
        try:
            if call.name == "read":
                return await asyncio.to_thread(self._read, call)
            if call.name == "write":
                return await asyncio.to_thread(self._write, call)
            if call.name == "edit":
                return await asyncio.to_thread(self._edit, call)
            if call.name == "exec_cmd":
                return await self._exec_cmd_async(call)
            return self._result(call, False, f"Unknown data tool: {call.name}")
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return self._result(call, False, str(exc))

    async def _exec_cmd_async(self, call: ToolCall) -> ToolResult:
        """Dispatch exec_cmd: builtins run in-process; declared scripts run as
        subprocesses with cooperative cancellation so runtime timeouts actually
        terminate the spawned process instead of leaking it."""
        action = str(call.arguments["action"])
        if action in self.actions:
            return await asyncio.to_thread(self._exec_builtin, call)
        proc_holder: dict[str, subprocess.Popen[bytes] | None] = {"proc": None}

        def runner() -> ToolResult:
            proc_holder["proc"] = subprocess.Popen(
                self._build_cmd(call),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self.policy.root),
                env={**os.environ, **self.extra_env},
                close_fds=True,
            )
            try:
                stdout_b, stderr_b = proc_holder["proc"].communicate(timeout=300)
            except subprocess.TimeoutExpired:
                self._kill(proc_holder["proc"])
                return self._result(call, False, f"Script timed out: {call.arguments.get('action')}")
            rc = proc_holder["proc"].returncode or 0
            return self._result(
                call,
                rc == 0,
                json.dumps(
                    {
                        "exit_code": rc,
                        "stdout": stdout_b.decode("utf-8", errors="replace")[-8000:],
                        "stderr": stderr_b.decode("utf-8", errors="replace")[-2000:],
                    },
                    ensure_ascii=False,
                ),
            )

        task = asyncio.get_running_loop().run_in_executor(None, runner)
        try:
            return await task
        except asyncio.CancelledError:
            # Runtime timed out (asyncio.wait_for) — kill the subprocess so it
            # doesn't keep consuming resources while we report the timeout.
            self._kill(proc_holder["proc"])
            raise

    @staticmethod
    def _kill(proc: subprocess.Popen[bytes] | None) -> None:
        if proc is None:
            return
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    def _build_cmd(self, call: ToolCall) -> list[str]:
        action = str(call.arguments["action"])
        args = dict(call.arguments.get("args", {}))
        if action not in self.allowed_actions and action not in self.scripts:
            raise ValueError(f"Action is not allowed: {action}")
        entry_rel, argv_template = self.scripts[action]
        entry = self.skill_dir / "scripts" / entry_rel
        if not entry.is_file():
            entry = self.skill_dir / "custom_scripts" / entry_rel
        if not entry.is_file():
            raise ValueError(f"Script not found for action: {action}")
        cmd = [sys.executable, str(entry)]
        cmd.extend(self._render(token, args) for token in argv_template)
        if env_args := args.get("env", {}):
            if not isinstance(env_args, dict):
                raise ValueError("env must be an object mapping variable names to values")
            cmd.extend(f"{name}={value}" for name, value in env_args.items())
        return cmd

    def _result(
        self,
        call: ToolCall,
        success: bool,
        content: str,
        images: list[ImageContent] | None = None,
    ) -> ToolResult:
        return ToolResult(
            tool_call_id=call.id,
            name=call.name,
            success=success,
            content=content,
            images=images or [],
        )

    def _read(self, call: ToolCall) -> ToolResult:
        path_str = str(call.arguments["path"])
        try:
            path = self.policy.require_file(path_str)
        except PolicyViolation:
            path = self.policy.find_readonly(path_str)
            if path is None:
                raise
        suffix = path.suffix.lower()
        if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
            if path.stat().st_size > MAX_IMAGE_BYTES:
                raise ValueError("Image exceeds the 12 MB read limit")
            media_type = (
                mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            )
            image = ImageContent(
                media_type=media_type,
                base64_data=base64.b64encode(path.read_bytes()).decode("ascii"),
            )
            return self._result(call, True, f"Read image: {path.name}", [image])
        if suffix not in TEXT_SUFFIXES:
            raise ValueError(f"Unsupported read format: {suffix or 'unknown'}")
        if path.stat().st_size > MAX_TEXT_BYTES:
            raise ValueError("Text file exceeds the 1 MB read limit")
        lines = path.read_text(encoding="utf-8").splitlines()
        offset = int(call.arguments.get("offset", 0))
        limit = int(call.arguments.get("limit", 500))
        content = "\n".join(lines[offset : offset + limit])
        return self._result(
            call,
            True,
            json.dumps(
                {"content": content, "offset": offset, "total_lines": len(lines)},
                ensure_ascii=False,
            ),
        )

    def _write(self, call: ToolCall) -> ToolResult:
        path = self.policy.resolve(str(call.arguments["path"]))
        content = str(call.arguments["content"])
        if path.suffix.lower() not in TEXT_SUFFIXES:
            raise ValueError("write only supports text files")
        if len(content.encode("utf-8")) > MAX_TEXT_BYTES:
            raise ValueError("Content exceeds the 1 MB write limit")
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(content, encoding="utf-8")
        temp.replace(path)
        return self._result(call, True, f"Wrote {path.relative_to(self.policy.root)}")

    def _edit(self, call: ToolCall) -> ToolResult:
        path = self.policy.require_file(str(call.arguments["path"]))
        if path.suffix.lower() not in TEXT_SUFFIXES:
            raise ValueError("edit only supports text files")
        content = path.read_text(encoding="utf-8")
        old_text = str(call.arguments["old_text"])
        new_text = str(call.arguments["new_text"])
        count = content.count(old_text)
        if count == 0:
            raise ValueError("old_text was not found")
        if count > 1 and not bool(call.arguments.get("replace_all", False)):
            raise ValueError("old_text is not unique; set replace_all=true")
        updated = content.replace(
            old_text, new_text, -1 if call.arguments.get("replace_all") else 1
        )
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(updated, encoding="utf-8")
        temp.replace(path)
        return self._result(call, True, f"Edited {path.relative_to(self.policy.root)}")

    def _exec_builtin(self, call: ToolCall) -> ToolResult:
        """Handle in-process builtin actions (e.g. build_simple_docx)."""
        action = str(call.arguments["action"])
        args = dict(call.arguments.get("args", {}))
        if action not in self.allowed_actions and action not in self.scripts:
            raise ValueError(f"Action is not allowed: {action}")
        builtin = self.actions.get(action)
        if builtin is None:
            raise ValueError(
                f"Action {action} is not a builtin; it must run via the subprocess path"
            )
        builtin(args)
        return self._result(call, True, f"Action completed: {action}")

    def _render(self, token: str, args: dict[str, Any]) -> str:
        for key, value in args.items():
            token = token.replace("{" + key + "}", str(value))
        return token.replace("${SKILL_DIR}", str(self.skill_dir))

    def _build_simple_docx(self, args: dict[str, Any]) -> None:
        spec_path = self.policy.require_file(str(args["spec_path"]))
        output_path = self.policy.resolve(str(args["output_path"]))
        build_simple_docx(spec_path, output_path)
