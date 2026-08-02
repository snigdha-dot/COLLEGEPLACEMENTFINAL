'use client';

import type { Filters, View } from '../lib/api';

/**
 * Marketing filters by state, stream, outreach status, and name.
 * Admin adds a pipeline-status filter — marketing never sees status at all.
 */

const STATES = [
  'Karnataka', 'Andhra Pradesh', 'Tamil Nadu', 'Telangana', 'Maharashtra',
  'Kerala', 'Gujarat', 'Uttar Pradesh', 'West Bengal', 'Delhi',
];

const STREAMS = ['Engineering', 'BCA'];
const OUTREACH = ['New', 'Contacted', 'Responded'];
const STATUSES = ['Verified', 'Needs Follow-up', 'Failed'];

export default function FilterBar({
  view,
  filters,
  onChange,
  disabled = false,
}: {
  view: View;
  filters: Filters;
  onChange: (next: Filters) => void;
  disabled?: boolean;
}) {
  const set = (key: keyof Filters, value: string) =>
    onChange({ ...filters, [key]: value || undefined });

  return (
    <div className="filters">
      <label className="field">
        <span>Search</span>
        <input
          type="search"
          placeholder="College name…"
          value={filters.search ?? ''}
          onChange={(event) => set('search', event.target.value)}
          disabled={disabled}
        />
      </label>

      <label className="field">
        <span>State</span>
        <select
          value={filters.state ?? ''}
          onChange={(event) => set('state', event.target.value)}
          disabled={disabled}
        >
          <option value="">All states</option>
          {STATES.map((state) => (
            <option key={state} value={state}>{state}</option>
          ))}
        </select>
      </label>

      <label className="field">
        <span>Stream</span>
        <select
          value={filters.stream ?? ''}
          onChange={(event) => set('stream', event.target.value)}
          disabled={disabled}
        >
          <option value="">All streams</option>
          {STREAMS.map((stream) => (
            <option key={stream} value={stream}>{stream}</option>
          ))}
        </select>
      </label>

      <label className="field">
        <span>Outreach</span>
        <select
          value={filters.outreach_status ?? ''}
          onChange={(event) => set('outreach_status', event.target.value)}
          disabled={disabled}
        >
          <option value="">Any</option>
          {OUTREACH.map((value) => (
            <option key={value} value={value}>{value}</option>
          ))}
        </select>
      </label>

      {view === 'admin' && (
        <label className="field">
          <span>Status</span>
          <select
            value={filters.status ?? ''}
            onChange={(event) => set('status', event.target.value)}
            disabled={disabled}
          >
            <option value="">Any</option>
            {STATUSES.map((value) => (
              <option key={value} value={value}>{value}</option>
            ))}
          </select>
        </label>
      )}

      <button
        type="button"
        className="btn btn-ghost"
        onClick={() => onChange({})}
        disabled={disabled}
      >
        Clear
      </button>
    </div>
  );
}
