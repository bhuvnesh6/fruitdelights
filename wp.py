import requests
import os
from dotenv import load_dotenv

load_dotenv()

WIREBASE_BASE_URL = os.getenv("WIREBASE_URL", "https://wirebase.phishnix.site")


def send_whatsapp(number, message, api_key=None, instance_name=None):
    """
    Sends a WhatsApp text message via Wirebase.

    api_key / instance_name are per-franchise (or per-HQ-admin) credentials
    stored in MongoDB's whatsapp_settings collection and resolved by
    app.py's _safe_send_whatsapp() before calling this. If they aren't
    supplied (legacy calls), falls back to global WIREBASE_API_KEY /
    WIREBASE_INSTANCE env vars so nothing silently breaks mid-migration.
    """
    api_key = api_key or os.getenv("WIREBASE_API_KEY")
    instance_name = instance_name or os.getenv("WIREBASE_INSTANCE")

    if not api_key or not instance_name:
        print(f"[Wirebase] Missing api_key/instance_name for send to {number} — "
              f"configure WhatsApp Setup in the Admin Dashboard.")
        return False

    try:
        number_str = "".join(filter(str.isdigit, str(number)))
        response = requests.post(
            f"{WIREBASE_BASE_URL}/api/public/send",
            headers={
                "X-API-Key": api_key,
                "Content-Type": "application/json"
            },
            json={
                "instanceName": instance_name,
                "to": number_str,
                "type": "text",
                "message": message,
            },
            timeout=30
        )
        return response.status_code in [200, 201]
    except Exception as e:
        print("Wirebase WhatsApp Error:", str(e))
        return False