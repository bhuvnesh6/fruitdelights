import requests
import os
from dotenv import load_dotenv

load_dotenv()

def send_whatsapp(number, message):
    try:
        response = requests.post(
            f"{os.getenv('EVOLUTION_URL')}/message/sendText/{os.getenv('EVOLUTION_INSTANCE')}",
            headers={
                "apikey": os.getenv("EVOLUTION_API_KEY"),
                "Content-Type": "application/json"
            },
            json={
                "number": str(number),
                "text": message
            },
            timeout=30
        )

        


        return response.status_code in [200, 201]

    except Exception as e:
        print("WhatsApp Error:", str(e))
        return False
    
    

