import os
import io
import csv
import json
from datetime import date, datetime
from calendar import monthrange

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, Response, send_file
from sqlalchemy import func, inspect, text

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
    # ترقية بسيطة: إضافة عمود "الوحدة" لو قاعدة بيانات قديمة من غير العمود ده
    inspector = inspect(db.engine)
    if "items" in inspector.get_table_names():
        existing_cols = [c["name"] for c in inspector.get_columns("items")]
        if "unit" not in existing_cols:
            with db.engine.begin() as conn:
                conn.execute(text("ALTER TABLE items ADD COLUMN unit VARCHAR(30) DEFAULT ''"))
            print("[سجل المخزون] تم إضافة عمود الوحدة (unit) لجدول الأصناف")


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
        unit = request.form.get("unit", "").strip()
        price_raw = request.form.get("price", "").strip()
        try:
            price = float(price_raw)
        except ValueError:
            price = None

        if not name:
            flash("اكتب اسم الصنف", "error")
        elif not unit:
            flash("اكتب وحدة القياس (مثال: كيلو، كرتونة، قطعة)", "error")
        elif price is None or price < 0:
            flash("اكتب سعر صحيح للصنف", "error")
        elif Item.query.filter_by(name=name).first():
            flash("الصنف ده موجود بالفعل", "error")
        else:
            db.session.add(Item(name=name, unit=unit, price=price))
            db.session.commit()
            flash(f'تم إضافة "{name}"', "success")
        return redirect(url_for("items"))

    all_items = Item.query.order_by(Item.name.asc()).all()
    return render_template("items.html", items=all_items)


@app.route("/items/<int:item_id>/update", methods=["POST"])
def update_item(item_id):
    item = Item.query.get_or_404(item_id)
    name = request.form.get("name", "").strip()
    unit = request.form.get("unit", "").strip()
    price_raw = request.form.get("price", "").strip()
    try:
        price = float(price_raw)
    except ValueError:
        price = None

    if not name or not unit or price is None or price < 0:
        flash("بيانات غير صحيحة", "error")
        return redirect(url_for("items"))

    item.name = name
    item.unit = unit
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
    return jsonify({"price": item.price, "unit": item.unit})


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
            Item.unit.label("unit"),
            func.sum(Entry.quantity).label("qty"),
            func.sum(Entry.quantity * Entry.unit_price).label("total"),
        )
        .join(Entry, Entry.item_id == Item.id)
        .filter(Entry.entry_date >= start, Entry.entry_date <= end)
        .group_by(Item.name, Item.unit)
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


# ---------------------------------------------------------------------------
# استيراد وتصدير جميع البيانات
# ---------------------------------------------------------------------------
@app.route("/data")
def data_page():
    return render_template(
        "data.html",
        items_count=Item.query.count(),
        entries_count=Entry.query.count(),
    )


def _build_backup_dict():
    items = Item.query.order_by(Item.name.asc()).all()
    entries = (
        Entry.query.join(Item, Entry.item_id == Item.id)
        .order_by(Entry.entry_date.asc(), Entry.id.asc())
        .all()
    )
    return {
        "exported_at": datetime.utcnow().isoformat(),
        "items": [
            {"name": it.name, "unit": it.unit, "price": it.price} for it in items
        ],
        "entries": [
            {
                "item_name": e.item.name,
                "quantity": e.quantity,
                "unit_price": e.unit_price,
                "entry_date": e.entry_date.isoformat(),
                "note": e.note or "",
            }
            for e in entries
        ],
    }


@app.route("/data/export.json")
def export_json():
    payload = json.dumps(_build_backup_dict(), ensure_ascii=False, indent=2)
    filename = f"inventory-backup-{date.today().isoformat()}.json"
    return Response(
        payload,
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/data/export/items.csv")
def export_items_csv():
    buf = io.StringIO()
    buf.write("\ufeff")  # BOM عشان الإكسل يقرأ العربي صح
    writer = csv.writer(buf)
    writer.writerow(["اسم الصنف", "الوحدة", "السعر"])
    for it in Item.query.order_by(Item.name.asc()).all():
        writer.writerow([it.name, it.unit, it.price])
    filename = f"items-{date.today().isoformat()}.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/data/export/entries.csv")
def export_entries_csv():
    buf = io.StringIO()
    buf.write("\ufeff")
    writer = csv.writer(buf)
    writer.writerow(["التاريخ", "الصنف", "الوحدة", "الكمية", "سعر الوحدة", "الإجمالي", "ملاحظات"])
    rows = (
        Entry.query.join(Item, Entry.item_id == Item.id)
        .order_by(Entry.entry_date.asc(), Entry.id.asc())
        .all()
    )
    for e in rows:
        writer.writerow(
            [
                e.entry_date.isoformat(),
                e.item.name,
                e.item.unit,
                e.quantity,
                e.unit_price,
                e.total,
                e.note or "",
            ]
        )
    filename = f"entries-{date.today().isoformat()}.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/data/import", methods=["POST"])
def import_json():
    mode = request.form.get("mode", "merge")  # merge | replace
    uploaded = request.files.get("file")

    if not uploaded or uploaded.filename == "":
        flash("اختار ملف نسخة احتياطية (JSON) الأول", "error")
        return redirect(url_for("data_page"))

    try:
        raw = uploaded.read().decode("utf-8-sig")
        payload = json.loads(raw)
        items_in = payload.get("items", [])
        entries_in = payload.get("entries", [])
    except (json.JSONDecodeError, UnicodeDecodeError):
        flash("الملف مش بصيغة صحيحة، لازم يكون ملف JSON متصدَّر من البرنامج", "error")
        return redirect(url_for("data_page"))

    if mode == "replace":
        Entry.query.delete()
        Item.query.delete()
        db.session.commit()

    # 1) الأصناف: upsert حسب الاسم
    name_to_item = {it.name: it for it in Item.query.all()}
    added_items = 0
    updated_items = 0
    for row in items_in:
        name = str(row.get("name", "")).strip()
        unit = str(row.get("unit", "")).strip()
        try:
            price = float(row.get("price", 0))
        except (TypeError, ValueError):
            price = 0
        if not name:
            continue
        if name in name_to_item:
            existing = name_to_item[name]
            existing.unit = unit or existing.unit
            existing.price = price
            updated_items += 1
        else:
            new_item = Item(name=name, unit=unit, price=price)
            db.session.add(new_item)
            db.session.flush()
            name_to_item[name] = new_item
            added_items += 1
    db.session.commit()

    # 2) الحركات: تُربط بالصنف عن طريق الاسم
    added_entries = 0
    skipped_entries = 0
    for row in entries_in:
        item_name = str(row.get("item_name", "")).strip()
        item = name_to_item.get(item_name)
        try:
            quantity = float(row.get("quantity"))
            unit_price = float(row.get("unit_price"))
            entry_date = datetime.strptime(str(row.get("entry_date")), "%Y-%m-%d").date()
        except (TypeError, ValueError):
            item = None

        if not item:
            skipped_entries += 1
            continue

        db.session.add(
            Entry(
                item_id=item.id,
                quantity=quantity,
                unit_price=unit_price,
                entry_date=entry_date,
                note=(row.get("note") or None),
            )
        )
        added_entries += 1
    db.session.commit()

    msg = f"تم الاستيراد: {added_items} صنف جديد، {updated_items} صنف اتحدث، {added_entries} عملية صرف جديدة"
    if skipped_entries:
        msg += f"، وتم تجاهل {skipped_entries} عملية لبيانات ناقصة أو صنف مش موجود"
    flash(msg, "success")
    return redirect(url_for("data_page"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
