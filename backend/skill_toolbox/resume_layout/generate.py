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

import json
import shutil
import subprocess
import sys
from pathlib import Path

# 兼容直接运行证据脚本；生产入口只导入 pipeline。
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from skill_toolbox.resume_layout import layout, spacing
from skill_toolbox.resume_layout.layout import LayoutUnsatisfiable
from skill_toolbox.resume_layout.t109 import TEMPLATE, RENDER_SCRIPT, SOURCE_SECTION_NAMES
from skill_toolbox.resume_layout import pipeline

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


def measure_scenario(scenario: dict, work_dir: Path):
    return pipeline.measure_scenario(scenario, work_dir, template=TEMPLATE)


def emit_scenario(scenario: dict, plan: layout.LayoutPlan, out_docx: Path):
    return pipeline.emit_scenario(scenario, plan, out_docx, template=TEMPLATE)


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
