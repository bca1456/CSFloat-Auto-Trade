import asyncio
import json
import time
import aiohttp
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse, parse_qs

from aiosteampy import SteamClient, AppContext
from aiosteampy.utils import get_jsonable_cookies
from aiosteampy.helpers import restore_from_cookies
from aiosteampy.mixins.guard import SteamGuardMixin
from aiosteampy.mixins.web_api import SteamWebApiMixin
from aiosteampy.models import ItemDescription, EconItem, BaseTradeOfferItem
from aiosteampy.constants import TradeOfferStatus, TRADABLE_AFTER_DATE_FORMAT

# =============================================================================
# ПАТЧИ ДЛЯ AIOSTEAMPY (фикс парсинга дат и inspect-ссылок)
# =============================================================================

def _patched_set_d_id(self):
    try:
        if (i_action := next(filter(lambda a: "Inspect" in a.name, self.actions), None)) is not None:
            link = i_action.link
            if "%D" in link:
                object.__setattr__(self, "d_id", int(link.split("%D")[1]))
            else:
                object.__setattr__(self, "d_id", 0)
    except (IndexError, ValueError):
        object.__setattr__(self, "d_id", 0)

ItemDescription._set_d_id = _patched_set_d_id


def _parse_tradable_after_date(date_string: str) -> datetime:
    date_string = date_string.strip().rstrip(".")
    try:
        return datetime.strptime(date_string, TRADABLE_AFTER_DATE_FORMAT)
    except ValueError:
        pass
    now = datetime.now()
    dt = datetime.strptime(f"{date_string} {now.year}", "%d %b @ %I:%M%p %Y")
    if dt < now:
        dt = dt.replace(year=now.year + 1)
    return dt


def _patched_set_tradable_after(self):
    desc = self.description
    if desc is None or not desc.market_tradable_restriction:
        return
    for sep in (
        "Tradable/Marketable After ",
        "This item is trade-protected and cannot be consumed, modified, or transferred until ",
    ):
        t_a_descr = next(
            filter(lambda d: sep in d.value, desc.owner_descriptions or ()),
            None,
        )
        if t_a_descr is not None:
            date_string = t_a_descr.value.split(sep, 1)[1]
            try:
                self.tradable_after = _parse_tradable_after_date(date_string)
            except ValueError:
                pass
            return


EconItem._set_tradable_after = _patched_set_tradable_after
BaseTradeOfferItem._set_tradable_after = _patched_set_tradable_after

# =============================================================================
# КОНФИГУРАЦИЯ И КОНСТАНТЫ
# =============================================================================

CHECK_INTERVAL_MINUTES = 25

API_USER_INFO = "https://csfloat.com/api/v1/me"
API_TRADES = "https://csfloat.com/api/v1/me/trades?state=queued,pending&limit=500"
API_ACCEPT_TRADE = "https://csfloat.com/api/v1/trades/{trade_id}/accept"

COOKIE_FILE = Path("cookies.json")
PROCESSED_TRADES_FILE = Path("processed_trades.json")
INCOMING_TRADES_LOG_FILE = Path("incoming_trades_log.json")
INCOMING_TRADES_IGNORED_FILE = Path("incoming_trades_ignored.json")
SELLER_PENDING_VERIFICATION_LOG_FILE = Path("seller_sent_pending_verification.json")
TG_CONFIG_FILE = Path("tg.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0"
)

# Настройки отправки Steam-офферов
STEAM_SEND_COOLDOWN_SEC = 15          # пауза между созданием офферов
MAX_STEAM_SENDS_PER_PASS = 3          # макс. офферов за один проход
STEAM_MOBILECONF_MIN_INTERVAL_SEC = 15 # минимальный интервал между вызовами mobileconf/getlist
SEND_TRADE_MAX_RETRIES = 1            # попыток при ошибке
SEND_TRADE_RATE_LIMIT_DELAY_SEC = 300 # пауза при 429
SEND_TRADE_RETRY_DELAY_SEC = 15       # пауза при 500

# Порог ошибок 429, после которого пропускаем оставшиеся трейды
MAX_429_ERRORS_PER_PASS = 2

# ==================== НАСТРОЙКИ ASF IPC (загружаются из asf.json) ====================

def load_asf_config():
    """Загружает параметры ASF IPC из файла asf.json."""
    config_path = Path("asf.json")
    if not config_path.is_file():
        print("asf.json не найден. ASF-подтверждение будет недоступно.")
        return None
    try:
        with config_path.open("r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg
    except Exception as e:
        print(f"Ошибка чтения asf.json: {e}")
        return None

_asf_cfg = load_asf_config()
if _asf_cfg:
    ASF_API_URL = _asf_cfg.get("api_url", "http://localhost:1242")
    ASF_PASSWORD = _asf_cfg.get("password", "")
    ASF_BOT_NAME = _asf_cfg.get("bot_name", None)  # новое поле
else:
    ASF_API_URL = "http://localhost:1242"
    ASF_PASSWORD = ""
    ASF_BOT_NAME = None

# =============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (Telegram, JSON-хранилища)
# =============================================================================

def load_steam_config(config_path='steam.json'):
    with open(config_path, 'r') as file:
        return json.load(file)


def load_tg_config():
    if not TG_CONFIG_FILE.is_file():
        return None
    with TG_CONFIG_FILE.open("r", encoding="utf-8") as f:
        try:
            cfg = json.load(f)
        except json.JSONDecodeError:
            return None
    if not isinstance(cfg, dict):
        return None
    token = cfg.get("bot_token") or cfg.get("api_key")
    chat_id = cfg.get("chat_id") or cfg.get("user_id")
    if not token or not chat_id:
        return None
    return {"token": str(token), "chat_id": str(chat_id)}


async def send_telegram_notification(text: str):
    cfg = load_tg_config()
    if not cfg:
        return
    url = f"https://api.telegram.org/bot{cfg['token']}/sendMessage"
    payload = {"chat_id": cfg["chat_id"], "text": text, "disable_web_page_preview": True}
    for attempt in range(1, 4):
        try:
            async with aiohttp.ClientSession() as tg_session:
                async with tg_session.post(url, json=payload) as resp:
                    if resp.status == 200:
                        return
                    if resp.status in (429, 500, 502, 503, 504) and attempt < 3:
                        await asyncio.sleep(2 * attempt)
                        continue
        except Exception:
            if attempt < 3:
                await asyncio.sleep(2 * attempt)
                continue


def load_processed_trades():
    if PROCESSED_TRADES_FILE.is_file():
        with PROCESSED_TRADES_FILE.open("r") as f:
            try:
                return set(json.load(f))
            except json.JSONDecodeError:
                return set()
    return set()


def save_processed_trades(processed_trades):
    with PROCESSED_TRADES_FILE.open("w") as f:
        json.dump(list(processed_trades), f, indent=2)


def load_incoming_trades_log():
    if INCOMING_TRADES_LOG_FILE.is_file():
        with INCOMING_TRADES_LOG_FILE.open("r", encoding="utf-8") as f:
            try:
                data = json.load(f)
                return data if isinstance(data, list) else []
            except json.JSONDecodeError:
                return []
    return []


def append_incoming_trade_log(entry: dict):
    log = load_incoming_trades_log()
    log.append(entry)
    with INCOMING_TRADES_LOG_FILE.open("w", encoding="utf-8") as f:
        json.dump(log, f, indent=2, ensure_ascii=False)


def load_incoming_trades_ignored():
    if INCOMING_TRADES_IGNORED_FILE.is_file():
        with INCOMING_TRADES_IGNORED_FILE.open("r", encoding="utf-8") as f:
            try:
                data = json.load(f)
                return data if isinstance(data, list) else []
            except json.JSONDecodeError:
                return []
    return []


def append_incoming_trade_ignored(entry: dict):
    data = load_incoming_trades_ignored()
    offer_id = str(entry.get("offer_id"))
    updated = False
    for i, old in enumerate(data):
        if str(old.get("offer_id")) == offer_id:
            data[i] = {**old, **entry}
            updated = True
            break
    if not updated:
        data.append(entry)
    with INCOMING_TRADES_IGNORED_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_seller_pending_verification_log():
    if SELLER_PENDING_VERIFICATION_LOG_FILE.is_file():
        with SELLER_PENDING_VERIFICATION_LOG_FILE.open("r", encoding="utf-8") as f:
            try:
                data = json.load(f)
                return data if isinstance(data, list) else []
            except json.JSONDecodeError:
                return []
    return []


def save_seller_pending_verification_log(entries: list[dict]):
    with SELLER_PENDING_VERIFICATION_LOG_FILE.open("w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)


def upsert_seller_pending_verification(entry: dict):
    entries = load_seller_pending_verification_log()
    trade_id = str(entry.get("trade_id"))
    updated = False
    for i, old in enumerate(entries):
        if str(old.get("trade_id")) == trade_id:
            entries[i] = {**old, **entry}
            updated = True
            break
    if not updated:
        entries.append(entry)
    save_seller_pending_verification_log(entries)


def remove_seller_pending_verification(trade_id):
    entries = load_seller_pending_verification_log()
    trade_id_str = str(trade_id)
    new_entries = [e for e in entries if str(e.get("trade_id")) != trade_id_str]
    if len(new_entries) != len(entries):
        save_seller_pending_verification_log(new_entries)


def load_seller_pending_by_trade_id() -> dict[str, dict]:
    return {
        str(e.get("trade_id")): e
        for e in load_seller_pending_verification_log()
        if isinstance(e, dict) and e.get("trade_id") is not None
    }


# =============================================================================
# ПОИСК УЖЕ ОТПРАВЛЕННЫХ STEAM-ОФФЕРОВ
# =============================================================================

async def _find_active_sent_offer_giving_asset(client: SteamClient, partner_steam_id64: int, asset_id_int: int):
    sent, _, _ = await client.get_trade_offers(active_only=True, sent=True, received=False)
    for off in sent:
        if not getattr(off, "is_our_offer", True):
            continue
        if off.partner_id64 != partner_steam_id64:
            continue
        for it in off.items_to_give or ():
            if getattr(it, "asset_id", None) == asset_id_int:
                return off.trade_offer_id
    return None


async def _find_recent_historical_sent_accepted_for_asset(client: SteamClient, partner_steam_id64: int, asset_id_int: int):
    cursor = 0
    for _ in range(6):
        sent, _, next_cursor = await client.get_trade_offers(
            historical_only=True, sent=True, received=False, cursor=cursor
        )
        for off in sent:
            if off.partner_id64 != partner_steam_id64:
                continue
            if off.status not in (TradeOfferStatus.ACCEPTED, TradeOfferStatus.STATE_IN_ESCROW):
                continue
            for it in off.items_to_give or ():
                if getattr(it, "asset_id", None) == asset_id_int:
                    return off.trade_offer_id, off.status
        if not next_cursor:
            break
        cursor = next_cursor
    return None, None


async def detect_existing_sent_offer_for_trade(client: SteamClient, buyer_id: int, asset_id: int):
    active_id = await _find_active_sent_offer_giving_asset(client, buyer_id, asset_id)
    if active_id is not None:
        return active_id, TradeOfferStatus.ACTIVE
    hist_id, hist_status = await _find_recent_historical_sent_accepted_for_asset(client, buyer_id, asset_id)
    if hist_id is not None:
        return hist_id, hist_status or TradeOfferStatus.ACCEPTED
    return None, None


# =============================================================================
# СИНХРОНИЗАЦИЯ PENDING-VERIFIED С STEAM
# =============================================================================

def _synthetic_trade_from_pending_entry(entry: dict) -> dict:
    return {
        "id": entry.get("trade_id"),
        "buyer_id": entry.get("buyer_id"),
        "state": entry.get("csfloat_state") or "unknown",
        "contract": {
            "item": {
                "asset_id": entry.get("asset_id"),
                "market_hash_name": entry.get("item_name"),
            }
        },
    }


async def refresh_all_seller_pending_verification_from_steam(client: SteamClient):
    for entry in load_seller_pending_verification_log():
        if not isinstance(entry, dict) or not entry.get("trade_id"):
            continue
        try:
            await seller_pending_should_hold_without_resend(
                client, _synthetic_trade_from_pending_entry(entry), entry
            )
        except Exception as e:
            print(f"seller_sent_pending_verification sync failed for {entry.get('trade_id')}: {e}")


async def seller_pending_should_hold_without_resend(client: SteamClient, trade: dict, entry: dict) -> bool:
    trade_id = str(trade.get("id"))
    trade_state = trade.get("state")
    item_name, sale_price = _extract_item_name_and_price(trade)
    if item_name == "unknown item" and entry.get("item_name"):
        item_name = entry["item_name"]
    if sale_price == "unknown price" and entry.get("sale_price"):
        sale_price = entry["sale_price"]
    try:
        buyer_id = int(trade.get("buyer_id"))
        asset_id_int = int(trade.get("contract", {}).get("item", {}).get("asset_id"))
    except (TypeError, ValueError):
        return False

    now_iso = datetime.now().isoformat(timespec="seconds")
    base_update = {
        "trade_id": trade_id,
        "item_name": item_name,
        "sale_price": sale_price,
        "asset_id": str(asset_id_int),
        "buyer_id": str(buyer_id),
        "csfloat_state": trade_state or "unknown",
        "last_checked_at": now_iso,
    }

    steam_oid = entry.get("steam_offer_id")
    if steam_oid is not None:
        try:
            steam_oid = int(steam_oid)
        except (TypeError, ValueError):
            steam_oid = None

    if steam_oid is None:
        active_id = await _find_active_sent_offer_giving_asset(client, buyer_id, asset_id_int)
        if active_id is not None:
            upsert_seller_pending_verification({**base_update, "steam_offer_id": active_id, "steam_offer_status": "ACTIVE", "note": "Оффер в Steam активен"})
            return True
        hist_id, hist_status = await _find_recent_historical_sent_accepted_for_asset(client, buyer_id, asset_id_int)
        if hist_id is not None:
            upsert_seller_pending_verification({**base_update, "steam_offer_id": hist_id, "steam_offer_status": hist_status.name if hist_status else "ACCEPTED", "note": "Покупатель принял (или эскроу)"})
            return True
        upsert_seller_pending_verification({**base_update, "note": "Оффер не найден"})
        return True

    try:
        offer = await client.get_trade_offer(steam_oid)
    except Exception:
        upsert_seller_pending_verification({**base_update, "steam_offer_id": steam_oid, "note": "Ошибка проверки статуса"})
        return True

    st = offer.status
    if st in (TradeOfferStatus.ACTIVE, TradeOfferStatus.CONFIRMATION_NEED):
        upsert_seller_pending_verification({**base_update, "steam_offer_id": steam_oid, "steam_offer_status": st.name, "note": "Активен / ждёт подтверждения"})
        return True
    if st in (TradeOfferStatus.ACCEPTED, TradeOfferStatus.STATE_IN_ESCROW):
        upsert_seller_pending_verification({**base_update, "steam_offer_id": steam_oid, "steam_offer_status": st.name, "note": "Принят / эскроу"})
        return True
    if st in (TradeOfferStatus.DECLINED, TradeOfferStatus.CANCELED, TradeOfferStatus.EXPIRED, TradeOfferStatus.INVALID, TradeOfferStatus.INVALID_ITEMS, TradeOfferStatus.CANCELED_BY_SECONDARY_FACTOR):
        upsert_seller_pending_verification({**base_update, "steam_offer_id": None, "steam_offer_status": st.name, "note": f"Завершён: {st.name}"})
        return False
    upsert_seller_pending_verification({**base_update, "steam_offer_id": steam_oid, "steam_offer_status": st.name, "note": f"Статус: {st.name}"})
    return True


# =============================================================================
# ИЗВЛЕЧЕНИЕ ДАННЫХ ИЗ ТРЕЙДА
# =============================================================================

def _extract_item_name_and_price(trade: dict) -> tuple[str, str]:
    contract = trade.get("contract", {}) if isinstance(trade, dict) else {}
    item = contract.get("item", {}) if isinstance(contract, dict) else {}
    item_name = (
        item.get("market_hash_name")
        or item.get("name")
        or item.get("item_name")
        or trade.get("item_name")
        or "unknown item"
    )
    raw_price = (
        trade.get("price")
        or trade.get("sale_price")
        or trade.get("listed_price")
        or trade.get("total_price")
        or contract.get("price")
        or item.get("price")
    )
    currency = trade.get("currency") or "USD"
    if raw_price is None:
        price_text = "unknown price"
    elif isinstance(raw_price, (int, float)):
        price_text = f"{float(raw_price) / 100:.2f} {currency}"
    else:
        try:
            cents = float(str(raw_price).strip())
            price_text = f"{cents / 100:.2f} {currency}"
        except ValueError:
            price_text = str(raw_price)
    return item_name, price_text


# =============================================================================
# CSFloat API
# =============================================================================

async def get_user_info(session, csfloat_api_key):
    headers = {'Authorization': csfloat_api_key}
    try:
        async with session.get(API_USER_INFO, headers=headers) as response:
            response.raise_for_status()
            return await response.json()
    except Exception as e:
        print(f"get_user_info error: {e}")
    return None


async def get_trades(session, csfloat_api_key):
    headers = {'Authorization': csfloat_api_key}
    try:
        async with session.get(API_TRADES, headers=headers) as response:
            response.raise_for_status()
            return await response.json()
    except Exception as e:
        print(f"get_trades error: {e}")
    return None


async def accept_trade(session, csfloat_api_key, trade_id, trade_token):
    url = API_ACCEPT_TRADE.format(trade_id=trade_id)
    headers = {'Authorization': csfloat_api_key, 'Content-Type': 'application/json'}
    payload = {'trade_token': trade_token}
    try:
        async with session.post(url, headers=headers, json=payload) as response:
            if response.status != 200:
                detail = await response.text()
                print(f"CSFloat accept failed {trade_id}: {response.status} {detail}")
                return False
            return True
    except Exception as e:
        print(f"CSFloat accept error {trade_id}: {e}")
    return False


# =============================================================================
# STEAM TRADE OFFER — ОТПРАВКА (БЕЗ ПОДТВЕРЖДЕНИЯ)
# =============================================================================

def csfloat_steam_offer_message(csfloat_trade_id) -> str:
    return f"CSFloat Trade Offer Ref #{csfloat_trade_id}"


def install_steam_mobileconf_throttle(client: SteamGuardMixin):
    """Только соблюдает минимальный интервал между вызовами, без повторов при 429."""
    lock = asyncio.Lock()
    last_call = 0.0
    orig = client.get_confirmations

    async def throttled(*args, **kwargs):
        nonlocal last_call
        async with lock:
            elapsed = time.monotonic() - last_call
            if elapsed < STEAM_MOBILECONF_MIN_INTERVAL_SEC:
                await asyncio.sleep(STEAM_MOBILECONF_MIN_INTERVAL_SEC - elapsed)
            result = await orig(*args, **kwargs)
            last_call = time.monotonic()
            return result

    client.get_confirmations = throttled


def _parse_trade_url(trade_url: str):
    """Возвращает (partner_id64, token) из trade URL или (None, None)."""
    try:
        parsed = urlparse(trade_url)
        params = parse_qs(parsed.query)
        partner = params.get('partner', [None])[0]
        token = params.get('token', [None])[0]
        if partner:
            return int(partner) + 76561197960265728, token
    except Exception:
        pass
    return None, None


async def send_steam_trade(
    client: SteamClient,
    trade_id,
    buyer_steam_id=None,
    trade_url=None,
    asset_id=None,
    trade_token=None,
):
    """
    Только создаёт Steam trade offer, НЕ подтверждает.
    Возвращает offer_id (int) или False.
    """
    try:
        asset_id_int = int(asset_id)
    except (TypeError, ValueError):
        print(f"Trade {trade_id}: некорректный asset_id={asset_id}")
        return False

    try:
        inv_result = await client.get_inventory(AppContext.CS2)
        my_inv = inv_result[0] if (isinstance(inv_result, (tuple, list)) and inv_result) else []
    except Exception as e:
        print(f"Trade {trade_id}: ошибка загрузки инвентаря: {e}")
        return False

    item_to_give = next((it for it in my_inv if it.asset_id == asset_id_int), None)
    if not item_to_give:
        print(f"Trade {trade_id}: предмет с asset_id {asset_id} не найден")
        return False

    message = csfloat_steam_offer_message(trade_id)

    if trade_url:
        target_steam_id, final_token = _parse_trade_url(trade_url)
        if not target_steam_id:
            print(f"Trade {trade_id}: не удалось извлечь partner_id из trade_url")
            return False
    elif buyer_steam_id:
        target_steam_id = int(buyer_steam_id)
        final_token = trade_token
    else:
        print(f"Trade {trade_id}: нет buyer_steam_id или trade_url")
        return False

    for attempt in range(1, SEND_TRADE_MAX_RETRIES + 1):
        try:
            kwargs = {
                "to_give": [item_to_give],
                "to_receive": [],
                "message": message,
                "confirm": False,
            }
            if final_token:
                kwargs["token"] = final_token

            offer_id = await client.make_trade_offer(target_steam_id, **kwargs)
            if offer_id:
                print(f"Trade {trade_id}: оффер {offer_id} создан")
                return offer_id
            print(f"Trade {trade_id}: make_trade_offer вернул None/False")
            return False
        except aiohttp.ClientResponseError as http_err:
            if http_err.status == 429 and attempt < SEND_TRADE_MAX_RETRIES:
                print(f"Trade {trade_id}: 429, жду {SEND_TRADE_RATE_LIMIT_DELAY_SEC} сек...")
                await asyncio.sleep(SEND_TRADE_RATE_LIMIT_DELAY_SEC)
                continue
            elif http_err.status == 500 and attempt < SEND_TRADE_MAX_RETRIES:
                print(f"Trade {trade_id}: 500, повтор через {SEND_TRADE_RETRY_DELAY_SEC} сек")
                await asyncio.sleep(SEND_TRADE_RETRY_DELAY_SEC)
                continue
            else:
                print(f"Trade {trade_id}: HTTP {http_err.status} – {http_err}")
                return False
        except Exception as e:
            print(f"Trade {trade_id}: ошибка отправки: {e}")
            if attempt < SEND_TRADE_MAX_RETRIES:
                await asyncio.sleep(SEND_TRADE_RETRY_DELAY_SEC)
                continue
            return False
    return False


async def send_steam_trade_limited(client: SteamClient, *, sends_this_pass: int, **kwargs):
    if sends_this_pass >= MAX_STEAM_SENDS_PER_PASS:
        trade_id = kwargs.get("trade_id", "?")
        print(f"Trade {trade_id}: лимит отправок за проход исчерпан, отложено")
        return None, sends_this_pass, True

    offer_id = await send_steam_trade(client, **kwargs)
    if offer_id:
        sends_this_pass += 1
        await asyncio.sleep(STEAM_SEND_COOLDOWN_SEC)
    return offer_id, sends_this_pass, False


# =============================================================================
# ОБРАБОТКА ВХОДЯЩИХ STEAM-ОФФЕРОВ (BUY ORDERS)
# =============================================================================

async def check_incoming_trade_offers(client: SteamGuardMixin):
    try:
        existing_log = load_incoming_trades_log()
        ignored_log = load_incoming_trades_ignored()
        processed_ids = {str(e.get("offer_id")) for e in existing_log if isinstance(e, dict)}
        processed_ids.update(str(e.get("offer_id")) for e in ignored_log if isinstance(e, dict))
        _, received, _ = await client.get_trade_offers(active_only=True, sent=False, received=True)
    except Exception as e:
        print(f"Incoming check error: {e}")
        return

    for offer in received:
        offer_id = getattr(offer, "trade_offer_id", None)
        if offer_id is None or str(offer_id) in processed_ids:
            continue
        items_to_give = getattr(offer, "items_to_give", []) or []
        items_to_receive = getattr(offer, "items_to_receive", []) or []
        if len(items_to_give) != 0 or len(items_to_receive) == 0:
            continue

        accepted = False
        for attempt in range(1, 4):
            try:
                await client.accept_trade_offer(offer)
                accepted = True
                break
            except aiohttp.ClientResponseError as http_err:
                if http_err.status == 500 and attempt < 3:
                    await asyncio.sleep(5)
                    continue
                print(f"Failed to accept incoming offer {offer_id}: {http_err}")
                break
            except Exception as e:
                print(f"Failed to accept incoming offer {offer_id}: {e}")
                break

        if not accepted:
            try:
                fetched = await client.get_trade_offer(int(offer_id))
                if fetched.status in {TradeOfferStatus.CANCELED, TradeOfferStatus.DECLINED, TradeOfferStatus.EXPIRED, TradeOfferStatus.INVALID, TradeOfferStatus.INVALID_ITEMS, TradeOfferStatus.CANCELED_BY_SECONDARY_FACTOR, TradeOfferStatus.TRADE_REVERSED}:
                    append_incoming_trade_ignored({"offer_id": int(offer_id), "status": fetched.status.name, "updated_at": datetime.now().isoformat(timespec="seconds"), "note": "Offer no longer active"})
                    processed_ids.add(str(offer_id))
            except Exception:
                pass
            continue

        item_names = []
        for it in items_to_receive:
            descr = getattr(it, "description", None)
            name = getattr(descr, "market_hash_name", None) or getattr(descr, "market_name", None) or getattr(descr, "name", None) if descr else None
            item_names.append(name or str(getattr(it, "asset_id", "unknown")))

        append_incoming_trade_log({
            "offer_id": int(offer_id),
            "accepted_at": datetime.now().isoformat(timespec="seconds"),
            "items": item_names,
            "partner": getattr(offer, "partner_id64", None) or getattr(offer, "partner_id", None),
            "message": getattr(offer, "message", ""),
        })
        processed_ids.add(str(offer_id))
        print(f"Accepted incoming trade offer {offer_id}: {', '.join(item_names)}")
        await send_telegram_notification(f"Accepted incoming Steam trade offer {offer_id}\nItems: {', '.join(item_names)}")


# =============================================================================
# ПОДТВЕРЖДЕНИЕ ОФФЕРОВ ЧЕРЕЗ ASF IPC
# =============================================================================

async def confirm_offers_via_asf():
    bot_name = ASF_BOT_NAME
    if not bot_name:
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(
                    f"{ASF_API_URL}/Api/Bot/ASF",
                    headers={"Authentication": ASF_PASSWORD}
                ) as resp:
                    if resp.status != 200:
                        print(f"ASF API недоступен: {resp.status}")
                        return False
                    bots_data = await resp.json()
                    if not bots_data.get("result"):
                        print("Нет активных ботов в ASF. Укажите bot_name в asf.json (Steam логин)")
                        return False
                    bot_name = list(bots_data["result"].keys())[0]
            except Exception as e:
                print(f"Ошибка подключения к ASF: {e}")
                return False

    cmd = f"2faok {bot_name}"

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(
                f"{ASF_API_URL}/Api/Command",
                json={"Command": cmd, "Bot": bot_name},
                headers={"Authentication": ASF_PASSWORD}
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # Учитываем оба варианта написания ключа
                    success = data.get("Success") or data.get("success")
                    if success is True:
                        print("ASF подтвердил офферы")
                        return True
                    # Если Result содержит "Successfully", тоже считаем успехом
                    result = data.get("Result", "")
                    if "successfully" in str(result).lower():
                        print("ASF подтвердил офферы")
                        return True
                    # Выводим сообщение об ошибке, если есть
                    err_msg = data.get("Message") or data.get("message")
                    print(f"ASF вернул ошибку: {err_msg}")
                    return False
                else:
                    print(f"Ошибка вызова ASF: {resp.status} {await resp.text()}")
                    return False
        except Exception as e:
            print(f"Ошибка отправки команды в ASF: {e}")
            return False


# =============================================================================
# ГЛАВНЫЙ ЦИКЛ ПРОВЕРКИ CSFloat ТРЕЙДОВ
# =============================================================================

async def check_actionable_trades(session, csfloat_api_key, client: SteamGuardMixin, processed_trades):
    await check_incoming_trade_offers(client)
    await refresh_all_seller_pending_verification_from_steam(client)

    user_info = await get_user_info(session, csfloat_api_key)
    if not user_info or user_info.get('actionable_trades', 0) == 0:
        print(f"No actionable trades. Waiting {CHECK_INTERVAL_MINUTES} min.")
        return

    print("Actionable trades found, fetching trade details...")
    trades_info = await get_trades(session, csfloat_api_key)
    if not isinstance(trades_info, dict):
        return
    trades_list = trades_info.get('trades', [])
    if not isinstance(trades_list, list):
        return

    seller_pending_by_id = load_seller_pending_by_trade_id()
    seller_pending_csfloat_ids = set(seller_pending_by_id.keys())

    stats = {"total": 0, "seller_sent": 0, "seller_waiting_verified": 0, "buyer_waiting_incoming": 0, "failed": 0, "deferred": 0, "skipped_429": 0}
    sends_this_pass = 0
    consecutive_429_errors = 0
    my_steam_id64 = int(client.steam_id)

    offers_to_confirm = []   # собираем offer_id созданных офферов

    for trade in trades_list:
        if not isinstance(trade, dict):
            continue
        if consecutive_429_errors >= MAX_429_ERRORS_PER_PASS:
            stats["skipped_429"] += 1
            continue

        stats["total"] += 1
        trade_id = trade.get('id')
        seller_id = trade.get('seller_id')
        buyer_id = trade.get('buyer_id')
        asset_id = trade.get('contract', {}).get('item', {}).get('asset_id')
        item_name, sale_price = _extract_item_name_and_price(trade)
        trade_token = trade.get('trade_token')
        trade_url = trade.get('trade_url')
        accepted_at = trade.get('accepted_at')
        trade_state = trade.get('state')

        if trade_state == "verified":
            processed_trades.add(str(trade_id))
            remove_seller_pending_verification(trade_id)
            continue

        if not (trade_id and seller_id and buyer_id and asset_id):
            continue

        try:
            sid = int(seller_id)
            bid = int(buyer_id)
        except (TypeError, ValueError):
            continue

        pending_entry = seller_pending_by_id.get(str(trade_id))
        if pending_entry and my_steam_id64 == sid and my_steam_id64 != bid:
            processed_trades.add(str(trade_id))
            stats["seller_waiting_verified"] += 1
            continue

        if str(trade_id) in processed_trades:
            if my_steam_id64 == bid and my_steam_id64 != sid:
                continue
            if str(trade_id) not in seller_pending_csfloat_ids:
                continue
            processed_trades.discard(str(trade_id))

        if my_steam_id64 == bid and my_steam_id64 != sid:
            processed_trades.add(str(trade_id))
            stats["buyer_waiting_incoming"] += 1
            continue

        if my_steam_id64 != sid:
            continue

        # Мы продавец
        send_success = False
        sent_offer_id = None

        if accepted_at:
            try:
                existing_offer_id, existing_offer_status = await detect_existing_sent_offer_for_trade(client, bid, int(asset_id))
            except Exception:
                existing_offer_id, existing_offer_status = None, None

            if existing_offer_id:
                send_success = True
                sent_offer_id = int(existing_offer_id)
                upsert_seller_pending_verification({
                    "trade_id": str(trade_id), "item_name": item_name, "sale_price": sale_price,
                    "asset_id": str(asset_id), "buyer_id": str(buyer_id),
                    "steam_offer_id": int(existing_offer_id),
                    "csfloat_state": trade_state or "unknown",
                    "steam_offer_status": existing_offer_status.name,
                    "last_checked_at": datetime.now().isoformat(timespec="seconds"),
                    "note": "Оффер уже отправлялся ранее; ждём verified на CSFloat",
                })
            else:
                print(f"Trade {trade_id} уже принято. Отправка '{item_name}' за {sale_price}.")
                try:
                    offer_id, sends_this_pass, deferred = await send_steam_trade_limited(
                        client, sends_this_pass=sends_this_pass,
                        trade_id=str(trade_id), buyer_steam_id=bid,
                        asset_id=int(asset_id), trade_token=trade_token, trade_url=trade_url
                    )
                except aiohttp.ClientResponseError as e:
                    if e.status == 429:
                        consecutive_429_errors += 1
                    continue

                if deferred:
                    stats["deferred"] += 1
                    continue

                if offer_id:
                    send_success = True
                    sent_offer_id = int(offer_id)
                    offers_to_confirm.append(sent_offer_id)
                    print(f"Trade {trade_id}: отправка подтверждена для '{item_name}' за {sale_price}.")
                    await send_telegram_notification(f"CSFloat sale sent\nTrade: {trade_id}\nItem: {item_name}\nPrice: {sale_price}")
                else:
                    try:
                        existing_offer_id, _ = await detect_existing_sent_offer_for_trade(client, bid, int(asset_id))
                    except Exception:
                        existing_offer_id = None
                    if existing_offer_id:
                        send_success = True
                        sent_offer_id = int(existing_offer_id)
        else:
            print(f"Accepting trade {trade_id} ({item_name}, {sale_price})...")
            accept_result = await accept_trade(session, csfloat_api_key, trade_id=str(trade_id), trade_token=trade_token)
            if accept_result:
                print(f"Sending '{item_name}' to buyer for trade {trade_id} ({sale_price})...")
                try:
                    offer_id, sends_this_pass, deferred = await send_steam_trade_limited(
                        client, sends_this_pass=sends_this_pass,
                        trade_id=str(trade_id), buyer_steam_id=bid,
                        asset_id=int(asset_id), trade_token=trade_token, trade_url=trade_url
                    )
                except aiohttp.ClientResponseError as e:
                    if e.status == 429:
                        consecutive_429_errors += 1
                    continue

                if deferred:
                    stats["deferred"] += 1
                    continue

                if offer_id:
                    send_success = True
                    sent_offer_id = int(offer_id)
                    offers_to_confirm.append(sent_offer_id)
                    print(f"Trade {trade_id}: отправка подтверждена для '{item_name}' за {sale_price}.")
                    await send_telegram_notification(f"CSFloat sale sent\nTrade: {trade_id}\nItem: {item_name}\nPrice: {sale_price}")
                else:
                    print(f"Failed to send trade for {trade_id}")
            else:
                print(f"Failed to accept trade {trade_id}")
                stats["failed"] += 1

        if send_success:
            processed_trades.add(str(trade_id))
            stats["seller_sent"] += 1
            upsert_seller_pending_verification({
                "trade_id": str(trade_id), "item_name": item_name, "sale_price": sale_price,
                "asset_id": str(asset_id), "buyer_id": str(buyer_id),
                "steam_offer_id": sent_offer_id,
                "sent_at": datetime.now().isoformat(timespec="seconds"),
                "csfloat_state": trade_state or "unknown",
                "steam_offer_status": "CONFIRMATION_NEED",
                "note": "Оффер создан, требуется подтверждение через ASF",
            })
        else:
            print(f"Trade {trade_id} не помечен как обработанный — повторная попытка при следующей проверке.")
            stats["failed"] += 1

    # ПОДТВЕРЖДЕНИЕ ЧЕРЕЗ ASF — собираем свежие + «зависшие»
    pending_all = load_seller_pending_verification_log()
    for entry in pending_all:
        if entry.get("steam_offer_status") == "CONFIRMATION_NEED" and entry.get("steam_offer_id"):
            oid = entry["steam_offer_id"]
            if oid not in offers_to_confirm:
                offers_to_confirm.append(oid)

    if offers_to_confirm:
        print(f"\nПодтверждаем {len(offers_to_confirm)} офферов через ASF...")
        success = await confirm_offers_via_asf()
        if success:
            # Обновляем статусы в логе для всех подтверждённых
            for oid in offers_to_confirm:
                for entry in pending_all:
                    if entry.get("steam_offer_id") == oid:
                        entry["steam_offer_status"] = "ACTIVE"
                        entry["note"] = "Оффер подтверждён через ASF, ждём покупателя"
                        entry["last_confirmed_at"] = datetime.now().isoformat(timespec="seconds")
                        upsert_seller_pending_verification(entry)
                        break
        else:
            print("Не удалось подтвердить офферы через ASF – они будут повторно отправлены в следующем цикле")

    print(
        f"Trade pass summary: total={stats['total']}, sent={stats['seller_sent']}, "
        f"waiting_verified={stats['seller_waiting_verified']}, "
        f"buyer_waiting_incoming={stats['buyer_waiting_incoming']}, "
        f"deferred={stats['deferred']}, skipped_429={stats['skipped_429']}, "
        f"failed={stats['failed']}"
    )


# =============================================================================
# MAIN
# =============================================================================

async def main():
    config = load_steam_config()
    csfloat_api_key = config['csfloat_api_key']
    steam_api_key = config['steam_api_key']
    steam_id = int(config['steam_id64'])
    steam_login = config['steam_login']
    steam_password = config['steam_password']
    shared_secret = config['shared_secret']
    identity_secret = config['identity_secret']

    class MySteamClient(SteamClient, SteamWebApiMixin, SteamGuardMixin):
        pass

    client = MySteamClient(
        steam_id=steam_id,
        username=steam_login,
        password=steam_password,
        shared_secret=shared_secret,
        identity_secret=identity_secret,
        api_key=steam_api_key,
        user_agent=USER_AGENT,
    )

    # install_steam_mobileconf_throttle(client)  # больше не нужно, т.к. подтверждения идут через ASF

    if COOKIE_FILE.is_file():
        with COOKIE_FILE.open("r") as f:
            cookies = json.load(f)
        await restore_from_cookies(cookies, client)
    else:
        await client.login()

    processed_trades = load_processed_trades()

    async with aiohttp.ClientSession() as session:
        try:
            while True:
                await check_actionable_trades(session, csfloat_api_key, client, processed_trades)
                save_processed_trades(processed_trades)
                await asyncio.sleep(CHECK_INTERVAL_MINUTES * 60)
        finally:
            with COOKIE_FILE.open("w") as f:
                json.dump(get_jsonable_cookies(client.session), f, indent=2)
            await client.session.close()


if __name__ == "__main__":
    asyncio.run(main())