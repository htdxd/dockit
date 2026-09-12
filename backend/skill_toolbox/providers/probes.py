"""Provider 能力探测共用的编排与错误分类；请求协议留在各 Provider。"""

from collections.abc import Awaitable, Callable

from skill_toolbox.unicode_utils import redact_secrets

ProbeResult = tuple[str, str | None]


async def run_probes(
    capabilities: list[str] | None,
    probes: dict[str, Callable[[], Awaitable[ProbeResult]]],
) -> dict[str, ProbeResult]:
    selected = capabilities or list(probes)
    return {name: await probe() for name, probe in probes.items() if name in selected}


def classify_probe_error(capability: str, exc: Exception) -> ProbeResult:
    """保留短错误描述并脱敏，只有明确不支持时才判 unsupported。"""
    if isinstance(exc, KeyboardInterrupt):  # pragma: no cover
        raise exc
    text = str(exc).lower()
    detail = redact_secrets(str(exc))[:200]
    if "unsupported" in text or "not support" in text or "does not support" in text:
        return ("unsupported", detail)
    return ("probe_error", detail)
