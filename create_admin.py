from pymongo import MongoClient
from werkzeug.security import generate_password_hash
from dotenv import load_dotenv
import os
from datetime import datetime

# Load environment variables
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
DB_NAME = os.getenv("DB_NAME", "delivery_platform")

client = MongoClient(MONGO_URI)
db = client[DB_NAME]

# Define your initial admin credentials here
ADMIN_EMAIL = "admin@company.com"
ADMIN_PASSWORD = "admin_password_123" # Change this!

def create_first_admin():
    # Check if this email already exists
    if db.users.find_one({"email": ADMIN_EMAIL}):
        print(f"User with email {ADMIN_EMAIL} already exists.")
        return

    # Insert the admin document
    db.users.insert_one({
        "name": "Fruite delight",
        "email": "rajpt772@gmail.com",
        "phone": "7828317123",
        "role": "admin",
        "password": generate_password_hash(ADMIN_PASSWORD),
        "created_at": datetime.now()
    })
    
    print(f"✅ Success! First admin created.")
    print(f"Email: {ADMIN_EMAIL}")
    print(f"Password: {ADMIN_PASSWORD}")

if __name__ == "__main__":
    create_first_admin()