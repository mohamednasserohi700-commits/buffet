import os
from datetime import date, datetime
from calendar import monthrange

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from sqlalchemy import func

from models import db, Item, Entry

# ---------------------------------------------------------------------------
# App / DB setup
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")

# Optional .env support for local testing (safe no-op if python-dotenv isn't installed)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Railway injects DATABASE_URL automatically once a PostgreSQL plugin is attached
# to the project. Any of these env var names will work, in this priority order.
database_url = (
    os.environ.get("DATABASE_URL")
    or os.environ.get("DATABASE_PUBLIC_URL")
    or os.environ.get("POSTGRES_URL")
    or ""
).strip()

if database_url.startswith("postgres://"):
    # Older-style Postgres URLs need the postgresql:// scheme for SQLAlchemy
    database_url = database_url.replace("postgres://", "postgresql://", 1)

if database_url:
    engine_options = {"pool_pre_ping": True, "pool_recycle": 280}
    db_kind = "PostgreSQL"
else:
    # Fallback for local dev only — on Railway without Postgres this resets on every deploy
    db_path = os.environ.get("SQLITE_PATH", os.path.join(os.path.dirname(__file__), "data.db"))
    database_url = f"sqlite:///{db_path}"
    engine_options = {}
    db_kind = "SQLite (local file — attach a Postgres plugin on Railway for persistence)"

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = engine_options
db.init_app(app)
print(f"[سجل المخزون] قاعدة البيانات المستخدمة: {db_kind}")

ARABIC_DAYS = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
ARABIC_MONTHS = [
    "يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
    "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر",
]


def arabic_day_name(d: date) -> str:
    return ARABIC_DAYS[d.weekday()]


def arabic_month_name(month: int) -> str:
    return ARABIC_MONTHS[month - 1]


@app.template_filter("money")
def money(value):
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return value


@app.context_processor
def inject_helpers():
    return {"now": datetime.utcnow()}


with app.app_context():
    db.create_all()


# ---------------------------------------------------------------------------
# Dashboard -> redirect to current month
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    today = date.today()
    return redirect(url_for("monthly_report", year=today.year, month=today.month))


# ---------------------------------------------------------------------------
# Items (اسماء الأصناف والأسعار)
# ---------------------------------------------------------------------------
@app.route("/items", methods=["GET", "POST"])
def items():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        price_raw = request.form.get("price", "").strip()
        try:
            price = float(price_raw)
        except ValueError:
            price = None

        if not name:
            flash("اكتب اسم الصنف", "error")
        elif price is None or price < 0:
            flash("اكتب سعر صحيح للصنف", "error")
        elif Item.query.filter_by(name=name).first():
            flash("الصنف ده موجود بالفعل", "error")
        else:
            db.session.add(Item(name=name, price=price))
            db.session.commit()
            flash(f'تم إضافة "{name}"', "success")
        return redirect(url_for("items"))

    all_items = Item.query.order_by(Item.name.asc()).all()
    return render_template("items.html", items=all_items)


@app.route("/items/<int:item_id>/update", methods=["POST"])
def update_item(item_id):
    item = Item.query.get_or_404(item_id)
    name = request.form.get("name", "").strip()
    price_raw = request.form.get("price", "").strip()
    try:
        price = float(price_raw)
    except ValueError:
        price = None

    if not name or price is None or price < 0:
        flash("بيانات غير صحيحة", "error")
        return redirect(url_for("items"))

    item.name = name
    item.price = price
    db.session.commit()
    flash("تم تحديث الصنف", "success")
    return redirect(url_for("items"))


@app.route("/items/<int:item_id>/delete", methods=["POST"])
def delete_item(item_id):
    item = Item.query.get_or_404(item_id)
    if item.entries:
        flash("لا يمكن حذف صنف له عمليات صرف مسجلة", "error")
        return redirect(url_for("items"))
    db.session.delete(item)
    db.session.commit()
    flash("تم حذف الصنف", "success")
    return redirect(url_for("items"))


# ---------------------------------------------------------------------------
# Entries (تسجيل بضاعة / الصرف اليومي)
# ---------------------------------------------------------------------------
@app.route("/entries", methods=["GET"])
def entries():
    today = date.today()
    year = request.args.get("year", default=today.year, type=int)
    month = request.args.get("month", default=today.month, type=int)
    return redirect(url_for("entries_month", year=year, month=month))


@app.route("/entries/<int:year>/<int:month>", methods=["GET", "POST"])
def entries_month(year, month):
    if month < 1 or month > 12:
        return redirect(url_for("entries"))

    if request.method == "POST":
        item_id = request.form.get("item_id", type=int)
        quantity = request.form.get("quantity", type=float)
        date_raw = request.form.get("entry_date", "").strip()
        note = request.form.get("note", "").strip()

        item = Item.query.get(item_id) if item_id else None
        try:
            entry_date = datetime.strptime(date_raw, "%Y-%m-%d").date()
        except ValueError:
            entry_date = None

        if not item:
            flash("اختر الصنف", "error")
        elif not quantity or quantity <= 0:
            flash("اكتب كمية صحيحة", "error")
        elif not entry_date:
            flash("اختر تاريخ صحيح", "error")
        else:
            db.session.add(
                Entry(
                    item_id=item.id,
                    quantity=quantity,
                    unit_price=item.price,
                    entry_date=entry_date,
                    note=note or None,
                )
            )
            db.session.commit()
            flash("تم تسجيل الصنف", "success")
            return redirect(url_for("entries_month", year=entry_date.year, month=entry_date.month))

        return redirect(url_for("entries_month", year=year, month=month))

    start = date(year, month, 1)
    end_day = monthrange(year, month)[1]
    end = date(year, month, end_day)

    rows = (
        Entry.query.filter(Entry.entry_date >= start, Entry.entry_date <= end)
        .order_by(Entry.entry_date.asc(), Entry.id.asc())
        .all()
    )

    grand_total = sum(r.total for r in rows)
    all_items = Item.query.order_by(Item.name.asc()).all()

    prev_month = month - 1 or 12
    prev_year = year - 1 if month == 1 else year
    next_month = month + 1 if month < 12 else 1
    next_year = year + 1 if month == 12 else year

    return render_template(
        "entries.html",
        rows=rows,
        items=all_items,
        year=year,
        month=month,
        month_name=arabic_month_name(month),
        grand_total=grand_total,
        today=date.today().isoformat(),
        prev_year=prev_year,
        prev_month=prev_month,
        next_year=next_year,
        next_month=next_month,
        day_name_fn=arabic_day_name,
    )


@app.route("/entries/<int:entry_id>/delete", methods=["POST"])
def delete_entry(entry_id):
    entry = Entry.query.get_or_404(entry_id)
    y, m = entry.entry_date.year, entry.entry_date.month
    db.session.delete(entry)
    db.session.commit()
    flash("تم حذف السطر", "success")
    return redirect(url_for("entries_month", year=y, month=m))


@app.route("/api/item-price/<int:item_id>")
def item_price(item_id):
    item = Item.query.get_or_404(item_id)
    return jsonify({"price": item.price})


# ---------------------------------------------------------------------------
# Monthly report (اجمالي السعر شهريا + طباعة)
# ---------------------------------------------------------------------------
@app.route("/report/<int:year>/<int:month>")
def monthly_report(year, month):
    if month < 1 or month > 12:
        return redirect(url_for("index"))

    start = date(year, month, 1)
    end_day = monthrange(year, month)[1]
    end = date(year, month, end_day)

    summary = (
        db.session.query(
            Item.name.label("name"),
            func.sum(Entry.quantity).label("qty"),
            func.sum(Entry.quantity * Entry.unit_price).label("total"),
        )
        .join(Entry, Entry.item_id == Item.id)
        .filter(Entry.entry_date >= start, Entry.entry_date <= end)
        .group_by(Item.name)
        .order_by(func.sum(Entry.quantity * Entry.unit_price).desc())
        .all()
    )

    grand_total = sum(row.total for row in summary) if summary else 0
    grand_qty = sum(row.qty for row in summary) if summary else 0

    detail_rows = (
        Entry.query.filter(Entry.entry_date >= start, Entry.entry_date <= end)
        .order_by(Entry.entry_date.asc(), Entry.id.asc())
        .all()
    )

    prev_month = month - 1 or 12
    prev_year = year - 1 if month == 1 else year
    next_month = month + 1 if month < 12 else 1
    next_year = year + 1 if month == 12 else year

    return render_template(
        "monthly.html",
        summary=summary,
        detail_rows=detail_rows,
        grand_total=grand_total,
        grand_qty=grand_qty,
        year=year,
        month=month,
        month_name=arabic_month_name(month),
        prev_year=prev_year,
        prev_month=prev_month,
        next_year=next_year,
        next_month=next_month,
        day_name_fn=arabic_day_name,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
