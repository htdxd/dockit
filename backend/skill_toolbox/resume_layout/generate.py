# -*- coding: utf-8 -*-
"""generate.py — P1 探针主流程：t109 组件化排版端到端生成。

流程（对每类证据样例）：
  1. 复制原件为宿主副本（原件只读）
  2. 【测量阶段】宿主副本 + Word COM：逐条写入条目文本读行位/行数（单实例批量）
  3. 【布局阶段】真实测量 → plan_layout（顺序/重排/分页，纯函数）
  4. 【落盘阶段】另一份副本：克隆栏目 drawing、全链组伸展、写 posV、真实分页符
  5. 【渲染阶段】render_pages.py 同路径（Word COM → PDF → PNG）
  6. 【QA 阶段】PDF 提取行位/页数/内容完整性 → 机械检查 + 误差表

CLI:
  python generate.py <workspace_out_dir> <scenario>
  scenario ∈ {replay, clone, grow, twopages}
"""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from lxml import etree

PROJECT_ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = (
    PROJECT_ROOT
    / "backend/skill_toolbox/skill_defs/resume_pro/templates/t109/template.docx"
)
RENDER_SCRIPT = (
    PROJECT_ROOT
    / "backend/skill_toolbox/skill_defs/resume_pro/scripts/render_pages.py"
)

sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from skill_toolbox.resume_layout import emit, layout, measure, qa, spacing  # noqa: E402
from skill_toolbox.resume_layout.layout import LayoutUnsatisfiable, MeasureResult  # noqa: E402

# 原件栏目 anchor 的 docPr name（P0 解析）：克隆源
SOURCE_SECTION_NAMES = {
    "education": "组合 215",
    "internship": "组合 216",
    "campus": "组合 218",
    "skills": "组合 219",
    "summary": "组合 220",
}
# Word COM Shapes 序号（宿主文档内，1-based；P0 探针验证）：
# 1=顶带 2=照片 3=教育 4=实习 5=校园 6=技能 7=自我 8=个人信息 9=底条
# 逐栏目测量：条目在「本栏目自己的正文框」里测（样式/项目符号影响 wrap）
# 母版 anchor 的 docPr name（个人信息区 / 照片）：header.fields 与照片替换用
MASTER_INFO_ANCHOR_NAME = "组合 214"
MASTER_PHOTO_ANCHOR_NAME = "组合 12"
MEASURE_SHAPE_BY_SECTION = {
    "education": 3, "internship": 4, "campus": 5,
    "skills": 6, "summary": 7,
}
MEASURE_BODY_ITEM_INDEX = 2        # GroupItems(2)=正文框
# 新增栏目（模板中不存在）复用实习栏原型（同为经历型：头段+职责段带符号）
FALLBACK_PROTO_SECTION = "internship"


# ---------------- 场景定义 ----------------

def _original_section_texts() -> dict[str, list[str]]:
    """从 t109 原件 document.xml 逐字提取各栏目正文段列表（重放唯一权威源）。

    manifest 的 text 字段与 XML 原文存在差异（如自我评价 106 字 vs 原件 87 字，
    R2 review 证实），重放必须以 XML 为准——包括段内首尾空白与空段。
    """
    import zipfile
    from lxml import etree as _et

    W_ = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    WP_ = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
    WPS_ = "{http://schemas.microsoft.com/office/word/2010/wordprocessingShape}"
    with zipfile.ZipFile(TEMPLATE) as z:
        root = _et.fromstring(z.read("word/document.xml"))
    out: dict[str, list[str]] = {}
    for sid, name in SOURCE_SECTION_NAMES.items():
        for a in root.findall(".//" + WP_ + "anchor"):
            dp = a.find(WP_ + "docPr")
            if dp is None or dp.get("name") != name:
                continue
            for wsp in a.iter(WPS_ + "wsp"):
                tx = wsp.find(".//" + W_ + "txbxContent")
                if tx is None:
                    continue
                t = "".join(x.text or "" for x in tx.iter(W_ + "t"))
                if len(t) > 40:
                    out[sid] = [
                        "".join(x.text or "" for x in p.iter(W_ + "t"))
                        for p in tx.iter(W_ + "p")
                    ]
                    break
            break
    missing = set(SOURCE_SECTION_NAMES) - set(out)
    if missing:
        raise RuntimeError(f"原件栏目原文提取失败: {sorted(missing)}")
    return out


def _internship_split(orig_paras: list[str]) -> list[list[str]]:
    """原件实习栏 7 段（条0/空段/条1）拆成两个条目的段列表。"""
    if len(orig_paras) >= 5 and not orig_paras[3].strip():
        return [orig_paras[:3], orig_paras[4:]]
    # 非预期结构：按头段（日期开头）切分兜底
    entries: list[list[str]] = []
    cur: list[str] = []
    for p in orig_paras:
        if p[:2].isdigit() and cur:
            entries.append(cur)
            cur = [p]
        else:
            cur.append(p)
    if cur:
        entries.append(cur)
    return entries


def scenario_replay() -> dict:
    """原内容重放：5 个栏目原文逐字回填（document.xml 为唯一权威源）。"""
    orig = _original_section_texts()
    intern_entries = _internship_split(orig["internship"])
    return {
        "id": "replay",
        "sections": [
            {"id": "education", "title": "教育背景（Education）", "entries": [
                {"id": "edu-1", "text": "\n".join(orig["education"])},
            ]},
            {"id": "internship", "title": "实习经历（Internship）", "entries": [
                {"id": f"intern-{i+1}", "text": "\n".join(paras)}
                for i, paras in enumerate(intern_entries)
            ]},
            {"id": "campus", "title": "校园经历（Campus）", "entries": [
                {"id": "campus-1", "text": "\n".join(orig["campus"])},
            ]},
            {"id": "skills", "title": "技能证书（Skills certificate）", "entries": [
                {"id": "skill-1", "text": "\n".join(orig["skills"])},
            ]},
            {"id": "summary", "title": "自我评价（Self-assessment）", "entries": [
                {"id": "summary-1", "text": orig["summary"][0]},
            ]},
        ],
    }


def scenario_clone() -> dict:
    """复制条目：实习经历 1 条 → 3 条同结构条目（验证不重复标题、槽位正确）。"""
    base = scenario_replay()
    intern = next(s for s in base["sections"] if s["id"] == "internship")
    intern["entries"] = [
        {"id": "intern-1", "text":
         "2021.07-至今               上海数据科技有限公司                           数据产品经理（实习）\n"
         "负责数据平台产品需求分析与原型设计，输出 PRD 十二份；\n"
         "协同研发团队完成三个数据看板上线，服务客户两百余家。"},
        {"id": "intern-2", "text":
         "2020.01-2021.06           杭州云服务信息技术有限公司                   产品运营（实习）\n"
         "负责社区内容运营与活动策划，季度活动覆盖用户五万；\n"
         "搭建用户分层触达体系，次月留存提升百分之六。"},
        {"id": "intern-3", "text":
         "2019.03-2019.09           深圳智能硬件创业公司                           市场助理（实习）\n"
         "支持线下展会与渠道拓展，整理竞品分析报告八份；\n"
         "协助完成两场新品发布会的物料与流程管理。"},
    ]
    base["id"] = "clone"
    return base


def scenario_grow() -> dict:
    """内容变长：教育/实习条目加长（测量驱动后续栏目重排）。"""
    base = scenario_replay()
    edu = next(s for s in base["sections"] if s["id"] == "education")
    edu["entries"][0]["text"] = (
        "2016.09-2020.06                北京简历模板资源网师范大学                       市场营销（本科）\n"
        "主修课程：\n"
        "管理学、微观经济学、宏观经济学、管理信息系统、统计学、会计学、财务管理、市场营销、经济法、消费者行为学、国际市场营销、"
        "组织行为学、人力资源管理、商务谈判、企业战略管理、市场调研方法、消费者心理学、品牌管理、数字化营销、供应链管理基础、"
        "数据分析导论、计量经济学入门、项目管理实务、商业伦理与企业社会责任、跨文化沟通、演讲与表达、第二外语（日语）基础"
    )
    intern = next(s for s in base["sections"] if s["id"] == "internship")
    intern["entries"][0]["text"] += (
        "\n负责季度复盘与增长实验设计，通过 A/B 测试优化投放组合，单位获客成本下降百分之十八；"
        "搭建渠道数据看板，实现投放效果的日级监控与异常预警，累计发现并止损渠道异常十一次。"
    )
    base["id"] = "grow"
    return base


def scenario_twopages() -> dict:
    """容量不足 → 真实第二页：新增项目经历栏目 + 全面加长。"""
    base = scenario_grow()
    intern = next(s for s in base["sections"] if s["id"] == "internship")
    intern["entries"].append(
        {"id": "intern-3", "text":
         "2018.06-2018.12           广州新媒体传播有限公司                         内容编辑（实习）\n"
         "负责公众号内容策划与撰写，累计产出原创稿件四十余篇，平均阅读量提升两倍；\n"
         "参与两次大型线上活动的文案统筹，活动新增关注用户逾三万。"}
    )
    campus = next(s for s in base["sections"] if s["id"] == "campus")
    campus["entries"].append(
        {"id": "campus-2", "text":
         "2017.09-2018.06           校学生会科技创新部                           部门骨干\n"
         "组织两届校园科技文化节，协调八个社团与三十余名志愿者；\n"
         "牵头完成活动赞助洽谈，获三家本地企业物资与资金支持。"}
    )
    # 新增栏目：项目经历（新标题 + 3 条长短不一条目）
    skills_idx = next(
        i for i, s in enumerate(base["sections"]) if s["id"] == "skills"
    )
    base["sections"].insert(skills_idx, {
        "id": "projects", "title": "项目经历（Projects）", "entries": [
            {"id": "proj-1", "text":
             "2022.03-2022.11           企业级知识库检索系统                           核心开发\n"
             "基于向量检索与重排模型构建知识库问答链路，回答准确率从百分之六十二提升到百分之八十一；\n"
             "负责索引管道与缓存设计，检索 P95 延迟降至三百毫秒以内。"},
            {"id": "proj-2", "text":
             "2021.05-2021.12           电商用户增长分析平台                           数据分析\n"
             "整合五类行为数据源，建立用户生命周期标签体系，覆盖活跃用户一百二十万；\n"
             "输出复购归因模型，指导运营策略调整后季度复购率提升百分之九。"},
            {"id": "proj-3", "text":
             "2020.09-2021.02           校园二手交易小程序                             独立开发\n"
             "完成前后端开发与上线，注册用户六千余，日均成交四十单。"},
        ],
    })
    base["id"] = "twopages"
    return base


SCENARIOS = {
    "replay": scenario_replay,
    "clone": scenario_clone,
    "grow": scenario_grow,
    "twopages": scenario_twopages,
}


# ---------------- 测量阶段 ----------------

def measure_scenario(scenario: dict, work_dir: Path) -> dict[str, MeasureResult]:
    """复制原件为测量宿主 → COM 批量测量所有条目。

    逐栏目测量：每栏条目写入**本栏目自己的正文框**（样式档案不同——
    实习/校园职责段带项目符号、宽度缩进不同会影响 wrap），单实例批量。
    """
    host = work_dir / "measure_host.docx"
    shutil.copy(TEMPLATE, host)
    probe = measure.WordMeasureProbe(host)
    all_results: list = []
    for sec in scenario["sections"]:
        entries = [
            {
                "id": e["id"],
                "text": e["text"].replace("\n", "\r"),
                **({"width_pt": e["width_pt"]} if e.get("width_pt") else {}),
            }
            for e in sec["entries"]
        ]
        if not entries:
            continue
        shape_idx = MEASURE_SHAPE_BY_SECTION.get(
            sec["id"], MEASURE_SHAPE_BY_SECTION[FALLBACK_PROTO_SECTION]
        )
        all_results.extend(
            probe.measure_entries(shape_idx, MEASURE_BODY_ITEM_INDEX, entries)
        )
    (work_dir / "measure_result.json").write_text(
        json.dumps(
            {"ok": True, "results": [r.to_dict() for r in all_results]},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    return {
        r.entry_id: MeasureResult(
            entry_id=r.entry_id,
            wrapped_lines=r.wrapped_lines,
            text_height_pt=r.text_height_pt,
        )
        for r in all_results
    }


# ---------------- 落盘阶段 ----------------

def emit_scenario(
    scenario: dict, plan: layout.LayoutPlan, out_docx: Path
) -> dict:
    """在原件副本上：克隆/重定位栏目、写文本、真实分页。

    scenario 可选字段（P2 修订）：
    - `header.fields`：母版个人信息区「标签：值」替换（键为 name/phone/…）
    - `header.photo_bytes` + `header.photo_part`：替换照片媒体部件（等比适配）
    - 条目级 `width_pt`：正文框宽度（由布局层按真实测量决定，变窄则多换行）
    """
    root = emit.load_document_xml(TEMPLATE)
    body = root.find(emit.W + "body")
    paragraphs = body.findall(emit.W + "p")

    # 收集原件 5 个栏目 anchor，并先深拷贝整个 AlternateContent 作克隆源
    # （后续原件栏目会被移除，原型必须先快照）。
    # **按栏目各自克隆**：图标 custGeom、横线、正文段落样式（粗体/numPr
    # 项目符号）在 5 个栏目间各不相同——克隆源必须用栏目自己的原型，
    # 才能保留视觉风格（review Fix4）。
    source_anchor_by_section = {}
    proto_snapshot: dict[str, etree._Element] = {}
    for sid, name in SOURCE_SECTION_NAMES.items():
        a = emit._anchor_by_docpr_name(root, name)
        if a is None:
            raise RuntimeError(f"找不到源栏目 {name}")
        source_anchor_by_section[sid] = a
        node = a
        while node is not None and etree.QName(node).localname != "AlternateContent":
            node = node.getparent()
        if node is None:
            raise RuntimeError(f"源栏目 {name} 缺 AlternateContent")
        proto_snapshot[sid] = copy.deepcopy(node)

    # 隐藏原件 5 个栏目（整块 drawing 从 AlternateContent 移除），
    # 只留母版（顶部色带/底部条/个人信息/照片）
    for sid, anchor in source_anchor_by_section.items():
        node = anchor
        while node is not None and etree.QName(node).localname != "AlternateContent":
            node = node.getparent()
        host_run = node.getparent()
        host_run.getparent().remove(host_run)

    # 每个栏目挂回自己的原型（克隆源 anchor）；模板没有的栏目
    # （如 projects）复用实习栏原型。
    proto_anchor_by_section: dict[str, etree._Element] = {}
    needed_sections = [s["id"] for s in scenario["sections"]]
    for sid in needed_sections:
        src = sid if sid in proto_snapshot else FALLBACK_PROTO_SECTION
        proto_anchor_by_section[sid] = emit.insert_proto(root, copy.deepcopy(proto_snapshot[src]))

    # 承载段落：最后一个空段落（p2..p8 中选最后一个 body 段）；真实分页符注入
    # 分页机制：每页一个承载段落；页与页之间插入「独立分页段落」
    # （段落内仅一个 <w:br w:type="page"/> run）。分页符不能与 anchor 同段
    # ——Word 会把同段 anchor 视为分页前内容（实测重叠）。
    carrier_by_page: dict[int, etree._Element] = {}
    p_first = paragraphs[2]
    carrier_by_page[0] = p_first
    prev_carrier = p_first
    for page in range(1, plan.pages):
        brk_p = etree.Element(emit.W + "p")
        r = etree.SubElement(brk_p, emit.W + "r")
        br = etree.SubElement(r, emit.W + "br")
        br.set(emit.W + "type", "page")
        prev_carrier.addnext(brk_p)
        carrier_p = etree.Element(emit.W + "p")
        brk_p.addnext(carrier_p)
        prev_carrier = carrier_p
        carrier_by_page[page] = carrier_p

    # 逐栏目克隆放置（用栏目自己的原型 → 图标/横线/样式随栏目保留）
    box_records = []
    emit_records = []
    for sec in plan.sections:
        scen_sec = next(s for s in scenario["sections"] if s["id"] == sec.section_id)
        proto_anchor = proto_anchor_by_section[sec.section_id]
        # 从原型正文框按**角色**分桶样式（P2-1：条目标题段 / 职责段，
        # 由 numPr 结构判定，不再按段落序号套用——否则新增职责行会继承
        # 第 2 条经历的标题样式，渲染成加粗且无项目符号）
        proto_body = emit.find_body_wsp(proto_anchor)
        style_roles = emit.build_style_roles(
            proto_body.find(".//" + emit.W + "txbxContent")
        )
        for k, entry_plan in enumerate(sec.entries):
            new_anchor = emit.clone_section_anchor(
                root, proto_anchor, title_text=scen_sec["title"] if k == 0 else ""
            )
            # 克隆链：anchor > drawing > Choice > AlternateContent > r
            node = new_anchor
            while node is not None and etree.QName(node).localname != "AlternateContent":
                node = node.getparent()
            if node is None:
                raise RuntimeError("克隆 anchor 缺 AlternateContent 祖先")
            ac = node
            ac.getparent().remove(ac)  # 先脱离原宿主 run
            carrier = carrier_by_page[entry_plan.page_index]
            run = etree.SubElement(carrier, emit.W + "r")
            run.append(ac)
            # posV：k=0 的克隆是「标题组合」（标题框在 anchor 顶 0~30.2，
            # 正文框在 anchor 内 off 29.2），放栏目标题 y；k>0 的克隆已剥掉
            # 标题/装饰，只剩正文框（anchor 内 off 29.2），布局给的
            # entry anchor_y 已按正文框顶计算，直接使用。
            if k == 0:
                emit.set_anchor_pos_v(new_anchor, sec.anchor_y_pt)
            else:
                emit.set_anchor_pos_v(new_anchor, entry_plan.anchor_y_pt - 29.2)
            # 正文框：写文本（按原型段落样式快照，保留 numPr/粗体模式）+ 全链伸展
            entry_text = next(
                e["text"] for e in scen_sec["entries"] if e["id"] == entry_plan.instance_id
            )
            body_wsp = emit.find_body_wsp(new_anchor)
            emit.set_box_text(body_wsp, entry_text, style_roles=style_roles)
            emit.resize_group_child_bottom(new_anchor, body_wsp, entry_plan.body_h_pt)
            # 宽度覆盖：布局按真实测量给出的宽度（≤ 模板正文宽）；测量侧
            # 用同一宽度，保证「换行行数」与产物一致
            width_pt = next(
                (e.get("width_pt") for e in scen_sec["entries"]
                 if e["id"] == entry_plan.instance_id),
                None,
            )
            if width_pt:
                emit.set_child_width(body_wsp, float(width_pt))
            if k > 0:
                emit.strip_title_decorations(new_anchor)
            box_records.append({
                "section_id": f"{sec.section_id}[{k}]",
                "page_index": entry_plan.page_index,
                "top_pt": entry_plan.anchor_y_pt,
                "bottom_pt": entry_plan.anchor_y_pt + entry_plan.body_h_pt + 29.2,
            })
            emit_records.append({
                "instance_id": entry_plan.instance_id,
                "page": entry_plan.page_index,
                "anchor_y_pt": entry_plan.anchor_y_pt,
                "body_h_pt": entry_plan.body_h_pt,
            })
    # 删除全部克隆源 proto 的宿主段落（其 anchor 是模板原文，不能进入产物）
    for sid, proto_anchor in proto_anchor_by_section.items():
        proto_host_p = proto_anchor.getparent()
        while proto_host_p is not None and etree.QName(proto_host_p).localname != "p":
            proto_host_p = proto_host_p.getparent()
        if proto_host_p is None:
            raise RuntimeError(f"proto({sid}) 宿主段落定位失败")
        proto_host_p.getparent().remove(proto_host_p)
    # 个人信息与照片（母版）：字段替换 + 照片媒体替换（图片等比适配）
    header_record: dict = {}
    header = scenario.get("header") or {}
    if header.get("fields"):
        info_anchor = emit._anchor_by_docpr_name(root, MASTER_INFO_ANCHOR_NAME)
        if info_anchor is None:
            raise RuntimeError(f"找不到母版个人信息 anchor {MASTER_INFO_ANCHOR_NAME}")
        header_record["fields"] = emit.set_info_fields(info_anchor, header["fields"])
    part_overrides: dict[str, bytes] = {}
    if header.get("photo_bytes"):
        photo_anchor = emit._anchor_by_docpr_name(root, MASTER_PHOTO_ANCHOR_NAME)
        if photo_anchor is None:
            raise RuntimeError(f"找不到照片 anchor {MASTER_PHOTO_ANCHOR_NAME}")
        header_record["photo"] = emit.replace_photo(photo_anchor, header["photo_bytes"])
        header_record["photo"]["part"] = header.get("photo_part", "")
        part_overrides[header["photo_part"]] = header["photo_bytes"]

    # 保存
    tmp_out = out_docx.with_suffix(".docx.tmp")
    emit.save_document_xml(root, TEMPLATE, tmp_out, part_overrides=part_overrides or None)
    tmp_out.replace(out_docx)
    return {
        "boxes": box_records,
        "emit_records": emit_records,
        "pages": plan.pages,
        "header": header_record,
    }


def main() -> None:
    out_dir = Path(sys.argv[1]).resolve()
    scenario_name = sys.argv[2]
    scenario = SCENARIOS[scenario_name]()
    work = out_dir / scenario_name
    work.mkdir(parents=True, exist_ok=True)

    # 1. 测量
    measurements = measure_scenario(scenario, work)
    # 1b. 原件间距档案（三级几何：文字边界/组件外框/栏目间距）。
    # 缺失或原件 hash 变化时用 Word COM 在**副本**上重建（原件只读）。
    archive_path = out_dir / "spacing_archive.json"
    archive = spacing.ensure_archive(TEMPLATE, archive_path)
    # 2. 布局（栏目推进量取档案；条目高度仍来自真实测量，不恢复固定框高）
    try:
        plan = layout.plan_layout(scenario["sections"], measurements, spacing=archive)
    except LayoutUnsatisfiable as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        sys.exit(3)
    (work / "layout_plan.json").write_text(
        json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 3. 落盘
    out_docx = work / f"resume_{scenario_name}.docx"
    emit_info = emit_scenario(scenario, plan, out_docx)
    # 4. 渲染（render_pages.py 以 cwd 为 workspace 根）
    render_dir = work / "render"
    render_dir.mkdir(exist_ok=True)
    shutil.copy(out_docx, work / "render_src.docx")
    import os
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    rc = subprocess.run(
        [sys.executable, str(RENDER_SCRIPT), str(out_docx), str(render_dir)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(work), env=env, timeout=300,
    )
    render_json = {}
    if rc.returncode == 0:
        try:
            render_json = json.loads(rc.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            render_json = {"error": rc.stdout[-300:]}
    else:
        render_json = {"error": (rc.stderr or rc.stdout)[-300:]}
    (work / "render_result.json").write_text(
        json.dumps(render_json, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({
        "ok": render_json.get("page_images_created") is not None,
        "scenario": scenario_name,
        "docx": str(out_docx),
        "plan_pages": plan.pages,
        "rendered_pages": render_json.get("page_images_created"),
        "emit": emit_info,
        "render": render_json,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
