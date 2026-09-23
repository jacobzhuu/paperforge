'use client';

import * as React from 'react';
import { API_BASE, listAssets, listSections } from '@/lib/api';
import type { UserAsset, PaperSection } from '@/lib/types';
import { Button } from '@/components/ui/button';

type Spec = {sheet: string | null; columns: string[]; group_by: string | null; units: Record<string,string>; confirmed: boolean};
type Analysis = {id: string; job_id: string; status: string; input: {goal: string; spec: Spec}; result?: {headers?: string[]; sheets?: string[]; interrupt_id?: string; message?: string; error?: string; chart_key?: string; bundle_key?: string; number_check?: {checked_count:number; unsourced_count:number; unsourced:{value:string;context:string}[]}; records?: Record<string,unknown>[]}; proposal?: {summary: string}; document_id?: string};
const statuses: Record<string,string> = {pending:'等待执行', running:'正在分析', needs_input:'需要确认分析口径', proposed:'等待确认文稿修改', completed:'分析完成', accepted:'已提交新版本', rejected:'已拒绝修改', failed:'分析未完成', cancelled:'已停止分析', paused:'已暂停'};

export function ResearchPanel({projectId}: {projectId: string}) {
  const [assets,setAssets] = React.useState<UserAsset[]>([]);
  const [sections,setSections] = React.useState<PaperSection[]>([]);
  const [runs,setRuns] = React.useState<Analysis[]>([]);
  const [goal,setGoal] = React.useState('按实验分组汇总描述统计，并核对论文中的结果');
  const [assetId,setAssetId] = React.useState('');
  const [section,setSection] = React.useState('');
  const [engine,setEngine] = React.useState('pi');
  const [error,setError] = React.useState('');
  const [busy,setBusy] = React.useState(false);
  const base = `${API_BASE}/api/v1/projects/${projectId}`;
  const request = React.useCallback(async (path: string, body?: unknown) => {
    const response = await fetch(base+path,{credentials:'include',...(body ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)} : {})});
    const data = await response.json();
    if(!response.ok) throw new Error(typeof data.detail==='string' ? data.detail : '请求失败，请刷新后重试');
    return data;
  },[base]);
  const reload = React.useCallback(async () => {
    setRuns(await request('/research-runs'));
    const [a,s] = await Promise.all([listAssets(projectId),listSections(projectId)]);
    setAssets(a.data.filter(a=>/\.(csv|tsv|xlsx)$/i.test(a.title || ''))); setSections(s.data);
  },[request,projectId]);
  React.useEffect(()=>{let alive=true; const refresh=()=>{if(alive) void reload().catch(e=>{if(alive)setError(String(e.message));});}; refresh();const timer=setInterval(refresh,5000);return()=>{alive=false;clearInterval(timer);};},[reload]);
  const action = async (work:()=>Promise<unknown>) => {setBusy(true);setError('');try{await work();await reload();}catch(e){setError(e instanceof Error?e.message:String(e));}finally{setBusy(false);}};
  return <section className="space-y-4 rounded-xl border bg-card p-4" aria-label="实验数据分析">
    <h2 className="text-lg font-medium">实验数据分析</h2>
    <p className="text-sm text-muted-foreground">计算描述统计、生成图表并提出有来源的文稿补充。确认后才写入新版本，不自动做显著性检验。</p>
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    <label className="block text-sm">分析目标<textarea className="mt-1 w-full rounded border bg-background p-2" value={goal} onChange={e=>setGoal(e.target.value)} maxLength={2000}/></label>
    <div className="grid gap-3 md:grid-cols-3">
      <label className="text-sm">数据素材<select className="mt-1 w-full rounded border bg-background p-2" value={assetId} onChange={e=>setAssetId(e.target.value)}><option value="">选择已上传的表格</option>{assets.map(a=><option key={a.id} value={a.id}>{a.title}</option>)}</select></label>
      <label className="text-sm">修改目标<select className="mt-1 w-full rounded border bg-background p-2" value={section} onChange={e=>setSection(e.target.value)}><option value="">仅查看分析结果</option>{sections.map(s=><option key={s.section_key} value={s.section_key}>{s.title}</option>)}</select></label>
      <label className="text-sm">执行方式<select className="mt-1 w-full rounded border bg-background p-2" value={engine} onChange={e=>setEngine(e.target.value)}><option value="pi">Agent 分析（使用模型额度）</option><option value="deterministic">直接计算（不调用模型）</option></select></label>
    </div>
    <Button disabled={busy || !assetId || !goal.trim()} onClick={()=>void action(()=>request('/research-runs',{goal,asset_id:assetId,section_key:section||null,engine}))}>开始分析</Button>
    {runs.map(run=><article className="space-y-3 rounded border p-3" key={run.id}>
      <p className="font-medium">{run.input.goal} · {statuses[run.status] || run.status}</p>
      {run.result?.error && <p role="alert" className="text-sm text-destructive">{run.result.error}</p>}
      {run.status==='needs_input' && <Clarification run={run} busy={busy} onSubmit={spec=>void action(()=>request(`/research-runs/${run.job_id}/responses`,{interrupt_id:run.result?.interrupt_id,spec}))}/>}
      {run.result?.records && <div className="overflow-x-auto"><table className="w-full text-sm"><caption className="text-left">标准差采用样本标准差；缺失值单独报告。— 表示不可计算。</caption><thead><tr>{['分组','字段','单位','样本数','缺失','均值','中位数','最小','最大','标准差'].map(h=><th className="p-2 text-left" key={h}>{h}</th>)}</tr></thead><tbody>{run.result.records.map((r,i)=><tr key={i}>{['group','column','unit','n','missing','mean','median','min','max','std'].map(k=><td key={k} className="p-2">{r[k]==null?'—':typeof r[k]==='number'?Number(r[k]).toLocaleString('zh-CN',{maximumSignificantDigits:10}):String(r[k])}</td>)}</tr>)}</tbody></table></div>}
      {run.result?.bundle_key && <a className="block text-sm underline" href={`${base}/analysis-results/${run.id}/reproducibility`}>下载复现包（原始表格、计算脚本、参数和结果）</a>}
      {['paused','failed'].includes(run.status) && <Button variant="outline" disabled={busy} onClick={()=>void action(()=>request(`/research-runs/${run.job_id}/resume`,{}))}>继续分析（保留累计预算）</Button>}
      {['pending','running'].includes(run.status) && <Button variant="outline" disabled={busy} onClick={()=>void action(()=>request(`/jobs/${run.job_id}/cancel`,{}))}>停止分析</Button>}
      {run.result?.chart_key && <img className="max-w-full" src={`${base}/analysis-results/${run.id}/chart`} alt="按确认字段计算的描述统计均值图"/>}
      {run.result?.number_check && <div className="text-sm"><p>目标章节数值出处检查：{run.result.number_check.checked_count} 项，其中 {run.result.number_check.unsourced_count} 项未找到来源。数值匹配不代表语义或统计方法已通过核验。</p>{run.result.number_check.unsourced.map((f,i)=><p key={i}>待核对 {f.value}：{f.context}</p>)}</div>}
      {run.proposal && <p className="text-sm">修改预览：{run.proposal.summary}</p>}
      {run.status==='proposed' && <div className="flex flex-wrap gap-2"><Button disabled={busy} onClick={()=>void action(()=>request(`/artifact-proposals/${run.id}/decision`,{choice:'accept'}))}>确认并提交新版本</Button><Button disabled={busy} variant="outline" onClick={()=>void action(()=>request(`/artifact-proposals/${run.id}/decision`,{choice:'reject'}))}>拒绝修改</Button></div>}
      {run.document_id && <a className="text-sm underline" href={`/projects/${projectId}/write`}>查看新版本文稿</a>}
    </article>)}
  </section>;
}

function Clarification({run,busy,onSubmit}:{run:Analysis;busy:boolean;onSubmit:(s:Spec)=>void}) {
  const [spec,setSpec] = React.useState<Spec>({...run.input.spec,confirmed:true});
  const unit = '1';
  const headers=run.result?.headers || [];
  return <div className="space-y-2 text-sm"><p>{run.result?.message}</p>
    {!!run.result?.sheets?.length && <label className="block">工作表<select className="ml-2 rounded border bg-background p-1" value={spec.sheet||''} onChange={e=>setSpec({...spec,sheet:e.target.value,columns:[]})}><option value="">请选择</option>{run.result.sheets.map(s=><option key={s}>{s}</option>)}</select></label>}
    <fieldset><legend>数值字段</legend><div className="flex flex-wrap gap-3">{headers.map(h=><label key={h}><input type="checkbox" checked={spec.columns.includes(h)} onChange={e=>setSpec({...spec,columns:e.target.checked?[...spec.columns,h]:spec.columns.filter(c=>c!==h)})}/> {h}</label>)}</div></fieldset>
    <label className="block">分组字段<select className="ml-2 rounded border bg-background p-1" value={spec.group_by||''} onChange={e=>setSpec({...spec,group_by:e.target.value||null})}><option value="">不分组</option>{headers.map(h=><option key={h}>{h}</option>)}</select></label>
    {spec.columns.map(c=><label className="block" key={c}>{c} 的单位<input className="ml-2 rounded border bg-background p-1" value={spec.units[c]??unit} onChange={e=>setSpec({...spec,units:{...spec.units,[c]:e.target.value}})}/></label>)}
    <p className="text-muted-foreground">单位 1 表示无量纲。每行按独立观测汇总；不识别配对关系，不推断统计显著性。</p>
    <Button disabled={busy || (!spec.columns.length && !spec.sheet)} onClick={()=>onSubmit({...spec,units:Object.fromEntries(spec.columns.map(c=>[c,spec.units[c]??unit]))})}>确认口径并继续</Button>
  </div>;
}
