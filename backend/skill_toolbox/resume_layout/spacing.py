"""读取已校准的原件间距；未登记的栏目组合使用排版器默认间距。"""
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

SCHEMA_VERSION = "spacing-archive-1"


@dataclass
class SectionSpacing:
    section_id: str
    frame_top_pt: float
    frame_bottom_pt: float
    body_wsp_off_y_pt: float
    body_h_pt: float
    text_ink_top_pt: float
    text_ink_bottom_pt: float
    content_paragraphs: int
    content_lines: int
    bottom_slack_pt: float
    entry_count: int = 1
    entry_gap_pt: float | None = None
    entry_lines: list[int] = field(default_factory=list)


@dataclass
class PairSpacing:
    prev_section_id: str
    next_section_id: str
    anchor_gap_pt: float
    visible_gap_pt: float
    anchor_delta_from_prev_text_pt: float


@dataclass
class SpacingArchive:
    template_id: str
    template_sha256: str
    schema_version: str
    title_text_top_off_pt: float
    sections: dict[str, SectionSpacing] = field(default_factory=dict)
    pairs: dict[tuple[str, str], PairSpacing] = field(default_factory=dict)

    def delta_after(self, prev_id, next_id, default):
        pair = self.pairs.get((prev_id, next_id))
        return pair.anchor_delta_from_prev_text_pt if pair is not None else default

    def entry_gap_for(self, section_id, default):
        section = self.sections.get(section_id)
        return section.entry_gap_pt if section is not None and section.entry_gap_pt is not None else default

    def to_dict(self):
        return {
            "template_id": self.template_id, "template_sha256": self.template_sha256,
            "schema_version": self.schema_version, "title_text_top_off_pt": self.title_text_top_off_pt,
            "sections": [asdict(s) for s in self.sections.values()],
            "pairs": [asdict(p) for p in self.pairs.values()],
        }


def load_archive(path: Path) -> SpacingArchive:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return SpacingArchive(
        raw["template_id"], raw["template_sha256"], raw["schema_version"], raw["title_text_top_off_pt"],
        {s["section_id"]: SectionSpacing(**s) for s in raw["sections"]},
        {(p["prev_section_id"], p["next_section_id"]): PairSpacing(**p) for p in raw["pairs"]},
    )


def save_archive(archive: SpacingArchive, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(archive.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
