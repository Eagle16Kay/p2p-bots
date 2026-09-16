# Binance P2P bot

Auto-confirms Binance C2C buy orders and tops ads back up afterwards.

- Detects new orders awaiting payment and sends a Telegram alert with the
  seller's payment details.
- Marks the order as paid after a configurable delay.
- Resets the ad's remaining amount to its configured target after each order.

## Run

    systemctl status binance-p2p-bot

Configuration comes from `binance_env.sh` on the server, which is never
committed: `BINANCE_API_KEY`, `BINANCE_SECRET_KEY`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`, `BINANCE_AD_RESET_TARGETS`, `CONFIRM_DELAY_SECONDS`.
