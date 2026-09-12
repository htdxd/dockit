"""Runtime routes and dispatcher follow the same visible tool definitions."""

from skill_toolbox.llm_tools.dispatcher import DomainServices, build_dispatcher
from skill_toolbox.runtime import _DOMAIN_TOOL_NAMES
from unittest.mock import Mock


def test_visible_domain_tools_have_dispatch_handlers() -> None:
    legacy = build_dispatcher(DomainServices())
    workflow = build_dispatcher(DomainServices(resume_workflow=Mock()))
    assert _DOMAIN_TOOL_NAMES == legacy.keys() | workflow.keys()
    assert "resume_generate_v2" not in workflow
    assert "resume_edit" in workflow
