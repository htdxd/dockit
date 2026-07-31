from pathlib import Path

import pytest
from skill_toolbox.policy import PolicyViolation, WorkspacePolicy


def test_workspace_policy_accepts_relative_path(tmp_path: Path) -> None:
    policy = WorkspacePolicy(tmp_path)

    resolved = policy.resolve("work/document.json")

    assert resolved == tmp_path / "work" / "document.json"


@pytest.mark.parametrize(
    "candidate",
    ["../outside.txt", "../../Windows/System32", "C:/Windows/System32/config"],
)
def test_workspace_policy_rejects_escape(tmp_path: Path, candidate: str) -> None:
    policy = WorkspacePolicy(tmp_path)

    with pytest.raises(PolicyViolation):
        policy.resolve(candidate)


def test_workspace_policy_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    link = tmp_path / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation is unavailable on this Windows host")

    policy = WorkspacePolicy(tmp_path)

    with pytest.raises(PolicyViolation):
        policy.resolve("linked/secret.txt")
