from pymongo import MongoClient
from werkzeug.security import generate_password_hash
from dotenv import load_dotenv
import os
import secrets
from datetime import datetime
from wp import send_whatsapp

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
DB_NAME   = os.getenv("DB_NAME", "delivery_platform")

client = MongoClient(MONGO_URI)
db     = client[DB_NAME]

ADMIN_EMAIL = "navin@fruitedelight"
ADMIN_PHONE = "7828317123"

def create_first_admin():
    if db.users.find_one({"email": ADMIN_EMAIL}):
        print(f"User with email {ADMIN_EMAIL} already exists.")
        return

    password = secrets.token_urlsafe(8)

    db.users.insert_one({
        "name":       "Navin Soni",
        "email":      ADMIN_EMAIL,
        "phone":      ADMIN_PHONE,
        "role":       "admin",
        "password":   generate_password_hash(password),
        "created_at": datetime.now()
    })

    # Send credentials via WhatsApp
    wa_phone = "91" + ADMIN_PHONE
    msg = (
        f"🍓 Fruit Delights – Admin Account\n\n"
        f"Your admin account has been created.\n\n"
        f"User ID: {ADMIN_EMAIL}\n"
        f"Password: {password}\n\n"
        f"Login here:\nhttps://fruitedelights.com/login"
    )
    send_whatsapp(wa_phone, msg)

    print(f"✅ Admin created and credentials sent to WhatsApp.")
    print(f"User ID : {ADMIN_EMAIL}")
    print(f"Password: {password}")

if __name__ == "__main__":
    create_first_admin()