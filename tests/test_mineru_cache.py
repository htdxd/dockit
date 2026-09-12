"""阶段 2 强制测试：MinerU 跨任务持久缓存（实施计划 §6.4）。

覆盖：
- 首次 miss 调用一次转换；同 hash 命中不调用 API
- 选项变化 / 版本变化 → miss（不同缓存项）
- 损坏缓存按 miss 处理，不覆盖其它有效 key
- 并发同 key 只转换一次（per-key 文件锁）
- 超时/失败不产生可复用半成品（无 COMPLETE / 无 data 目录）
- Token 不写入缓存、metadata、日志
"""

from __future__ import annotations

import json
import subprocess
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from skill_toolbox.contracts.common import ToolError
from skill_toolbox.tools.mineru import (
    MINERU_ADAPTER_VERSION,
    MineruOptions,
    MineruService,
    mineru_cli_version,
)


def _make_pdf(path: Path) -> None:
    path.write_bytes(b"%PDF-1.4\nfixture-bytes")


def _make_docx(path: Path) -> None:
    """手工构造最小合法 OOXML 包（word/document.xml 存在即可读）。"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
            'package/2006/content-types"><Default Extension="rels" ContentType='
            '"application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType='
            '"application/vnd.openxmlformats-officedocument.wordprocessingml.'
            'document.main+xml"/></Types>',
        )
        z.writestr(
            "_rels/.rels",
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.'
            'org/package/2006/relationships"><Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" Target="word/document.xml"/></Relationships>',
        )
        z.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.'
            'org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>converted</w:t>'
            "</w:r></w:p></w:body></w:document>",
        )


def _recording_runner(calls: list[list[str]]) -> object:
    def run(command: list[str], *, timeout: float | None = None, **kwargs):
        calls.append(command)
        output = Path(command[3])
        output.parent.mkdir(parents=True, exist_ok=True)
        _make_docx(output)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    return run


@pytest.fixture
def service(tmp_path: Path) -> MineruService:
    convert_script = tmp_path / "fake_convert.py"
    convert_script.write_text("# fake convert entry (never executed)\n", encoding="utf-8")
    return MineruService(
        cache_root=tmp_path / "cache",
        convert_script=convert_script,
        runner=_recording_runner([]),
    )


# ---------------- 命中 / 未命中 ----------------

def test_first_convert_misses_and_second_hits(tmp_path: Path) -> None:
    """首次 miss 调用一次转换并写入缓存；同 hash 二次命中不调用 API。"""
    calls: list[list[str]] = []
    convert_script = tmp_path / "fake_convert.py"
    convert_script.write_text("# fake\n", encoding="utf-8")
    service = MineruService(
        cache_root=tmp_path / "cache",
        convert_script=convert_script,
        runner=_recording_runner(calls),
    )
    pdf = tmp_path / "report.pdf"
    _make_pdf(pdf)

    first = service.convert(pdf, tmp_path / "nested" / "first" / "out1.docx")
    assert first["status"] == "cache_miss"
    assert (tmp_path / "nested" / "first" / "out1.docx").is_file()
    assert len(calls) == 1

    second = service.convert(pdf, tmp_path / "nested" / "second" / "out2.docx")
    assert second["status"] == "cache_hit"
    assert len(calls) == 1, "cache hit 不得再次调用 MinerU"
    # 命中复制的结果与首次一致（可读 OOXML）
    with zipfile.ZipFile(tmp_path / "nested" / "second" / "out2.docx") as z:
        assert "word/document.xml" in z.namelist()


def test_cache_hit_metadata_preserved(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    convert_script = tmp_path / "fake_convert.py"
    convert_script.write_text("# fake\n", encoding="utf-8")
    service = MineruService(
        cache_root=tmp_path / "cache",
        convert_script=convert_script,
        runner=_recording_runner(calls),
    )
    pdf = tmp_path / "report.pdf"
    _make_pdf(pdf)
    service.convert(pdf, tmp_path / "out1.docx")

    hit = service.convert(pdf, tmp_path / "out2.docx")
    assert hit["metadata"]["adapter_version"] == MINERU_ADAPTER_VERSION
    assert hit["metadata"]["pdf_sha256"]
    assert hit["metadata"]["model"] == "auto"


# ---------------- 选项变化 / 版本变化 ----------------

def test_option_change_misses(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    convert_script = tmp_path / "fake_convert.py"
    convert_script.write_text("# fake\n", encoding="utf-8")
    service = MineruService(
        cache_root=tmp_path / "cache",
        convert_script=convert_script,
        runner=_recording_runner(calls),
    )
    pdf = tmp_path / "report.pdf"
    _make_pdf(pdf)

    service.convert(pdf, tmp_path / "out1.docx", MineruOptions(language="ch"))
    assert len(calls) == 1
    second = service.convert(pdf, tmp_path / "out2.docx", MineruOptions(language="en"))
    assert second["status"] == "cache_miss"
    assert len(calls) == 2, "language 变化必须产生不同缓存项"

    third = service.convert(
        pdf, tmp_path / "out3.docx", MineruOptions(model="auto", table=False)
    )
    assert third["status"] == "cache_miss"
    assert len(calls) == 3, "table 开关变化必须产生不同缓存项"
    # 原语言缓存仍有效
    back = service.convert(pdf, tmp_path / "out4.docx", MineruOptions(language="ch"))
    assert back["status"] == "cache_hit"


def test_adapter_version_change_misses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    convert_script = tmp_path / "fake_convert.py"
    convert_script.write_text("# fake\n", encoding="utf-8")
    service = MineruService(
        cache_root=tmp_path / "cache",
        convert_script=convert_script,
        runner=_recording_runner(calls),
    )
    pdf = tmp_path / "report.pdf"
    _make_pdf(pdf)

    service.convert(pdf, tmp_path / "out1.docx")
    assert len(calls) == 1

    monkeypatch.setattr(
        "skill_toolbox.tools.mineru.MINERU_ADAPTER_VERSION", "2"
    )
    second = service.convert(pdf, tmp_path / "out2.docx")
    assert second["status"] == "cache_miss", "adapter 版本变化必须失效旧缓存"
    assert len(calls) == 2


def test_cli_version_change_misses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    convert_script = tmp_path / "fake_convert.py"
    convert_script.write_text("# fake\n", encoding="utf-8")
    service = MineruService(
        cache_root=tmp_path / "cache",
        convert_script=convert_script,
        runner=_recording_runner(calls),
    )
    pdf = tmp_path / "report.pdf"
    _make_pdf(pdf)

    service.convert(pdf, tmp_path / "out1.docx")
    assert len(calls) == 1

    monkeypatch.setattr(
        "skill_toolbox.tools.mineru.mineru_cli_version", lambda: "1.2.3"
    )
    second = service.convert(pdf, tmp_path / "out2.docx")
    assert second["status"] == "cache_miss", "MinerU CLI 版本变化必须失效旧缓存"
    assert len(calls) == 2


# ---------------- 损坏缓存 ----------------

def test_corrupted_cache_misses_and_recovers(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    convert_script = tmp_path / "fake_convert.py"
    convert_script.write_text("# fake\n", encoding="utf-8")
    service = MineruService(
        cache_root=tmp_path / "cache",
        convert_script=convert_script,
        runner=_recording_runner(calls),
    )
    pdf = tmp_path / "report.pdf"
    _make_pdf(pdf)

    service.convert(pdf, tmp_path / "out1.docx")
    # 从缓存根目录定位已提交的缓存项
    entry = next(
        d for d in (tmp_path / "cache" / "mineru").iterdir() if (d / "COMPLETE").is_file()
    )
    # 删除产物文件 → 损坏缓存按 miss 处理并重新转换
    (entry / "data" / "result.docx").unlink()
    second = service.convert(pdf, tmp_path / "out2.docx")
    assert second["status"] == "cache_miss"
    assert len(calls) == 2
    # 恢复后命中
    third = service.convert(pdf, tmp_path / "out3.docx")
    assert third["status"] == "cache_hit"


def test_corrupted_metadata_misses(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    convert_script = tmp_path / "fake_convert.py"
    convert_script.write_text("# fake\n", encoding="utf-8")
    service = MineruService(
        cache_root=tmp_path / "cache",
        convert_script=convert_script,
        runner=_recording_runner(calls),
    )
    pdf = tmp_path / "report.pdf"
    _make_pdf(pdf)
    service.convert(pdf, tmp_path / "out1.docx")

    entry = next(
        d for d in (tmp_path / "cache" / "mineru").iterdir() if (d / "COMPLETE").is_file()
    )
    (entry / "metadata.json").write_text("{broken json", encoding="utf-8")
    second = service.convert(pdf, tmp_path / "out2.docx")
    assert second["status"] == "cache_miss"
    assert len(calls) == 2


# ---------------- 并发同 key 只转换一次 ----------------

def test_concurrent_same_key_converts_once(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    calls_lock = threading.Lock()
    convert_script = tmp_path / "fake_convert.py"
    convert_script.write_text("# fake\n", encoding="utf-8")
    service = MineruService(
        cache_root=tmp_path / "cache",
        convert_script=convert_script,
        runner=_recording_runner(calls),
    )
    pdf = tmp_path / "report.pdf"
    _make_pdf(pdf)

    def run_one(index: int) -> dict:
        return service.convert(pdf, tmp_path / f"out-{index}.docx")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(run_one, range(16)))

    assert len(calls) == 1, "并发同 key 只应转换一次"
    assert any(r["status"] == "cache_miss" for r in results)
    assert all(r["status"] in ("cache_hit", "cache_miss") for r in results)
    # 所有并发调用都拿到可读产物
    for result in results:
        with zipfile.ZipFile(Path(result["output"])) as z:
            assert "word/document.xml" in z.namelist()


# ---------------- 超时 / 失败无半成品 ----------------

def test_timeout_leaves_no_partial_cache(tmp_path: Path) -> None:
    def hanging(command: list[str], *, timeout: float | None = None, **kwargs):
        raise subprocess.TimeoutExpired(command, timeout or 0)

    convert_script = tmp_path / "fake_convert.py"
    convert_script.write_text("# fake\n", encoding="utf-8")
    service = MineruService(
        cache_root=tmp_path / "cache",
        convert_script=convert_script,
        runner=hanging,
    )
    pdf = tmp_path / "report.pdf"
    _make_pdf(pdf)

    with pytest.raises(subprocess.TimeoutExpired):
        service.convert(pdf, tmp_path / "out.docx")
    assert not (tmp_path / "out.docx").exists()
    assert _no_complete_or_data(tmp_path / "cache" / "mineru")


def test_failed_convert_leaves_no_complete(tmp_path: Path) -> None:
    def failing(command: list[str], *, timeout: float | None = None, **kwargs):
        return subprocess.CompletedProcess(command, 1, b"", b"token 401 unauth")

    convert_script = tmp_path / "fake_convert.py"
    convert_script.write_text("# fake\n", encoding="utf-8")
    service = MineruService(
        cache_root=tmp_path / "cache",
        convert_script=convert_script,
        runner=failing,
    )
    pdf = tmp_path / "report.pdf"
    _make_pdf(pdf)

    with pytest.raises(ToolError) as exc:
        service.convert(pdf, tmp_path / "out.docx")
    assert exc.value.code == "MINERU_TOKEN_MISSING"
    assert not (tmp_path / "out.docx").exists()
    assert _no_complete_or_data(tmp_path / "cache" / "mineru")


def _no_complete_or_data(cache_root: Path) -> bool:
    if not cache_root.is_dir():
        return True
    for entry in cache_root.iterdir():
        if (entry / "COMPLETE").is_file():
            return False
        if (entry / "data").is_dir():
            return False
    return True


# ---------------- Token 不落缓存 / metadata ----------------

def test_token_never_written_to_cache(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    convert_script = tmp_path / "fake_convert.py"
    convert_script.write_text("# fake\n", encoding="utf-8")
    service = MineruService(
        cache_root=tmp_path / "cache",
        convert_script=convert_script,
        runner=_recording_runner(calls),
    )
    pdf = tmp_path / "report.pdf"
    _make_pdf(pdf)
    service.convert(pdf, tmp_path / "out.docx")

    for json_path in (tmp_path / "cache" / "mineru").rglob("*.json"):
        content = json_path.read_text(encoding="utf-8")
        assert "sk-test" not in content
        assert "token" not in content.lower().replace("adapter", "")
    # 转换命令里也不带 Token（Token 只走子进程环境变量，由调用方注入）。
    # 注意：pytest 临时目录名含测试名（...test_token_never...），所以只检查
    # 参数形态而非整个命令行。
    assert calls
    for command in calls:
        joined = " ".join(command).lower()
        assert "sk-" not in joined
        assert "--token" not in joined
        assert "mineru_token" not in joined
        assert "authorization" not in joined
