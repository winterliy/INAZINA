// Small signed, expiring values for cookies (HMAC-SHA256 over the JSON body). Anything tampered with or expired reads as nothing.
import { createHmac, timingSafeEqual } from "node:crypto";

export type Signer = {
  sign: (value: Record<string, unknown>, ttlSeconds: number) => string;
  unsign: (raw: string | undefined) => Record<string, any> | undefined;
};

export function createSigner(secret: string): Signer {
  const mac = (body: string) => createHmac("sha256", secret).update(body).digest();
  return {
    sign(value, ttlSeconds) {
      const body = Buffer.from(JSON.stringify({ ...value, exp: Math.floor(Date.now() / 1000) + ttlSeconds })).toString("base64url");
      return `${body}.${mac(body).toString("base64url")}`;
    },
    unsign(raw) {
      const [body, signature] = (raw ?? "").split(".");
      if (!body || !signature) return undefined;
      const expected = mac(body);
      const actual = Buffer.from(signature, "base64url");
      if (actual.length !== expected.length || !timingSafeEqual(actual, expected)) return undefined;
      try {
        const value = JSON.parse(Buffer.from(body, "base64url").toString("utf8"));
        return value.exp > Math.floor(Date.now() / 1000) ? value : undefined;
      } catch { return undefined; }
    },
  };
}

export function readCookie(request: { headers: { cookie?: string } }, name: string): string | undefined {
  const entry = (request.headers.cookie ?? "").split(";").map((part) => part.trim()).find((part) => part.startsWith(`${name}=`));
  if (!entry) return undefined;
  try { return decodeURIComponent(entry.slice(name.length + 1)); } catch { return undefined; }
}
