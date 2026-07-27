import { PromptCanvas } from '@/components/home/prompt-canvas';

/**
 * 首页。
 *
 * 此前是 `redirect('/projects')`——落在一个带搜索框与两个筛选下拉的 SaaS 卡片网格上。
 * 那个页面回答「我有哪些项目」，但用户打开 PaperForge 想做的第一件事是**开始一篇论文**
 * （docs/ui-design.md 原则 02）。`/projects` 保留为完整列表页，只是不再是入口。
 */
export default function Home() {
  return <PromptCanvas />;
}
