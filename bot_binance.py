"""
Binance C2C P2P Order Auto-Confirm Bot
---------------------------------------
Polls for orders awaiting buyer payment (orderStatus 1 = "Wait for payment"),
sends a Telegram notification, waits a configurable delay, then automatically
calls markOrderAsPaid.
"""

import os
import time
import hmac
import hashlib
import json
import logging
import requests
from urllib.parse import urlencode

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bot_binance")

BASE_URL = "https://api.binance.com"

BINANCE_API_KEY = os.environ["BINANCE_API_KEY"]
BINANCE_SECRET_KEY = os.environ["BINANCE_SECRET_KEY"]

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
EXCHANGE_NAME = os.environ.get("EXCHANGE_NAME", "Binance")

CONFIRM_DELAY_SECONDS = int(os.environ.get("CONFIRM_DELAY_SECONDS", "14"))
POLL_SECONDS = int(os.environ.get("ORDER_POLL_SECONDS", "15"))
TRADE_TYPE = os.environ.get("BINANCE_TRADE_TYPE", "BUY")
RECV_WINDOW = int(os.environ.get("RECV_WINDOW_MS", "10000"))

WAIT_FOR_PAYMENT_STATUS = 1

# Format: "advNo1:targetRemaining1,advNo2:targetRemaining2"
AD_RESET_TARGETS_RAW = os.environ.get("BINANCE_AD_RESET_TARGETS", "")

SEEN_ORDERS_FILE = os.path.join(os.path.dirname(__file__), "seen_orders_binance.json")


def parse_reset_targets(raw):
    targets = {}
    for pair in (raw or "").split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        adv_no, amount = pair.split(":", 1)
        try:
            targets[adv_no.strip()] = float(amount.strip())
        except ValueError:
            log.error(f"Ignoring malformed reset target: {pair!r}")
    return targets


class BinanceC2C:
    def __init__(self, api_key, secret_key):
        self.api_key = api_key
        self.secret_key = secret_key

    def _signed_params(self, extra_params=None):
        params = {"timestamp": int(time.time() * 1000), "recvWindow": RECV_WINDOW}
        if extra_params:
            params.update(extra_params)
        query_string = urlencode(params)
        signature = hmac.new(self.secret_key.encode(), query_string.encode(), hashlib.sha256).hexdigest()
        params["signature"] = signature
        return params

    def _headers(self):
        return {
            "X-MBX-APIKEY": self.api_key,
            "clientType": "web",
            "Content-Type": "application/json",
        }

    def _post(self, path, body=None, query_extra=None):
        params = self._signed_params(query_extra)
        res = requests.post(BASE_URL + path, params=params, headers=self._headers(),
                             data=json.dumps(body or {}), timeout=15)
        if not res.ok:
            log.error(f"HTTP {res.status_code} response body: {res.text}")
        res.raise_for_status()
        return res.json()

    def list_orders(self, trade_type, order_status_list, page=1, rows=20):
        body = {
            "tradeType": trade_type,
            "orderStatusList": order_status_list,
            "page": page,
            "rows": rows,
        }
        return self._post("/sapi/v1/c2c/orderMatch/listOrders", body=body)

    def get_order_detail(self, ad_order_no):
        return self._post("/sapi/v1/c2c/orderMatch/getUserOrderDetail", body={"adOrderNo": ad_order_no})

    def mark_order_as_paid(self, order_number, pay_id):
        return self._post("/sapi/v1/c2c/orderMatch/markOrderAsPaid",
                           body={"orderNumber": order_number, "payId": pay_id})

    def get_my_ad(self, adv_no):
        res = self._post("/sapi/v1/c2c/ads/listWithPagination", body={"page": 1, "rows": 50})
        data = res.get("data")
        rows = data.get("list", []) if isinstance(data, dict) else (data or [])
        for row in rows:
            if str(row.get("advNo")) == str(adv_no):
                return row
        return None

    def reset_ad_amount(self, adv_no, target_remaining):
        """Top an ad back up so its REMAINING amount equals target_remaining.

        initAmount is the ad's total size and Binance derives remaining as
        initAmount - sold, so the amount already sold has to be added back or
        the ad would shrink instead of refill. Price is deliberately never
        sent: these ads may be floating-price, and echoing a price would
        fight the float.
        """
        ad = self.get_my_ad(adv_no)
        if not ad:
            return False, f"ad {adv_no} not found"

        try:
            init = float(ad.get("initAmount"))
            remaining = float(ad.get("surplusAmount"))
        except (TypeError, ValueError):
            return False, f"ad {adv_no} has unreadable amounts"

        sold = init - remaining
        needed_init = target_remaining + sold
        if abs(remaining - target_remaining) < 1e-8:
            return True, f"already at {target_remaining}"

        res = self._post("/sapi/v1/c2c/ads/update", body={
            "advNo": str(adv_no),
            "updateMode": "selective",
            "priceType": ad.get("priceType", 1),
            "initAmount": f"{needed_init:.8f}",
        })
        ok = str(res.get("code")) in ("000000", "0") and res.get("success", True)
        detail = (f"remaining {remaining:.8f} -> {target_remaining:.8f} "
                  f"(initAmount {init:.8f} -> {needed_init:.8f})")
        return ok, detail if ok else (res.get("message") or json.dumps(res))


def escape_html(text):
    if text is None:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_telegram_message(text):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        log.warning("Telegram not configured — skipping notification.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        res = requests.post(url, json=payload, timeout=10)
        if not res.ok:
            log.error(f"Telegram send failed: {res.status_code} {res.text}")
    except requests.exceptions.RequestException as e:
        log.error(f"Telegram send error: {e}")


def extract_pay_id(detail):
    """Pull the seller's payment-method id out of an order detail.

    listOrders never carries payId — the API only exposes it on the order
    detail, as selectedPayId with the chosen method repeated in payMethods.
    """
    data = (detail or {}).get("data") or {}
    pay_id = data.get("selectedPayId")
    if pay_id:
        return pay_id, data
    for method in (data.get("payMethods") or []):
        if method.get("id"):
            return method["id"], data
    return None, data


def selected_pay_method(detail_data):
    """The payment method the seller actually chose for this order."""
    methods = detail_data.get("payMethods") or []
    selected_id = detail_data.get("selectedPayId")
    for m in methods:
        if selected_id and str(m.get("id")) == str(selected_id):
            return m
    return methods[0] if methods else {}


def format_order_message(order, detail_data=None):
    d = detail_data or {}
    # The summary calls the currency `fiat`; only the detail has `fiatUnit`.
    fiat = d.get("fiatUnit") or order.get("fiat") or ""
    asset = d.get("asset") or order.get("asset") or ""
    crypto_amount = d.get("amount") or order.get("amount") or ""
    fiat_total = d.get("totalPrice") or order.get("totalPrice") or ""
    rate = d.get("price") or ""

    method = selected_pay_method(d)
    method_name = method.get("tradeMethodName") or d.get("payType") or "N/A"

    # Every payment method exposes its details the same way — a list of
    # name/value fields — so they are rendered generically rather than
    # special-cased per bank or wallet. <code> makes each tap-to-copy.
    pay_lines = ""
    for field in (method.get("fields") or []):
        name = escape_html(field.get("fieldName"))
        value = escape_html(field.get("fieldValue"))
        if value:
            pay_lines += f"\n{name}: <code>{value}</code>"

    account_name = d.get("sellerName") or d.get("sellerNickname") or order.get("sellerNickname")

    msg = (
        f"\U0001F514 <b>New Order — {escape_html(EXCHANGE_NAME)}</b>\n\n"
        f"Order Number: <code>{escape_html(order.get('orderNumber'))}</code>\n"
        f"Pair: {escape_html(asset)} → {escape_html(fiat)}\n"
        f"Amount to pay: {escape_html(fiat_total)} {escape_html(fiat)}\n"
        f"Rate: {escape_html(rate)} {escape_html(fiat)} per {escape_html(asset)}\n"
        f"Quantity: {escape_html(crypto_amount)} {escape_html(asset)}\n"
        f"Account name: {escape_html(account_name)}\n"
        f"Seller: {escape_html(d.get('sellerNickname') or order.get('sellerNickname'))}\n"
        f"Payment method: {escape_html(method_name)}"
        f"{pay_lines}\n"
    )
    if d.get("remark"):
        msg += f"\nNote: {escape_html(d.get('remark'))}\n"
    msg += f"\n⏱ Auto-confirming payment in {CONFIRM_DELAY_SECONDS}s."
    return msg


def load_seen_orders():
    if os.path.exists(SEEN_ORDERS_FILE):
        try:
            with open(SEEN_ORDERS_FILE) as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def save_seen_orders(seen):
    with open(SEEN_ORDERS_FILE, "w") as f:
        json.dump(list(seen), f)


def run():
    client = BinanceC2C(BINANCE_API_KEY, BINANCE_SECRET_KEY)
    seen_orders = load_seen_orders()
    reset_targets = parse_reset_targets(AD_RESET_TARGETS_RAW)
    if reset_targets:
        log.info(f"Ad reset targets configured: {reset_targets}")

    log.info(f"Starting Binance C2C order auto-confirm loop [{EXCHANGE_NAME}] "
             f"(trade_type={TRADE_TYPE}, delay={CONFIRM_DELAY_SECONDS}s, poll every {POLL_SECONDS}s). Ctrl+C to stop.")

    while True:
        try:
            result = client.list_orders(TRADE_TYPE, [WAIT_FOR_PAYMENT_STATUS])
            orders = (result.get("data") or {}).get("list", []) if isinstance(result.get("data"), dict) else (result.get("data") or [])

            for order in orders:
                order_number = order.get("orderNumber")
                if not order_number or order_number in seen_orders:
                    continue

                log.info(f"New pending order detected: {order_number}")
                seen_orders.add(order_number)
                save_seen_orders(seen_orders)

                # payId lives only on the order detail, so it is fetched up
                # front — both to notify with real figures and to have the id
                # ready before the confirm window starts counting down.
                try:
                    detail = client.get_order_detail(order_number)
                    pay_id, detail_data = extract_pay_id(detail)
                except Exception as e:
                    log.error(f"Could not fetch order detail for {order_number}: {e}")
                    pay_id, detail_data = None, {}

                message = format_order_message(order, detail_data)
                send_telegram_message(message)
                log.info(message)

                if not pay_id:
                    log.error(f"No payId on order detail for {order_number} — cannot mark as paid, skipping.")
                    send_telegram_message(
                        f"⚠️ Order <code>{escape_html(order_number)}</code> — no payId on the order "
                        f"detail. Needs manual confirmation in the app.")
                    continue

                log.info(f"Waiting {CONFIRM_DELAY_SECONDS}s before auto-confirming {order_number} "
                         f"(payId {pay_id})...")
                time.sleep(CONFIRM_DELAY_SECONDS)

                confirm_result = client.mark_order_as_paid(order_number, pay_id)
                if confirm_result.get("code") in ("000000", "0", None) and confirm_result.get("success", True):
                    send_telegram_message(f"✅ Order <code>{escape_html(order_number)}</code> marked as paid.")

                    # The order detail names the ad it came from, so the right
                    # ad is topped back up without having to guess.
                    adv_no = str(detail_data.get("advOrderNumber") or "")
                    target = reset_targets.get(adv_no)
                    if target is not None:
                        ok, detail = client.reset_ad_amount(adv_no, target)
                        if ok:
                            log.info(f"Ad {adv_no} reset: {detail}")
                            send_telegram_message(
                                f"♻️ Ad <code>{escape_html(adv_no)}</code> topped back up to "
                                f"{target:g}.")
                        else:
                            log.error(f"Ad {adv_no} reset FAILED: {detail}")
                            send_telegram_message(
                                f"⚠️ Could not reset ad <code>{escape_html(adv_no)}</code>: "
                                f"{escape_html(detail)}")
                    elif adv_no:
                        log.info(f"Ad {adv_no} has no reset target configured — leaving it alone.")
                else:
                    log.error(f"Mark as paid failed for {order_number}: {confirm_result}")
                    send_telegram_message(f"❌ Failed to mark order <code>{escape_html(order_number)}</code> as paid — check logs.")

        except requests.exceptions.RequestException as e:
            log.error(f"Network/API error: {e}")
        except Exception as e:
            log.error(f"Unexpected error: {e}")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    run()
