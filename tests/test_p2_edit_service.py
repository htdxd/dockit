# -*- coding: utf-8 -*-
"""P2-3 测试：版本化编辑服务（ResumeEditService）与多模态投递契约。

分层：
- 逻辑层（无 Word）：渲染步骤以 stub 替换，验证 revision/幂等/并发/原子性/
  动作边界/视觉覆盖门 —— 这些判定与渲染实现无关，必须能在任何环境跑。
- e2e（Word COM，标记 e2e）：真实 t109 生成 → 编辑 → 接受，检查产物与 QA。

运行：
  uv run pytest tests/test_p2_edit_service.py -q            # 逻辑层
  uv run pytest tests/test_p2_edit_service.py -q -k e2e     # 需 Windows+Word
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from skill_toolbox.contracts.common import ToolError
from skill_toolbox.contracts.resume import (
    ResumeAcceptRequest,
    ResumeContentV2,
    ResumeEditV2,
    ResumeGenerateV2Request,
    ResumePreviewRequest,
    ResumeRepairV2Request,
    ResumeRestoreRequest,
)
from skill_toolbox.tools.resume import ResumeEditService, format_head_slots

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = PROJECT_ROOT / "backend/skill_toolbox/skill_defs/resume_pro/templates"

SECTION = {
    "key": "internship", "title": "实习经历（Internship）", "prototype": "experience_v1",
    "entries": [
        {"id": "intern-1",
         "head": {"date": "2012.06-至今", "org": "广州某公司", "role": "市场营销"},
         "bullets": ["负责线上端资源的销售工作；", "跟踪客户的详细数据。"]},
        {"id": "intern-2",
         "head": {"date": "2010.03-2012.03", "org": "广州一百丁", "role": "软件工程师"},
         "bullets": ["负责公司业务系统的设计及改进。"]},
    ],
}


def _content() -> dict:
    return {
        "schema_version": "resume-content-v2",
        "template_id": "t109",
        "sections": [json.loads(json.dumps(SECTION, ensure_ascii=False))],
    }


def _m(eid: str, lines: int):
    """测量结果替身（纯布局测试用；与 e2e 的真实 COM 测量区分）。"""
    from skill_toolbox.resume_layout.layout import MeasureResult

    return MeasureResult(eid, lines, lines * 18.0)


_new_revision_calls: list[dict] = []


def _stub_render(self, artifact_id, revision, content, *, layout_mode,
                 base_revision, changes, rev_dir):  # noqa: ANN001
    """替换真实渲染：写最小合法 revision 记录，便于验证版本语义。"""
    rev_dir.mkdir(parents=True, exist_ok=True)
    page = rev_dir / "render" / "page-1.png"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_bytes(b"\x89PNG\r\n\x1a\nstub")
    record = {
        "artifact_id": artifact_id, "revision": revision, "parent": base_revision,
        "layout_mode": layout_mode, "changes": changes, "content": content,
        "content_hash": "stub", "page_count": 1,
        "mechanical": {"passed": True, "errors": []},
        "visual": {"status": "pending", "delivered_pages": []},
        "docx": f"work/resume/{artifact_id}/revisions/{revision}/resume.docx",
        "pdf": None, "pages": [f"work/resume/{artifact_id}/revisions/{revision}/render/page-1.png"],
        "docx_sha256": "stub", "pdf_sha256": None,
        "instance_model": [], "renderer": "stub",
    }
    (rev_dir / "revision.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    self._write_qa_report(revision, {"passed": True, "issues": [], "checks_run": []})
    _new_revision_calls.append({"revision": revision, "changes": changes})
    return record


@pytest.fixture()
def svc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ResumeEditService:
    _new_revision_calls.clear()
    monkeypatch.setattr(ResumeEditService, "_render_revision", _stub_render)
    return ResumeEditService(tmp_path, TEMPLATES, capabilities={"vision": True})


def _gen(svc: ResumeEditService, request_id: str = "gen-1"):
    return svc.generate(ResumeGenerateV2Request(
        template_id="t109", content=ResumeContentV2(**_content()), request_id=request_id))


# ---------------- 版本与幂等 ----------------

def test_generate_creates_revision_1_and_schema_version(svc: ResumeEditService) -> None:
    res = _gen(svc)
    assert res.ok and res.revision == 1
    assert res.data["candidate_revision"] == 1
    assert res.data["page_count"] == 1


def test_same_request_id_same_payload_is_idempotent(svc: ResumeEditService) -> None:
    first = _gen(svc)
    second = _gen(svc)  # 同 request_id 同载荷
    assert second.revision == first.revision
    assert second.data["idempotent_replay"] is True
    assert len(_new_revision_calls) == 1, "幂等重放不得新建 revision"


def test_inflight_request_id_rejects_different_artifact_payload(svc, monkeypatch) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    started, release = Event(), Event()

    def render(self, *args, **kwargs):
        if not started.is_set():
            started.set()
            assert release.wait(10), "测试未释放首个生成请求"
        return _stub_render(self, *args, **kwargs)

    monkeypatch.setattr(ResumeEditService, "_render_revision", render)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(_gen, svc, "shared-inflight")
        try:
            assert started.wait(10)
            other = _content()
            other["sections"][0]["entries"][0]["bullets"] = ["不同的请求内容"]
            with pytest.raises(ToolError) as exc:
                svc.generate(ResumeGenerateV2Request(
                    template_id="t109", content=ResumeContentV2(**other),
                    request_id="shared-inflight"))
            assert exc.value.code == "REQUEST_CONFLICT"
        finally:
            release.set()
        result = first.result(timeout=10)
    assert svc._request_index()["shared-inflight"]["artifact_id"] == result.artifact_id
    assert len(_new_revision_calls) == 1


def test_failed_generation_can_retry_reserved_request(svc, monkeypatch) -> None:
    def fail(self, *args, **kwargs):
        raise ToolError("RENDER_FAILED", "测试渲染失败")

    monkeypatch.setattr(ResumeEditService, "_render_revision", fail)
    with pytest.raises(ToolError, match="测试渲染失败"):
        _gen(svc, "retry-after-failure")
    monkeypatch.setattr(ResumeEditService, "_render_revision", _stub_render)
    result = _gen(svc, "retry-after-failure")
    assert result.ok and result.revision == 1
    assert _gen(svc, "retry-after-failure").data["idempotent_replay"] is True


def test_reflow_upward_outside_content_area_is_rejected() -> None:
    from skill_toolbox.resume_layout import layout as RL

    sections = [{"id": "edu", "title": "教育背景", "entries": [{"id": "e"}]}]
    measured = {"e": RL.MeasureResult("e", 2, 36.0)}
    with pytest.raises(RL.LayoutUnsatisfiable, match="上边界"):
        RL.plan_layout(sections, measured, entry_adjust={"e": {"dy_pt": -10.0}})


def test_same_request_id_different_payload_conflicts(svc: ResumeEditService) -> None:
    _gen(svc)
    other = _content()
    other["sections"][0]["entries"][0]["bullets"] = ["改过的内容"]
    with pytest.raises(ToolError) as exc:
        svc.generate(ResumeGenerateV2Request(
            template_id="t109", content=ResumeContentV2(**other), request_id="gen-1"))
    assert exc.value.code == "REQUEST_CONFLICT"


def test_repair_stale_base_revision_returns_version_conflict(svc: ResumeEditService) -> None:
    gen = _gen(svc)
    rep = svc.repair(ResumeRepairV2Request(
        artifact_id=gen.artifact_id, base_revision=1, request_id="rep-1",
        changes=[ResumeEditV2(op="set_entry_gap", section_key="internship", gap_pt=6.0)]))
    assert rep.revision == 2
    with pytest.raises(ToolError) as exc:
        svc.repair(ResumeRepairV2Request(
            artifact_id=gen.artifact_id, base_revision=1, request_id="rep-2",
            changes=[ResumeEditV2(op="set_entry_gap", section_key="internship", gap_pt=7.0)]))
    assert exc.value.code == "VERSION_CONFLICT"
    assert "current=2" in str(exc.value)


def test_restore_creates_new_traceable_revision(svc: ResumeEditService) -> None:
    gen = _gen(svc)
    rep = svc.repair(ResumeRepairV2Request(
        artifact_id=gen.artifact_id, base_revision=1, request_id="rep-1",
        changes=[ResumeEditV2(op="set_entry_gap", section_key="internship", gap_pt=6.0)]))
    restored = svc.restore(ResumeRestoreRequest(
        artifact_id=gen.artifact_id, target_revision=1,
        expected_accepted_revision=0, request_id="res-1"))
    assert restored.revision == rep.revision + 1, "恢复必须新建版本，不复用旧号"
    rec = svc._load_revision(gen.artifact_id, restored.revision)
    assert rec["changes"][0]["op"] == "restore"
    assert rec["changes"][0]["from_revision"] == 1


# ---------------- 编辑动作语义 ----------------

def test_update_entry_only_touches_provided_fields(svc: ResumeEditService) -> None:
    """回归：只给 bullets 时不得清空 head（否则条目标题整行消失）。"""
    gen = _gen(svc)
    rep = svc.repair(ResumeRepairV2Request(
        artifact_id=gen.artifact_id, base_revision=1, request_id="rep-1",
        changes=[ResumeEditV2(op="update_entry", instance_id="internship#intern-1",
                              entry={"id": "intern-1", "bullets": ["只有要点被替换；"]})]))
    assert rep.ok
    rec = svc._load_revision(gen.artifact_id, rep.revision)
    entry = rec["content"]["sections"][0]["entries"][0]
    assert entry["bullets"] == ["只有要点被替换；"]
    assert entry["head"]["org"] == "广州某公司", "未提供的 head 字段必须保留"
    assert rep.data["actual_changes"][0]["fields"] == ["bullets"]


def test_insert_move_remove_entry_order(svc: ResumeEditService) -> None:
    gen = _gen(svc)
    rep = svc.repair(ResumeRepairV2Request(
        artifact_id=gen.artifact_id, base_revision=1, request_id="rep-1",
        changes=[
            ResumeEditV2(op="insert_entry", section_key="internship",
                         entry={"id": "intern-3", "head": {"date": "2008.01-2008.12",
                                                           "org": "某公司", "role": "实习生"},
                                "bullets": ["新条目；"]}),
            ResumeEditV2(op="move_entry", instance_id="internship#intern-3",
                         before="internship#intern-1"),
            ResumeEditV2(op="remove_entry", instance_id="internship#intern-2"),
        ]))
    entries = svc._load_revision(gen.artifact_id, rep.revision)["content"]["sections"][0]["entries"]
    assert [e["id"] for e in entries] == ["intern-3", "intern-1"]


def test_atomic_failure_leaves_no_new_revision(svc: ResumeEditService) -> None:
    """批内非法动作令整批失败：不得部分提交、不得推进 current。"""
    gen = _gen(svc)
    with pytest.raises(ToolError) as exc:
        svc.repair(ResumeRepairV2Request(
            artifact_id=gen.artifact_id, base_revision=1, request_id="rep-bad",
            changes=[
                ResumeEditV2(op="set_entry_gap", section_key="internship", gap_pt=6.0),
                ResumeEditV2(op="remove_entry", instance_id="internship#no-such"),
            ]))
    assert exc.value.code == "TARGET_NOT_FOUND"
    assert svc._index(gen.artifact_id)["current"] == 1
    assert len(_new_revision_calls) == 1, "失败批次不得写任何 revision"


def test_action_bounds_and_axis_rules(svc: ResumeEditService) -> None:
    gen = _gen(svc)
    cases = [
        (ResumeEditV2(op="set_entry_gap", section_key="internship", gap_pt=1.0), "BOUNDS_VIOLATION"),
        (ResumeEditV2(op="set_entry_gap", section_key="internship", gap_pt=30.0), "BOUNDS_VIOLATION"),
        (ResumeEditV2(op="move_component", instance_id="internship#intern-1", dx_pt=5.0),
         "ACTION_NOT_ALLOWED"),
        (ResumeEditV2(op="move_component", instance_id="internship#intern-1", dy_pt=200.0),
         "BOUNDS_VIOLATION"),
        (ResumeEditV2(op="update_entry", instance_id="internship#nope",
                      entry={"id": "x"}), "TARGET_NOT_FOUND"),
    ]
    for change, code in cases:
        with pytest.raises(ToolError) as exc:
            svc.repair(ResumeRepairV2Request(
                artifact_id=gen.artifact_id, base_revision=1,
                request_id=f"rep-{code}-{change.op}-{change.gap_pt}", changes=[change]))
        assert exc.value.code == code, f"{change.op}: {exc.value.code} != {code}"


def test_empty_changes_rejected(svc: ResumeEditService) -> None:
    gen = _gen(svc)
    with pytest.raises(ToolError) as exc:
        svc.repair(ResumeRepairV2Request(
            artifact_id=gen.artifact_id, base_revision=1, request_id="rep-empty", changes=[]))
    assert exc.value.code == "CONTENT_INVALID"


# ---------------- 视觉覆盖与接受门 ----------------

def test_accept_requires_full_visual_coverage_when_vision(svc: ResumeEditService) -> None:
    """超过单次投递预算的页未补看前，accept 必须被拒（VISUAL_PENDING）。"""
    gen = _gen(svc)
    svc._load_revision  # noqa: B018 - 语义说明：多页版本才有剩余页
    # 把该 revision 伪造成 4 页（预算 3 页），投递只覆盖 1-3 页 → 第 4 页待补看
    rec = svc._load_revision(gen.artifact_id, 1)
    rec_dir = svc._revision_dir(gen.artifact_id, 1)
    for n in (2, 3, 4):
        p = rec_dir / "render" / f"page-{n}.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\nstub")
        rec["pages"].append(
            f"work/resume/{gen.artifact_id}/revisions/1/render/page-{n}.png")
    rec["page_count"] = 4
    (rec_dir / "revision.json").write_text(
        json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    delivered = svc.preview(ResumePreviewRequest(artifact_id=gen.artifact_id, revision=1))
    assert delivered.data["delivered_pages"] == [1, 2, 3], "预算 3 页"
    assert delivered.data["remaining_pages"] == [4], "超预算页必须显式返回"
    with pytest.raises(ToolError) as exc:
        svc.accept(ResumeAcceptRequest(
            artifact_id=gen.artifact_id, candidate_revision=1,
            expected_accepted_revision=0), visual_notes="只看了一部分")
    assert exc.value.code == "VISUAL_PENDING"
    # 补看剩余页后可接受
    svc.preview(ResumePreviewRequest(artifact_id=gen.artifact_id, revision=1, pages=[4]))
    acc = svc.accept(ResumeAcceptRequest(
        artifact_id=gen.artifact_id, candidate_revision=1,
        expected_accepted_revision=0), visual_notes="4 页都看过了：无重叠、无越界。")
    assert acc.ok and acc.data["accepted_revision"] == 1


def test_accept_requires_visual_notes_when_vision(svc: ResumeEditService) -> None:
    gen = _gen(svc)
    svc.preview(ResumePreviewRequest(artifact_id=gen.artifact_id, revision=1))
    with pytest.raises(ToolError) as exc:
        svc.accept(ResumeAcceptRequest(
            artifact_id=gen.artifact_id, candidate_revision=1,
            expected_accepted_revision=0), visual_notes="")
    assert exc.value.code == "VISUAL_RECORD_MISSING"


def test_accept_updates_pointer_and_logs_notes(svc: ResumeEditService) -> None:
    gen = _gen(svc)
    svc.preview(ResumePreviewRequest(artifact_id=gen.artifact_id, revision=1))
    acc = svc.accept(ResumeAcceptRequest(
        artifact_id=gen.artifact_id, candidate_revision=1, expected_accepted_revision=0),
        visual_notes="第 1 页：五栏齐全、无重叠、无越界。")
    assert acc.ok and acc.data["accepted_revision"] == 1
    index = svc._index(gen.artifact_id)
    assert index["accepted"] == 1
    assert index["accepted_log"][-1]["notes"].startswith("第 1 页")
    with pytest.raises(ToolError) as exc:
        svc.accept(ResumeAcceptRequest(
            artifact_id=gen.artifact_id, candidate_revision=1,
            expected_accepted_revision=0), visual_notes="重复接受")
    assert exc.value.code == "VERSION_CONFLICT"


def test_accept_without_vision_does_not_require_coverage(tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ResumeEditService, "_render_revision", _stub_render)
    svc = ResumeEditService(tmp_path, TEMPLATES, capabilities={"vision": False})
    gen = svc.generate(ResumeGenerateV2Request(
        template_id="t109", content=ResumeContentV2(**_content()), request_id="g"))
    acc = svc.accept(ResumeAcceptRequest(
        artifact_id=gen.artifact_id, candidate_revision=1, expected_accepted_revision=0))
    assert acc.ok and acc.data["visual"] in {"pending", "not_run"}


def test_mechanical_failure_blocks_result_and_accept(tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_render(self, artifact_id, revision, content, *, layout_mode,
                       base_revision, changes, rev_dir):  # noqa: ANN001
        record = _stub_render(self, artifact_id, revision, content,
                              layout_mode=layout_mode, base_revision=base_revision,
                              changes=changes, rev_dir=rev_dir)
        record["mechanical"] = {"passed": False, "errors": ["内容缺失"]}
        (rev_dir / "revision.json").write_text(
            json.dumps(record, ensure_ascii=False), encoding="utf-8")
        self._write_qa_report(revision, {"passed": False,
                                         "issues": [{"severity": "error", "detail": "内容缺失"}],
                                         "checks_run": []})
        return record

    monkeypatch.setattr(ResumeEditService, "_render_revision", failing_render)
    svc = ResumeEditService(tmp_path, TEMPLATES, capabilities={"vision": True})
    gen = svc.generate(ResumeGenerateV2Request(
        template_id="t109", content=ResumeContentV2(**_content()), request_id="g"))
    assert gen.ok is False and gen.data["usable"] is False
    with pytest.raises(ToolError) as exc:
        svc.accept(ResumeAcceptRequest(
            artifact_id=gen.artifact_id, candidate_revision=1,
            expected_accepted_revision=0), visual_notes="看过")
    assert exc.value.code == "MECHANICAL_FAILED"


def test_qa_report_written_for_runtime_gate(svc: ResumeEditService) -> None:
    gen = _gen(svc)
    qa = json.loads((svc.workspace / "work" / "qa" / "resume.json").read_text(encoding="utf-8"))
    assert qa["mechanical"] == "passed"
    assert qa["revision"] == gen.revision
    assert qa["template"] == "t109"


# ---------------- 槽位与工具面 ----------------

def test_format_head_slots_keeps_columns_and_skips_missing() -> None:
    pattern = "2012.06-至今               广州简历模板资源网信息科技有限公司           市场营销（实习生）"
    full = format_head_slots(["2019.07-2021.08", "某某科技股份有限公司", "产品运营"], pattern)
    assert full.startswith("2019.07-2021.08")
    assert "某某科技股份有限公司" in full and full.endswith("产品运营")
    assert "  " in full, "槽位之间必须有列间隔"
    two = format_head_slots(["2019.07-2021.08", "", "产品运营"], pattern)
    assert two.startswith("2019.07-2021.08") and two.endswith("产品运营")
    assert "  " in two.strip()
    assert format_head_slots(["", "", ""], pattern) == ""


def test_v2_tools_exposed_and_registered() -> None:
    from skill_toolbox.llm_tools.resume import resume_v2_tools
    from skill_toolbox import runtime as RT

    names = {t["name"] for t in resume_v2_tools()}
    assert names == {"resume_prepare_v2", "resume_generate_v2", "resume_repair_v2",
                     "resume_accept", "resume_restore", "resume_preview"}
    assert names <= RT._DOMAIN_TOOL_NAMES
    assert {"resume_generate_v2", "resume_repair_v2", "resume_restore"} <= RT.DOMAIN_ARTIFACT_TOOLS
    assert all(name in RT.DOMAIN_TOOL_TIMEOUTS for name in names)


def test_dispatch_with_media_returns_images(tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """dispatcher 必须把 OperationResult.images 交给 Runtime（不是只给路径）。"""
    from skill_toolbox.llm_tools.dispatcher import DomainServices, dispatch_with_media

    monkeypatch.setattr(ResumeEditService, "_render_revision", _stub_render)
    svc = ResumeEditService(tmp_path, TEMPLATES, capabilities={"vision": True})
    services = DomainServices(resume_v2=svc)
    text, images, meta = dispatch_with_media(
        "resume_generate_v2",
        {"template_id": "t109", "content": _content(), "request_id": "g"},
        services,
    )
    payload = json.loads(text)
    assert payload["ok"] is True
    assert meta["revision"] == 1
    assert len(images) == 1 and images[0]["media_type"] == "image/png"
    assert images[0]["base64_data"], "图像必须是真实 base64 载荷"
    assert payload["images"][0]["inline"] is True


def test_dispatch_error_is_stable_not_traceback(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    from skill_toolbox.llm_tools.dispatcher import DomainServices, dispatch_with_media

    monkeypatch.setattr(ResumeEditService, "_render_revision", _stub_render)
    svc = ResumeEditService(tmp_path, TEMPLATES, capabilities={"vision": True})
    text, images, meta = dispatch_with_media(
        "resume_repair_v2",
        {"artifact_id": "resume-nope", "base_revision": 1, "request_id": "r",
         "changes": [{"op": "set_entry_gap", "section_key": "internship", "gap_pt": 6.0}]},
        DomainServices(resume_v2=svc),
    )
    payload = json.loads(text)
    assert payload["code"] == "ARTIFACT_UNKNOWN" and images == []
    # 空 changes 是请求级错误，同样必须给稳定错误码而不是 traceback
    text2, _img2, _meta2 = dispatch_with_media(
        "resume_repair_v2",
        {"artifact_id": "resume-nope", "base_revision": 1, "request_id": "r2",
         "changes": []},
        DomainServices(resume_v2=svc),
    )
    assert json.loads(text2)["code"] == "CONTENT_INVALID"


# ---------------- e2e（Word COM） ----------------

e2e = pytest.mark.e2e


@e2e
def test_e2e_v2_full_cycle_with_real_render(tmp_path: Path) -> None:
    """真实 Word：generate(1 页) → repair 新增条目 → accept；产物可打开、
    revision 记录含 hash、QA 报告与视觉覆盖一致。"""
    pytest.importorskip("pythoncom")
    svc = ResumeEditService(tmp_path, TEMPLATES, capabilities={"vision": True})
    content = _content()
    gen = svc.generate(ResumeGenerateV2Request(
        template_id="t109", content=ResumeContentV2(**content), request_id="gen-1"))
    assert gen.ok is True and gen.revision == 1
    assert gen.data["visual"] == "delivered"
    assert len(gen.images) == gen.data["page_count"] >= 1

    aid = gen.artifact_id
    rep = svc.repair(ResumeRepairV2Request(
        artifact_id=aid, base_revision=1, request_id="rep-1",
        changes=[ResumeEditV2(op="insert_entry", section_key="internship",
                              entry={"id": "intern-3",
                                     "head": {"date": "2019.07-2021.08",
                                              "org": "某某科技股份有限公司",
                                              "role": "产品运营"},
                                     "bullets": ["负责增长实验设计与复盘；",
                                                 "搭建渠道数据看板。"]})]))
    assert rep.ok is True, rep.issues
    record = svc._load_revision(aid, rep.revision)
    docx = svc.workspace / record["docx"]
    assert docx.is_file() and record["docx_sha256"]
    with zipfile.ZipFile(docx) as z:
        xml = z.read("word/document.xml")
    assert "2019.07-2021.08".encode() in xml

    acc = svc.accept(ResumeAcceptRequest(
        artifact_id=aid, candidate_revision=rep.revision,
        expected_accepted_revision=0, request_id="acc-1"),
        visual_notes="测试：看图确认无重叠与越界。")
    assert acc.ok is True and acc.data["accepted_revision"] == rep.revision
    qa = json.loads((svc.workspace / "work" / "qa" / "resume.json").read_text(encoding="utf-8"))
    assert qa["mechanical"] == "passed"
    assert qa["accepted_revision"] == rep.revision


# ============ P2-R1 修订：review 5 项反例（先复现缺陷再验证修复） ============

def _two_revisions(svc: ResumeEditService):
    gen = _gen(svc)
    rep = svc.repair(ResumeRepairV2Request(
        artifact_id=gen.artifact_id, base_revision=1, request_id="rep-1",
        changes=[ResumeEditV2(op="set_entry_gap", section_key="internship", gap_pt=6.0)]))
    return gen, rep


def test_accept_stale_candidate_rejected_and_no_cross_version_qa(
    svc: ResumeEditService,
) -> None:
    """review #1：创建 rev2 后接受 rev1 必须被拒；且公共 QA 不得混版。"""
    gen, rep = _two_revisions(svc)
    assert rep.revision == 2
    with pytest.raises(ToolError) as exc:
        svc.accept(ResumeAcceptRequest(
            artifact_id=gen.artifact_id, candidate_revision=1,
            expected_accepted_revision=0), visual_notes="旧版结论")
    assert exc.value.code == "STALE_CANDIDATE"
    # 拒绝后公共 QA 不得出现「revision=2 但 accepted_revision=1/visual=passed」
    qa_path = svc.workspace / "work" / "qa" / "resume.json"
    if qa_path.is_file():
        qa = json.loads(qa_path.read_text(encoding="utf-8"))
        assert qa.get("visual") != "passed", "过期候选不得把 QA 标成视觉通过"
        assert int(qa.get("accepted_revision") or 0) == 0
    assert svc._index(gen.artifact_id)["accepted"] == 0


def test_accept_current_version_writes_same_version_delivery_report(
    svc: ResumeEditService,
) -> None:
    """review #1 后半：交付报告必须与被接受版本同版。"""
    gen, rep = _two_revisions(svc)
    svc.preview(ResumePreviewRequest(artifact_id=gen.artifact_id, revision=2))
    acc = svc.accept(ResumeAcceptRequest(
        artifact_id=gen.artifact_id, candidate_revision=2,
        expected_accepted_revision=0), visual_notes="rev2 看图结论")
    assert acc.ok and acc.data["accepted_revision"] == 2
    report_path = svc.workspace / acc.data["delivery_report"]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["revision"] == 2
    assert report["visual"]["status"] == "passed"
    assert report["mechanical"]["passed"] is True
    assert report["docx"] == svc._load_revision(gen.artifact_id, 2)["docx"]
    qa = json.loads((svc.workspace / "work" / "qa" / "resume.json").read_text(encoding="utf-8"))
    assert qa["revision"] == 2 and qa["accepted_revision"] == 2 and qa["visual"] == "passed"


def test_repair_successful_retry_is_idempotent(svc: ResumeEditService) -> None:
    """review #2：同 request_id + 同载荷重发 repair 必须重放原结果。"""
    gen = _gen(svc)
    changes = [ResumeEditV2(op="set_entry_gap", section_key="internship", gap_pt=6.0)]
    first = svc.repair(ResumeRepairV2Request(
        artifact_id=gen.artifact_id, base_revision=1, request_id="rep-retry",
        changes=changes))
    second = svc.repair(ResumeRepairV2Request(
        artifact_id=gen.artifact_id, base_revision=1, request_id="rep-retry",
        changes=changes))
    assert second.ok and second.revision == first.revision
    assert second.data["idempotent_replay"] is True
    assert svc._index(gen.artifact_id)["current"] == first.revision, "重试不得新建版本"


def test_repair_retry_with_different_payload_conflicts(svc: ResumeEditService) -> None:
    gen = _gen(svc)
    svc.repair(ResumeRepairV2Request(
        artifact_id=gen.artifact_id, base_revision=1, request_id="rep-x",
        changes=[ResumeEditV2(op="set_entry_gap", section_key="internship", gap_pt=6.0)]))
    with pytest.raises(ToolError) as exc:
        svc.repair(ResumeRepairV2Request(
            artifact_id=gen.artifact_id, base_revision=1, request_id="rep-x",
            changes=[ResumeEditV2(op="set_entry_gap", section_key="internship", gap_pt=9.0)]))
    assert exc.value.code == "REQUEST_CONFLICT"


def test_accept_successful_retry_is_idempotent(svc: ResumeEditService) -> None:
    """review #2：accept 也必须幂等（超时重试不能报版本冲突）。"""
    gen = _gen(svc)
    svc.preview(ResumePreviewRequest(artifact_id=gen.artifact_id, revision=1))
    first = svc.accept(ResumeAcceptRequest(
        artifact_id=gen.artifact_id, candidate_revision=1,
        expected_accepted_revision=0, request_id="acc-retry"),
        visual_notes="看图通过")
    second = svc.accept(ResumeAcceptRequest(
        artifact_id=gen.artifact_id, candidate_revision=1,
        expected_accepted_revision=0, request_id="acc-retry"),
        visual_notes="看图通过")
    assert second.ok and second.data["accepted_revision"] == first.data["accepted_revision"]
    assert second.data["delivery_report"] == first.data["delivery_report"]
    # 同 id 不同载荷（结论不同）必须冲突
    with pytest.raises(ToolError) as exc:
        svc.accept(ResumeAcceptRequest(
            artifact_id=gen.artifact_id, candidate_revision=1,
            expected_accepted_revision=0, request_id="acc-retry"),
            visual_notes="改了结论")
    assert exc.value.code == "REQUEST_CONFLICT"


def test_cross_page_section_is_not_a_collision(svc: ResumeEditService) -> None:
    """review #3：同栏目跨页的合法布局不能被误判为碰撞。"""
    from skill_toolbox.resume_layout import layout as RL

    plan = RL.plan_layout(
        [{"id": "internship", "title": "实习经历", "entries": [
            {"id": "a"}, {"id": "b"}, {"id": "c"}]}],
        {"a": _m("a", 20), "b": _m("b", 12), "c": _m("c", 8)},
    )
    assert plan.pages == 2, "构造前提：第三条被推到第 2 页"
    entry_pages = [e.page_index for e in plan.sections[0].entries]
    assert entry_pages[0] == entry_pages[1] == 0 and entry_pages[2] == 1
    content = {"sections": [{"key": "internship", "entries": [
        {"id": "a"}, {"id": "b"}, {"id": "c"}]}]}
    out = svc._apply_local_geometry(plan, content, RL)  # 无几何修改：必须通过
    assert [e.page_index for e in out.sections[0].entries] == entry_pages


def test_local_mode_does_not_move_untargeted_entries(svc: ResumeEditService) -> None:
    """review #4：local 模式只动目标条目，不推动未选中条目。"""
    from skill_toolbox.resume_layout import layout as RL

    entries = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    plan = RL.plan_layout(
        [{"id": "s", "title": "T", "entries": entries}],
        {"a": _m("a", 2), "b": _m("b", 2), "c": _m("c", 2)},
    )
    before = {e.instance_id: e.anchor_y_pt for e in plan.sections[0].entries}
    content = {"sections": [{"key": "s", "entries": [
        {"id": "a"}, {"id": "b", "dy_pt": 5.0}, {"id": "c"}]}]}
    local = svc._apply_local_geometry(plan, content, RL)
    after_local = {e.instance_id: e.anchor_y_pt for e in local.sections[0].entries}
    assert after_local["b"] == pytest.approx(before["b"] + 5.0)
    assert after_local["a"] == pytest.approx(before["a"]), "local 不得动未选中条目"
    assert after_local["c"] == pytest.approx(before["c"]), "local 不得推动后续条目"


def test_reflow_pushes_subsequent_sections(svc: ResumeEditService) -> None:
    """review #2（P2-R2）：reflow 的下移必须推动**后续栏目**并重排。

    旧实现只平移本栏条目：第一栏下移 80pt 后，第二栏纹丝不动 → 两栏交叠。
    现在几何约束进入完整 flow 排版器（plan_layout 的 entry_adjust）。
    """
    from skill_toolbox.resume_layout import layout as RL

    secs = [
        {"id": "s1", "title": "A", "entries": [{"id": "a1"}, {"id": "a2"}]},
        {"id": "s2", "title": "B", "entries": [{"id": "b1"}]},
    ]
    ms = {"a1": _m("a1", 2), "a2": _m("a2", 2), "b1": _m("b1", 2)}
    base = RL.plan_layout(secs, ms)
    moved = RL.plan_layout(
        secs, ms,
        entry_adjust=svc._geometry_adjust({"sections": [
            {"key": "s1", "entries": [{"id": "a1", "dy_pt": 80.0}, {"id": "a2"}]},
            {"key": "s2", "entries": [{"id": "b1"}]},
        ]}),
    )
    # 目标条目下移 80
    assert moved.sections[0].entries[0].anchor_y_pt == pytest.approx(
        base.sections[0].entries[0].anchor_y_pt + 80.0
    )
    # 同栏后续条目、后续栏目都跟着下移 80（不再交叠）
    assert moved.sections[0].entries[1].anchor_y_pt == pytest.approx(
        base.sections[0].entries[1].anchor_y_pt + 80.0
    )
    assert moved.sections[1].anchor_y_pt == pytest.approx(
        base.sections[1].anchor_y_pt + 80.0
    )
    # 跨栏目文字间距保持不变（无交叠）
    prev_text_bottom = (
        moved.sections[0].entries[-1].anchor_y_pt
        + RL.BODY_FIRST_TEXT_OFF_PT + 36.0
    )
    title_top = moved.sections[1].anchor_y_pt + RL.TITLE_TEXT_TOP_OFF_PT
    assert title_top - prev_text_bottom == pytest.approx(
        (base.sections[1].anchor_y_pt + RL.TITLE_TEXT_TOP_OFF_PT)
        - (base.sections[0].entries[-1].anchor_y_pt + RL.BODY_FIRST_TEXT_OFF_PT + 36.0)
    )


def test_reflow_height_growth_pushes_sections_and_repaginates() -> None:
    """reflow 加高也会推动后续栏目；超出页容量时重新分页（页数增加）。"""
    from skill_toolbox.resume_layout import layout as RL

    secs = [
        {"id": "s1", "title": "A", "entries": [{"id": "a1"}]},
        {"id": "s2", "title": "B", "entries": [{"id": "b1"}]},
    ]
    ms = {"a1": _m("a1", 8), "b1": _m("b1", 2)}
    base = RL.plan_layout(secs, ms)
    assert base.pages == 1
    # dy 取值使「本条仍在页内、但其后的第二栏必须翻页」
    grown = RL.plan_layout(secs, ms, entry_adjust={"a1": {"dy_pt": 455.0}})
    assert grown.sections[0].entries[0].anchor_y_pt > base.sections[0].entries[0].anchor_y_pt
    assert grown.pages >= 2, "下移把内容推出第 1 页时必须真实分页"
    # 分页后第二栏必须在第 2 页（不能与前栏同页交叠）
    assert grown.sections[1].page_index == 1


def test_reflow_negative_dy_collision_is_rejected() -> None:
    """reflow 上移过多 → 与上一条重叠，必须整批失败而不是产出交叠计划。"""
    from skill_toolbox.resume_layout import layout as RL

    secs = [{"id": "s", "title": "T", "entries": [{"id": "a"}, {"id": "b"}]}]
    ms = {"a": _m("a", 2), "b": _m("b", 2)}
    with pytest.raises(RL.LayoutUnsatisfiable) as exc:
        RL.plan_layout(secs, ms, entry_adjust={"b": {"dy_pt": -30.0}})
    assert "重叠" in str(exc.value)


def test_local_mode_rejects_collision_instead_of_pushing(svc: ResumeEditService) -> None:
    """local 模式与邻居相撞时整批失败（不偷偷推开别人）。"""
    from skill_toolbox.resume_layout import layout as RL

    plan = RL.plan_layout(
        [{"id": "s", "title": "T", "entries": [{"id": "a"}, {"id": "b"}]}],
        {"a": _m("a", 2), "b": _m("b", 2)},
    )
    content = {"sections": [{"key": "s", "entries": [
        {"id": "a", "dy_pt": 10.0}, {"id": "b"}]}]}
    with pytest.raises(ToolError) as exc:
        svc._apply_local_geometry(plan, content, RL)
    assert exc.value.code == "COLLISION"


def test_resize_parameters_are_honored_or_rejected(svc: ResumeEditService) -> None:
    """review #4：width_pt/height_pt 必须真正生效；不支持的组合显式拒绝。"""
    gen = _gen(svc)
    # height_delta_pt 生效
    rep = svc.repair(ResumeRepairV2Request(
        artifact_id=gen.artifact_id, base_revision=1, request_id="res-h",
        changes=[ResumeEditV2(op="resize_component", instance_id="internship#intern-1",
                              height_delta_pt=20.0)]))
    assert rep.ok and rep.data["actual_changes"][0]["height_delta_pt"] == 20.0
    # width_pt 生效并进入内容模型（供真实重排测量）
    rep2 = svc.repair(ResumeRepairV2Request(
        artifact_id=gen.artifact_id, base_revision=rep.revision, request_id="res-w",
        changes=[ResumeEditV2(op="resize_component", instance_id="internship#intern-2",
                              width_pt=300.0)]))
    assert rep2.ok
    entry = svc._load_revision(gen.artifact_id, rep2.revision)["content"]["sections"][0]["entries"][1]
    assert entry["width_pt"] == pytest.approx(300.0)
    # height_pt 绝对高度生效
    rep3 = svc.repair(ResumeRepairV2Request(
        artifact_id=gen.artifact_id, base_revision=rep2.revision, request_id="res-h2",
        changes=[ResumeEditV2(op="resize_component", instance_id="internship#intern-1",
                              height_pt=120.0)]))
    assert rep3.ok and rep3.data["actual_changes"][0]["height_pt"] == 120.0
    # 不支持的用法显式拒绝（过去会被静默丢弃或误当高度增量）
    bad_cases = [
        (ResumeEditV2(op="resize_component", instance_id="internship#intern-1",
                      dy_pt=10.0), "ACTION_NOT_ALLOWED"),
        (ResumeEditV2(op="resize_component", instance_id="internship#intern-1",
                      width_pt=300.0, height_pt=120.0, height_delta_pt=5.0), "BOUNDS_VIOLATION"),
        (ResumeEditV2(op="resize_component", instance_id="internship#intern-1"), "CONTENT_INVALID"),
        (ResumeEditV2(op="move_component", instance_id="internship#intern-1",
                      width_pt=300.0), "ACTION_NOT_ALLOWED"),
        (ResumeEditV2(op="move_component", instance_id="internship#intern-1"), "CONTENT_INVALID"),
        (ResumeEditV2(op="resize_component", instance_id="internship#intern-1",
                      width_pt=100.0), "BOUNDS_VIOLATION"),
        (ResumeEditV2(op="resize_component", instance_id="internship#intern-1",
                      height_delta_pt=-10.0), "BOUNDS_VIOLATION"),
    ]
    for change, code in bad_cases:
        with pytest.raises(ToolError) as exc:
            svc.repair(ResumeRepairV2Request(
                artifact_id=gen.artifact_id, base_revision=rep3.revision,
                request_id=f"bad-{change.op}-{code}-{change.width_pt}-{change.dy_pt}",
                changes=[change]))
        assert exc.value.code == code, f"{change}: {exc.value.code} != {code}"


def test_unknown_edit_parameter_is_rejected_by_contract() -> None:
    """review #4：未知参数不能被 pydantic 静默丢弃。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ResumeEditV2(op="move_component", instance_id="a#b", width_mm=10)  # type: ignore[call-arg]


def test_header_unknown_key_and_missing_photo_rejected(svc: ResumeEditService) -> None:
    """review #5：header 未知键 / 不存在照片必须显式拒绝（在渲染前就失败）。"""
    bad = _content()
    bad["header"] = {"fields": {"nickname": "小张"}}
    with pytest.raises(ToolError) as exc:
        svc.generate(ResumeGenerateV2Request(
            template_id="t109", content=ResumeContentV2(**bad), request_id="g-head-bad"))
    assert exc.value.code == "FIELD_UNSUPPORTED"
    assert "nickname" in str(exc.value)
    bad2 = _content()
    bad2["header"] = {"photo_path": "sources/nope.png"}
    with pytest.raises(ToolError) as exc2:
        svc.generate(ResumeGenerateV2Request(
            template_id="t109", content=ResumeContentV2(**bad2), request_id="g-photo-bad"))
    assert exc2.value.code == "ASSET_UNKNOWN"
    # 合法键通过校验并进入内容模型（应用效果由 e2e 用真实渲染验证）
    ok = _content()
    ok["header"] = {"fields": {"name": "张三"}}
    gen = svc.generate(ResumeGenerateV2Request(
        template_id="t109", content=ResumeContentV2(**ok), request_id="g-head-ok"))
    assert gen.ok is True
    record = svc._load_revision(gen.artifact_id, gen.revision)
    assert record["content"]["header"]["fields"]["name"] == "张三"


@e2e
def test_e2e_header_fields_and_photo_applied(tmp_path: Path) -> None:
    """review #5：真实 Word 路径下 header.fields 与照片必须落到产物。"""
    pytest.importorskip("pythoncom")
    from PIL import Image as PILImage

    svc = ResumeEditService(tmp_path, TEMPLATES, capabilities={"vision": True})
    src = svc.workspace / "sources" / "photo.png"
    src.parent.mkdir(parents=True, exist_ok=True)
    PILImage.new("RGB", (600, 900), (10, 90, 160)).save(src)
    content = _content()
    content["header"] = {
        "fields": {"name": "张三", "phone": "138-0000-0000", "school": "某某大学"},
        "photo_path": "sources/photo.png",
    }
    gen = svc.generate(ResumeGenerateV2Request(
        template_id="t109", content=ResumeContentV2(**content), request_id="g-head-e2e"))
    assert gen.ok is True, gen.issues
    record = svc._load_revision(gen.artifact_id, gen.revision)
    applied = record["header"]["fields"]
    assert applied["name"]["after"] == "张三" and applied["name"]["before"] == "简历模板资源网"
    assert applied["phone"]["after"] == "138-0000-0000"
    photo = record["header"]["photo"]
    assert photo["part"] == "word/media/image1.png" and photo["height_pt"] > 0
    # 产物 DOCX：媒体确实被替换（新 PNG 字节出现在包里），XML 里姓名/电话已更新
    docx = svc.workspace / record["docx"]
    with zipfile.ZipFile(docx) as z:
        media = z.read("word/media/image1.png")
        xml = z.read("word/document.xml").decode("utf-8")
    assert media.startswith(b"\x89PNG")
    assert media == src.read_bytes(), "替换后的媒体应等于传入照片（重编码为 PNG）"
    template_media = zipfile.ZipFile(TEMPLATES / "t109" / "template.docx").read(
        "word/media/image1.png"
    )
    assert media != template_media, "模板原照片必须被替换掉"
    assert "张三" in xml and "138-0000-0000" in xml
    # 渲染成功且机械门通过（照片替换不得破坏产物）
    assert record["mechanical"]["passed"] is True, record["mechanical"]["errors"]


# ============ P2-R2：执行中并发重试的幂等（review 第 1 项） ============

def _concurrently(fn, count: int = 2):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(lambda _i: fn(), range(count)))


def test_concurrent_identical_repair_is_idempotent(svc: ResumeEditService) -> None:
    """review：两个**同时进入**的相同 repair 必须都成功，而不是一个冲突。

    旧实现只在加锁前查幂等：后到者拿到锁时版本已 2，于是报 VERSION_CONFLICT。
    现在锁内二次查询，后到者应重放前者结果（同 revision）。
    """
    gen = _gen(svc)
    changes = [ResumeEditV2(op="set_entry_gap", section_key="internship", gap_pt=6.0)]

    def once():
        try:
            return svc.repair(ResumeRepairV2Request(
                artifact_id=gen.artifact_id, base_revision=1,
                request_id="rep-concurrent", changes=changes))
        except ToolError as exc:  # noqa: BLE001 - 失败也要拿到错误码
            return exc

    results = _concurrently(once)
    errors = [r for r in results if isinstance(r, ToolError)]
    assert not errors, f"并发相同重试不应失败：{[e.code for e in errors]}"
    revisions = {r.revision for r in results}
    assert revisions == {2}, f"两个请求必须指向同一版本：{revisions}"
    assert svc._index(gen.artifact_id)["current"] == 2, "只允许新建一个版本"
    idempotent = [r.data.get("idempotent_replay") for r in results]
    assert sorted(idempotent) == [False, True], f"恰好一个真实执行 + 一个重放：{idempotent}"


def test_concurrent_identical_accept_is_idempotent(svc: ResumeEditService) -> None:
    """并发相同 accept 同样幂等；且登记与版本提交在同一锁内完成。"""
    gen = _gen(svc)
    svc.preview(ResumePreviewRequest(artifact_id=gen.artifact_id, revision=1))

    def once():
        try:
            return svc.accept(ResumeAcceptRequest(
                artifact_id=gen.artifact_id, candidate_revision=1,
                expected_accepted_revision=0, request_id="acc-concurrent"),
                visual_notes="并发接受")
        except ToolError as exc:  # noqa: BLE001
            return exc

    results = _concurrently(once)
    errors = [r for r in results if isinstance(r, ToolError)]
    assert not errors, f"并发相同 accept 不应失败：{[e.code for e in errors]}"
    assert {r.data["accepted_revision"] for r in results} == {1}
    assert svc._index(gen.artifact_id)["accepted"] == 1
    # accept 返回后幂等记录必须**立即可见**（提交与登记同锁）
    entry = svc._request_index()["acc-concurrent"]
    assert entry["kind"] == "accept" and entry["revision"] == 1


def test_concurrent_generate_different_artifacts_keeps_all_request_records(
    svc: ResumeEditService,
) -> None:
    """不同产物并发写工作区级请求索引时不得丢记录（请求索引独立加锁）。"""
    def make(suffix: str):
        content = _content()
        content["sections"][0]["entries"][0]["bullets"] = [f"内容 {suffix}；"]
        return lambda: svc.generate(ResumeGenerateV2Request(
            template_id="t109", content=ResumeContentV2(**content),
            request_id=f"req-{suffix}"))

    results = _concurrently_many([make("a"), make("b"), make("c"), make("d")])
    assert all(r.ok for r in results)
    index = svc._request_index()
    for suffix in ("a", "b", "c", "d"):
        assert f"req-{suffix}" in index, f"请求记录丢失：req-{suffix}"


def _concurrently_many(fns):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=len(fns)) as pool:
        return list(pool.map(lambda f: f(), fns))


def test_request_index_lock_is_separate_from_artifact_lock(svc: ResumeEditService) -> None:
    """请求索引锁是独立文件，且不与 artifact 锁互相嵌套加锁（无死锁）。"""
    gen = _gen(svc)
    locks = svc.workspace / "work" / "resume" / "_locks"
    with svc._request_lock():
        assert (locks / "_requests.lock").is_file()
    with svc._artifact_lock(gen.artifact_id):
        assert (locks / f"{gen.artifact_id}.lock").is_file()
        with svc._request_lock():      # 锁序：artifact → request（不允许反向）
            assert True
    assert not (locks / "_requests.lock").exists()


def test_concurrent_retry_while_first_still_running(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """**确定性**并发：第一个请求还在渲染时第二个进入 → 必须等锁后重放。

    用慢速渲染打桩，保证第二个请求的**首次**幂等查询一定发生在第一个完成之前
    （这正是 review 复现的场景：加锁前查不到记录，若锁内不再查就会报冲突）。
    """
    import threading
    import time as _time

    started = threading.Event()

    def slow_render(self, artifact_id, revision, content, *, layout_mode,
                    base_revision, changes, rev_dir):  # noqa: ANN001
        started.set()
        _time.sleep(0.4)          # 让第二个请求在此期间进入并阻塞在锁上
        return _stub_render(self, artifact_id, revision, content,
                            layout_mode=layout_mode, base_revision=base_revision,
                            changes=changes, rev_dir=rev_dir)

    monkeypatch.setattr(ResumeEditService, "_render_revision", slow_render)
    svc = ResumeEditService(tmp_path, TEMPLATES, capabilities={"vision": True})
    gen = svc.generate(ResumeGenerateV2Request(
        template_id="t109", content=ResumeContentV2(**_content()), request_id="race-gen"))
    changes = [ResumeEditV2(op="set_entry_gap", section_key="internship", gap_pt=6.0)]
    outcomes: list = []

    def first():
        started.wait(2.0)
        outcomes.append(svc.repair(ResumeRepairV2Request(
            artifact_id=gen.artifact_id, base_revision=1,
            request_id="race-rep", changes=changes)))

    def second():
        started.wait(2.0)         # 确保在第一个渲染期间进入
        try:
            outcomes.append(svc.repair(ResumeRepairV2Request(
                artifact_id=gen.artifact_id, base_revision=1,
                request_id="race-rep", changes=changes)))
        except ToolError as exc:  # noqa: BLE001
            outcomes.append(exc)

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    errors = [o for o in outcomes if isinstance(o, ToolError)]
    assert not errors, f"执行中重试必须幂等，实际失败：{[e.code for e in errors]}"
    assert {o.revision for o in outcomes} == {2}
    assert [o.data.get("idempotent_replay") for o in outcomes].count(True) == 1


@e2e
def test_e2e_reflow_moves_subsequent_sections(tmp_path: Path) -> None:
    """review 第 2 项的真实 Word 反例：第一栏条目下移必须推动第二栏。

    旧实现只平移本栏条目：第一栏正文底到 X，第二栏仍在原位 → 交叠却被接受。
    """
    pytest.importorskip("pythoncom")
    svc = ResumeEditService(tmp_path, TEMPLATES, capabilities={"vision": True})
    content = {
        "schema_version": "resume-content-v2", "template_id": "t109",
        "sections": [
            {"key": "edu", "title": "教育背景（Education）", "prototype": "plain_lines_v1",
             "entries": [{"id": "e1", "lines": [
                 "2005.07-2009.06            北京简历模板资源网师范大学             市场营销（本科）",
                 "主修课程：管理学、微观经济学、宏观经济学。"]}]},
            {"key": "intern", "title": "实习经历（Internship）", "prototype": "experience_v1",
             "entries": [{"id": "i1",
                          "head": {"date": "2012.06-至今", "org": "某公司", "role": "实习生"},
                          "bullets": ["负责线上端资源的销售工作；", "跟踪客户的详细数据。"]}]},
        ],
    }
    gen = svc.generate(ResumeGenerateV2Request(
        template_id="t109", content=ResumeContentV2(**content), request_id="r2-gen"))
    assert gen.ok is True, gen.issues
    base = svc._load_revision(gen.artifact_id, gen.revision)
    before = {s["section_key"]: s["anchor_y_pt"] for s in base["instance_model"]}
    before_edu_bottom = max(
        e["region"]["y_pt"] + e["region"]["h_pt"]
        for s in base["instance_model"] if s["section_key"] == "edu"
        for e in s["entries"]
    )

    rep = svc.repair(ResumeRepairV2Request(
        artifact_id=gen.artifact_id, base_revision=gen.revision, request_id="r2-reflow",
        layout_mode="reflow",
        changes=[ResumeEditV2(op="move_component", instance_id="edu#e1", dy_pt=80.0)]))
    assert rep.ok is True, rep.issues
    rec = svc._load_revision(gen.artifact_id, rep.revision)
    after = {s["section_key"]: s["anchor_y_pt"] for s in rec["instance_model"]}
    after_edu_bottom = max(
        e["region"]["y_pt"] + e["region"]["h_pt"]
        for s in rec["instance_model"] if s["section_key"] == "edu"
        for e in s["entries"]
    )
    assert after_edu_bottom - before_edu_bottom == pytest.approx(80.0, abs=0.6)
    drift = after["intern"] - before["intern"]
    assert drift == pytest.approx(80.0, abs=1.0), (
        f"reflow 必须把后续栏目一起推动：actual drift={drift}pt"
    )
    # 第 2 栏标题必须仍在前一栏文字底之下（无交叠）
    intern_title_top = after["intern"] + 10.55
    assert intern_title_top > after_edu_bottom
    plan = json.loads(
        (svc._revision_dir(gen.artifact_id, rep.revision) / "layout_plan.json")
        .read_text(encoding="utf-8")
    )
    assert plan["pages"] >= 1


@e2e
def test_e2e_reflow_repaginates_when_pushed_out(tmp_path: Path) -> None:
    """下移把后续栏目推出页底时，必须真实分页（而不是让它越界/交叠）。"""
    pytest.importorskip("pythoncom")
    svc = ResumeEditService(tmp_path, TEMPLATES, capabilities={"vision": True})
    content = {
        "schema_version": "resume-content-v2", "template_id": "t109",
        "sections": [
            {"key": "edu", "title": "教育背景（Education）", "prototype": "plain_lines_v1",
             "entries": [{"id": "e1", "lines": [
                 "2005.07-2009.06 北京简历模板资源网师范大学 市场营销（本科）",
                 "主修课程：管理学、微观经济学、宏观经济学、管理信息系统、统计学。",
                 "第二段补充内容用于加高该条目。",
                 "第三段补充内容用于加高该条目。"]}]},
            {"key": "intern", "title": "实习经历（Internship）", "prototype": "experience_v1",
             "entries": [{"id": "i1",
                          "head": {"date": "2012.06-至今", "org": "某公司", "role": "实习生"},
                          "bullets": ["负责线上端资源的销售工作；", "跟踪客户的详细数据。"]}]},
        ],
    }
    gen = svc.generate(ResumeGenerateV2Request(
        template_id="t109", content=ResumeContentV2(**content), request_id="r2-gen2"))
    assert gen.ok is True, gen.issues
    base = svc._load_revision(gen.artifact_id, gen.revision)
    assert base["page_count"] == 1
    rep = svc.repair(ResumeRepairV2Request(
        artifact_id=gen.artifact_id, base_revision=gen.revision, request_id="r2-push",
        layout_mode="reflow",
        changes=[
            # 单次上限 ±120pt / +200pt；同批多次合法增量可累积，用于把内容
            # 推出第 1 页（这是真实场景：连续微调把后续栏目挤到下一页）
            ResumeEditV2(op="move_component", instance_id="edu#e1", dy_pt=120.0),
            ResumeEditV2(op="resize_component", instance_id="edu#e1",
                         height_delta_pt=200.0),
            ResumeEditV2(op="resize_component", instance_id="edu#e1",
                         height_delta_pt=200.0),
        ]))
    assert rep.ok is True, rep.issues
    rec = svc._load_revision(gen.artifact_id, rep.revision)
    assert rec["page_count"] == 2, (
        f"内容被推出第 1 页时必须真实分页，实际 {rec['page_count']} 页"
    )
    pages = {s["section_key"]: s["page_index"] for s in rec["instance_model"]}
    assert pages["intern"] == 1, f"第 2 栏应落到第 2 页：{pages}"
    # 加高在本栏布局里真实生效（不是被忽略）
    edu_h = max(e["region"]["h_pt"] for s in rec["instance_model"]
                if s["section_key"] == "edu" for e in s["entries"])
    base_h = max(e["region"]["h_pt"] for s in base["instance_model"]
                 if s["section_key"] == "edu" for e in s["entries"])
    assert edu_h - base_h == pytest.approx(400.0, abs=1.0)


def test_request_index_survives_concurrent_writers(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """不同产物并发写工作区级请求索引时，不得丢记录或崩溃。

    `_requests.json` 被所有 artifact 共享：固定 `.tmp` 名 + 无锁 read-modify-write
    会互踩（Windows 上直接 PermissionError）——这是 review 第 1 项第四点。
    """
    monkeypatch.setattr(ResumeEditService, "_render_revision", _stub_render)
    svc = ResumeEditService(tmp_path, TEMPLATES, capabilities={"vision": True})

    def make(tag: str):
        def run():
            content = _content()
            content["sections"][0]["entries"][0]["bullets"] = [f"内容{tag}；"]
            return svc.generate(ResumeGenerateV2Request(
                template_id="t109", content=ResumeContentV2(**content),
                request_id=f"req-{tag}"))

        return run

    from concurrent.futures import ThreadPoolExecutor

    fns = [make(t) for t in ("a", "b", "c", "d", "e")]
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda f: f(), fns))
    assert all(r.ok for r in results), [r.issues for r in results if not r.ok]
    index = svc._request_index()
    for t in ("a", "b", "c", "d", "e"):
        assert f"req-{t}" in index, f"请求记录丢失: req-{t}"
    # 没有残留临时文件
    leftovers = list((svc.workspace / "work" / "resume").glob("*.tmp"))
    assert not leftovers, f"残留临时文件: {leftovers}"
