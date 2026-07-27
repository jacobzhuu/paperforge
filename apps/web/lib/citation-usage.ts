import type { PaperSection } from './types';

/**
 * cite-key → 引用它的章节标题（按正文顺序）。
 *
 * 这是文献工作台从「数据库视图」变成「Research Context」的那块数据
 * （docs/ui-design.md §3.6）：用户关心的不是"库里有什么"，而是
 * **这篇文献为什么出现在我的论文里**。
 *
 * 数据来自 `PaperSection.cite_keys`（后端 `paper_section.cite_keys_json`，
 * 引用审计用的也是它），不需要新端点。
 *
 * 也考虑过 `GET /citations/audit` 的 `rows`——它带 `context_snippet`，信息更丰富，
 * 但每行只给 `section_id`，而 `SectionResponse` 不返回 id，前端拼不出章节标题。
 * 真要用那份数据得先改后端；`cite_keys` 已经够回答"被引用于哪几章"。
 */
export type CiteKeyUsage = Map<string, string[]>;

export function buildCiteKeyUsage(sections: PaperSection[]): CiteKeyUsage {
  const usage: CiteKeyUsage = new Map();
  // 按 order_no 排一遍，保证"引言、相关工作"是正文顺序而不是接口返回顺序。
  const ordered = [...sections].sort((a, b) => a.order_no - b.order_no);
  for (const section of ordered) {
    for (const key of section.cite_keys ?? []) {
      if (!key) continue;
      const titles = usage.get(key);
      // 同一章里引用多次只算一次。
      if (titles) {
        if (!titles.includes(section.title)) titles.push(section.title);
      } else {
        usage.set(key, [section.title]);
      }
    }
  }
  return usage;
}

/**
 * 一条文献被引用于哪些章节。
 *
 * 用 `bibtex_key` 而不是 work id 去查：cite-key 才是正文里真正落下的标识
 * （R3 持久化），审计与 BibTeX 生成用的也是它。
 */
export function sectionsCiting(
  usage: CiteKeyUsage,
  bibtexKey: string | null | undefined,
): string[] {
  if (!bibtexKey) return [];
  return usage.get(bibtexKey) ?? [];
}
