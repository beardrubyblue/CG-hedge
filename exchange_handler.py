import logging
import eth_account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from config import Config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class HyperliquidHandler:
    def __init__(self, address, private_key=None, api_secret=None):
        self.address = address
        self.secret = api_secret if api_secret else private_key
        if not self.secret:
            raise ValueError("Private key or API secret is required")
            
        self.account = eth_account.Account.from_key(self.secret)
        self.info = Info(constants.MAINNET_API_URL, skip_ws=True)
        self.exchange = Exchange(self.account, constants.MAINNET_API_URL, account_address=self.address)
        
        try:
            self.meta = self.info.meta()
            self.sz_decimals_map = {item["name"]: item["szDecimals"] for item in self.meta["universe"]}
        except Exception as e:
            logger.error(f"Could not fetch meta: {e}")
            self.sz_decimals_map = {"BTC": 5}
        
    def get_market_price(self, asset="BTC"):
        try:
            m_data = self.info.all_mids()
            if asset in m_data:
                return float(m_data[asset])
            return None
        except Exception as e:
            logger.error(f"Error fetching market price: {e}")
            return None

    def place_hedge_order(self, price, amount_usdc, sl_offset, is_market=False, is_stop_market=False):
        """
        Выставляет SHORT ордер (лимитный, маркет или стоп-маркет) и Stop-Loss.
         amount_usdc - количество USDC для хеджа.
         price - цена открытия (для лимитки/стоп-маркета) или ориентировочная текущая цена для расчета (для маркета).
         sl_offset - смещение для SL.
         is_market - если True, то открывает шорт маркетом.
         is_stop_market - если True, то выставляет отложенный триггер "Stop Market" на биржу вместо лимитки.
        """
        try:
            sz_decimals = self.sz_decimals_map.get("BTC", 5)
            # Расчет размера BTC (size = amount / price)
            size = round(amount_usdc / price, sz_decimals)
            
            # Hyperliquid требует, чтобы цена состояла максимум из 5 значащих цифр
            hl_price = float(f"{float(f'{price:.5g}'):.5g}")
            
            # Направление SHORT = is_buy=False
            if is_market:
                # В Hyperliquid нет чистого "Market" ордера, мы отправляем Limit ордер с параметром IOC (Immediate-or-Cancel).
                # При продаже в шорт мы должны поставить цену, которая гарантированно НИЖЕ рынка, 
                # чтобы она мгновенно "съела" стакан покупателей.
                # Но если мы ставим цену СЛИШКОМ низко ("хуже" рынка более чем на 5-10%), то биржа выдает ошибку IOC.
                m_price = self.get_market_price(asset="BTC")
                if not m_price:
                    m_price = price
                
                # Ставим цену на 3% ниже реального рынка (а не от 80000, которые вы ввели "в уме")
                slippage_price = m_price * 0.97
                hl_slippage_price = float(f"{float(f'{slippage_price:.5g}'):.5g}")
                
                logger.info(f"Placing MARKET SHORT order for {self.address}: {size} BTC (~{hl_slippage_price}) / Market Price: {m_price}")
                order_result = self.exchange.order("BTC", False, size, hl_slippage_price, {"limit": {"tif": "Ioc"}})
            elif is_stop_market:
                # Отложенный вход через Stop Market ("sl" означает Stop Loss триггер в API, он сработает когда цена упадет до hl_price)
                logger.info(f"Placing STOP-MARKET SHORT ENTRY for {self.address}: {size} BTC @ {hl_price}")
                order_result = self.exchange.order(
                    "BTC", 
                    False, # SHORT
                    size, 
                    hl_price, 
                    {"trigger": {"triggerPx": hl_price, "isMarket": True, "tpsl": "sl"}},
                    reduce_only=False # НЕ reduce only, так как это вход в позицию
                )
            else:
                logger.info(f"Placing LIMIT SHORT order for {self.address}: {size} BTC @ {hl_price}")
                order_result = self.exchange.order("BTC", False, size, hl_price, {"limit": {"tif": "Gtc"}})
            
            if order_result["status"] == "ok":
                statuses = order_result["response"]["data"]["statuses"][0]
                
                if "resting" in statuses:
                    order_id = statuses["resting"]["oid"]
                elif "filled" in statuses:
                    order_id = statuses["filled"]["oid"]
                else:
                    logger.error(f"Unexpected order status: {statuses}")
                    return {"success": False, "error": f"Unknown order status structure: {statuses}"}
                    
                logger.info(f"Order placed successfully. OID: {order_id}")
                
                if is_stop_market:
                    # При Stop-Market мы не ставим SL сразу (цена SL < текущей, он бы сработал мгновенно)
                    # Монитор поставит его сам, когда сработает вход
                    return {
                        "success": True,
                        "order_id": order_id,
                        "size": size,
                        "price": hl_price,
                        "sl_price": "Ожидание входа"
                    }
                
                # 2. Выставляем Stop Loss
                sl_price = price + sl_offset
                hl_sl_price = float(f"{float(f'{sl_price:.5g}'):.5g}")
                logger.info(f"Setting Stop-Loss at {hl_sl_price}")
                
                # SL в Hyperliquid - это триггерный ордер. 
                sl_result = self.exchange.order(
                    "BTC", 
                    True, 
                    size, 
                    hl_sl_price, 
                    {"trigger": {"triggerPx": hl_sl_price, "isMarket": True, "tpsl": "sl"}},
                    reduce_only=True
                )
                
                return {
                    "success": True,
                    "order_id": order_id,
                    "size": size,
                    "price": hl_price,
                    "sl_price": hl_sl_price
                }
            else:
                logger.error(f"Order failed: {order_result}")
                return {"success": False, "error": str(order_result)}
                
        except Exception as e:
            logger.error(f"Exception logic: {e}")
            return {"success": False, "error": str(e)}

    def place_sl_order(self, price, amount_usdc, sl_offset):
        """
        Отдельное выставление Stop-Loss ордера (когда позиция уже открыта)
        """
        try:
            sz_decimals = self.sz_decimals_map.get("BTC", 5)
            size = round(amount_usdc / price, sz_decimals)
            sl_price = price + sl_offset
            hl_sl_price = float(f"{float(f'{sl_price:.5g}'):.5g}")
            
            logger.info(f"Placing separate Stop-Loss at {hl_sl_price}")
            
            sl_result = self.exchange.order(
                "BTC", 
                True, 
                size, 
                hl_sl_price, 
                {"trigger": {"triggerPx": hl_sl_price, "isMarket": True, "tpsl": "sl"}},
                reduce_only=True
            )
            return {"success": True, "sl_price": hl_sl_price}
        except Exception as e:
            logger.error(f"Error placing SL logic: {e}")
            return {"success": False, "error": str(e)}

    def cancel_all_orders(self, asset="BTC"):
        try:
            # Используем frontend_open_orders, так как обычный open_orders не возвращает Stop Market (trigger) ордера
            orders = self.info.frontend_open_orders(self.address)
            cancel_requests = [{"coin": o["coin"], "oid": o["oid"]} for o in orders if o["coin"] == asset]
            if cancel_requests:
                logger.info(f"Canceling {len(cancel_requests)} open/trigger orders for {asset}")
                result = self.exchange.bulk_cancel(cancel_requests)
                return {"success": True, "result": result}
            return {"success": True, "result": "No open orders"}
        except Exception as e:
            logger.error(f"Error canceling orders: {e}")
            return {"success": False, "error": str(e)}

    def cancel_order_by_id(self, asset, oid):
        try:
            logger.info(f"Canceling specific order {oid} for {asset}")
            result = self.exchange.cancel(asset, oid)
            return {"success": True, "result": result}
        except Exception as e:
            logger.error(f"Error canceling order {oid}: {e}")
            return {"success": False, "error": str(e)}

    def close_position(self, asset="BTC"):
        try:
            state = self.info.user_state(self.address)
            positions = state.get("assetPositions", [])
            for p_data in positions:
                pos = p_data["position"]
                if pos["coin"] == asset:
                    szi = float(pos["szi"])
                    if szi != 0:
                        is_buy = szi < 0 # Если были в шорте, значит нужно купить
                        size = abs(szi)
                        mids = self.info.all_mids()
                        current_px = float(mids.get(asset, 0))
                        if current_px > 0:
                            slippage_px = current_px * 1.05 if is_buy else current_px * 0.95
                            hl_slippage_px = float(f"{float(f'{slippage_px:.5g}'):.5g}")
                            logger.info(f"Closing position {szi} {asset} at market (limit ~{hl_slippage_px})")
                            res = self.exchange.order(asset, is_buy, size, hl_slippage_px, {"limit": {"tif": "Ioc"}}, reduce_only=True)
                            return {"success": True, "result": res}
            return {"success": True, "result": "No open position to close"}
        except Exception as e:
            logger.error(f"Error closing position: {e}")
            return {"success": False, "error": str(e)}

    def set_leverage(self, leverage=1, asset="BTC"):
        try:
            logger.info(f"Setting leverage to {leverage}x for {asset} on {self.address}")
            res = self.exchange.update_leverage(leverage, asset, is_cross=True)
            return {"success": True, "result": res}
        except Exception as e:
            logger.error(f"Error setting leverage: {e}")
            return {"success": False, "error": str(e)}

    def get_position_pnl(self, asset="BTC"):
        try:
            state = self.info.user_state(self.address)
            positions = state.get("assetPositions", [])
            for p_data in positions:
                pos = p_data["position"]
                if pos["coin"] == asset:
                    szi = float(pos["szi"])
                    if szi != 0:
                        return float(pos["unrealizedPnl"])
        except Exception as e:
            logger.error(f"Error getting PnL: {e}")
        return None
