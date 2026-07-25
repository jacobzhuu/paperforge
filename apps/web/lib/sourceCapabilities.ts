// 移植自 DeepSearch apps/web/src/components/literatureReview/sourceCapabilities.ts
// 各学术源（OpenAlex/Crossref/Semantic Scholar/arXiv/EuropePMC）能力与筛选项元数据。

export type SourceCapabilityId =
  | 'scholarly_indexes'
  | 'citation_doi'
  | 'cs_preprints'
  | 'oa_fulltext'
  | 'biomedical';

export interface SourceCapability {
  id: SourceCapabilityId;
  label: string;
  description: string;
  providers: string[];
}

export const SOURCE_CAPABILITIES: SourceCapability[] = [
  {
    id: 'scholarly_indexes',
    label: '综合学术索引',
    description: '跨学科论文元数据与开放学术图谱',
    providers: ['openalex', 'semantic_scholar'],
  },
  {
    id: 'citation_doi',
    label: '引文与 DOI 数据',
    description: 'DOI 注册与引文关系',
    providers: ['crossref'],
  },
  {
    id: 'cs_preprints',
    label: '计算机科学预印本',
    description: 'arXiv 预印本与快速更新文献',
    providers: ['arxiv'],
  },
  {
    id: 'oa_fulltext',
    label: '开放获取全文',
    description: '优先开放获取全文与仓储回退',
    providers: ['openalex', 'europe_pmc'],
  },
  {
    id: 'biomedical',
    label: '生物医学文献',
    description: 'Europe PMC / 生命科学文献增强',
    providers: ['europe_pmc'],
  },
];

export const ALL_PROVIDER_OPTIONS = [
  { id: 'openalex', label: 'OpenAlex' },
  { id: 'crossref', label: 'Crossref' },
  { id: 'semantic_scholar', label: 'Semantic Scholar' },
  { id: 'arxiv', label: 'arXiv' },
  { id: 'europe_pmc', label: 'Europe PMC' },
] as const;

export const providersFromCapabilities = (
  capabilityIds: SourceCapabilityId[],
  currentProviders: string[] = [],
): string[] => {
  const selected = new Set<string>();
  for (const capability of SOURCE_CAPABILITIES) {
    if (!capabilityIds.includes(capability.id)) continue;
    for (const provider of capability.providers) selected.add(provider);
  }
  // Preserve any advanced-only providers the operator already selected.
  for (const provider of currentProviders) {
    const covered = SOURCE_CAPABILITIES.some((capability) => capability.providers.includes(provider));
    if (!covered) selected.add(provider);
  }
  return Array.from(selected);
};

export const capabilitiesFromProviders = (providers: string[]): SourceCapabilityId[] => {
  const set = new Set(providers.map((item) => item.toLowerCase()));
  return SOURCE_CAPABILITIES
    .filter((capability) => capability.providers.some((provider) => set.has(provider)))
    .map((capability) => capability.id);
};

export const summarizeQueryStrategy = (synthesisPlan: Record<string, unknown>): string => {
  const queries = synthesisPlan.provider_native_queries;
  if (!Array.isArray(queries) || queries.length === 0) {
    return '查询策略将在任务启动后写入检索账本';
  }
  const groups = new Set<string>();
  for (const item of queries) {
    if (item && typeof item === 'object') {
      const group = (item as { query_group?: unknown }).query_group;
      if (typeof group === 'string' && group.trim()) groups.add(group.trim());
    }
  }
  const groupLabel = groups.size > 0 ? `${groups.size} 个查询组` : '未分组';
  return `${queries.length} 条检索查询 · ${groupLabel}`;
};
