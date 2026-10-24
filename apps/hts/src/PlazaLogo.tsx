/** 광장 mark: bold italic "nz" in a white-ringed navy tile, a gallery-board style wordmark of our own. */
export function PlazaLogo({ size = 40 }: { size?: number }) {
  return <svg className="plaza-mark" width={size} height={size} viewBox="0 0 48 48" role="img" aria-label="NZ">
    <rect x="1" y="1" width="46" height="46" rx="10" fill="var(--plaza-key)" />
    <rect x="4" y="4" width="40" height="40" rx="7.5" fill="none" stroke="#fff" strokeOpacity=".35" strokeWidth="1.5" />
    <text x="24.5" y="32.5" textAnchor="middle" fill="#fff" fontFamily="'Arial Black', 'Helvetica Neue', Arial, sans-serif" fontSize="23" fontWeight="900" fontStyle="italic" letterSpacing="-1.5">nz</text>
    <rect x="11" y="36.5" width="26" height="2.6" rx="1.3" fill="#ffd400" />
  </svg>;
}
