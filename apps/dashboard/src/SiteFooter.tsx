import { Logo } from "../../shared/Logo";
import { appUrl } from "../../shared/api";
import { footerBar, footerColumns } from "./footerLinks";

/** The foot of the console: the wordmark with a line about the service, link columns and the policy row (like a company's site footer). */
export function SiteFooter() {
  const columns = footerColumns(appUrl("hts"), appUrl("hts", "/board"));
  const external = (href: string) => href.startsWith("http") ? { target: "_blank", rel: "noopener noreferrer" } : {};
  return <footer className="site-foot" aria-label="사이트 안내">
    <div className="site-foot-main">
      <div className="site-foot-brand">
        <p className="site-foot-tag">함께 만드는<br />가상 경제</p>
        <p className="site-foot-mark"><span aria-hidden="true">©</span><Logo className="site-foot-logo" title="INAZINA" /></p>
      </div>
      <nav className="site-foot-cols" aria-label="바로가기">
        {columns.map((column) => <div key={column.title}>
          <h2>{column.title}</h2>
          <ul>{column.links.map((link) => <li key={link.label}><a href={link.href} {...external(link.href)}>{link.label}</a></li>)}</ul>
        </div>)}
      </nav>
    </div>
    <div className="site-foot-bar">
      <small>© INAZINA. 가상 경제 서비스이며 실제 금융상품이 아닙니다.</small>
      <nav aria-label="약관과 정책">{footerBar().map((link) => <a key={link.label} href={link.href} {...external(link.href)}>{link.label}</a>)}</nav>
    </div>
  </footer>;
}
