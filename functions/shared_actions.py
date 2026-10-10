"""Business actions used by both slash commands and administrator commands."""


def execute_shared_action(bot, user_id, action, *values):
    user_id = str(user_id)
    if action in {"buy", "sell"}:
        asset, quantity, price = values
        return bot.order_book.place_limit_order(
            user_id, asset, action, int(quantity), int(price)
        )
    if action == "transfer":
        recipient, amount, source_account = values
        return bot.user_mgr.transfer(
            user_id, recipient, amount, source_account or None
        )
    if action in {"fund_subscribe", "fund_redeem"}:
        fund, quantity = values
        return bot.investment_mgr.trade_fund(
            user_id, fund, int(quantity), action == "fund_redeem"
        )
    if action == "option_write":
        (parameters,) = values
        if isinstance(parameters, dict):
            return bot.option_mgr.create_and_write(user_id, **parameters)
        return bot.option_mgr.create_and_write(user_id, *parameters)
    if action == "currency_exchange":
        return bot.currency_mgr.exchange(user_id, *values)
    raise ValueError(f"지원하지 않는 공통 명령 처리입니다: {action}")
