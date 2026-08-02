'use client';

/**
 * Spinner shown during every async action, per the UX requirements.
 * `label` is announced to screen readers so a wait is not silent.
 */
export default function LoadingSpinner({
  label = 'Loading',
  inline = false,
}: {
  label?: string;
  inline?: boolean;
}) {
  return (
    <span
      role="status"
      aria-live="polite"
      className={inline ? 'spinner-wrap spinner-inline' : 'spinner-wrap'}
    >
      <span className="spinner" aria-hidden="true" />
      <span className={inline ? 'visually-hidden' : 'spinner-label'}>{label}</span>
    </span>
  );
}
