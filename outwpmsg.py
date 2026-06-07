import requests
from dotenv import load_dotenv
import os

load_dotenv()


def send_whatsapp_msg(identifier: str, message: str) -> bool:
    """
    Send a WhatsApp message from admin to any contact.
    
    identifier: whatsappId (e.g. '63582796566668@lid') OR phone number string.
                If it contains '@', it is sent as-is as the number field.
                Otherwise it is treated as a plain phone number.
    message:    Plain text message to send.
    
    Uses the same Api endpoint as wp.py but is a separate function
    so admin-CRM sends don't interfere with credential-sending logic.
    """
    load_dotenv()
    api_key = os.getenv("WP_API_KEY")
    base_url = os.getenv("WP_API_BASE", "https://wpvo.phishnix.site")
    instance = os.getenv("WP_INSTANCE", "Fruit_Delights")

    # Normalise identifier
    number = identifier.strip() if identifier else ""
    if not number:
        print("outwpmsg: empty identifier, skipping send.")
        return False

    try:
        response = requests.post(
            f"{base_url}/admin/send-message",
            headers={
                "x-api-key": api_key,
                "Content-Type": "application/json"
            },
            json={
                "instanceName": instance,
                "number": number,
                "message": message
            },
            timeout=30
        )
        print(
            f"[outwpmsg] Sent to {number} | "
            f"Status: {response.status_code} | "
            f"Body: {response.text[:120]}"
        )
        return response.status_code == 200

    except Exception as e:
        print(f"[outwpmsg] Error sending to {number}: {e}")
        return False
    
  