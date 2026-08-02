'use client';

import Link from 'next/link';
import type { AdminCollege, MarketingCollege, View } from '../lib/api';

/**
 * Renders the column set matching `view`.
 *
 * Every cell renders scraped text as a React child, never via
 * dangerouslySetInnerHTML — a college's page content can contain HTML and must
 * never be executed (AGENTS.md frontend security rule).
 *
 * The marketing branch has no access to status, confidence_score, or
 * last_scraped: the API does not send them for view=marketing, so there is
 * nothing here to leak even by mistake.
 */

const MARKETING_COLUMNS = [
  'College',
  'State',
  'Stream',
  'Affiliation',
  'Website',
  'Contact Person',
  'Email',
  'Phone',
  'Other Emails',
  'Other Phones',
] as const;

const ADMIN_COLUMNS = [
  'College',
  'District',
  'Stream',
  'Website',
  'Placement Email',
  'Placement Phone',
  'Fallback Email',
  'Fallback Phone',
  'Confidence',
  'Verified',
  'Status',
  'Last Scraped',
  '',
] as const;

function Website({ url }: { url: string | null }) {
  if (!url) return <span className="muted">—</span>;
  return (
    <a href={url} target="_blank" rel="noopener noreferrer" className="link">
      {url.replace(/^https?:\/\//, '').replace(/\/$/, '')}
    </a>
  );
}

function Mail({ address }: { address: string | null }) {
  if (!address) return <span className="muted">—</span>;
  return (
    <a href={`mailto:${address}`} className="link">
      {address}
    </a>
  );
}

function List({ value }: { value: string | null }) {
  if (!value) return <span className="muted">—</span>;
  const items = value.split(',').map((item) => item.trim()).filter(Boolean);
  if (items.length === 0) return <span className="muted">—</span>;
  return (
    <span title={items.join('\n')} className="truncate">
      {items.length === 1 ? items[0] : `${items[0]} +${items.length - 1} more`}
    </span>
  );
}

function StatusPill({ status }: { status: string }) {
  const tone =
    status === 'Verified' ? 'ok' : status === 'Failed' ? 'bad' : 'warn';
  return <span className={`pill pill-${tone}`}>{status}</span>;
}

export default function CollegeTable({
  view,
  rows,
}: {
  view: View;
  rows: MarketingCollege[] | AdminCollege[];
}) {
  const columns = view === 'marketing' ? MARKETING_COLUMNS : ADMIN_COLUMNS;

  if (rows.length === 0) {
    return (
      <div className="empty">
        <p>No colleges match these filters.</p>
      </div>
    );
  }

  return (
    <div className="table-scroll">
      <table className="table">
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column} scope="col">
                {column}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {view === 'marketing'
            ? (rows as MarketingCollege[]).map((row, index) => (
                <tr key={`${row.college_name}-${index}`}>
                  <td className="strong">{row.college_name}</td>
                  <td>{row.state}</td>
                  <td>{row.stream}</td>
                  <td>{row.affiliation || <span className="muted">—</span>}</td>
                  <td><Website url={row.website} /></td>
                  <td>{row.contact_person || <span className="muted">—</span>}</td>
                  <td><Mail address={row.email} /></td>
                  <td>{row.phone || <span className="muted">—</span>}</td>
                  <td><List value={row.all_emails_found} /></td>
                  <td><List value={row.all_phones_found} /></td>
                </tr>
              ))
            : (rows as AdminCollege[]).map((row) => (
                <tr key={row.id}>
                  <td className="strong">
                    <Link href={`/colleges/${row.id}`} className="link">
                      {row.college_name}
                    </Link>
                  </td>
                  <td>{row.district || <span className="muted">—</span>}</td>
                  <td>{row.stream}</td>
                  <td><Website url={row.website} /></td>
                  <td><Mail address={row.placement_email} /></td>
                  <td>{row.placement_phone || <span className="muted">—</span>}</td>
                  <td><Mail address={row.fallback_contact_email} /></td>
                  <td>{row.fallback_contact_phone || <span className="muted">—</span>}</td>
                  <td>{row.confidence_score}</td>
                  <td>{row.email_verified ? 'yes' : 'no'}</td>
                  <td><StatusPill status={row.status} /></td>
                  <td className="muted small">
                    {row.last_scraped ? row.last_scraped.slice(0, 10) : '—'}
                  </td>
                  <td>
                    <Link href={`/colleges/${row.id}`} className="link small">
                      view
                    </Link>
                  </td>
                </tr>
              ))}
        </tbody>
      </table>
    </div>
  );
}
