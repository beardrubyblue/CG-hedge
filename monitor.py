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
        self.attempts = 0
        self.max_attempts = 6

    async def start(self):
        self.is_running = True
        logger.info("Price monitor started")
        
        # Принудительно устанавливаем 1x плечо при запуске
        self.hl.set_leverage(1, self.asset)
        
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
                amount_usdc = self.config.get("option_deposit", self.config.get("amount", 47250.0))
                
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
                
                # Проверка достижения SL или просадки PnL (если позиция уже открыта)
                if self.hedge_active:
                    option_profit = self.config.get("option_profit", 1000.0)
                    loss_limit = option_profit * 0.20
                    
                    pnl = self.hl.get_position_pnl(self.asset)
                    reason = None
                    
                    if pnl is None:
                        # Позиции нет, значит закрылась по защитному SL!
                        logger.info("Position absent! Probably closed by exchange StopLoss.")
                        self.hedge_active = False
                        self.attempts += 1
                        reason = "сработал защитный SL (цена)"
                        
                    else:
                        logger.info(f"Monitoring PnL: {pnl} USDC | Limit constraint: <= -{loss_limit} USDC (Attempt: {self.attempts+1}/6)")
                        
                        if self.attempts < 5 and pnl <= -loss_limit:
                            # Программное закрытие по лимиту убытка (только для первых 5 попыток)
                            logger.info(f"PnL {pnl} <= -{loss_limit}. Closing position.")
                        self.hl.close_position(self.asset)
                        self.hl.cancel_all_orders(self.asset) # Убираем отложенные SL
                        self.hedge_active = False
                        self.attempts += 1
                        reason = f"достигнут лимит убытка {pnl} USDC"
                    
                    if not self.hedge_active:
                        if self.attempts < 5:
                            await self.notify(f"⚠️ **Убыток зафиксирован: {reason}**\n"
                                              f"👤 Кошелек: `{self.hl.address[:6]}...` \n"
                                              f"🔄 Использовано попыток: {self.attempts}/5 (лимит на попытку: -{loss_limit} USDC)\n"
                                              f"⏳ Ожидание цены {trigger_price} USD для следующего хеджа...")
                        elif self.attempts == 5:
                            await self.notify(f"⚠️ **Прибыль от опциона исчерпана! ({reason})**\n"
                                              f"👤 Кошелек: `{self.hl.address[:6]}...` \n"
                                              f"🔄 Попытка: 6 (Последний жесткий хедж)\n"
                                              f"⏳ Бот ждет {trigger_price} USD для финального входа со строгим ценовым SL.")
                        else:
                            await self.notify(f"🛑 **ФАТАЛЬНЫЙ УБЫТОК!**\n"
                                              f"👤 Кошелек: `{self.hl.address[:6]}...` \n"
                                              f"❌ Зафиксирован 6-й убыток ({reason}).\n"
                                              f"🤖 Работа бота по данному кошельку остановлена навсегда.")
                            self.is_running = False
                            break
                        
                        await asyncio.sleep(3)
                        continue

                await asyncio.sleep(3) # Проверка положения дел
                
            except Exception as e:
                logger.error(f"Error in monitor loop: {e}")
                await asyncio.sleep(5)

    def stop(self):
        self.is_running = False
