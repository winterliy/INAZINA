import { useEffect, useState } from "react";

type ApiStatus = "checking" | "connected" | "unavailable";

export default function App() {
  const [apiStatus, setApiStatus] = useState<ApiStatus>("checking");

  useEffect(() => {
    const controller = new AbortController();
    fetch("/api/health", { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error("API request failed");
        return response.json();
      })
      .then(() => setApiStatus("connected"))
      .catch(() => {
        if (!controller.signal.aborted) setApiStatus("unavailable");
      });

    return () => controller.abort();
  }, []);

  return (
    <main className="shell">
      <header className="topbar">
        <a className="brand" href="/">INAZINA <span>운영</span></a>
        <span className={`status status-${apiStatus}`}>
          <i />
          {apiStatus === "connected" ? "API 연결됨" : apiStatus === "checking" ? "API 확인 중" : "API 대기 중"}
        </span>
      </header>
      <section className="content">
        <p className="eyebrow">LOCAL ADMIN</p>
        <h1>관리 대시보드</h1>
        <p className="description">프론트엔드와 웹 API의 기본 실행 환경입니다.</p>
        <div className="rule" />
        <p className="note">관리 기능은 다음 단계에서 연결합니다.</p>
      </section>
    </main>
  );
}
