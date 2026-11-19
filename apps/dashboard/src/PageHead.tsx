import { groupOf } from "./nav";

/** Group and page as a small path, the page title, the group's pages as text links, and the search box where the page has one. */
export function PageHead({ page, title, labels, badges, searchable, query, placeholder, onQuery, onPage }: {
  page: string; title: string; labels: Map<string, string>; badges: Record<string, number>; searchable: boolean;
  query: string; placeholder: string; onQuery: (value: string) => void; onPage: (page: string) => void;
}) {
  const group = groupOf(page);
  const siblings = group?.pages.filter((id) => labels.has(id)) ?? [];
  const external = group?.external;
  return <section className="page-head">
    <div className="page-wash" aria-hidden="true" />
    <p className="eyebrow">{group ? `${group.label} / ${title}` : page === "home" ? "inazina" : "내 계정"}</p>
    <h1 className="title">{title}</h1>
    {(siblings.length > 0 || searchable) && <div className="page-subs">
      <nav className="subs" aria-label="하위 메뉴">
        {siblings.map((id) => <a key={id} href={`#${id}`} aria-current={id === page ? "page" : undefined}
          onClick={(event) => { event.preventDefault(); onPage(id); }}>{labels.get(id)}{(badges[id] ?? 0) > 0 && <span className="nav-badge" aria-label={`대기 ${badges[id]}건`}>{badges[id]}</span>}</a>)}
        {external && <a className="ext" href={external.hash} target="_blank" rel="noopener noreferrer">{external.label}</a>}
      </nav>
      {searchable && <label className="search">
        <span className="vh">검색</span>
        <input type="search" placeholder={placeholder} value={query} onChange={(event) => onQuery(event.target.value)} />
      </label>}
    </div>}
  </section>;
}
