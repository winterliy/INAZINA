/** The soft wall and colour blobs behind every HTS screen. */
export function Backdrop() {
  return <>
    <div className="wall" aria-hidden="true" />
    <div className="blob b1" aria-hidden="true" />
    <div className="blob b2" aria-hidden="true" />
  </>;
}
