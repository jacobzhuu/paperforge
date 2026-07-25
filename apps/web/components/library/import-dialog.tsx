'use client';

import * as React from 'react';
import { ShieldCheck } from 'lucide-react';
import { Dialog } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';

export function ImportDialog({
  open,
  onClose,
  onImport,
}: {
  open: boolean;
  onClose: () => void;
  onImport: (kind: 'doi' | 'bibtex', payload: string) => void | Promise<void>;
}) {
  const [doi, setDoi] = React.useState('');
  const [bibtex, setBibtex] = React.useState('');
  const [tab, setTab] = React.useState('doi');

  const submit = () => {
    if (tab === 'doi' && doi.trim()) void onImport('doi', doi.trim());
    if (tab === 'bibtex' && bibtex.trim()) void onImport('bibtex', bibtex.trim());
    setDoi('');
    setBibtex('');
    onClose();
  };

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="导入文献"
      description="DOI / BibTeX 导入将触发 Crossref/OpenAlex 反查核验（R1）"
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            取消
          </Button>
          <Button onClick={submit}>核验并导入</Button>
        </>
      }
    >
      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="doi">DOI</TabsTrigger>
          <TabsTrigger value="bibtex">BibTeX</TabsTrigger>
        </TabsList>
        <TabsContent value="doi">
          <Textarea
            value={doi}
            onChange={(e) => setDoi(e.target.value)}
            placeholder={'每行一个 DOI，例如：\n10.1016/j.media.2023.102846'}
            className="min-h-[120px] font-mono text-xs"
          />
        </TabsContent>
        <TabsContent value="bibtex">
          <Textarea
            value={bibtex}
            onChange={(e) => setBibtex(e.target.value)}
            placeholder={'@article{key, title={...}, author={...}, year={2023}, doi={...}}'}
            className="min-h-[160px] font-mono text-xs"
          />
        </TabsContent>
      </Tabs>
      <div className="mt-3 flex items-start gap-2 rounded-md bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
        <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0" />
        <span>
          导入条目必须成功反查为真实 scholarly_work 才能进入文献库；核验失败将被丢弃并记录
          verification_failed（方案 §4.4.3 R1）。
        </span>
      </div>
    </Dialog>
  );
}
