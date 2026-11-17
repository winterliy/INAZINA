/** The small circular arrow beside a figure that is converted at the live exchange rate.
 *  It spins while the new rate is being fetched; the figure follows from the caller's reload. */
export function RefreshRateButton({ onRefresh, busy = false, label = "실시간 환율로 새로고침" }: {
  onRefresh: () => void; busy?: boolean; label?: string;
}) {
  return <button type="button" className={busy ? "rate-refresh spinning" : "rate-refresh"} aria-label={label} title={label}
    disabled={busy} onClick={onRefresh}>
    <svg aria-hidden="true" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
      <path d="M20 12a8 8 0 1 1-2.6-5.9" /><path d="M20 4v5h-5" />
    </svg>
  </button>;
}
