import { Component, type ReactNode } from "react";

/** Keeps one broken panel (e.g. the chart) from blanking the whole 나루; "다시 시도" re-renders it. */
export class ErrorBoundary extends Component<{ label: string; resetKey?: string; children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) { return { error }; }
  componentDidCatch(error: Error) { console.error(`${this.props.label} 표시 중 오류`, error); }
  componentDidUpdate(previous: { resetKey?: string }) { if (this.state.error && previous.resetKey !== this.props.resetKey) this.setState({ error: null }); }
  render() {
    if (!this.state.error) return this.props.children;
    return <section className="panel-error" role="alert"><b>{this.props.label}을(를) 표시하지 못했습니다.</b><small>{this.state.error.message}</small><button type="button" onClick={() => this.setState({ error: null })}>다시 시도</button></section>;
  }
}
