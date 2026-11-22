import { authPanelRequested } from "../../shared/sso";

/** Pages a visitor without an account may open: the market home and the heatmap. Everything else sends the visitor to the login. */
export const guestCanView = (pathname: string): boolean => pathname === "/" || /^\/heatmap\/?$/.test(pathname);

/** True when the address asks for the sign-in gate: register, reset, a sign-in error or an old Discord round trip. */
export const loginPanelRequested = (search: string): boolean => authPanelRequested(search);
