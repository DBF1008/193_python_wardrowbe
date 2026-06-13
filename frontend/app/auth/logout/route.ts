import { NextResponse } from 'next/server';

export async function GET(request: Request) {
  const appUrl = process.env.NEXTAUTH_URL || 'http://localhost:3000';
  const endSessionUrl = process.env.OIDC_END_SESSION_URL;
  const tinyAuthUrl = process.env.TINYAUTH_URL;

  let logoutUrl: string;

  if (endSessionUrl) {
    logoutUrl = `${endSessionUrl}?post_logout_redirect_uri=${encodeURIComponent(appUrl + '/login')}`;
  } else if (tinyAuthUrl) {
    logoutUrl = `${tinyAuthUrl}/logout?redirect_uri=${encodeURIComponent(appUrl)}`;
  } else {
    logoutUrl = `${appUrl}/login`;
  }

  const signOutUrl = `${appUrl}/api/auth/signout?callbackUrl=${encodeURIComponent(logoutUrl)}`;

  return NextResponse.redirect(signOutUrl);
}
