"""通过正式工具绑定验证领域行为；仅 Word 渲染由调用测试的 fixture 替换。"""

from skill_toolbox.agent_loop import execute_tool
from skill_toolbox.models import ToolCall
from skill_toolbox.resume_task import ResumeTask
from skill_toolbox.runtime import TaskRequest, UserInputBroker
from skill_toolbox.skills import load_skill


def task_for(workflow, emit=lambda e: None):
    request = TaskRequest(
        "resume_pro",
        "test",
        workflow.engine.workspace / "out",
        template_id=workflow.template_id,
        capabilities={"vision": True},
    )
    task = ResumeTask(
        workflow.engine.workspace,
        load_skill("resume_pro"),
        request,
        None,
        emit,
        lambda e: None,
        UserInputBroker(),
        10,
        90,
    )
    task.workflow = workflow
    return task


async def invoke(task, name, args):
    return await execute_tool(
        ToolCall(id="test", name=name, arguments=args),
        task.tools(),
        emit=task.emit,
        debug=task.debug,
        cancel=task.cancel,
    )
