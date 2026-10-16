"""Run user commands (buy, sell, transfer, accounts, loans...) as a company's capital account."""
from functions import user_actions
from managers.company_account_manager import CompanyAccounts

def company_account(bot, code):
    with bot.db.atomic() as conn:
        return CompanyAccounts.account_id(conn, code)


def run(bot, actor, code, command, values):
    """Execute an HTS/Discord user command for ``code``'s capital account and book its cash."""
    bot.admin_control.require(actor)
    if command not in user_actions.USER_ACTIONS or command in user_actions.JOB_ACTIONS:
        raise ValueError("법인 계좌로 실행할 수 없는 명령어입니다.")
    account = company_account(bot, code)
    success, message, data = user_actions.execute(bot, account, command, values or {})
    with bot.db.atomic() as conn:
        issuer = CompanyAccounts.issuer_of(conn, account)
        CompanyAccounts.reconcile(conn, issuer)
        bot.admin_control._audit(conn, actor, "company." + command,
                                 {"company": issuer, "values": {k: v for k, v in (values or {}).items() if k != "password"},
                                  "success": success, "message": str(message)[:500]})
    return success, message, data


def run_positional(bot, actor, code, command, arguments):
    """``$법인 [법인ID] [명령어] [인자 ...]``: arguments follow the command's field order."""
    if command not in user_actions.USER_ACTIONS:
        raise ValueError("명령어: " + ", ".join(sorted(set(user_actions.USER_ACTIONS) - user_actions.JOB_ACTIONS)))
    fields = user_actions.USER_ACTIONS[command][3]
    if len(arguments) > len(fields):
        raise ValueError(f"${'법인'} {code} {command} " + " ".join(f"[{label}]" for _, label, *_ in fields))
    values = {key: value for (key, *_), value in zip(fields, arguments)}
    return run(bot, actor, code, command, values)
