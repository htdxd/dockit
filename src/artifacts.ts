import { escapeHtml } from "./ui";

export type ArtifactBuckets = Record<string, string[]>;

/** 纯扩展名分类（用于无 skill 标记的历史产物兜底）。md/txt 归属不明确，不放入任何功能页。 */
function classifyByExt(files: string[]): ArtifactBuckets {
  const buckets: ArtifactBuckets = { ppt: [], resume: [], docx: [] };
  for (const f of files) {
    const name = f.toLowerCase();
    if (name.endsWith(".pptx") || name.endsWith(".ppt")) buckets.ppt.push(f);
    else if (name.endsWith(".docx") || name.endsWith(".doc")) buckets.docx.push(f);
    else if (name.endsWith(".pdf")) buckets.docx.push(f);
  }
  return buckets;
}

/** 把扫描结果按 skill 归属填入各工具产物页。
 *  by_skill 来自 sidecar _list_artifacts：带标记的文件按产生它的 skill 归位
 * （resume 页从此也会被填充）；unmarked（升级前的历史产物）按扩展名兜底，
 * 不进入简历页。每个文件只归一个桶，杜绝串检/多检。 */
export function setArtifacts(bySkill: ArtifactBuckets): void {
  const buckets: ArtifactBuckets = { ppt: [], resume: [], docx: [] };
  buckets.ppt.push(...(bySkill.ppt ?? []));
  buckets.resume.push(...(bySkill.resume ?? []));
  buckets.docx.push(...(bySkill.docx ?? []));
  buckets.docx.push(...(bySkill.pdf ?? []));
  const fallback = classifyByExt(bySkill.unmarked ?? []);
  buckets.ppt.push(...fallback.ppt);
  buckets.docx.push(...fallback.docx);

  // 按修改时间降序（新产物在前）——list_artifacts 已按 mtime 排序
  for (const tool of Object.keys(buckets) as Array<keyof typeof buckets>) {
    const host = document.getElementById(`art-${tool}`);
    if (!host) continue;
    host.innerHTML = buckets[tool]
      .map((p, i) => {
        const ext = (p.split(".").at(-1) ?? "FILE").toUpperCase();
        const name = p.split(/[\\/]/).at(-1) ?? p;
        const badgeCls = ext === "PDF" ? "v-pdf" : "v-docx";
        const badge = ext === "PDF" ? "P" : "W";
        return `<div class="vrow ${i === 0 ? "cur" : ""}">
          <div class="v-badge ${badgeCls}">${badge}</div>
          <div><div class="v-title">${escapeHtml(name)}</div><div class="v-sub">${escapeHtml(p)} · ${p.includes(".pdf_docx_routing.") ? "历史 PDF 转 DOCX（功能已下线） · " : ""}可在默认专业软件中打开</div></div>
          <div class="v-open" data-artifact-path="${escapeHtml(p)}">打开</div>
        </div>`;
      })
      .join("");
  }
}
