"""固定回归集的输入与证据读取测试；真实 Word 仅由脚本显式执行。"""
import importlib.util
from pathlib import Path
from zipfile import ZipFile

import pytest

from skill_toolbox.contracts.resume_workflow import GenerateRequest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/verify_resume_highlights.py"
spec = importlib.util.spec_from_file_location("verify_resume_highlights", SCRIPT)
cases = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cases)


def test_fixed_matrix_has_twenty_distinct_positive_cases_and_separate_negatives():
    matrix = cases.case_matrix()
    assert len(matrix) == 20
    assert len({(c["template"], c["scenario"]) for c in matrix}) == 20
    assert not set(cases.NEGATIVES) & {c["scenario"] for c in matrix}
    assert {c["template"] for c in matrix} == {"t001", "t109"}


@pytest.mark.parametrize("scenario", cases.SCENARIOS + cases.NEGATIVES)
def test_fixed_case_input_satisfies_public_schema_and_preserves_source(scenario):
    content = cases.case_content(scenario)
    request = GenerateRequest(content=content)
    entry = request.content.sections[0].entries[0]
    assert entry.source_ids == ["fixture"]
    assert "检索耗时降低 30%" in entry.text[0]
    assert entry.details[0].label == "Stars"
    assert entry.details[1].label == "Forks"
    assert entry.details[2].link.startswith("https://github.com/")
    # 每次生成独立对象，编辑样例不会污染后续样例。
    content["sections"][0]["entries"][0]["text"].clear()
    assert cases.case_content(scenario)["sections"][0]["entries"][0]["text"]


def test_evidence_reader_handles_split_runs_and_ignores_fallback(tmp_path):
    docx = tmp_path / "example.docx"
    xml = f'''<w:document xmlns:w="{cases.W[1:-1]}" xmlns:mc="{cases.MC[1:-1]}">
      <w:body><mc:AlternateContent><mc:Choice Requires="w">
      <w:p><w:r><w:t>优化：</w:t></w:r>
      <w:r><w:rPr><w:b w:val="1"/><w:color w:val="244761"/></w:rPr><w:t>检索耗时</w:t></w:r>
      <w:r><w:rPr><w:b w:val="1"/><w:shd w:fill="EEEEEE"/></w:rPr><w:t>降低 30%</w:t></w:r></w:p>
      </mc:Choice><mc:Fallback><w:p><w:r><w:t>检索耗时降低 30%</w:t></w:r></w:p></mc:Fallback>
      </mc:AlternateContent></w:body></w:document>'''
    with ZipFile(docx, "w") as archive:
        archive.writestr("word/document.xml", xml)
    rows = cases.phrase_styles(docx, cases.PHRASE)
    assert len(rows) == 1
    assert "".join(r["text"] for r in rows[0]) == cases.PHRASE
    assert [r["bold"] for r in rows[0]] == ["1", "1"]
    assert rows[0][0]["color"] == "244761"
    assert rows[0][1]["fill"] == "EEEEEE"


def test_default_cli_only_lists_cases(monkeypatch, capsys):
    import json
    monkeypatch.setattr("sys.argv", [str(SCRIPT)])
    monkeypatch.setattr(cases, "run_case", lambda *args: pytest.fail("dry run 不得渲染"))
    assert cases.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["execute"] is False
    assert len(result["selected"]) == 24


def test_evidence_reader_does_not_count_textbox_text_in_anchor_host(tmp_path):
    docx = tmp_path / "anchor.docx"
    xml = f'''<w:document xmlns:w="{cases.W[1:-1]}"><w:body>
      <w:p><w:r><w:drawing><w:txbxContent><w:p>
        <w:r><w:rPr><w:b w:val="1"/></w:rPr><w:t>检索耗时降低 30%</w:t></w:r>
      </w:p></w:txbxContent></w:drawing></w:r></w:p>
      </w:body></w:document>'''
    with ZipFile(docx, "w") as archive:
        archive.writestr("word/document.xml", xml)
    assert cases.phrase_styles(docx, cases.PHRASE) == [[{
        "text": cases.PHRASE, "bold": "1", "color": None, "fill": None,
    }]]
