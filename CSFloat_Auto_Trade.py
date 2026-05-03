import asyncio
import json
import aiohttp
from pathlib import Path
from datetime import datetime
from aiosteampy import SteamClient, AppContext
from aiosteampy.utils import get_jsonable_cookies
from aiosteampy.helpers import restore_from_cookies
from aiosteampy.mixins.guard import SteamGuardMixin  # Импортируем SteamGuard для подтверждения трейдов
from aiosteampy.mixins.web_api import SteamWebApiMixin  # Импортируем WebApiMixin для работы с Web API
from aiosteampy.models import ItemDescription
from aiosteampy.constants import TradeOfferStatus

# Патч для aiosteampy: у части предметов CS2 ссылка Inspect не содержит "%D",
# из-за чего _set_d_id() падает с "list index out of range" на split("%D")[1].
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

# Продолжительность ожидания между проверками (в минутах)
CHECK_INTERVAL_MINUTES = 25

# Constants for API endpoints
API_USER_INFO = "https://csfloat.com/api/v1/me"
API_TRADES = "https://csfloat.com/api/v1/me/trades?state=queued,pending&limit=500"
API_ACCEPT_TRADE = "https://csfloat.com/api/v1/trades/{trade_id}/accept"  # Define the accept trade endpoint

# Path to a file to save cookies, will be created at end of a script run if do not exist
COOKIE_FILE = Path("cookies.json")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0"

# Path to store processed trade IDs
PROCESSED_TRADES_FILE = Path("processed_trades.json")

INCOMING_TRADES_LOG_FILE = Path("incoming_trades_log.json")
INCOMING_TRADES_IGNORED_FILE = Path("incoming_trades_ignored.json")
SELLER_PENDING_VERIFICATION_LOG_FILE = Path("seller_sent_pending_verification.json")
TG_CONFIG_FILE = Path("tg.json")

def load_steam_config(config_path='steam.json'):
    with open(config_path, 'r') as file:
        return json.load(file)


def load_tg_config():
    """
    Поддерживаем оба варианта ключей:
    - bot_token/chat_id
    - api_key/user_id (как в другом проекте)
    """
    if not TG_CONFIG_FILE.is_file():
        return None

    with TG_CONFIG_FILE.open("r", encoding="utf-8") as f:
        try:
            cfg = json.load(f)
        except json.JSONDecodeError:
            print("tg.json is invalid JSON. Telegram notifications disabled.")
            return None

    if not isinstance(cfg, dict):
        return None

    token = cfg.get("bot_token") or cfg.get("api_key")
    chat_id = cfg.get("chat_id") or cfg.get("user_id")
    if not token or not chat_id:
        print("tg.json missing bot_token/api_key or chat_id/user_id. Telegram notifications disabled.")
        return None
    return {"token": str(token), "chat_id": str(chat_id)}


async def send_telegram_notification(text: str):
    cfg = load_tg_config()
    if not cfg:
        return

    url = f"https://api.telegram.org/bot{cfg['token']}/sendMessage"
    payload = {
        "chat_id": cfg["chat_id"],
        "text": text,
        "disable_web_page_preview": True,
    }

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            async with aiohttp.ClientSession() as tg_session:
                async with tg_session.post(url, json=payload) as resp:
                    if resp.status == 200:
                        return

                    if resp.status in (429, 500, 502, 503, 504) and attempt < max_attempts:
                        await asyncio.sleep(2 * attempt)
                        continue

                    detail = await resp.text()
                    print(f"Telegram notify failed: {resp.status} {detail}")
                    return
        except Exception as e:
            if attempt < max_attempts:
                await asyncio.sleep(2 * attempt)
                continue
            print(f"Telegram notify error: {e}")
            return

def load_processed_trades():
    if PROCESSED_TRADES_FILE.is_file():
        with PROCESSED_TRADES_FILE.open("r") as f:
            try:
                return set(json.load(f))  # trade_id остаются строками
            except json.JSONDecodeError:

                return set()
    return set()

def save_processed_trades(processed_trades):
    with PROCESSED_TRADES_FILE.open("w") as f:
        json.dump(list(processed_trades), f, indent=2)

def load_incoming_trades_log():
    """
    Лог принятых входящих Steam-трейдов (когда нам отправляют скин, а с нашей стороны ничего).
    Храним список записей, а для дедупликации используем offer_id.
    """
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
    # upsert by offer_id
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
    """Если оффер уже не active, но покупатель принял — ищем в недавней истории (несколько страниц)."""
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
    """
    Проверяет, что оффер по этому asset_id уже был отправлен этому покупателю:
    - сначала активные исходящие,
    - затем недавняя история (accepted/escrow).
    """
    active_id = await _find_active_sent_offer_giving_asset(client, buyer_id, asset_id)
    if active_id is not None:
        return active_id, TradeOfferStatus.ACTIVE
    hist_id, hist_status = await _find_recent_historical_sent_accepted_for_asset(client, buyer_id, asset_id)
    if hist_id is not None:
        return hist_id, hist_status or TradeOfferStatus.ACCEPTED
    return None, None


def _synthetic_trade_from_pending_entry(entry: dict) -> dict:
    """Минимальный объект «трейда» для опроса Steam, когда CSFloat сейчас не отдаёт список (actionable_trades=0)."""
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
    """Обновляет JSON даже без actionable_trades на CSFloat (только Steam + поля из файла)."""
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
    """
    True — не слать новый Steam-оффер: уже есть активный, или покупатель принял / эскроу.
    Обновляет запись в seller_sent_pending_verification.json (state, note, steam_offer_id).
    """
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
            upsert_seller_pending_verification({
                **base_update,
                "steam_offer_id": active_id,
                "steam_offer_status": "ACTIVE",
                "note": "Оффер в Steam активен, ждём принятия покупателем; затем verified на CSFloat",
            })
            return True
        hist_id, hist_status = await _find_recent_historical_sent_accepted_for_asset(
            client, buyer_id, asset_id_int
        )
        if hist_id is not None:
            upsert_seller_pending_verification({
                **base_update,
                "steam_offer_id": hist_id,
                "steam_offer_status": hist_status.name if hist_status else "ACCEPTED",
                "note": "Покупатель принял в Steam (или эскроу); ждём verified на CSFloat",
            })
            return True
        upsert_seller_pending_verification({
            **base_update,
            "note": (
                "В активных исходящих и недавней истории Steam оффер по buyer+asset не найден "
                "(возможно очень старая сделка или сменился asset_id). Проверьте вручную на CSFloat/Steam."
            ),
        })
        return True

    try:
        offer = await client.get_trade_offer(steam_oid)
    except Exception:
        upsert_seller_pending_verification({
            **base_update,
            "steam_offer_id": steam_oid,
            "note": "Не удалось запросить статус Steam-оффера; следующая проверка повторит",
        })
        return True

    st = offer.status
    if st in (TradeOfferStatus.ACTIVE, TradeOfferStatus.CONFIRMATION_NEED):
        upsert_seller_pending_verification({
            **base_update,
            "steam_offer_id": steam_oid,
            "steam_offer_status": st.name,
            "note": "Оффер в Steam активен / ждёт подтверждения; ждём покупателя и verified на CSFloat",
        })
        return True
    if st in (TradeOfferStatus.ACCEPTED, TradeOfferStatus.STATE_IN_ESCROW):
        upsert_seller_pending_verification({
            **base_update,
            "steam_offer_id": steam_oid,
            "steam_offer_status": st.name,
            "note": "Покупатель принял в Steam (или эскроу); ждём verified на CSFloat",
        })
        return True
    if st in (
        TradeOfferStatus.DECLINED,
        TradeOfferStatus.CANCELED,
        TradeOfferStatus.EXPIRED,
        TradeOfferStatus.INVALID,
        TradeOfferStatus.INVALID_ITEMS,
        TradeOfferStatus.CANCELED_BY_SECONDARY_FACTOR,
    ):
        upsert_seller_pending_verification({
            **base_update,
            "steam_offer_id": None,
            "steam_offer_status": st.name,
            "note": f"Steam-оффер завершён со статусом {st.name}; можно отправить снова",
        })
        return False

    upsert_seller_pending_verification({
        **base_update,
        "steam_offer_id": steam_oid,
        "steam_offer_status": st.name,
        "note": f"Steam-оффер: {st.name}",
    })
    return True


def _extract_item_name_and_price(trade: dict) -> tuple[str, str]:
    """
    Достаёт из ответа CSFloat имя предмета и цену для логов.
    Цена в API приходит в центах; в лог выводится уже пересчитанная сумма (/ 100).
    """
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
        # CSFloat отдаёт цену в центах (minor units) — всегда делим на 100 для отображения в долларах/единицах валюты.
        price_text = f"{float(raw_price) / 100:.2f} {currency}"
    else:
        try:
            cents = float(str(raw_price).strip())
            price_text = f"{cents / 100:.2f} {currency}"
        except ValueError:
            price_text = str(raw_price)

    return item_name, price_text

async def get_user_info(session, csfloat_api_key):
    headers = {'Authorization': csfloat_api_key}
    try:
        async with session.get(API_USER_INFO, headers=headers) as response:
            response.raise_for_status()
            return await response.json()
    except aiohttp.ClientResponseError as http_err:
        print(f"HTTP error occurred while fetching user info: {http_err}")
    except Exception as err:
        print(f"Other error occurred while fetching user info: {err}")
    return None

async def get_trades(session, csfloat_api_key):
    headers = {'Authorization': csfloat_api_key}
    try:
        async with session.get(API_TRADES, headers=headers) as response:
            response.raise_for_status()
            trades_data = await response.json()
            return trades_data
    except aiohttp.ClientResponseError as http_err:
        print(f"HTTP error occurred while fetching trades: {http_err}")
    except Exception as err:
        print(f"Other error occurred while fetching trades: {err}")
    return None

async def accept_trade(session, csfloat_api_key, trade_id, trade_token):
    url = API_ACCEPT_TRADE.format(trade_id=trade_id)
    headers = {
        'Authorization': csfloat_api_key,
        'Content-Type': 'application/json'
    }
    payload = {
        'trade_token': trade_token  # Передача trade_token в тело запроса, если требуется API
    }
    try:
        async with session.post(url, headers=headers, json=payload) as response:
            if response.status != 200:
                # Логирование подробностей ошибки
                error_detail = await response.text()
                print(f"Failed to accept trade {trade_id}. Status: {response.status}, Detail: {error_detail}")
                return False
            result = await response.json()

            return True
    except aiohttp.ClientResponseError as http_err:
        print(f"HTTP error occurred while accepting trade {trade_id}: {http_err}")
    except Exception as err:
        print(f"Other error occurred while accepting trade {trade_id}: {err}")
    return False

# Количество попыток при ошибке 500 (Steam иногда возвращает Internal Server Error)
SEND_TRADE_MAX_RETRIES = 3
SEND_TRADE_RETRY_DELAY_SEC = 10

async def check_incoming_trade_offers(client: SteamGuardMixin):
    """
    Ищет входящие Steam-трейдофферы (received), где мы ничего не отдаём, но получаем предмет(ы),
    принимает их и пишет в отдельный лог: дата принятия + названия скинов.
    """
    try:
        existing_log = load_incoming_trades_log()
        ignored_log = load_incoming_trades_ignored()
        processed_offer_ids = {str(e.get("offer_id")) for e in existing_log if isinstance(e, dict)}
        processed_offer_ids.update(str(e.get("offer_id")) for e in ignored_log if isinstance(e, dict))
        _, received, _ = await client.get_trade_offers(active_only=True, sent=False, received=True)
    except Exception as e:
        print(f"An error occurred while checking incoming trade offers: {e}")
        return

    for offer in received:
        offer_id = getattr(offer, "trade_offer_id", None)
        if offer_id is None:
            continue

        if str(offer_id) in processed_offer_ids:
            continue

        items_to_give = getattr(offer, "items_to_give", []) or []
        items_to_receive = getattr(offer, "items_to_receive", []) or []

        # Принимаем только "входящие подарки": мы ничего не отдаём, но получаем предметы
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
                print(f"Failed to accept incoming trade offer {offer_id}: {http_err}")
                break
            except Exception as e:
                print(f"Failed to accept incoming trade offer {offer_id}: {e}")
                break

        if not accepted:
            # Если оффер уже завершён (отменён/отклонён/истёк), больше не пытаемся его принимать.
            try:
                fetched = await client.get_trade_offer(int(offer_id))
                status = fetched.status
                terminal = {
                    TradeOfferStatus.CANCELED,
                    TradeOfferStatus.DECLINED,
                    TradeOfferStatus.EXPIRED,
                    TradeOfferStatus.INVALID,
                    TradeOfferStatus.INVALID_ITEMS,
                    TradeOfferStatus.CANCELED_BY_SECONDARY_FACTOR,
                    TradeOfferStatus.TRADE_REVERSED,
                }
                if status in terminal:
                    append_incoming_trade_ignored({
                        "offer_id": int(offer_id),
                        "status": status.name,
                        "updated_at": datetime.now().isoformat(timespec="seconds"),
                        "note": "Incoming offer no longer active; stop retrying accept",
                    })
                    processed_offer_ids.add(str(offer_id))
            except Exception:
                pass
            continue

        item_names = []
        for it in items_to_receive:
            descr = getattr(it, "description", None)
            if descr is not None:
                name = getattr(descr, "market_hash_name", None) or getattr(descr, "market_name", None) or getattr(descr, "name", None)
            else:
                name = None
            item_names.append(name or str(getattr(it, "asset_id", "unknown")))

        append_incoming_trade_log({
            "offer_id": int(offer_id),
            "accepted_at": datetime.now().isoformat(timespec="seconds"),
            "items": item_names,
            "partner": getattr(offer, "partner_id64", None) or getattr(offer, "partner_id", None),
            "message": getattr(offer, "message", ""),
        })

        processed_offer_ids.add(str(offer_id))
        print(f"Accepted incoming trade offer {offer_id}: {', '.join(item_names)}")
        await send_telegram_notification(
            f"Accepted incoming Steam trade offer {offer_id}\nItems: {', '.join(item_names)}"
        )


async def send_steam_trade(client: SteamClient, trade_id, buyer_steam_id=None, trade_url=None, asset_id=None, trade_token=None):
    last_error = None
    for attempt in range(1, SEND_TRADE_MAX_RETRIES + 1):
        try:
            # Определение контекста игры, например, CS2
            game_context = AppContext.CS2

            # Получение вашего инвентаря (библиотека возвращает tuple из 3 элементов; при сбое может быть иначе)
            try:
                inv_result = await client.get_inventory(game_context)
                if not isinstance(inv_result, (tuple, list)) or len(inv_result) < 1:
                    print("Неожиданный формат ответа инвентаря (ожидался кортеж из 3 элементов).")
                    last_error = ValueError("invalid inventory result")
                    continue
                my_inv = inv_result[0] if len(inv_result) > 0 else []
            except (ValueError, IndexError) as e:
                print(f"Ошибка при разборе инвентаря (list index out of range или unpack): {e}")
                last_error = e
                if attempt < SEND_TRADE_MAX_RETRIES:
                    await asyncio.sleep(SEND_TRADE_RETRY_DELAY_SEC)
                continue

            # Проверка структуры предметов в инвентаре
            if not my_inv:
                print("Ваш инвентарь пуст или не удалось его загрузить.")
                return False

            # Попытка найти предмет по asset_id
            try:
                asset_id_int = int(asset_id)
                item_to_give = next((item for item in my_inv if item.asset_id == asset_id_int), None)
            except ValueError:
                item_to_give = next((item for item in my_inv if item.asset_id == asset_id), None)

            if not item_to_give:
                print(f"Предмет с asset_id {asset_id} не найден в инвентаре.")
                return False

            # Вызов make_trade_offer с использованием Steam ID или Trade URL
            if trade_url:
                offer_id = await client.make_trade_offer(
                    trade_url,
                    to_give=[item_to_give],
                    to_receive=[],
                    message=""
                )
            elif buyer_steam_id:
                if trade_token:
                    offer_id = await client.make_trade_offer(
                        buyer_steam_id,
                        to_give=[item_to_give],
                        to_receive=[],
                        message="",
                        token=trade_token
                    )
                else:
                    offer_id = await client.make_trade_offer(
                        buyer_steam_id,
                        to_give=[item_to_give],
                        to_receive=[],
                        message=""
                    )
            else:
                print("Необходимо указать либо buyer_steam_id, либо trade_url.")
                return False

            if offer_id:
                print(f"Торговое предложение {trade_id} отправлено!")
                return offer_id
            else:
                print("Не удалось отправить торговое предложение.")
                return False

        except aiohttp.ClientResponseError as http_err:
            last_error = http_err
            print(f"HTTP error occurred while sending trade offer (attempt {attempt}/{SEND_TRADE_MAX_RETRIES}): {http_err}")
            if http_err.status == 500 and attempt < SEND_TRADE_MAX_RETRIES:
                print(f"Повтор через {SEND_TRADE_RETRY_DELAY_SEC} сек из-за ошибки 500 Steam...")
                await asyncio.sleep(SEND_TRADE_RETRY_DELAY_SEC)
            else:
                return False
        except IndexError as e:
            last_error = e
            print(f"List index out of range while sending trade offer (attempt {attempt}/{SEND_TRADE_MAX_RETRIES}): {e}")
            if attempt < SEND_TRADE_MAX_RETRIES:
                await asyncio.sleep(SEND_TRADE_RETRY_DELAY_SEC)
            else:
                return False
        except Exception as e:
            last_error = e
            print(f"An error occurred while sending trade offer (attempt {attempt}/{SEND_TRADE_MAX_RETRIES}): {e}")
            if attempt < SEND_TRADE_MAX_RETRIES:
                await asyncio.sleep(SEND_TRADE_RETRY_DELAY_SEC)
            else:
                return False
    return False

# Функция подтверждения трейдов, если требуется
async def confirm_trade(client: SteamGuardMixin):
    try:
        confirmations = await client.get_confirmations()

        if not confirmations:
            print("No pending confirmations.")
            return

        for confirmation in confirmations:
            confirmation_key, timestamp = await client._gen_confirmation_key(tag="conf")

            # Подтверждение трейда
            result = await client.confirm_confirmation(confirmation, confirmation_key, timestamp)
            if result:
                print(f"Successfully confirmed trade offer {confirmation.offer_id}")
            else:
                print(f"Failed to confirm trade offer {confirmation.offer_id}")

    except Exception as e:
        print(f"An error occurred while confirming trades: {e}")

async def check_actionable_trades(session, csfloat_api_key, client: SteamGuardMixin, shared_secret, identity_secret, processed_trades, check_interval_minutes):
    # Сначала обрабатываем входящие Steam-трейды (buy order: продавец присылает скин, мы ничего не отдаём).
    await check_incoming_trade_offers(client)

    # Обновляем seller_sent_pending_verification.json по Steam даже если CSFloat сейчас не показывает actionable_trades.
    await refresh_all_seller_pending_verification_from_steam(client)

    user_info = await get_user_info(session, csfloat_api_key)

    if user_info and user_info.get('actionable_trades', 0) > 0:
        print("Actionable trades found, fetching trade details...")
        trades_info = await get_trades(session, csfloat_api_key)

        if isinstance(trades_info, dict):
            trades_list = trades_info.get('trades', [])

            if isinstance(trades_list, list):
                seller_pending_by_id = load_seller_pending_by_trade_id()
                seller_pending_csfloat_ids = set(seller_pending_by_id.keys())
                stats = {
                    "total": 0,
                    "seller_sent": 0,
                    "seller_waiting_verified": 0,
                    "buyer_waiting_incoming": 0,
                    "failed": 0,
                }
                for trade in trades_list:
                    if isinstance(trade, dict):
                        stats["total"] += 1
                        trade_id = trade.get('id')

                        seller_id = trade.get('seller_id')  # ID отправителя
                        buyer_id = trade.get('buyer_id')    # ID получателя
                        asset_id = trade.get('contract', {}).get('item', {}).get('asset_id')
                        item_name, sale_price = _extract_item_name_and_price(trade)
                        trade_token = trade.get('trade_token')  # Получаем trade_token
                        trade_url = trade.get('trade_url')      # Получаем trade_url
                        accepted_at = trade.get('accepted_at')  # Получаем время принятия, если есть
                        trade_state = trade.get('state')        # Получаем состояние трейда


                        if trade_state == "verified":
                            # Если трейд уже подтвержден, добавляем его в обработанные и пропускаем
                            processed_trades.add(str(trade_id))
                            remove_seller_pending_verification(trade_id)

                            continue

                        if trade_id and seller_id and buyer_id and asset_id:
                            my_steam_id64 = int(client.steam_id)
                            try:
                                sid = int(seller_id)
                                bid = int(buyer_id)
                            except (TypeError, ValueError):
                                print(f"Trade {trade_id}: некорректные seller_id/buyer_id, пропуск.")
                                continue

                            pending_entry = seller_pending_by_id.get(str(trade_id))
                            if pending_entry and my_steam_id64 == sid and my_steam_id64 != bid:
                                # Подробная Steam-синхронизация уже сделана выше в refresh_all_seller_pending_verification_from_steam.
                                # Здесь просто не пытаемся слать оффер повторно.
                                processed_trades.add(str(trade_id))
                                stats["seller_waiting_verified"] += 1
                                continue

                            # Повторная попытка только для трейдов из seller_sent_pending_verification.json
                            # (Steam уже отправили, ждём verified на CSFloat). Иначе старые pending в API
                            # не заставляют заново слать офферы по давно закрытым сделкам.
                            if str(trade_id) in processed_trades:
                                if my_steam_id64 == bid and my_steam_id64 != sid:
                                    continue
                                if str(trade_id) not in seller_pending_csfloat_ids:
                                    continue
                                processed_trades.discard(str(trade_id))
                                print(
                                    f"Trade {trade_id}: state={trade_state}, повторная проверка "
                                    f"(ожидание verified на CSFloat после отправки в Steam)."
                                )

                            # Покупатель на CSFloat: предмет пришлёт продавец в Steam — мы не отправляем оффер из своего инвентаря.
                            # Входящие Steam-трейды обрабатываются в check_incoming_trade_offers().
                            if my_steam_id64 == bid and my_steam_id64 != sid:
                                # На стороне CSFloat принимать должен seller, покупатель ждёт входящий Steam-трейд.
                                # Поэтому не вызываем accept_trade (иначе 403 "you must be the seller to accept the trade").
                                processed_trades.add(str(trade_id))
                                stats["buyer_waiting_incoming"] += 1
                                continue

                            if my_steam_id64 != sid:
                                print(
                                    f"Trade {trade_id}: ваш Steam ID не совпадает с seller_id (вы не продавец этой сделки), пропуск."
                                )
                                continue

                            # Ниже — только роль продавца: принять на CSFloat и отправить предмет покупателю в Steam.
                            send_success = False
                            sent_offer_id = None
                            if accepted_at:
                                # Старые pending-трейды могут быть уже фактически отправлены ранее.
                                # Проверяем активный/исторический Steam-оффер и не шлём повторно.
                                try:
                                    existing_offer_id, existing_offer_status = await detect_existing_sent_offer_for_trade(
                                        client, int(buyer_id), int(asset_id)
                                    )
                                except Exception:
                                    existing_offer_id, existing_offer_status = None, None

                                if existing_offer_id:
                                    send_success = True
                                    sent_offer_id = int(existing_offer_id)
                                    upsert_seller_pending_verification({
                                        "trade_id": str(trade_id),
                                        "item_name": item_name,
                                        "sale_price": sale_price,
                                        "asset_id": str(asset_id),
                                        "buyer_id": str(buyer_id),
                                        "steam_offer_id": int(existing_offer_id),
                                        "csfloat_state": trade_state or "unknown",
                                        "steam_offer_status": existing_offer_status.name,
                                        "last_checked_at": datetime.now().isoformat(timespec="seconds"),
                                        "note": "Оффер уже отправлялся ранее; ждём verified на CSFloat",
                                    })
                                else:
                                # Предложение уже принято, отправляем торговое предложение
                                    print(
                                        f"Trade {trade_id} уже принято. Отправка '{item_name}' за {sale_price}."
                                    )
                                    offer_id = await send_steam_trade(
                                        client,
                                        trade_id=str(trade_id),
                                        buyer_steam_id=int(buyer_id),
                                        asset_id=int(asset_id),
                                        trade_token=trade_token,
                                        trade_url=trade_url
                                    )
                                    send_success = bool(offer_id)
                                    sent_offer_id = int(offer_id) if offer_id else None
                                    if offer_id:
                                        print(
                                            f"Trade {trade_id}: отправка подтверждена для '{item_name}' за {sale_price}."
                                        )
                                        await confirm_trade(client)
                                        await send_telegram_notification(
                                            f"CSFloat sale sent\nTrade: {trade_id}\nItem: {item_name}\nPrice: {sale_price}"
                                        )
                                    else:
                                        # После неудачи проверяем, не отправляли ли уже ранее.
                                        try:
                                            existing_offer_id, existing_offer_status = await detect_existing_sent_offer_for_trade(
                                                client, int(buyer_id), int(asset_id)
                                            )
                                        except Exception:
                                            existing_offer_id, existing_offer_status = None, None
                                        if existing_offer_id:
                                            send_success = True
                                            sent_offer_id = int(existing_offer_id)
                                            upsert_seller_pending_verification({
                                                "trade_id": str(trade_id),
                                                "item_name": item_name,
                                                "sale_price": sale_price,
                                                "asset_id": str(asset_id),
                                                "buyer_id": str(buyer_id),
                                                "steam_offer_id": int(existing_offer_id),
                                                "csfloat_state": trade_state or "unknown",
                                                "steam_offer_status": existing_offer_status.name,
                                                "last_checked_at": datetime.now().isoformat(timespec="seconds"),
                                                "note": "Оффер уже отправлялся ранее; ждём verified на CSFloat",
                                            })
                                        else:
                                            print(f"Failed to send trade for {trade_id}")
                            else:
                                # Предложение ещё не принято, принимаем его
                                print(f"Accepting trade {trade_id} ({item_name}, {sale_price})...")
                                accept_result = await accept_trade(session, csfloat_api_key, trade_id=str(trade_id), trade_token=trade_token)

                                if accept_result:
                                    print(f"Sending '{item_name}' to buyer for trade {trade_id} ({sale_price})...")
                                    offer_id = await send_steam_trade(
                                        client,
                                        trade_id=str(trade_id),
                                        buyer_steam_id=int(buyer_id),
                                        asset_id=int(asset_id),
                                        trade_token=trade_token,
                                        trade_url=trade_url
                                    )
                                    send_success = bool(offer_id)
                                    sent_offer_id = int(offer_id) if offer_id else None
                                    if offer_id:
                                        print(
                                            f"Trade {trade_id}: отправка подтверждена для '{item_name}' за {sale_price}."
                                        )
                                        await confirm_trade(client)
                                        await send_telegram_notification(
                                            f"CSFloat sale sent\nTrade: {trade_id}\nItem: {item_name}\nPrice: {sale_price}"
                                        )
                                    else:
                                        print(f"Failed to send trade for {trade_id}")
                                else:
                                    print(f"Failed to accept trade {trade_id}")
                                    stats["failed"] += 1

                            # Помечаем как обработанный только при успешной отправке предложения в Steam.
                            # При ошибке отправки не помечаем — при следующей проверке будет повторная попытка.
                            if send_success:
                                processed_trades.add(str(trade_id))
                                stats["seller_sent"] += 1
                                upsert_seller_pending_verification({
                                    "trade_id": str(trade_id),
                                    "item_name": item_name,
                                    "sale_price": sale_price,
                                    "asset_id": str(asset_id),
                                    "buyer_id": str(buyer_id),
                                    "steam_offer_id": sent_offer_id,
                                    "sent_at": datetime.now().isoformat(timespec="seconds"),
                                    "csfloat_state": trade_state or "unknown",
                                    "steam_offer_status": "ACTIVE",
                                    "note": "Оффер отправлен в Steam; ждём покупателя и verified на CSFloat",
                                })
                            else:
                                print(f"Trade {trade_id} не помечен как обработанный — повторная попытка при следующей проверке.")
                                stats["failed"] += 1

                print(
                    "Trade pass summary: "
                    f"total={stats['total']}, sent={stats['seller_sent']}, "
                    f"waiting_verified={stats['seller_waiting_verified']}, "
                    f"buyer_waiting_incoming={stats['buyer_waiting_incoming']}, "
                    f"failed={stats['failed']}"
                )

            else:
                print(f"Unexpected trades list format: {type(trades_list)}")
        else:
            print(f"Unexpected trades data format: {type(trades_info)}")
    else:
        print(f"No actionable trades at the moment. Waiting for {check_interval_minutes} minutes before next check.")

async def main():
    config = load_steam_config()  # Загрузка конфигурации

    csfloat_api_key = config['csfloat_api_key']
    steam_api_key = config['steam_api_key']
    steam_id = int(config['steam_id64'])  # Убедитесь, что это целое число
    steam_login = config['steam_login']
    steam_password = config['steam_password']
    shared_secret = config['shared_secret']
    identity_secret = config['identity_secret']

    # Определение продолжительности ожидания (в минутах)
    CHECK_INTERVAL_MINUTES = 25  # Вы можете легко изменить это значение

    # Инициализация SteamClient с необходимыми аргументами
    class MySteamClient(SteamClient, SteamWebApiMixin, SteamGuardMixin):
        pass

    client = MySteamClient(
        steam_id=steam_id,              # Steam ID64 как целое число
        username=steam_login,
        password=steam_password,
        shared_secret=shared_secret,
        identity_secret=identity_secret,
        api_key=steam_api_key,          # Передача API ключа
        user_agent=USER_AGENT,
    )

    # Восстановление cookies, если они существуют
    if COOKIE_FILE.is_file():
        with COOKIE_FILE.open("r") as f:
            cookies = json.load(f)
        await restore_from_cookies(cookies, client)
    else:
        await client.login()

    # Загрузка обработанных трейдов
    processed_trades = load_processed_trades()
    
    async with aiohttp.ClientSession() as session:
        try:
            while True:
                await check_actionable_trades(
                    session,
                    csfloat_api_key,
                    client,
                    shared_secret,
                    identity_secret,
                    processed_trades,           # Передача набора обработанных трейдов
                    CHECK_INTERVAL_MINUTES      # Передача продолжительности ожидания
                )
                save_processed_trades(processed_trades)  # Сохранение после каждой проверки
                await asyncio.sleep(CHECK_INTERVAL_MINUTES * 60)  # Ожидание заданное количество минут
        finally:
            # Сохранение cookies
            with COOKIE_FILE.open("w") as f:
                json.dump(get_jsonable_cookies(client.session), f, indent=2)

            await client.session.close()

if __name__ == "__main__":
    asyncio.run(main())
