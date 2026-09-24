import type { ReactNode } from 'react';
import './globals.css';
import { AppShell } from '@/components/auth/app-shell';
import { AuthProvider } from '@/components/auth/auth-provider';
import { ThemeProvider } from '@/components/theme-provider';
import { THEME_INIT_SCRIPT } from '@/lib/theme';
import { ToastProvider } from '@/components/ui/toast';
import { WebVitalsReporter } from '@/components/performance/web-vitals';

export const metadata = {
  title: 'PaperForge',
  description: '成稿优先、引用真实、流程宽松的科研论文生成系统',
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh" suppressHydrationWarning>
      <head>
        {/* hydration 前打好 dark class，避免深色偏好下先闪一屏白。 */}
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
      </head>
      <body>
        <WebVitalsReporter />
        <ThemeProvider>
          <ToastProvider>
            <AuthProvider>
              <AppShell>{children}</AppShell>
            </AuthProvider>
          </ToastProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
