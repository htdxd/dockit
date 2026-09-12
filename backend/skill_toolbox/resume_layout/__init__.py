# -*- coding: utf-8 -*-
"""resume_layout — 简历组件化排版内部包（实施计划 P1：t109 技术探针）。

职责拆分（均不依赖模型 / UI / 真实 Word，可单独测试）：
- template.py   预处理组件包（v2 package：原型、槽位、页面母版）
- measure.py    Word COM 测量探针（真实行位/行高，含缓存）
- layout.py     确定性排版器（顺序布局、变长重排、真实分页）
- emit.py       LayoutPlan → DOCX（克隆原型、全链组变换、承载段落分页）
- qa.py         机械检查（内容完整性 / 重叠 / 越界 / 残留）
"""
