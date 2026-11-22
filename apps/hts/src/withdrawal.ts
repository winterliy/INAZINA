/** The withdrawal request is for an ordinary member: not for an administrator, a country or company account, or a session an
 *  administrator opened as the member ($login). */
export const canRequestWithdrawal = (user: { role: string; delegatedBy?: string }): boolean => user.role === "user" && !user.delegatedBy;
