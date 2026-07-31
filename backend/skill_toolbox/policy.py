from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class PolicyViolation(ValueError):
    """Raised when a tool tries to leave its task workspace."""


@dataclass(frozen=True)
class WorkspacePolicy:
    root: Path
    read_roots: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.resolve())
        object.__setattr__(
            self, "read_roots", tuple(path.resolve() for path in self.read_roots)
        )

    def resolve(self, relative_path: str, *, must_exist: bool = False) -> Path:
        raw = Path(relative_path)
        if raw.is_absolute():
            raise PolicyViolation("Absolute paths are not allowed")
        candidate = (self.root / raw).resolve(strict=False)
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise PolicyViolation("Path escapes the task workspace") from exc
        if must_exist and not candidate.exists():
            raise PolicyViolation(f"Path does not exist: {relative_path}")
        return candidate

    def require_file(self, relative_path: str) -> Path:
        path = self.resolve(relative_path, must_exist=True)
        if not path.is_file():
            raise PolicyViolation(f"Path is not a file: {relative_path}")
        return path

    def find_readonly(self, relative_path: str) -> Path | None:
        """Resolve a path under any read-only root (skill assets). None if absent."""
        raw = Path(relative_path)
        if raw.is_absolute():
            return None
        for base in self.read_roots:
            candidate = (base / raw).resolve()
            try:
                candidate.relative_to(base)
            except ValueError:
                continue
            if candidate.is_file():
                return candidate
        return None
