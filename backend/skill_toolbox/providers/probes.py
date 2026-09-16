"""Provider 能力探测共用的编排与错误分类；请求协议留在各 Provider。"""

from collections.abc import Awaitable, Callable
import asyncio

from skill_toolbox.unicode_utils import redact_secrets

ProbeResult = tuple[str, str | None]
PROBE_ITEM_TIMEOUT = 45.0


async def run_probes(
    capabilities: list[str] | None,
    probes: dict[str, Callable[[], Awaitable[ProbeResult]]],
) -> dict[str, ProbeResult]:
    selected = capabilities or list(probes)
    results = {}
    for name, probe in probes.items():
        if name not in selected:
            continue
        try:
            results[name] = await asyncio.wait_for(probe(), PROBE_ITEM_TIMEOUT)
        except TimeoutError:
            results[name] = ("probe_error", f"本项检测超过 {PROBE_ITEM_TIMEOUT:g} 秒，未能确认；不代表模型不支持。")
        except Exception as exc:
            results[name] = classify_probe_error(name, exc)
    return results


def classify_probe_error(capability: str, exc: Exception) -> ProbeResult:
    """保留短错误描述并脱敏，只有明确不支持时才判 unsupported。"""
    if isinstance(exc, KeyboardInterrupt):  # pragma: no cover
        raise exc
    text = str(exc).lower()
    detail = redact_secrets(str(exc))[:200]
    if "unsupported" in text or "not support" in text or "does not support" in text:
        return ("unsupported", detail)
    return ("probe_error", detail)
