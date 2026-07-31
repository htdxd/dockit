"""Generate a structured ML tutorial docx for testing the Generate PPT route.

Covers: heading hierarchy, prose paragraphs, bulleted/numbered lists, a table,
and a code block — so we can verify the model extracts and reorganizes all
content forms when building SVG slides.
"""
from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.shared import Pt, RGBColor


def build(out_path: Path) -> None:
    doc = Document()

    # Title
    doc.add_heading("机器学习入门教程", level=0)
    doc.add_paragraph(
        "本教程面向零基础读者，用最简练的语言讲清机器学习的核心概念、"
        "主流算法分类、训练流程，以及如何选择第一个实战项目。"
    ).paragraph_format.space_after = Pt(12)

    # Section 1
    doc.add_heading("一、什么是机器学习", level=1)
    doc.add_paragraph(
        "机器学习是人工智能的一个分支，它让计算机从数据中自动学习规律，"
        "而不是靠人工编写固定规则。Tom Mitchell 给出的经典定义是："
        "如果一个程序在任务 T 上的表现 P 随着经验 E 的积累而提升，"
        "就说它在从经验 E 中学习。"
    )
    doc.add_paragraph(
        "举个例子：要识别邮件是否为垃圾邮件，传统做法是写一堆关键词规则；"
        "而机器学习的做法是给模型看成千上万封已标注的邮件，让它自己学会区分。"
    )

    # Section 2 — with bulleted list
    doc.add_heading("二、三大学习范式", level=1)
    doc.add_paragraph("按数据是否带标签，机器学习主要分三类：")
    for item in [
        '监督学习（Supervised Learning）：数据带标签，如给图片标注"猫/狗"。代表任务：分类、回归。',
        "无监督学习（Unsupervised Learning）：数据无标签，让模型自己发现结构。代表任务：聚类、降维。",
        "强化学习（Reinforcement Learning）：智能体通过试错与环境交互，最大化累积奖励。代表任务：游戏 AI、机器人控制。",
    ]:
        doc.add_paragraph(item, style="List Bullet")

    # Section 3 — with numbered list
    doc.add_heading("三、模型训练的五个步骤", level=1)
    for i, step in enumerate(
        [
            "数据收集与清洗：高质量数据是一切的基础。",
            "特征工程：把原始数据转成模型能理解的数值特征。",
            "选择模型：根据任务类型选算法（线性回归、决策树、神经网络等）。",
            "训练与验证：用训练集拟合参数，用验证集调超参数。",
            "评估与部署：在测试集上衡量泛化能力，再上线服务。",
        ],
        start=1,
    ):
        doc.add_paragraph(step, style="List Number")

    # Section 4 — with table
    doc.add_heading("四、常见算法速查", level=1)
    doc.add_paragraph("下表汇总了几种入门级算法的适用场景与优缺点：")
    table = doc.add_table(rows=1, cols=4)
    table.style = "Light Grid Accent 1"
    hdr = table.rows[0].cells
    hdr[0].text = "算法"
    hdr[1].text = "类型"
    hdr[2].text = "适用场景"
    hdr[3].text = "主要优点"
    for row_data in [
        ("线性回归", "监督", "预测连续数值，如房价", "简单、可解释"),
        ("逻辑回归", "监督", "二分类，如垃圾邮件检测", "概率输出、训练快"),
        ("K-Means", "无监督", "客户分群、图像压缩", "无需标签、速度快"),
        ("决策树", "监督", "分类与回归皆可", "可解释、能处理混合特征"),
    ]:
        cells = table.add_row().cells
        for i, val in enumerate(row_data):
            cells[i].text = val

    # Section 5 — with code block (as monospace paragraph)
    doc.add_heading("五、第一个实战项目：手写数字识别", level=1)
    doc.add_paragraph(
        "建议从 MNIST 数据集起步：它包含 6 万张 28×28 像素的手写数字图片，"
        "目标是识别 0-9。用 scikit-learn 几行代码就能跑通一个 baseline。"
    )
    doc.add_paragraph("示例代码（scikit-learn）：").runs[0].bold = True
    code = doc.add_paragraph()
    code.paragraph_format.left_indent = Pt(20)
    run = code.add_run(
        "from sklearn.datasets import fetch_openml\n"
        "from sklearn.model_selection import train_test_split\n"
        "from sklearn.ensemble import RandomForestClassifier\n"
        "\n"
        "X, y = fetch_openml('mnist_784', version=1, return_X_y=True)\n"
        "X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2)\n"
        "clf = RandomForestClassifier(n_estimators=100)\n"
        "clf.fit(X_train, y_train)\n"
        "print('准确率:', clf.score(X_test, y_test))"
    )
    run.font.name = "Consolas"
    run.font.size = Pt(10)

    # Section 6 — closing
    doc.add_heading("六、学习路径建议", level=1)
    doc.add_paragraph(
        "入门路线推荐：先掌握 Python 与 NumPy/Pandas，再学 scikit-learn 跑通经典算法，"
        "最后进入深度学习框架（PyTorch / TensorFlow）。理论方面，"
        "周志华的《机器学习》或 Andrew Ng 的 Coursera 课程都是经典起点。"
    )
    doc.add_paragraph(
        '切记：不要陷入"只看不练"的陷阱。每学一个算法，就找一个数据集动手实现一遍，'
        "哪怕只是调库也胜过纯读理论。"
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(out_path)
    print(f"已生成: {out_path}  ({out_path.stat().st_size} bytes)")


if __name__ == "__main__":
    build(Path(r"E:\learning\skill-project\test-materials\ml_tutorial.docx"))
