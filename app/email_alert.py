import os
import smtplib
from email.mime.text import MIMEText
from dotenv import load_dotenv

load_dotenv()
EMAIL = os.getenv("EMAIL")
APP_PASSWORD = os.getenv("APP_PASSWORD")
TO_EMAIL = os.getenv("TO_EMAIL")

def send_email_alert(items):
    if not (EMAIL and APP_PASSWORD and TO_EMAIL):
        return  # silently skip if not configured

    lines = ["⚠️ The following insurances expire within 7 days:\n"]
    for it in items:
        lines.append(
            f"- {it.get('name')} ({it.get('car_name')}, {it.get('plate_number')}) "
            f"expires on {it.get('insurance_end').date() if it.get('insurance_end') else 'N/A'}"
        )
    msg = MIMEText("\n".join(lines))
    msg["Subject"] = "Insurance Expiry Reminder"
    msg["From"] = EMAIL
    msg["To"] = TO_EMAIL

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(EMAIL, APP_PASSWORD)
        s.send_message(msg)
