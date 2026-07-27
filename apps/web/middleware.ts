import { NextResponse, type NextRequest } from 'next/server';

const PUBLIC_PATHS = new Set([
  '/login',
  '/register',
  '/forgot-password',
  '/reset-password',
  '/verify-email',
]);

export function middleware(request: NextRequest) {
  const pathname = request.nextUrl.pathname;
  const hasSession =
    request.cookies.has('paperforge_session') ||
    request.cookies.has('__Host-paperforge_session');
  if (!hasSession && !PUBLIC_PATHS.has(pathname)) {
    // Reverse proxies can present their loopback upstream as request.url. Production
    // deployments provide the canonical public origin so redirects never leak localhost.
    const redirectBase = process.env.PAPERFORGE_PUBLIC_APP_URL?.trim() || request.url;
    const login = new URL('/login', redirectBase);
    login.searchParams.set('next', `${pathname}${request.nextUrl.search}`);
    return NextResponse.redirect(login);
  }
  return NextResponse.next();
}

export const config = {
  matcher: ['/((?!api|_next/static|_next/image|favicon.ico).*)'],
};
