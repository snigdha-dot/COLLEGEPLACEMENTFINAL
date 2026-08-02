'use client';

import { use, useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { toast } from 'sonner';

import LoadingSpinner from '../../../components/LoadingSpinner';
import {
  getCollege,
  rescrapeCollege,
  updateCollege,
  type AdminCollege,
} from '../../../lib/api';

/** Fields the marketing team may correct by hand. */
const EDITABLE: { key: keyof AdminCollege; label: string }[] = [
  { key: 'college_name', label: 'College name' },
  { key: 'affiliation', label: 'Affiliation' },
  { key: 'website', label: 'Website' },
  { key: 'placement_officer_name', label: 'Contact person' },
  { key: 'placement_email', label: 'Placement email' },
  { key: 'placement_phone', label: 'Placement phone' },
  { key: 'fallback_contact_email', label: 'Fallback email' },
  { key: 'fallback_contact_phone', label: 'Fallback phone' },
];

/** Read-only, pipeline-owned. Shown for context; the API rejects edits. */
const READ_ONLY: { key: keyof AdminCollege; label: string }[] = [
  { key: 'state', label: 'State' },
  { key: 'district', label: 'District' },
  { key: 'stream', label: 'Stream' },
  { key: 'status', label: 'Status' },
  { key: 'confidence_score', label: 'Confidence' },
  { key: 'last_scraped', label: 'Last scraped' },
];

export default function CollegeDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  // Next 16: route params arrive as a promise and are unwrapped with use().
  const { id } = use(params);
  const collegeId = Number(id);

  const [college, setCollege] = useState<AdminCollege | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [rescraping, setRescraping] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await getCollege(collegeId);
      setCollege(data);
      setDraft(
        Object.fromEntries(
          EDITABLE.map(({ key }) => [key, (data[key] as string | null) ?? '']),
        ),
      );
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Unknown error');
    } finally {
      setLoading(false);
    }
  }, [collegeId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function handleSave() {
    setSaving(true);
    try {
      const updated = await updateCollege(collegeId, draft);
      setCollege(updated);
      toast.success('Changes saved.');
    } catch (caught) {
      toast.error(
        `Save failed: ${caught instanceof Error ? caught.message : 'unknown'}`,
      );
    } finally {
      setSaving(false);
    }
  }

  async function handleRescrape() {
    setRescraping(true);
    try {
      const result = await rescrapeCollege(collegeId);
      toast.success(`Re-scrape finished: ${result.status}`);
      await load();
    } catch (caught) {
      toast.error(
        `Re-scrape failed: ${caught instanceof Error ? caught.message : 'unknown'}`,
      );
    } finally {
      setRescraping(false);
    }
  }

  if (loading) {
    return (
      <main className="page center-pad">
        <LoadingSpinner label="Loading college…" />
      </main>
    );
  }

  if (error || !college) {
    return (
      <main className="page">
        <p className="mono small">{error ?? 'Not found'}</p>
        <Link href="/admin" className="link">
          Back to QA view
        </Link>
      </main>
    );
  }

  return (
    <main className="page">
      <header className="page-head">
        <div>
          <h1>{college.college_name}</h1>
          <p className="sub">
            {college.district ? `${college.district}, ` : ''}
            {college.state} · {college.stream}
          </p>
        </div>
        <div className="head-actions">
          <button
            type="button"
            className="btn"
            onClick={handleRescrape}
            disabled={rescraping || saving}
          >
            {rescraping ? <LoadingSpinner label="Scraping…" inline /> : 'Re-scrape'}
          </button>
          <Link href="/admin" className="link small">
            Back
          </Link>
        </div>
      </header>

      <section className="card">
        <h2>Editable details</h2>
        <p className="sub small">
          Corrections made here survive a re-scrape: the pipeline only fills a
          field it finds empty.
        </p>
        <div className="form-grid">
          {EDITABLE.map(({ key, label }) => (
            <label key={key} className="field">
              <span>{label}</span>
              <input
                type="text"
                value={draft[key] ?? ''}
                onChange={(event) =>
                  setDraft((current) => ({ ...current, [key]: event.target.value }))
                }
                disabled={saving}
              />
            </label>
          ))}
        </div>
        <button
          type="button"
          className="btn btn-primary"
          onClick={handleSave}
          disabled={saving}
        >
          {saving ? <LoadingSpinner label="Saving…" inline /> : 'Save changes'}
        </button>
      </section>

      <section className="card">
        <h2>Pipeline record</h2>
        <dl className="detail-grid">
          {READ_ONLY.map(({ key, label }) => (
            <div key={key}>
              <dt>{label}</dt>
              <dd>{String(college[key] ?? '—')}</dd>
            </div>
          ))}
          <div>
            <dt>Email verified</dt>
            <dd>{college.email_verified ? 'yes' : 'no'}</dd>
          </div>
        </dl>
      </section>

      <section className="card">
        <h2>Everything found</h2>
        <dl className="detail-grid">
          <div>
            <dt>Other emails</dt>
            <dd>{college.backup_emails_found || '—'}</dd>
          </div>
          <div>
            <dt>Other phones</dt>
            <dd>{college.backup_phones_found || '—'}</dd>
          </div>
        </dl>
        <h3 className="small">Pages crawled</h3>
        <ul className="url-list">
          {(college.source_urls ?? '')
            .split(',')
            .map((url) => url.trim())
            .filter(Boolean)
            .map((url) => (
              <li key={url}>
                <a href={url} target="_blank" rel="noopener noreferrer" className="link mono small">
                  {url}
                </a>
              </li>
            ))}
          {!college.source_urls && <li className="muted small">none recorded</li>}
        </ul>
      </section>
    </main>
  );
}
