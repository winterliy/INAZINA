export class ApiError extends Error { constructor(public status: number, message: string) { super(message); } }

export async function api<T = any>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(`/api/work${path}`, {
    method, credentials: "same-origin",
    headers: { "content-type": "application/json", "x-work-request": "1" },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
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
  invalid_credentials: "아이디 또는 비밀번호가 맞지 않습니다.", account_locked: "시도가 너무 많아 잠겼습니다. 15분 뒤에 다시 해 주세요.", too_many_attempts: "시도가 너무 많습니다. 잠시 뒤에 다시 해 주세요.",
  pending_approval: "가입 신청이 아직 승인되지 않았습니다.", id_taken: "이미 쓰고 있거나 신청 중인 아이디입니다.", id_invalid: "아이디는 영문 소문자·숫자·._- 로 3~32자여야 합니다.",
  password_weak: "비밀번호는 10자 이상이어야 하고 아이디를 포함하거나 같은 글자만 반복할 수 없습니다.", invite_invalid: "초대 코드가 올바르지 않거나 만료되었습니다.", try_later: "거절된 신청은 24시간 뒤에 다시 할 수 있습니다.",
  wrong_password: "현재 비밀번호가 맞지 않습니다.", password_same: "새 비밀번호가 현재와 같습니다.", last_admin: "마지막 관리자는 탈퇴할 수 없습니다. 다른 관리자를 먼저 지정하세요.",
  too_large: "파일이 너무 큽니다(최대 15MB).", self_approval: "본인을 결재자로 지정할 수 없습니다.", login_unavailable: "로그인 서버에 연결할 수 없습니다.",
};
export const errorText = (error: unknown) => (error instanceof ApiError ? messages[error.message] ?? `요청을 처리하지 못했습니다 (${error.message})` : "네트워크 오류입니다.");
