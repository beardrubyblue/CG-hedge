import json
import os
from config import Config

DB_FILE = "users_db.json"

def load_db():
    if not os.path.exists(DB_FILE):
        return {}
    with open(DB_FILE, "r") as f:
        return json.load(f)

def save_db(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=4)

def init_db():
    db = load_db()
    needs_save = False
    
    if "global" not in db:
        db["global"] = {"wallets": {}}
        needs_save = True
        # Миграция старых данных
        for k in list(db.keys()):
            if k not in ["global", "users"]:
                if isinstance(db[k], dict) and "wallets" in db[k]:
                    for w_name, w_data in db[k]["wallets"].items():
                        db["global"]["wallets"][w_name] = w_data
                # Не удаляем старые ключи на всякий случай, но перестаем их использовать
                
    if "users" not in db:
        db["users"] = {}
        needs_save = True
        
    if needs_save:
        save_db(db)
    return db

def get_user_status(user_id):
    db = init_db()
    # Админ всегда одобрен
    if str(user_id) == str(Config.ADMIN_ID):
        return "approved"
    return db["users"].get(str(user_id), "unknown")

def set_user_status(user_id, status: str):
    db = init_db()
    db["users"][str(user_id)] = status
    save_db(db)

def get_user_config(user_id=None):
    # Теперь игнорируем user_id: все пользователи видят общие кошельки
    db = init_db()
    return db["global"]

def update_wallet_config(user_id, wallet_name, **kwargs):
    # user_id передается для совместимости, но игнорируется
    db = init_db()
    
    if wallet_name not in db["global"]["wallets"]:
        db["global"]["wallets"][wallet_name] = {
            "address": "",
            "private_key": "",
            "api_secret": "",
            "trigger_price": 62000,
            "amount": 15,
            "sl_offset": 1000,
            "option_profit": 1000,
            "option_deposit": 47250,
            "is_running": False
        }
    
    db["global"]["wallets"][wallet_name].update(kwargs)
    save_db(db)

def delete_wallet(user_id, wallet_name):
    # user_id передается для совместимости, но игнорируется
    db = init_db()
    if wallet_name in db["global"]["wallets"]:
        del db["global"]["wallets"][wallet_name]
        save_db(db)
