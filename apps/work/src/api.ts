export class ApiError extends Error { constructor(public status: number, message: string) { super(message); } }

export async function api<T = any>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(`/api/work${path}`, {
    method, credentials: "same-origin",
    headers: { "content-type": "application/json", "x-work-request": "1" },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (response.status === 401) { location.href = `/api/auth/oidc/login?returnTo=${encodeURIComponent(location.pathname)}`; return new Promise(() => {}); }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(response.status, data.error ?? "error");
  return data as T;
}
export const get = <T = any>(path: string) => api<T>("GET", path);
export const post = <T = any>(path: string, body: unknown = {}) => api<T>("POST", path, body);
export const del = <T = any>(path: string) => api<T>("DELETE", path);

const messages: Record<string, string> = {
  forbidden: "권한이 없습니다.", not_found: "찾을 수 없습니다.", version_conflict: "다른 사람이 먼저 저장했습니다. 새로 고친 뒤 다시 시도해 주세요.",
  resource_busy: "그 시간에는 이미 예약되어 있습니다.", not_pending: "이미 처리된 건입니다.", name_invalid: "사용할 수 없는 파일 이름입니다.",
  too_large: "파일이 너무 큽니다(최대 15MB).", self_approval: "본인을 결재자로 지정할 수 없습니다.", login_unavailable: "로그인 서버에 연결할 수 없습니다.",
};
export const errorText = (error: unknown) => (error instanceof ApiError ? messages[error.message] ?? `요청을 처리하지 못했습니다 (${error.message})` : "네트워크 오류입니다.");
