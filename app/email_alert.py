# app/email_alert.py
import os
from datetime import datetime
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")
SENDER = os.getenv("ALERT_SENDER_EMAIL")
RECEIVER = os.getenv("ALERT_RECEIVER_EMAIL")

def send_email_alert(items):
    """Send an insurance expiration alert using SendGrid"""
    if not SENDGRID_API_KEY:
        print("⚠️ Missing SENDGRID_API_KEY — cannot send email.")
        return

    try:
        subject = "🚨 Insurance Expiration Alert - 7 Days Remaining"
        html_body = """
        <h2 style="color:#d9534f;">Upcoming Insurance Expirations</h2>
        <p>The following insurance policies will expire within 7 days:</p>
        <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;">
            <tr style="background:#f2f2f2;">
                <th>Name</th><th>Car</th><th>Plate</th><th>End Date</th><th>Days Left</th>
            </tr>
        """
        today = datetime.utcnow().date()
        for item in items:
            name = item.get("name", "—")
            car = item.get("car_name", "—")
            plate = item.get("plate_number", "—")
            end = item.get("insurance_end")
            if hasattr(end, "date"):
                end = end.date()
            days_left = (end - today).days if end else "?"
            html_body += f"""
                <tr>
                    <td>{name}</td>
                    <td>{car}</td>
                    <td>{plate}</td>
                    <td>{end}</td>
                    <td>{days_left}</td>
                </tr>
            """
        html_body += "</table><p>—<br><em>Automated Insurance Tracker</em></p>"

        message = Mail(
            from_email=SENDER,
            to_emails=RECEIVER,
            subject=subject,
            html_content=html_body
        )

        sg = SendGridAPIClient(SENDGRID_API_KEY)
        response = sg.send(message)

        print(f"✅ Alert email sent via SendGrid: {response.status_code}")

    except Exception as e:
        print("❌ Failed to send SendGrid email:", e)
