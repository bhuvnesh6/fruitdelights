import requests
from dotenv import load_dotenv
import os

def send_whatsapp(number, message):
    try:
        response = requests.post(
            "https://wpvo.phishnix.site/admin/send-message",
            headers={
                "x-api-key": os.getenv("WP_API_KEY"),
                "Content-Type": "application/json"
            },
            json={
                "instanceName": "Fruit_Delights",
                "number": str(number),
                "message": message
            },
            timeout=30
        )

        print(
            f"WhatsApp sent to {number} | "
            f"Status: {response.status_code}"
        )

        return response.status_code == 200

    except Exception as e:
        print("WhatsApp Error:", str(e))
        return False
    
    
    
