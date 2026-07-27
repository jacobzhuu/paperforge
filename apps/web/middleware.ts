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
    const login = new URL('/login', request.url);
    login.searchParams.set('next', `${pathname}${request.nextUrl.search}`);
    return NextResponse.redirect(login);
  }
  return NextResponse.next();
}

export const config = {
  matcher: ['/((?!api|_next/static|_next/image|favicon.ico).*)'],
};
