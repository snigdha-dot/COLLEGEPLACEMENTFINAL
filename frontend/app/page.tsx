'use client';

import { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { toast } from 'sonner';

import CollegeTable from '../components/CollegeTable';
import FilterBar from '../components/FilterBar';
import LoadingSpinner from '../components/LoadingSpinner';
import {
  downloadExport,
  listMarketingColleges,
  type Filters,
  type MarketingCollege,
} from '../lib/api';

/**
 * Marketing view.
 *
 * Never surfaces pipeline status, confidence scores, or failure states — the
 * API does not send them for this view, and the messaging here stays generic
 * and positive. If a scrape run had failures, that belongs in the admin view.
 */
export default function MarketingPage() {
  const [rows, setRows] = useState<MarketingCollege[]>([]);
  const [filters, setFilters] = useState<Filters>({});
  const [loading, setLoading] = useState(true);
  const [exporting, setExporting] = useState(false);
  const [unreachable, setUnreachable] = useState(false);

  const load = useCallback(async (active: Filters) => {
    setLoading(true);
    try {
      const data = await listMarketingColleges(active);
      setRows(data.results);
      setUnreachable(false);
    } catch {
      // Deliberately vague: marketing does not need to reason about the
      // pipeline, only about whether the list is usable right now.
      setUnreachable(true);
      setRows([]);
      toast.error('Could not load the contact list. Please try again shortly.');
    } finally {
      setLoading(false);
    }
  }, []);

  // Debounced so typing in the search box does not fire a request per keystroke.
  useEffect(() => {
    const timer = setTimeout(() => void load(filters), 250);
    return () => clearTimeout(timer);
  }, [filters, load]);

  async function handleExport() {
    setExporting(true);
    try {
      const count = await downloadExport(filters, 'xlsx');
      toast.success(
        count > 0 ? `Export ready — ${count} colleges.` : 'Export ready.',
      );
    } catch {
      toast.error('Export could not be prepared. Please try again.');
    } finally {
      setExporting(false);
    }
  }

  return (
    <main className="page">
      <header className="page-head">
        <div>
          <h1>College Contacts</h1>
          <p className="sub">
            Search, filter, and export placement contacts for outreach.
          </p>
        </div>
        <div className="head-actions">
          <button
            type="button"
            className="btn btn-primary"
            onClick={handleExport}
            disabled={exporting || loading}
          >
            {exporting ? <LoadingSpinner label="Preparing…" inline /> : 'Export to Excel'}
          </button>
          <Link href="/admin" className="link small">
            Admin
          </Link>
        </div>
      </header>

      <FilterBar
        view="marketing"
        filters={filters}
        onChange={setFilters}
        disabled={loading}
      />

      {loading ? (
        <div className="center-pad">
          <LoadingSpinner label="Loading colleges…" />
        </div>
      ) : unreachable ? (
        <div className="empty">
          <p>The contact list is unavailable right now.</p>
          <button type="button" className="btn" onClick={() => void load(filters)}>
            Try again
          </button>
        </div>
      ) : (
        <>
          <p className="count">
            {rows.length} {rows.length === 1 ? 'college' : 'colleges'}
          </p>
          <CollegeTable view="marketing" rows={rows} />
        </>
      )}
    </main>
  );
}
