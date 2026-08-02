/**
 * Fetch wrapper for the FastAPI backend.
 *
 * The backend URL comes from an env var, never a hardcoded string, and no
 * secret ever appears here — this file ships in the client bundle. The
 * Ollagraph key lives only in the backend's .env.
 */

const BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? 'http://localhost:8000';

export type View = 'marketing' | 'admin';

/** Exactly the columns marketing may see. No status, confidence, or timestamps. */
export interface MarketingCollege {
  college_name: string;
  state: string;
  stream: string;
  affiliation: string | null;
  website: string | null;
  email: string | null;
  phone: string | null;
  all_emails_found: string | null;
  all_phones_found: string | null;
  outreach_status: string;
}

/** The full internal record — admin/QA only. */
export interface AdminCollege extends Omit<MarketingCollege, 'email' | 'phone' | 'all_emails_found' | 'all_phones_found'> {
  id: number;
  district: string | null;
  placement_officer_name: string | null;
  placement_email: string | null;
  placement_phone: string | null;
  backup_emails_found: string | null;
  backup_phones_found: string | null;
  fallback_contact_email: string | null;
  fallback_contact_phone: string | null;
  confidence_score: number;
  source_urls: string | null;
  email_verified: number;
  last_scraped: string | null;
  status: string;
}

export interface ListResponse<T> {
  view: View;
  count: number;
  results: T[];
}

export interface Filters {
  state?: string;
  stream?: string;
  status?: string;
  outreach_status?: string;
  search?: string;
  /** '' (name A-Z), 'newest', or 'oldest' — translated below. */
  sort?: string;
}

/**
 * Turn the UI's sort choice into the backend's sort + direction pair.
 *
 * Sorts on created_at, not last_scraped: re-scraping a college updates
 * last_scraped, which would shuffle long-standing rows to the top as they are
 * refreshed. created_at is when the college actually entered the dataset.
 */
function sortParams(sort?: string): { sort?: string; direction?: string } {
  if (sort === 'newest') return { sort: 'created_at', direction: 'desc' };
  if (sort === 'oldest') return { sort: 'created_at', direction: 'asc' };
  return {};
}

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = 'ApiError';
  }
}

function toQuery(params: Record<string, string | number | undefined>): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') query.set(key, String(value));
  }
  const encoded = query.toString();
  return encoded ? `?${encoded}` : '';
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE_URL}${path}`, {
      ...init,
      headers: { 'Content-Type': 'application/json', ...init?.headers },
    });
  } catch {
    // A network failure here means the backend is not running, which is a
    // different problem from a 4xx and deserves a different message.
    throw new ApiError('Could not reach the server. Is the backend running?', 0);
  }

  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      if (typeof body?.detail === 'string') detail = body.detail;
    } catch {
      /* response had no JSON body; keep the generic message */
    }
    throw new ApiError(detail, response.status);
  }

  return response.json() as Promise<T>;
}

export function listMarketingColleges(
  filters: Filters = {},
): Promise<ListResponse<MarketingCollege>> {
  const { sort, ...rest } = filters;
  return request(
    `/api/colleges${toQuery({
      view: 'marketing',
      ...rest,
      ...sortParams(sort),
      status: undefined,
      limit: 1000,
    })}`,
  );
}

export function listAdminColleges(
  filters: Filters = {},
): Promise<ListResponse<AdminCollege>> {
  const { sort, ...rest } = filters;
  return request(
    `/api/colleges${toQuery({
      view: 'admin',
      ...rest,
      ...sortParams(sort),
      limit: 1000,
    })}`,
  );
}

export function getCollege(id: number): Promise<AdminCollege> {
  return request(`/api/colleges/${id}`);
}

export function updateCollege(
  id: number,
  changes: Partial<Record<string, string>>,
): Promise<AdminCollege> {
  return request(`/api/colleges/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(changes),
  });
}

export function getStats(state?: string): Promise<{
  state: string | null;
  by_status: Record<string, number>;
}> {
  return request(`/api/colleges/stats${toQuery({ state })}`);
}

export function rescrapeCollege(
  id: number,
): Promise<{ college_id: number; status: string; notes: string }> {
  return request(`/api/scrape/college/${id}`, { method: 'POST' });
}

export function startScrapeRun(body: {
  state: string;
  stream: string;
  limit?: number;
}): Promise<{ started: boolean }> {
  return request('/api/scrape/run', { method: 'POST', body: JSON.stringify(body) });
}

export function buildSeedList(body: {
  state: string;
  stream: string;
  force_refresh?: boolean;
}): Promise<{ count: number; meta: unknown; sample: string[] }> {
  return request('/api/seed/build', { method: 'POST', body: JSON.stringify(body) });
}

export function listRuns(): Promise<{ runs: Record<string, unknown>[] }> {
  return request('/api/scrape/runs');
}

/**
 * Trigger a file download.
 *
 * The export is always the marketing schema with the completeness filter
 * applied server-side, whichever view the user is on — so this takes no
 * `view` parameter by design.
 */
export async function downloadExport(
  filters: Filters = {},
  format: 'xlsx' | 'csv' = 'xlsx',
): Promise<number> {
  const url = `${BASE_URL}/api/export${toQuery({
    format,
    state: filters.state,
    stream: filters.stream,
    outreach_status: filters.outreach_status,
    search: filters.search,
  })}`;

  const response = await fetch(url);
  if (!response.ok) throw new ApiError(`Export failed (${response.status})`, response.status);

  const rowCount = Number(response.headers.get('X-Row-Count') ?? '0');
  const blob = await response.blob();

  const disposition = response.headers.get('Content-Disposition') ?? '';
  const match = disposition.match(/filename="?([^"]+)"?/);
  const filename = match?.[1] ?? `college_contacts.${format}`;

  const objectUrl = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = objectUrl;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(objectUrl);

  return rowCount;
}
