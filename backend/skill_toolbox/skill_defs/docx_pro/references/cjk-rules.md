# CJK 版式规则

来源：MiniMax cjk_typography（MIT）+ Z.AI common-rules。`build_docx.py` 必须落实以下每条。

## 四字体槽位（多语言混排）

OpenXML 一个 run 有 4 个字体槽，Word 按字符集自动切换：

```
w:rFonts
  w:ascii="Times New Roman"   ← 基础拉丁 U+0000–U+007F
  w:hAnsi="Times New Roman"   ← 拉丁扩展/希腊/西里尔
  w:eastAsia="SimSun"         ← CJK 统一表意文字/假名/谚文（唯一能设置中文字体的地方）
  w:cs="Arial"                ← 复杂文种
```

**只设 `ascii` 不设 `eastAsia` = 中文全部回退默认字体。** 中英混排同一 run 自动按字符切换，无需分 run。

### 文档默认（docDefaults）

```xml
<w:rPrDefault>
  <w:rPr>
    <w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="SimSun" w:cs="Arial"/>
    <w:sz w:val="22"/>       <!-- 11pt -->
    <w:szCs w:val="22"/>
    <w:lang w:val="en-US" w:eastAsia="zh-CN"/>
  </w:rPr>
</w:rPrDefault>
```

`w:lang eastAsia` 帮 Word 消解中英共享标点。

## 字号对照（CJK 命名 → pt → w:sz 半磅值）

| 字号 | pt | w:sz | 用途 |
|---|---|---|---|
| 初号 | 42 | 84 | 展示标题 |
| 小初 | 36 | 72 | 大标题 |
| 一号 | 26 | 52 | 章标题 |
| 小一 | 24 | 48 | 大节标题 |
| 二号 | 22 | 44 | 公文标题 |
| 小二 | 18 | 36 | 西文 H1 等效 |
| 三号 | 16 | 32 | 公文正文/中文标题 |
| 小三 | 15 | 30 | 小标题 |
| 四号 | 14 | 28 | 中文小标题 |
| 小四 | 12 | 24 | 中文正文标准 |
| 五号 | 10.5 | 21 | 紧凑正文 |
| 小五 | 9 | 18 | 脚注 |

## 标点与禁则

- 中文用全角标点（。，、：；）与弯引号（"“”‘’"）。
- 禁则处理（kinsoku）：行首禁 `）」』】〉》。、，！？；：`；行尾禁 `（「『【〈《`。Word 开 `w:kinsoku` 自动处理。
- 中英之间自动加 ~¼em 空隙：`w:autoSpaceDE=true`、`w:autoSpaceDN=true`（**推荐常开**）。

## 段落

- 中文正文：两端对齐 + 首行缩进 **2 字** → `w:ind w:firstLineChars="200"`（**用 Chars 而非固定 DXA**，随字号缩放；`firstLine=480` 固定值仅用于无字体场景的兜底）。
- 标题段落：**无**首行缩进。
- 行距：1.3× = `line=312`；公文固定 `line=560 exact`；简历 1.15×；合同 1.5×。
- 对齐：中文默认两端对齐；英文正文左对齐无缩进。

## 中文无斜体

中文没有真正的斜体，Word 合成斜体会很丑。强调用**加粗**或着重号 `<w:em w:val="dot"/>`。

## 中英混排字号补偿

同 pt 下中文字符视觉更大；实践中正文同字号即可，不必刻意调小 0.5–1pt（过度优化）。

## 实现清单（python-docx oxml）

1. docDefaults：`docDefaults.xml` 用 oxml 写入 4 槽位 + lang。
2. 每个 style/run：`w:rFonts` 同时写 `ascii` + `hAnsi` + `eastAsia`（`cs` 用默认 Arial）。
3. 正文段落 pPr：`w:ind firstLineChars=200`（python-docx 无 API，直接 oxml）。
4. 段落 pPr：`w:autoSpaceDE/DN true`。
5. 行距：`paragraph_format.line_spacing = Pt(?)` 或 oxml `w:spacing line=312 lineRule=auto`。
