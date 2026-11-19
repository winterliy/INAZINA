import { Logo } from "../../shared/Logo";
/** inazina and the state of each server on the left; a link to each API port on the right. */
export function Footer({ connected }: { connected: boolean }) {
  const dot = connected ? "sv ok" : "sv";
  return <footer className="bot">
    <div className="bot-in">
      <span className="bot-status"><Logo className="bot-logo" />
        <span className={dot}><i />웹 API <em>:4312</em></span>
        <span className={dot}><i />Python 서버 <em>:4313</em></span>
        <span className={dot}><i />SQL 서버</span></span>
      <span className="bot-links">
        <a className="link-out" href={`${location.protocol}//${location.hostname}:4312/`} target="_blank" rel="noopener noreferrer" aria-label="API 서버 4312 열기"><em>4312</em></a>
        <a className="link-out" href={`${location.protocol}//${location.hostname}:4313/`} target="_blank" rel="noopener noreferrer" aria-label="데이터 서비스 4313 열기"><em>4313</em></a>
      </span>
    </div>
  </footer>;
}
