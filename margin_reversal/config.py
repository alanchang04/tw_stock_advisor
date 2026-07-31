"""Configuration isolated from the swing strategy."""
import os

MARGIN_REVERSAL_CONFIG = {
    # Disabled until the post-wash confirmation variant passes research.
    "enabled": False,
    "capital": int(os.getenv("MARGIN_REVERSAL_CAPITAL", "300000")),
    "max_positions": 5,
    "position_fraction": 0.20,
    "market_drawdown_20d": -0.08,
    "market_daily_drop": -0.025,
    "margin_drop_5d": -0.15,
    "confirmation_min_days": 2,
    "confirmation_max_days": 10,
    "higher_low_lookback": 3,
    "require_close_above_ma5": False,
    "min_margin_balance": 100,
    "price_drawdown_20d": -0.15,
    "max_rsi": 38.0,
    "min_revenue_yoy": 0.0,
    "institutional_buy_days": 2,
    "min_avg_turnover": 20_000_000,
    "stop_loss": 0.07,
    "take_profit": 0.12,
    "max_hold_days": 10,
    "fee_rate": 0.001425 * 0.58,
    "tax_rate": 0.003,
    "slippage": 0.001,
}
