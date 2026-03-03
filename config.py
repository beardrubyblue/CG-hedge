import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # API Keys
    WALLET_ADDRESS = os.getenv("HL_WALET_ADDRESS")
    PRIVATE_KEY = os.getenv("HL_PRIVATE_KEY")
    API_SECRET = os.getenv("HL_API_SECRET") # Для Agent Key API
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_TOKEN:
        print("❌ ОШИБКА: TELEGRAM_BOT_TOKEN не найден в .env файле!")
        print("Убедитесь, что вы создали файл .env на основе .env.example")
    
    CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    ADMIN_ID = 863693785

    # Strategy Parameters (load defaults from env)
    TRIGGER_PRICE = float(os.getenv("DEFAULT_TRIGGER_PRICE", 62000))
    HEDGE_AMOUNT_USDC = float(os.getenv("DEFAULT_HEDGE_AMOUNT_USDC", 1000))
    SL_OFFSET = float(os.getenv("DEFAULT_SL_OFFSET", 1000))
    
    # Asset settings
    ASSET = "BTC"
    
    @classmethod
    def update_params(cls, trigger_price=None, amount=None, sl_offset=None):
        if trigger_price is not None:
            cls.TRIGGER_PRICE = trigger_price
        if amount is not None:
            cls.HEDGE_AMOUNT_USDC = amount
        if sl_offset is not None:
            cls.SL_OFFSET = sl_offset

    @classmethod
    def get_summary(cls):
        return (f"📊 Текущие настройки:\n"
                f"🔹 Инструмент: {cls.ASSET}\n"
                f"🎯 Триггер цена: {cls.TRIGGER_PRICE} USD\n"
                f"💰 Объем хеджа: {cls.HEDGE_AMOUNT_USDC} USDC\n"
                f"🛡 Stop-Loss: {cls.TRIGGER_PRICE + cls.SL_OFFSET} (+{cls.SL_OFFSET})")
