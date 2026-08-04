from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class Item(db.Model):
    __tablename__ = "items"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    price = db.Column(db.Float, nullable=False, default=0)

    entries = db.relationship("Entry", backref="item", lazy=True)


class Entry(db.Model):
    __tablename__ = "entries"

    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    quantity = db.Column(db.Float, nullable=False)
    unit_price = db.Column(db.Float, nullable=False)  # snapshot of item price at entry time
    entry_date = db.Column(db.Date, nullable=False)
    note = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    @property
    def total(self):
        return self.quantity * self.unit_price
