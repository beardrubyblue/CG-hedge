import asyncio
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import Config
from exchange_handler import HyperliquidHandler
from monitor import PriceMonitor
from database import get_user_config, update_wallet_config, delete_wallet, get_user_status, set_user_status

# Инициализация логов
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Состояния для FSM
class BotState(StatesGroup):
    waiting_for_wallet_name = State()
    waiting_for_address = State()
    waiting_for_api_secret = State()
    # Settings
    waiting_for_price = State()
    waiting_for_amount = State()
    waiting_for_sl = State()
    waiting_for_profit = State()
    waiting_for_deposit = State()

# Объекты API
bot = Bot(token=Config.TELEGRAM_TOKEN)
dp = Dispatcher()

# Глобальный словарь активных мониторов: {wallet_name: monitor}
active_monitors = {}

# --- Клавиатуры ---

def get_main_keyboard():
    buttons = [
        [
            InlineKeyboardButton(text="💼 Мои кошельки", callback_data="list_wallets"),
            InlineKeyboardButton(text="➕ Добавить кошелек", callback_data="add_wallet")
        ],
        [InlineKeyboardButton(text="📊 Общий статус", callback_data="general_status")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_cancel_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="cancel_action")]])

def get_back_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")]])

def get_wallets_keyboard(user_id):
    config = get_user_config(user_id)
    buttons = []
    for w_name in config["wallets"]:
        status = "🟢" if w_name in active_monitors else "🔴"
        buttons.append([InlineKeyboardButton(text=f"{status} {w_name}", callback_data=f"manage_{w_name}")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_wallet_manage_keyboard(wallet_name, is_running, user_id):
    buttons = [
        [InlineKeyboardButton(text="🚀 Запустить" if not is_running else "🛑 Остановить", callback_data=f"toggle_{wallet_name}")],
    ]
    
    # Кнопка 'Тестовый триггер' только для админа
    if str(user_id) == str(Config.ADMIN_ID):
        buttons.append([InlineKeyboardButton(text="🧪 Тестовый триггер (Админ)", callback_data=f"test_{wallet_name}")])
        
    buttons.extend([
        [InlineKeyboardButton(text="⚙️ Настройки параметров", callback_data=f"params_{wallet_name}")],
        [InlineKeyboardButton(text="🔑 Обновить ключи", callback_data=f"keys_{wallet_name}")],
        [InlineKeyboardButton(text="🗑 Удалить кошелек", callback_data=f"delete_{wallet_name}")],
        [InlineKeyboardButton(text="◀️ К списку", callback_data="list_wallets")]
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_params_keyboard(wallet_name):
    buttons = [
        [InlineKeyboardButton(text="🎯 Цена триггера", callback_data=f"edit_price_{wallet_name}")],
        [InlineKeyboardButton(text="💰 Объем (USDC)", callback_data=f"edit_amount_{wallet_name}")],
        [InlineKeyboardButton(text="🛡 SL Offset", callback_data=f"edit_sl_{wallet_name}")],
        [InlineKeyboardButton(text="💵 Опц.Прибыль (P)", callback_data=f"edit_profit_{wallet_name}")],
        [InlineKeyboardButton(text="🏦 Опц.Депозит", callback_data=f"edit_deposit_{wallet_name}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"manage_{wallet_name}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_decision_keyboard(user_id):
    buttons = [
        [
            InlineKeyboardButton(text="✅ Принять", callback_data=f"approve_{user_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{user_id}")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# --- Уведомления ---

async def send_tg_notification(user_id, text: str):
    try:
        await bot.send_message(chat_id=user_id, text=text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Failed to send TG notification to {user_id}: {e}")

# --- Хендлеры ---

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    
    status = get_user_status(message.from_user.id)
    
    if status == "rejected":
        return # Полное игнорирование
    
    if status == "pending":
        await message.answer("Ваша заявка находится на рассмотрении у администратора.")
        return
        
    if status == "unknown":
        set_user_status(message.from_user.id, "pending")
        
        # Отправляем заявку админу
        user_info = f"Новая заявка на доступ!\nПользователь: @{message.from_user.username} (ID: {message.from_user.id}, Имя: {message.from_user.first_name})"
        try:
            await bot.send_message(Config.ADMIN_ID, user_info, reply_markup=get_admin_decision_keyboard(message.from_user.id))
        except Exception as e:
            logger.error(f"Failed to send admin notification: {e}")
            
        await message.answer("Заявка на доступ отправлена администратору. Ожидайте подтверждения.")
        return
        
    # Если approved:
    msg = await message.answer(
        "👋 Добро пожаловать! Это бот для мульти-аккаунт хеджирования на Hyperliquid.\n\n"
        "Вы можете добавить несколько кошельков и настраивать их независимо.",
        reply_markup=get_main_keyboard()
    )
    try: await message.delete()
    except: pass

@dp.callback_query(F.data.startswith("approve_"))
async def approve_user_cb(callback: CallbackQuery):
    if str(callback.from_user.id) != str(Config.ADMIN_ID):
        return
        
    target_id = callback.data.split("_")[1]
    set_user_status(target_id, "approved")
    
    await callback.message.edit_text(callback.message.text + "\n\n✅ **ПРИНЯТ**", parse_mode="Markdown")
    try:
        await bot.send_message(target_id, "✅ Ваш доступ подтвержден! Введите /start для начала работы.")
    except: pass

@dp.callback_query(F.data.startswith("reject_"))
async def reject_user_cb(callback: CallbackQuery):
    if str(callback.from_user.id) != str(Config.ADMIN_ID):
        return
        
    target_id = callback.data.split("_")[1]
    set_user_status(target_id, "rejected")
    
    await callback.message.edit_text(callback.message.text + "\n\n❌ **ОТКЛОНЕН**", parse_mode="Markdown")

@dp.callback_query(F.data == "back_to_main")
async def back_to_main_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "👋 Добро пожаловать! Это бот для мульти-аккаунт хеджирования на Hyperliquid.\n\n"
        "Вы можете добавить несколько кошельков и настраивать их независимо.",
        reply_markup=get_main_keyboard()
    )

@dp.callback_query(F.data == "cancel_action")
async def cancel_action_cb(callback: CallbackQuery, state: FSMContext):
    await back_to_main_cb(callback, state)

@dp.callback_query(F.data == "general_status")
async def general_status_cb(callback: CallbackQuery):
    if not active_monitors:
        await callback.message.edit_text(
            "🤷‍♂️ Ни один кошелек сейчас не запущен.\nПерейдите в '💼 Мои кошельки' и нажмите '🚀 Запустить' у нужных.",
            reply_markup=get_back_keyboard()
        )
        return

    text = "📊 **Статус активных кошельков:**\n\n"
    for w_name, monitor in active_monitors.items():
        is_running = "🟢 Запущен" if monitor.is_running else "🔴 Остановлен"
        hedge_status = "📉 ХЕДЖ ОТКРЫТ" if monitor.hedge_active else "⏳ Ожидает триггер"
        cfg = monitor.config
        
        text += (
            f"**{w_name}** | {is_running}\n"
            f"└ Статус позиции: `{hedge_status}`\n"
            f"└ Настроен на: `{cfg['trigger_price']} USD`\n"
            f"└ Объем: `{cfg['amount']} USDC`\n"
            f"└ Попытки (PnL): `{getattr(monitor, 'attempts', 0)}/{getattr(monitor, 'max_attempts', 6)}`\n\n"
        )
        
    await callback.message.edit_text(text, reply_markup=get_back_keyboard(), parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "list_wallets")
async def list_wallets_cb(callback: CallbackQuery):
    await callback.message.edit_text("Ваши кошельки:", reply_markup=get_wallets_keyboard(callback.from_user.id))

@dp.callback_query(F.data == "add_wallet")
async def add_wallet_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(BotState.waiting_for_wallet_name)
    await state.update_data(menu_msg_id=callback.message.message_id)
    await callback.message.edit_text("Введите название для кошелька (например, 'Main' или 'Client1'):", reply_markup=get_cancel_keyboard())

@dp.message(BotState.waiting_for_wallet_name)
async def wallet_name_chosen(message: Message, state: FSMContext):
    await state.update_data(wallet_name=message.text)
    data = await state.get_data()
    try: await message.delete()
    except: pass
    await state.set_state(BotState.waiting_for_address)
    try:
        await bot.edit_message_text(f"Принято: '{message.text}'. Теперь введите адрес кошелька (Public Address) от биржи:", chat_id=message.chat.id, message_id=data.get("menu_msg_id"), reply_markup=get_cancel_keyboard())
    except: pass

@dp.message(BotState.waiting_for_address)
async def wallet_address_chosen(message: Message, state: FSMContext):
    data = await state.get_data()
    try: await message.delete()
    except: pass
    if not message.text.startswith("0x") or len(message.text) != 42:
        try:
            await bot.edit_message_text("❌ Кажется, это невалидный адрес. Попробуйте еще раз (Public Address):", chat_id=message.chat.id, message_id=data.get("menu_msg_id"), reply_markup=get_cancel_keyboard())
        except: pass
        return
    await state.update_data(address=message.text)
    await state.set_state(BotState.waiting_for_api_secret)
    try:
        await bot.edit_message_text(f"Принято: `{message.text[:10]}...`\nТеперь введите API ключ (Agent Secret) от биржи:", chat_id=message.chat.id, message_id=data.get("menu_msg_id"), reply_markup=get_cancel_keyboard(), parse_mode="Markdown")
    except: pass

@dp.message(BotState.waiting_for_api_secret)
async def wallet_secret_chosen(message: Message, state: FSMContext):
    secret = message.text
    data = await state.get_data()
    try: await message.delete()
    except: pass
    
    wallet_name = data["wallet_name"]
    address = data["address"]
    
    update_wallet_config(
        message.from_user.id, 
        wallet_name, 
        address=address, 
        private_key="", 
        api_secret=secret
    )
    
    try:
        await bot.edit_message_text(f"✅ Кошелек успешно добавлен под именем `{wallet_name}`!\nТеперь вы можете им управлять.", chat_id=message.chat.id, message_id=data.get("menu_msg_id"), reply_markup=get_main_keyboard(), parse_mode="Markdown")
    except: pass
    await state.clear()

@dp.callback_query(F.data.startswith("manage_"))
async def manage_wallet(callback: CallbackQuery):
    w_name = callback.data.split("_")[1]
    is_running = w_name in active_monitors
    
    config = get_user_config(callback.from_user.id)["wallets"][w_name]
    text = (
        f"⚙️ **Управление: {w_name}**\n"
        f"Адрес: `{config['address'][:10]}...` \n"
        f"Статус: {'🟢 Активен' if is_running else '🔴 Остановлен'}\n\n"
        f"🎯 Триггер: {config['trigger_price']} USD\n"
        f"💰 Объем: {config['amount']} USDC\n"
        f"🛡 SL Offset: {config['sl_offset']}\n"
        f"💵 Опц.Прибыль (P): {config.get('option_profit', 1000)} USDC\n"
        f"🏦 Опц.Депозит: {config.get('option_deposit', 47250)} USDC"
    )
    await callback.message.edit_text(text, reply_markup=get_wallet_manage_keyboard(w_name, is_running, callback.from_user.id), parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data.startswith("toggle_"))
async def toggle_wallet(callback: CallbackQuery):
    w_name = callback.data.split("_")[1]
    
    if w_name in active_monitors:
        monitor = active_monitors.pop(w_name)
        monitor.stop()
        
        # Отменяем все ордера и закрываем позицию
        try:
            await callback.answer(f"🛑 Останавливаем '{w_name}' и очищаем биржу...")
            # Выполняем синхронные запросы к бирже
            monitor.hl.cancel_all_orders()
            if monitor.active_order_ids:
                for oid in list(monitor.active_order_ids):
                    monitor.hl.cancel_order_by_id("BTC", oid)
            monitor.hl.close_position()
            await bot.send_message(callback.from_user.id, f"🛑 Мониторинг '{w_name}' выключен.\n✅ Все отложенные ордера отменены.\n✅ Открытая позиция закрыта по маркету.")
        except Exception as e:
            await bot.send_message(callback.from_user.id, f"🛑 Мониторинг '{w_name}' выключен, но возникла ошибка при очистке: {e}")
            
    else:
        config = get_user_config(callback.from_user.id)["wallets"][w_name]
        try:
            handler = HyperliquidHandler(config["address"], config["private_key"], config["api_secret"])
            # Уведомления шлем админу или инициатору (можно инициатору)
            notify_func = lambda msg: send_tg_notification(callback.from_user.id, msg)
            monitor = PriceMonitor(handler, config, notify_func)
            asyncio.create_task(monitor.start())
            active_monitors[w_name] = monitor
            await callback.answer(f"🚀 Мониторинг '{w_name}' запущен!", show_alert=True)
        except Exception as e:
            await callback.answer(f"❌ Ошибка запуска '{w_name}': {e}", show_alert=True)
            
    await manage_wallet(callback)

@dp.callback_query(F.data.startswith("params_"))
async def params_wallet(callback: CallbackQuery):
    w_name = callback.data.split("_")[1]
    await callback.message.edit_text(f"Параметры '{w_name}':", reply_markup=get_params_keyboard(w_name))
    await callback.answer()

@dp.callback_query(F.data.startswith("edit_price_"))
async def edit_price_start(callback: CallbackQuery, state: FSMContext):
    w_name = callback.data.split("_")[-1]
    await state.set_state(BotState.waiting_for_price)
    await state.update_data(edit_wallet=w_name)
    await state.update_data(menu_msg_id=callback.message.message_id)
    await callback.message.edit_text(f"Введите новую цену триггера для '{w_name}':", reply_markup=get_cancel_keyboard())
    await callback.answer()

@dp.message(BotState.waiting_for_price)
async def edit_price_finish(message: Message, state: FSMContext):
    data = await state.get_data()
    try: await message.delete()
    except: pass
    try:
        val = float(message.text)
        update_wallet_config(message.from_user.id, data["edit_wallet"], trigger_price=val)
        try:
            await bot.edit_message_text(f"✅ Цена для '{data['edit_wallet']}' обновлена: {val}\n\nВозврат в меню...", chat_id=message.chat.id, message_id=data.get("menu_msg_id"), reply_markup=get_main_keyboard())
        except: pass
        await state.clear()
    except ValueError:
        try:
            await bot.edit_message_text(f"❌ '{message.text}' не является числом. Введите число для '{data['edit_wallet']}':", chat_id=message.chat.id, message_id=data.get("menu_msg_id"), reply_markup=get_cancel_keyboard())
        except: pass

@dp.callback_query(F.data.startswith("edit_amount_"))
async def edit_amount_start(callback: CallbackQuery, state: FSMContext):
    w_name = callback.data.split("_")[-1]
    await state.set_state(BotState.waiting_for_amount)
    await state.update_data(edit_wallet=w_name)
    await state.update_data(menu_msg_id=callback.message.message_id)
    await callback.message.edit_text(f"Введите новый объем в USDC для '{w_name}' (мин 10-15$):", reply_markup=get_cancel_keyboard())
    await callback.answer()

@dp.message(BotState.waiting_for_amount)
async def edit_amount_finish(message: Message, state: FSMContext):
    data = await state.get_data()
    try: await message.delete()
    except: pass
    try:
        val = float(message.text)
        update_wallet_config(message.from_user.id, data["edit_wallet"], amount=val)
        try:
            await bot.edit_message_text(f"✅ Объем для '{data['edit_wallet']}' обновлен: {val}\n\nВозврат в меню...", chat_id=message.chat.id, message_id=data.get("menu_msg_id"), reply_markup=get_main_keyboard())
        except: pass
        await state.clear()
    except ValueError:
        try:
            await bot.edit_message_text(f"❌ Ошибка. Введите число для объема (USDC):", chat_id=message.chat.id, message_id=data.get("menu_msg_id"), reply_markup=get_cancel_keyboard())
        except: pass

@dp.callback_query(F.data.startswith("edit_sl_"))
async def edit_sl_start(callback: CallbackQuery, state: FSMContext):
    w_name = callback.data.split("_")[-1]
    await state.set_state(BotState.waiting_for_sl)
    await state.update_data(edit_wallet=w_name)
    await state.update_data(menu_msg_id=callback.message.message_id)
    await callback.message.edit_text(f"Введите новое смещение SL для '{w_name}' (в USD от цены точки):", reply_markup=get_cancel_keyboard())
    await callback.answer()

@dp.message(BotState.waiting_for_sl)
async def edit_sl_finish(message: Message, state: FSMContext):
    data = await state.get_data()
    try: await message.delete()
    except: pass
    try:
        val = float(message.text)
        update_wallet_config(message.from_user.id, data["edit_wallet"], sl_offset=val)
        try:
            await bot.edit_message_text(f"✅ SL Offset для '{data['edit_wallet']}' обновлен: {val}\n\nВозврат в меню...", chat_id=message.chat.id, message_id=data.get("menu_msg_id"), reply_markup=get_main_keyboard())
        except: pass
        await state.clear()
    except ValueError:
        try:
            await bot.edit_message_text(f"❌ Ошибка. Введите число для SL:", chat_id=message.chat.id, message_id=data.get("menu_msg_id"), reply_markup=get_cancel_keyboard())
        except: pass

@dp.callback_query(F.data.startswith("edit_profit_"))
async def edit_profit_start(callback: CallbackQuery, state: FSMContext):
    w_name = callback.data.split("_")[-1]
    await state.set_state(BotState.waiting_for_profit)
    await state.update_data(edit_wallet=w_name)
    await state.update_data(menu_msg_id=callback.message.message_id)
    await callback.message.edit_text(f"Введите новую прибыль опциона (P) для '{w_name}' в USDC:\nЭто значение будет определять лимит убытка (20% от P).", reply_markup=get_cancel_keyboard())
    await callback.answer()

@dp.message(BotState.waiting_for_profit)
async def edit_profit_finish(message: Message, state: FSMContext):
    data = await state.get_data()
    try: await message.delete()
    except: pass
    try:
        val = float(message.text)
        update_wallet_config(message.from_user.id, data["edit_wallet"], option_profit=val)
        try:
            await bot.edit_message_text(f"✅ Прибыль опциона (P) обновлена: {val} USDC\nЛимит одной просадки теперь: {val * 0.2} USDC\n\nВозврат в меню...", chat_id=message.chat.id, message_id=data.get("menu_msg_id"), reply_markup=get_main_keyboard())
        except: pass
        await state.clear()
    except ValueError:
        try:
            await bot.edit_message_text(f"❌ '{message.text}' не является числом. Введите число для прибыли:", chat_id=message.chat.id, message_id=data.get("menu_msg_id"), reply_markup=get_cancel_keyboard())
        except: pass

@dp.callback_query(F.data.startswith("edit_deposit_"))
async def edit_deposit_start(callback: CallbackQuery, state: FSMContext):
    w_name = callback.data.split("_")[-1]
    await state.set_state(BotState.waiting_for_deposit)
    await state.update_data(edit_wallet=w_name)
    await state.update_data(menu_msg_id=callback.message.message_id)
    await callback.message.edit_text(f"Введите сумму депозита под опцион для '{w_name}' в USDC:", reply_markup=get_cancel_keyboard())
    await callback.answer()

@dp.message(BotState.waiting_for_deposit)
async def edit_deposit_finish(message: Message, state: FSMContext):
    data = await state.get_data()
    try: await message.delete()
    except: pass
    try:
        val = float(message.text)
        update_wallet_config(message.from_user.id, data["edit_wallet"], option_deposit=val)
        try:
            await bot.edit_message_text(f"✅ Депозит опциона обновлен: {val} USDC\n\nВозврат в меню...", chat_id=message.chat.id, message_id=data.get("menu_msg_id"), reply_markup=get_main_keyboard())
        except: pass
        await state.clear()
    except ValueError:
        try:
            await bot.edit_message_text(f"❌ Ошибка. Введите число для депозита:", chat_id=message.chat.id, message_id=data.get("menu_msg_id"), reply_markup=get_cancel_keyboard())
        except: pass

@dp.callback_query(F.data.startswith("test_"))
async def test_order_cb(callback: CallbackQuery):
    w_name = callback.data.split("_")[1]
    u_id = str(callback.from_user.id)
    config = get_user_config(u_id)["wallets"].get(w_name)
    
    if not config:
        await callback.answer("Кошелек не найден.", show_alert=True)
        return
        
    await callback.message.edit_text(f"🔄 Проверка ключей и тестовое выставление ордера для {w_name}...\n\n(Выставляется по рыночной цене)")
    
    try:
        # Проверяем, хватит ли объема на минимально (мин обьем обычно зависит от монеты, для BTC это ~$10)
        test_amount = 15.0 if config["amount"] < 15.0 else config["amount"]
        
        handler = HyperliquidHandler(config["address"], config["private_key"], config["api_secret"])
        current_price = handler.get_market_price("BTC")
        if not current_price:
            await callback.message.edit_text("❌ Ошибка: Не удалось получить текущую цену BTC с биржи.", reply_markup=get_wallet_manage_keyboard(w_name, is_running=False, user_id=callback.from_user.id))
            return
            
        test_trigger = float(current_price)
        res = handler.place_hedge_order(test_trigger, test_amount, config["sl_offset"], is_market=True)
        if res["success"]:
            msg = (
                f"✅ **ТЕСТ УСПЕШЕН**\n"
                f"Ордер SHORT выставлен!\n\n"
                f"ID ордера: `{res['order_id']}`\n"
                f"Размер: {res['size']} BTC\n"
                f"Цена: {res['price']}\n"
                f"Стоп-лосс на: {res['sl_price']}"
            )
        else:
            msg = f"❌ **ОШИБКА ТЕСТА**\nОтвет биржи: {res['error']}"
        
        await callback.message.edit_text(msg, parse_mode="Markdown", reply_markup=get_wallet_manage_keyboard(w_name, is_running=False, user_id=callback.from_user.id))
        
    except Exception as e:
        await callback.message.edit_text(f"❌ Системная ошибка: {e}", reply_markup=get_wallet_manage_keyboard(w_name, is_running=False, user_id=callback.from_user.id))
    
    await callback.answer()

@dp.callback_query(F.data.startswith("delete_"))
async def delete_wallet_cb(callback: CallbackQuery):
    w_name = callback.data.split("_")[1]
    
    if w_name in active_monitors:
        monitor = active_monitors.pop(w_name)
        monitor.stop()
        try:
            monitor.hl.cancel_all_orders()
            if monitor.active_order_ids:
                for oid in list(monitor.active_order_ids):
                    monitor.hl.cancel_order_by_id("BTC", oid)
            monitor.hl.close_position()
        except:
            pass
    
    delete_wallet(callback.from_user.id, w_name)
    await callback.answer(f"🗑 Кошелек '{w_name}' удален.", show_alert=True)
    await list_wallets_cb(callback)

async def main():
    logger.info("Starting Multi-User bot...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped.")
