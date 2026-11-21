// Discord OAuth2 for proving who owns a Discord id (account register, password reset, find id). Only the `identify` scope.
export type DiscordClient = {
  configured: boolean;
  authorizeUrl: (state: string) => string;
  identify: (code: string) => Promise<{ id: string; nickname: string }>;
};

export function createDiscordClient(env: NodeJS.ProcessEnv, redirectUri: string, fetchImpl: typeof fetch = fetch): DiscordClient {
  const clientId = env.DISCORD_CLIENT_ID ?? "";
  const clientSecret = env.DISCORD_CLIENT_SECRET ?? "";
  return {
    configured: Boolean(clientId && clientSecret),
    authorizeUrl(state) {
      const url = new URL("https://discord.com/oauth2/authorize");
      url.search = new URLSearchParams({ response_type: "code", client_id: clientId, scope: "identify", redirect_uri: redirectUri, state }).toString();
      return url.href;
    },
    async identify(code) {
      const token = await fetchImpl("https://discord.com/api/v10/oauth2/token", {
        method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" }, signal: AbortSignal.timeout(10_000),
        body: new URLSearchParams({ client_id: clientId, client_secret: clientSecret, grant_type: "authorization_code", code, redirect_uri: redirectUri }),
      });
      if (!token.ok) throw new Error("discord_token_exchange_failed");
      const accessToken = ((await token.json()) as { access_token?: string }).access_token;
      if (!accessToken) throw new Error("discord_token_missing");
      const response = await fetchImpl("https://discord.com/api/v10/users/@me", { headers: { authorization: `Bearer ${accessToken}` }, signal: AbortSignal.timeout(10_000) });
      if (!response.ok) throw new Error("discord_identity_failed");
      const user = await response.json() as { id?: string; global_name?: string | null; username?: string };
      if (!user.id || !/^\d{1,25}$/.test(user.id)) throw new Error("discord_identity_invalid");
      return { id: user.id, nickname: user.global_name || user.username || user.id };
    },
  };
}
