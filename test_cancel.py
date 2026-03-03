from config import Config
from exchange_handler import HyperliquidHandler
import sys

address="0x7789e2e2f74059ae58a9f8e38d9410f770559c4e"
config = Config()
h = HyperliquidHandler(address, Config.PRIVATE_KEY, Config.API_SECRET)

print("Open orders:")
print(h.info.open_orders(address))
print("Frontend Open orders:")
orders = h.info.frontend_open_orders(address)
print(orders)

cancel_reqs = []
for o in orders:
    if o["coin"] == "BTC":
        cancel_reqs.append({"coin": o["coin"], "oid": o["oid"]})
        
print("Canceling:", cancel_reqs)
if cancel_reqs:
    print(h.exchange.bulk_cancel(cancel_reqs))

