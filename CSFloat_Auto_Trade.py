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

def load_steam_config(config_path='steam.json'):
    with open(config_path, 'r') as file:
        return json.load(file)

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
        processed_offer_ids = {str(e.get("offer_id")) for e in existing_log if isinstance(e, dict)}

        _, received, _ = await client.get_trade_offers(active_only=True, sent=False, received=True)

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

            # Принимаем оффер
            await client.accept_trade_offer(offer)

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

    except Exception as e:
        print(f"An error occurred while checking incoming trade offers: {e}")


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

    user_info = await get_user_info(session, csfloat_api_key)

    if user_info and user_info.get('actionable_trades', 0) > 0:
        print("Actionable trades found, fetching trade details...")
        trades_info = await get_trades(session, csfloat_api_key)

        if isinstance(trades_info, dict):
            trades_list = trades_info.get('trades', [])

            if isinstance(trades_list, list):
                for trade in trades_list:
                    if isinstance(trade, dict):
                        trade_id = trade.get('id')

                        # Проверка, был ли уже обработан этот trade_id
                        if str(trade_id) in processed_trades:
                            print(f"Trade {trade_id} has already been processed. Skipping.")
                            continue  # Пропустить уже обработанные трейды

                        seller_id = trade.get('seller_id')  # ID отправителя
                        buyer_id = trade.get('buyer_id')    # ID получателя
                        asset_id = trade.get('contract', {}).get('item', {}).get('asset_id')
                        trade_token = trade.get('trade_token')  # Получаем trade_token
                        trade_url = trade.get('trade_url')      # Получаем trade_url
                        accepted_at = trade.get('accepted_at')  # Получаем время принятия, если есть
                        trade_state = trade.get('state')        # Получаем состояние трейда


                        if trade_state == "verified":
                            # Если трейд уже подтвержден, добавляем его в обработанные и пропускаем
                            processed_trades.add(str(trade_id))

                            continue

                        if trade_id and seller_id and buyer_id and asset_id:
                            send_success = False
                            if accepted_at:
                                # Предложение уже принято, отправляем торговое предложение
                                print(f"Trade {trade_id} уже принято. Переходим к отправке торгового предложения.")
                                offer_id = await send_steam_trade(
                                    client,
                                    trade_id=str(trade_id),
                                    buyer_steam_id=int(buyer_id),
                                    asset_id=int(asset_id),
                                    trade_token=trade_token,
                                    trade_url=trade_url
                                )
                                send_success = bool(offer_id)
                                if offer_id:
                                    await confirm_trade(client)
                                else:
                                    print(f"Failed to send trade for {trade_id}")
                            else:
                                # Предложение ещё не принято, принимаем его
                                print(f"Accepting trade {trade_id}...")
                                accept_result = await accept_trade(session, csfloat_api_key, trade_id=str(trade_id), trade_token=trade_token)

                                if accept_result:
                                    print(f"Sending item to buyer for trade {trade_id}...")
                                    offer_id = await send_steam_trade(
                                        client,
                                        trade_id=str(trade_id),
                                        buyer_steam_id=int(buyer_id),
                                        asset_id=int(asset_id),
                                        trade_token=trade_token,
                                        trade_url=trade_url
                                    )
                                    send_success = bool(offer_id)
                                    if offer_id:
                                        await confirm_trade(client)
                                    else:
                                        print(f"Failed to send trade for {trade_id}")
                                else:
                                    print(f"Failed to accept trade {trade_id}")

                            # Помечаем как обработанный только при успешной отправке предложения в Steam.
                            # При ошибке отправки не помечаем — при следующей проверке будет повторная попытка.
                            if send_success:
                                processed_trades.add(str(trade_id))
                            else:
                                print(f"Trade {trade_id} не помечен как обработанный — повторная попытка при следующей проверке.")
                        
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
