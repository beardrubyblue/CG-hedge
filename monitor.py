import asyncio
import logging
from config import Config
from exchange_handler import HyperliquidHandler

logger = logging.getLogger(__name__)

class PriceMonitor:
    def __init__(self, exchange_handler: HyperliquidHandler, wallet_config, notify_callback):
        self.hl = exchange_handler
        self.config = wallet_config
        self.notify = notify_callback
        self.is_running = False
        self.hedge_active = False
        self.last_price = None
        self.asset = "BTC"
        self.active_order_ids = set()

    async def start(self):
        self.is_running = True
        logger.info("Price monitor started")
        
        while self.is_running:
            try:
                current_price = self.hl.get_market_price()
                if current_price is None:
                    await asyncio.sleep(1)
                    continue
                
                self.last_price = current_price
                
                # Логика срабатывания (одиночный проход при первом запуске или постоянный мониторинг, если SL)
                trigger_price = self.config["trigger_price"]
                sl_offset = self.config["sl_offset"]
                amount_usdc = self.config["amount"]
                
                if not self.hedge_active and getattr(self, "waiting_for_entry", False):
                    # Мы выставили Stop-Market ордер, но цена еще не дошла. Ждем!
                    if current_price <= trigger_price:
                        # УРА! Цена дошла. Значит, Stop-Market вход на бирже тоже выполнился.
                        logger.info("Pending entry filled! Placing SL.")
                        # Выставляем Stop-Loss через биржу
                        sl_res = self.hl.place_sl_order(trigger_price, amount_usdc, sl_offset)
                        
                        self.waiting_for_entry = False
                        self.hedge_active = True
                        
                        if sl_res.get("success") and "order_id" in sl_res:
                            self.active_order_ids.add(sl_res["order_id"])
                        
                        if sl_res.get("success"):
                            await self.notify(f"🚀 **ОРДЕР ИСПОЛНЕН!** (Вход по {trigger_price} или ниже)\n"
                                              f"🛡 Stop-Loss теперь установлен на: `{sl_res.get('sl_price')} USD`")
                        else:
                            await self.notify(f"❌ Вход выполнен, но **ошибка выставления SL**: {sl_res.get('error')}")
                    # Дальше не идем, на этом тике больше делать нечего
                    await asyncio.sleep(3)
                    continue

                if not self.hedge_active and not getattr(self, "waiting_for_entry", False):
                    # Решаем, как зайти в позицию
                    if current_price <= trigger_price:
                        # Цена УЖЕ ниже или равна триггеру - бьем маркетом!
                        logger.info(f"Price {current_price} <= {trigger_price}. Entering MARKET.")
                        result = self.hl.place_hedge_order(trigger_price, amount_usdc, sl_offset, is_market=True)
                        if result.get("success"):
                            self.hedge_active = True
                            if "order_id" in result: self.active_order_ids.add(result["order_id"])
                            if "sl_order_id" in result: self.active_order_ids.add(result["sl_order_id"])
                            message = (f"🚀 **ХЕДЖ ОТКРЫТ (MARKET)!**\n"
                                       f"👤 Кошелек: `{self.hl.address[:6]}...` \n"
                                       f"📉 BTC Цена: {current_price} USD\n"
                                       f"💰 Объем: {result['size']} BTC (~{amount_usdc} USDC)\n"
                                       f"🛡 Stop-Loss установлен на: {result['sl_price']} USD")
                            await self.notify(message)
                        else:
                            await self.notify(f"❌ Ошибка выставления ордера ({self.hl.address[:6]}): {result['error']}")
                            self.is_running = False
                            break
                    else:
                        # Цена ВЫШЕ триггера - выставляем отложенный Stop Market ордер
                        logger.info(f"Price {current_price} > {trigger_price}. Entering STOP-MARKET.")
                        result = self.hl.place_hedge_order(trigger_price, amount_usdc, sl_offset, is_stop_market=True)
                        if result.get("success"):
                            self.waiting_for_entry = True
                            if "order_id" in result: self.active_order_ids.add(result["order_id"])
                            message = (f"⏳ **ВЫСТАВЛЕН STOP-MARKET ОРДЕР ВХОДА!**\n"
                                       f"👤 Кошелек: `{self.hl.address[:6]}...` \n"
                                       f"📉 Ждем падения BTC до: `{trigger_price} USD`\n"
                                       f"(Stop-Loss будет установлен автоматически после входа)")
                            await self.notify(message)
                        else:
                            await self.notify(f"❌ Ошибка выставления ордера ({self.hl.address[:6]}): {result['error']}")
                            self.is_running = False
                            break
                
                # Проверка достижения SL (если позиция уже точно открыта, т.е. hedge_active == True)
                if self.hedge_active:
                    # Уведомляем пользователя программно (ордер на бирже уже есть, он сам закроется)
                    sl_price = trigger_price + sl_offset
                    if current_price >= sl_price:
                        logger.info(f"STOP-LOSS REACHED! Price: {current_price} >= {sl_price}")
                        self.hedge_active = False
                        await self.notify(f"🛑 **STOP-LOSS СРАБОТАЛ!** (Цена {current_price} >= {sl_price})\n👤 Кошелек: `{self.hl.address[:6]}...` \nХедж закрыт.")
                        self.is_running = False # Завершаем работу монитора
                        break

                await asyncio.sleep(3) # Проверка положения дел
                
            except Exception as e:
                logger.error(f"Error in monitor loop: {e}")
                await asyncio.sleep(5)

    def stop(self):
        self.is_running = False
