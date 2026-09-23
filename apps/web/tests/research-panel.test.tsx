import * as React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
const api = vi.hoisted(()=>({API_BASE:'',listAssets:vi.fn(),listSections:vi.fn()}));
vi.mock('@/lib/api',()=>api);
import { ResearchPanel } from '@/components/assets/research-panel';
const fetcher=vi.fn();
beforeEach(()=>{
  vi.clearAllMocks();vi.stubGlobal('fetch',fetcher);
  api.listAssets.mockResolvedValue({data:[{id:'a',title:'data.csv'}]});
  api.listSections.mockResolvedValue({data:[]});
});
afterEach(()=>vi.unstubAllGlobals());
it('requires explicit acceptance and displays the exact proposal boundary',async()=>{
  fetcher.mockResolvedValue({ok:true,json:async()=>[{id:'r',job_id:'j',status:'proposed',input:{goal:'summarize',spec:{}},proposal:{summary:'只追加结果，不改原稿'}}]});
  render(<ResearchPanel projectId="p"/>);
  expect(await screen.findByText(/只追加结果，不改原稿/)).toBeInTheDocument();
  expect(fetcher.mock.calls.every(([,o])=>o.method!=='POST')).toBe(true);
  await userEvent.click(screen.getByRole('button',{name:'确认并提交新版本'}));
  await waitFor(()=>expect(fetcher).toHaveBeenCalledWith('/api/v1/projects/p/artifact-proposals/r/decision',expect.objectContaining({method:'POST',body:'{"choice":"accept"}'})));
});
it('keeps conflict errors visible without claiming a successful commit',async()=>{
  fetcher.mockImplementation(async(_url,options)=>options.method==='POST'?{ok:false,json:async()=>({detail:'文稿已变化，请重新分析'})}:{ok:true,json:async()=>[{id:'r',job_id:'j',status:'proposed',input:{goal:'summarize',spec:{}},proposal:{summary:'追加统计'}}]});
  render(<ResearchPanel projectId="p"/>);
  await userEvent.click(await screen.findByRole('button',{name:'确认并提交新版本'}));
  expect(await screen.findByRole('alert')).toHaveTextContent('文稿已变化');
  expect(screen.queryByText('已提交新版本')).not.toBeInTheDocument();
});
