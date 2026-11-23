export * from "../../shared/tabKeys";

/** Only http(s) URLs may become links; anything else (javascript:, data:, relative) is plain text. */
export const safeUrl = (value: string | null | undefined): string | null => /^https?:\/\//i.test((value ?? "").trim()) ? (value as string).trim() : null;
