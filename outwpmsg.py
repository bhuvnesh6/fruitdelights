import requests
from dotenv import load_dotenv
import os

load_dotenv()


def send_whatsapp_msg(phone: str, message: str) -> bool:
    """
    Send WhatsApp message using Evolution API.

    phone:
        917303938618

    message:
        Plain text message
    """

    phone = str(phone).strip() if phone else ""

    if not phone:
        print("[outwpmsg] Empty phone number")
        return False

    # Prevent accidental LID/group sends
    if "@" in phone:
        print(f"[outwpmsg] Invalid phone number received: {phone}")
        return False

    try:
        response = requests.post(
            f"{os.getenv('EVOLUTION_URL')}/message/sendText/{os.getenv('EVOLUTION_INSTANCE')}",
            headers={
                "apikey": os.getenv("EVOLUTION_API_KEY"),
                "Content-Type": "application/json"
            },
            json={
                "number": phone,
                "text": message
            },
            timeout=30
        )

        print(
            f"[outwpmsg] Sent to {phone} | "
            f"Status: {response.status_code} | "
            f"Body: {response.text[:300]}"
        )

        return response.status_code in (200, 201)

    except Exception as e:
        print(f"[outwpmsg] Error sending to {phone}: {e}")
        return False