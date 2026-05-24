import resend
import os
from dotenv import load_dotenv

load_dotenv()

resend.api_key = os.getenv("RESEND_API_KEY")


def send_invoice_email(customer, invoice):

    html = f"""
    
    <div style="font-family: Arial; max-width:600px; margin:auto;">

        <h1 style="color:#16a34a;">
            Fruit Delight Invoice
        </h1>

        <p>Hello {customer['name']},</p>

        <p>
            Your monthly invoice has been generated successfully.
        </p>

        <table style="width:100%; border-collapse: collapse;">

            <tr>
                <td style="padding:10px; border:1px solid #ddd;">
                    Billing Month
                </td>

                <td style="padding:10px; border:1px solid #ddd;">
                    {invoice['month']}/{invoice['year']}
                </td>
            </tr>

            <tr>
                <td style="padding:10px; border:1px solid #ddd;">
                    Billable Days
                </td>

                <td style="padding:10px; border:1px solid #ddd;">
                    {invoice['billable_days']}
                </td>
            </tr>

            <tr>
                <td style="padding:10px; border:1px solid #ddd;">
                    Subtotal
                </td>

                <td style="padding:10px; border:1px solid #ddd;">
                    ₹{invoice['subtotal']}
                </td>
            </tr>

            <tr>
                <td style="padding:10px; border:1px solid #ddd;">
                    Tax
                </td>

                <td style="padding:10px; border:1px solid #ddd;">
                    ₹{invoice['tax']}
                </td>
            </tr>

            <tr>
                <td style="padding:10px; border:1px solid #ddd; font-weight:bold;">
                    Total
                </td>

                <td style="padding:10px; border:1px solid #ddd; font-weight:bold;">
                    ₹{invoice['total']}
                </td>
            </tr>

        </table>

        <p style="margin-top:30px;">
            Thank you for choosing Fruit Delight ❤️
        </p>

    </div>

    """

    resend.Emails.send({
        "from": "Fruit Delight <billing@phishnix.site>",
        "to": customer["email"],
        "subject": f"Your Invoice - {invoice['month']}/{invoice['year']}",
        "html": html
    })