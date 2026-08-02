import type { Metadata } from 'next';
import { Toaster } from 'sonner';

import './globals.css';

export const metadata: Metadata = {
  title: 'College Placement Contacts',
  description:
    'Search, filter, and export placement-cell contacts for Engineering and BCA colleges.',
};

// Deliberately no next/font/google: it fetches at build time, and an internal
// tool should build on a machine with no outbound network access.
export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>
        {children}
        <Toaster position="top-right" richColors closeButton />
      </body>
    </html>
  );
}
