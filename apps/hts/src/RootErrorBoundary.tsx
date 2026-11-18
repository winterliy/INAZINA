import { Component, type ReactNode } from "react";

/** If a screen fails to draw, show a way out instead of a blank page. */
export class RootErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  componentDidCatch(error: unknown) { console.error("screen failed to draw", error); }
  render() {
    if (!this.state.failed) return this.props.children;
    return <div className="stage"><main className="page-loading" role="alert" style={{ display: "grid", gap: 12, justifyItems: "center", textAlign: "center" }}>
      <p>화면을 그리는 중에 문제가 생겼습니다.</p>
      <p style={{ color: "var(--ink2)", fontSize: 13 }}>잠시 후 다시 시도해 주세요. 계속되면 이 화면과 시각을 알려 주세요.</p>
      <button type="button" className="btn primary" onClick={() => location.reload()}>새로고침</button>
    </main></div>;
  }
}
