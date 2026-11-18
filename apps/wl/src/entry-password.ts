// SHA-256 of the built-in entry password, so the password itself is not in the source. It only applies until
// the password is changed in the settings tab; the changed one is kept, salted, in database/wl-security.json
// (ignored by git) and judged by the dev server (security-store.ts).
export const PASSWORD_SHA256 = "04b2ad6979a436129b5f30cd00a3804bd87fc8bef73b0bb38640bf49a8479c64";
