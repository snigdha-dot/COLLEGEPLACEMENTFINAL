import type { NextConfig } from 'next';

const nextConfig: NextConfig = {
  // Next 16 refuses cross-origin requests to the dev server unless the host is
  // listed here. Without it, opening the app from another machine on the LAN
  // serves the page shell but blocks its internal asset and data requests, so
  // the table renders empty with no obvious error.
  //
  // Private ranges only — this is a dev-server allowance, not a public host.
  // Add the machine's own LAN IP if it changes (DHCP reassigns it).
  allowedDevOrigins: [
    '192.168.7.16',
    '192.168.*.*',
    '10.*.*.*',
    '172.16.*.*',
  ],
};

export default nextConfig;
