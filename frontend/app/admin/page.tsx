'use client';

import { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { toast } from 'sonner';

import CollegeTable from '../../components/CollegeTable';
import FilterBar from '../../components/FilterBar';
import LoadingSpinner from '../../components/LoadingSpinner';
import {
  buildSeedList,
  getStats,
  listAdminColleges,
  startScrapeRun,
  type AdminCollege,
  type Filters,
} from '../../lib/api';

/**
 * Admin / QA view.
 *
 * This one is for pipeline monitoring, so it is deliberately more technical
 * and more verbose than the marketing view: real statuses, confidence scores,
 * failure counts, and job controls all belong here and nowhere else.
 */
export default function AdminPage() {
  const [rows, setRows] = useState<AdminCollege[]>([]);
  const [filters, setFilters] = useState<Filters>({ state: 'Karnataka' });
  const [stats, setStats] = useState<Record<string, number>>({});
  const [loading, setLoading] = useState(true);
  const [seeding, setSeeding] = useState(false);
  const [scraping, setScraping] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (active: Filters) => {
    setLoading(true);
    try {
      const [data, statsData] = await Promise.all([
        listAdminColleges(active),
        getStats(active.state),
      ]);
      setRows(data.results);
      setStats(statsData.by_status);
      setError(null);
    } catch (caught) {
      // Unlike marketing, show the real error — this view exists to diagnose.
      const message = caught instanceof Error ? caught.message : 'Unknown error';
      setError(message);
      setRows([]);
      toast.error(`Load failed: ${message}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const timer = setTimeout(() => void load(filters), 250);
    return () => clearTimeout(timer);
  }, [filters, load]);

  async function handleSeed() {
    if (!filters.state) {
      toast.error('Pick a state before building a seed list.');
      return;
    }
    setSeeding(true);
    try {
      const result = await buildSeedList({
        state: filters.state,
        stream: filters.stream ?? 'Engineering',
      });
      toast.success(`Seed list ready: ${result.count} colleges.`);
    } catch (caught) {
      toast.error(
        `Seed build failed: ${caught instanceof Error ? caught.message : 'unknown'}`,
      );
    } finally {
      setSeeding(false);
    }
  }

  async function handleScrape() {
    if (!filters.state) {
      toast.error('Pick a state before starting a scrape run.');
      return;
    }
    setScraping(true);
    try {
      await startScrapeRun({
        state: filters.state,
        stream: filters.stream ?? 'Engineering',
        limit: 25,
      });
      toast.success('Scrape run started. Refresh to watch progress.');
    } catch (caught) {
      toast.error(
        `Could not start run: ${caught instanceof Error ? caught.message : 'unknown'}`,
      );
    } finally {
      setScraping(false);
    }
  }

  const needsFollowUp = stats['Needs Follow-up'] ?? 0;
  const failed = stats['Failed'] ?? 0;

  return (
    <main className="page">
      <header className="page-head">
        <div>
          <h1>Pipeline QA</h1>
          <p className="sub">
            Full internal records, including status and confidence. Not the
            marketing view.
          </p>
        </div>
        <div className="head-actions">
          <button
            type="button"
            className="btn"
            onClick={handleSeed}
            disabled={seeding || loading}
          >
            {seeding ? <LoadingSpinner label="Building…" inline /> : 'Build seed list'}
          </button>
          <button
            type="button"
            className="btn btn-primary"
            onClick={handleScrape}
            disabled={scraping || loading}
          >
            {scraping ? <LoadingSpinner label="Starting…" inline /> : 'Run scrape (25)'}
          </button>
          <Link href="/" className="link small">
            Marketing view
          </Link>
        </div>
      </header>

      <div className="stat-row">
        <span className="stat stat-ok">
          <strong>{stats['Verified'] ?? 0}</strong> verified
        </span>
        <span className="stat stat-warn">
          <strong>{needsFollowUp}</strong> need follow-up
        </span>
        <span className="stat stat-bad">
          <strong>{failed}</strong> failed
        </span>
      </div>

      {(needsFollowUp > 0 || failed > 0) && !loading && (
        <p className="notice">
          {needsFollowUp > 0 && `${needsFollowUp} colleges need follow-up`}
          {needsFollowUp > 0 && failed > 0 && ', '}
          {failed > 0 && `${failed} failed outright`}
          {' — see the status column. Rows missing an email or a phone never reach the marketing view or an export.'}
        </p>
      )}

      <FilterBar
        view="admin"
        filters={filters}
        onChange={setFilters}
        disabled={loading}
      />

      {loading ? (
        <div className="center-pad">
          <LoadingSpinner label="Loading records…" />
        </div>
      ) : error ? (
        <div className="empty">
          <p className="mono small">{error}</p>
          <button type="button" className="btn" onClick={() => void load(filters)}>
            Retry
          </button>
        </div>
      ) : (
        <>
          <p className="count">{rows.length} records</p>
          <CollegeTable view="admin" rows={rows} />
        </>
      )}
    </main>
  );
}
