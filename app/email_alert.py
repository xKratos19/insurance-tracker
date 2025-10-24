import smtplib
from email.mime.text import MIMEText

EMAIL = "your_email@gmail.com"
APP_PASSWORD = "your_app_password"
TO_EMAIL = "your_email@gmail.com"

def send_email_alert(records):
    msg_content = "⚠️ The following insurances expire within 7 days:\n\n"
    for r in records:
        msg_content += f"- {r.name} ({r.car_name}, {r.plate_number}) expires on {r.insurance_end}\n"
    msg = MIMEText(msg_content)
    msg['Subject'] = "Insurance Expiry Reminder"
    msg['From'] = EMAIL
    msg['To'] = TO_EMAIL

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
        server.login(EMAIL, APP_PASSWORD)
        server.send_message(msg)
