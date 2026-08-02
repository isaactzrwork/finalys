from datetime import date

from flask import Flask, render_template

from services.dashboard_data import (
    get_bank_flagged_expiries,
    get_miles_expiry_timeline,
    get_spending_by_category,
    get_spending_by_payment_method,
    get_total_miles_by_card,
)

app = Flask(__name__)


@app.route("/")
def index():
    today = date.today()
    return render_template(
        "index.html",
        category_data=get_spending_by_category(today),
        payment_method_data=get_spending_by_payment_method(today),
        miles_timeline=get_miles_expiry_timeline(within_days=365),
        total_miles=get_total_miles_by_card(),
        bank_flagged=get_bank_flagged_expiries(),
        month_label=today.strftime("%B %Y"),
    )


@app.route("/miles/all")
def miles_all():
    return render_template("miles_all.html", miles_timeline=get_miles_expiry_timeline())


if __name__ == "__main__":
    app.run(debug=True, port=5000)
