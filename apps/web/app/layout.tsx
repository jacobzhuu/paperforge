import type { ReactNode } from 'react';
import './globals.css';
import { Sidebar } from '@/components/layout/sidebar';

export const metadata = {
  title: 'PaperForge',
  description: '成稿优先、引用真实、流程宽松的科研论文生成系统',
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh" suppressHydrationWarning>
      <body>
        <div className="flex min-h-screen">
          <Sidebar />
          <main className="flex-1 overflow-x-hidden">
            <div className="mx-auto max-w-6xl px-8 py-8">{children}</div>
          </main>
        </div>
      </body>
    </html>
  );
}
