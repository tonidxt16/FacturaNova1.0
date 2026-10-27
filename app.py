import hashlib
import os
import re
import secrets
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from pathlib import Path
from xml.sax.saxutils import escape

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
INSTANCE_DIR.mkdir(exist_ok=True)

app = Flask(__name__, instance_path=str(INSTANCE_DIR))
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
database_url = os.environ.get("DATABASE_URL")
if database_url and database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)
sqlite_path = Path(os.environ.get("DATABASE_PATH", INSTANCE_DIR / "facturas.sqlite3"))
app.config["SQLALCHEMY_DATABASE_URI"] = database_url or f"sqlite:///{sqlite_path}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("FLASK_ENV") == "production"

if os.environ.get("RENDER"):
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

db = SQLAlchemy(app)
csrf = CSRFProtect(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Inicia sesión para continuar."
login_manager.login_message_category = "info"


class User(UserMixin, db.Model):
    __tablename__ = "app_user"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="facturador")
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.Date, nullable=False, default=date.today)
    invoices = db.relationship("Invoice", backref="owner", lazy=True)

    @property
    def is_admin(self):
        return self.role == "admin"


class Invoice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(32), unique=True, nullable=False)
    customer_name = db.Column(db.String(160), nullable=False)
    customer_email = db.Column(db.String(254), nullable=True)
    customer_address = db.Column(db.String(400), nullable=True)
    customer_contact = db.Column(db.String(160), nullable=True)
    customer_tax_id = db.Column(db.String(80), nullable=True)
    customer_phone = db.Column(db.String(80), nullable=True)
    payment_method = db.Column(db.String(120), nullable=True)
    seller_name = db.Column(db.String(160), nullable=True)
    seller_tax_id = db.Column(db.String(80), nullable=True)
    seller_phone = db.Column(db.String(80), nullable=True)
    seller_address = db.Column(db.String(400), nullable=True)
    issue_date = db.Column(db.Date, nullable=False)
    currency = db.Column(db.String(3), nullable=False)
    tax_rate = db.Column(db.Numeric(5, 2), nullable=False)
    created_at = db.Column(db.Date, nullable=False, default=date.today)
    owner_id = db.Column(db.Integer, db.ForeignKey("app_user.id"), nullable=True)
    items = db.relationship(
        "InvoiceItem", backref="invoice", cascade="all, delete-orphan", lazy=True
    )

    @property
    def subtotal(self):
        return sum((item.line_total for item in self.items), Decimal("0"))

    @property
    def tax_amount(self):
        return (self.subtotal * self.tax_rate / Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

    @property
    def total(self):
        return self.subtotal + self.tax_amount


class InvoiceItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoice.id"), nullable=False)
    description = db.Column(db.String(240), nullable=False)
    quantity = db.Column(db.Numeric(10, 2), nullable=False)
    unit_price = db.Column(db.Numeric(12, 2), nullable=False)

    @property
    def line_total(self):
        return (self.quantity * self.unit_price).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )


class AdminRecoveryUse(db.Model):
    __tablename__ = "admin_recovery_use"

    token_fingerprint = db.Column(db.String(64), primary_key=True)
    used_at = db.Column(db.Date, nullable=False, default=date.today)


@login_manager.user_loader
def load_user(user_id):
    if user_id.isdigit():
        return db.session.get(User, int(user_id))
    return User.query.filter_by(username=user_id).first()


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


@app.before_request
def ensure_database():
    db.create_all()
    columns = {column["name"] for column in inspect(db.engine).get_columns("invoice")}
    new_columns = {
        "owner_id": db.Integer(),
        "customer_contact": db.String(160),
        "customer_tax_id": db.String(80),
        "customer_phone": db.String(80),
        "payment_method": db.String(120),
        "seller_name": db.String(160),
        "seller_tax_id": db.String(80),
        "seller_phone": db.String(80),
        "seller_address": db.String(400),
    }
    missing_columns = {
        name: column_type for name, column_type in new_columns.items() if name not in columns
    }
    if missing_columns:
        with db.engine.begin() as connection:
            for name, column_type in missing_columns.items():
                sql_type = column_type.compile(dialect=db.engine.dialect)
                if name == "owner_id":
                    table_name = db.engine.dialect.identifier_preparer.quote(User.__tablename__)
                    sql_type += f" REFERENCES {table_name} (id)"
                connection.execute(
                    text(f'ALTER TABLE invoice ADD COLUMN "{name}" {sql_type}')
                )

    if User.query.count() == 0:
        password_hash = os.environ.get("APP_PASSWORD_HASH", "")
        username = os.environ.get("APP_USERNAME", "admin").strip()
        if password_hash and username:
            db.session.add(
                User(
                    username=username,
                    password_hash=password_hash,
                    role="admin",
                    is_active=True,
                )
            )
            db.session.commit()

    if current_user.is_authenticated:
        active_user = db.session.get(User, current_user.id)
        if active_user is None or not active_user.is_active:
            logout_user()


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()
        if user and user.is_active and check_password_hash(user.password_hash, password):
            login_user(user)
            return redirect(url_for("index"))
        if User.query.count() == 0 and not os.environ.get("APP_PASSWORD_HASH"):
            abort(503, description="Falta configurar APP_PASSWORD_HASH.")
        flash("Usuario o contraseña incorrectos.", "error")

    return render_template(
        "login.html",
        admin_recovery_enabled=bool(os.environ.get("ADMIN_RECOVERY_TOKEN")),
    )


@app.route("/admin/recovery", methods=["GET", "POST"])
def recover_admin_password():
    recovery_token = os.environ.get("ADMIN_RECOVERY_TOKEN", "")
    if not recovery_token:
        abort(404)

    token_fingerprint = hashlib.sha256(recovery_token.encode("utf-8")).hexdigest()
    if db.session.get(AdminRecoveryUse, token_fingerprint):
        abort(410, description="El token de recuperación ya fue utilizado.")

    if request.method == "POST":
        submitted_token = request.form.get("token", "")
        password = request.form.get("password", "")
        password_confirmation = request.form.get("password_confirmation", "")
        if not secrets.compare_digest(submitted_token, recovery_token):
            flash("Token de recuperación no válido.", "error")
        elif len(password) < 10:
            flash("La contraseña debe tener al menos 10 caracteres.", "error")
        elif password != password_confirmation:
            flash("Las contraseñas no coinciden.", "error")
        else:
            username = os.environ.get("APP_USERNAME", "admin").strip()
            admin = User.query.filter_by(
                username=username, role="admin", is_active=True
            ).first()
            if admin is None:
                abort(
                    409,
                    description=(
                        "No se encontró una cuenta administradora activa "
                        "para el valor configurado en APP_USERNAME."
                    ),
                )

            admin.password_hash = generate_password_hash(password)
            db.session.add(AdminRecoveryUse(token_fingerprint=token_fingerprint))
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                abort(410, description="El token de recuperación ya fue utilizado.")

            flash(
                "Contraseña del administrador actualizada. Ya puedes iniciar sesión.",
                "success",
            )
            return redirect(url_for("login"))

    return render_template("admin_recovery.html")


@app.post("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.get("/")
@login_required
def index():
    invoices_query = Invoice.query.order_by(Invoice.id.desc())
    if not current_user.is_admin:
        invoices_query = invoices_query.filter_by(owner_id=current_user.id)
    invoices = invoices_query.all()
    return render_template("index.html", invoices=invoices)


@app.get("/usuarios")
@admin_required
def manage_users():
    users = User.query.order_by(User.username).all()
    invoice_counts = dict(
        db.session.query(Invoice.owner_id, db.func.count(Invoice.id))
        .filter(Invoice.owner_id.is_not(None))
        .group_by(Invoice.owner_id)
        .all()
    )
    return render_template(
        "users.html",
        users=users,
        invoice_counts=invoice_counts,
        active_admin_count=User.query.filter_by(role="admin", is_active=True).count(),
    )


def validate_username(username):
    username = username.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", username):
        raise ValueError("El usuario debe tener entre 3 y 64 caracteres: letras, números, punto, guion o guion bajo.")
    return username


def validate_role(role):
    if role not in {"admin", "facturador"}:
        raise ValueError("Selecciona un rol válido.")
    return role


@app.post("/usuarios/nuevo")
@admin_required
def create_user():
    try:
        username = validate_username(request.form.get("username", ""))
        password = request.form.get("password", "")
        if len(password) < 10:
            raise ValueError("La contraseña debe tener al menos 10 caracteres.")
        role = validate_role(request.form.get("role", "facturador"))
        if User.query.filter_by(username=username).first():
            raise ValueError("Ese nombre de usuario ya está en uso.")
        user = User(
            username=username,
            password_hash=generate_password_hash(password),
            role=role,
            is_active=True,
        )
        db.session.add(user)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("Ese nombre de usuario ya está en uso.", "error")
            return redirect(url_for("manage_users"))
    except ValueError as error:
        db.session.rollback()
        flash(str(error), "error")
        return redirect(url_for("manage_users"))

    flash(f"Usuario {user.username} creado.", "success")
    return redirect(url_for("manage_users"))


@app.post("/usuarios/<int:user_id>/actualizar")
@admin_required
def update_user(user_id):
    user = db.get_or_404(User, user_id)
    try:
        username = validate_username(request.form.get("username", ""))
        role = validate_role(request.form.get("role", ""))
        password = request.form.get("password", "")
        is_active = request.form.get("is_active") == "on"
        existing = User.query.filter(
            User.username == username, User.id != user.id
        ).first()
        if existing:
            raise ValueError("Ese nombre de usuario ya está en uso.")
        if password and len(password) < 10:
            raise ValueError("La contraseña nueva debe tener al menos 10 caracteres.")
        if user.id == current_user.id and (role != "admin" or not is_active):
            raise ValueError("No puedes quitarte el rol de administrador ni desactivar tu propia cuenta.")
        if user.role == "admin" and user.is_active and (
            role != "admin" or not is_active
        ):
            if User.query.filter_by(role="admin", is_active=True).count() <= 1:
                raise ValueError("Debe quedar al menos un administrador activo.")
        user.username = username
        user.role = role
        user.is_active = is_active
        if password:
            user.password_hash = generate_password_hash(password)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("No se pudo guardar: ese nombre de usuario ya está en uso.", "error")
            return redirect(url_for("manage_users"))
    except ValueError as error:
        db.session.rollback()
        flash(str(error), "error")
        return redirect(url_for("manage_users"))

    flash(f"Usuario {user.username} actualizado.", "success")
    return redirect(url_for("manage_users"))


@app.post("/usuarios/<int:user_id>/eliminar")
@admin_required
def delete_user(user_id):
    user = db.get_or_404(User, user_id)
    if user.id == current_user.id:
        flash("No puedes eliminar tu propia cuenta.", "error")
        return redirect(url_for("manage_users"))
    if user.role == "admin" and user.is_active and (
        User.query.filter_by(role="admin", is_active=True).count() <= 1
    ):
        flash("Debe quedar al menos un administrador activo.", "error")
        return redirect(url_for("manage_users"))
    if Invoice.query.filter_by(owner_id=user.id).first():
        flash(
            "No se puede eliminar un usuario con facturas. Desactívalo para conservar su historial.",
            "error",
        )
        return redirect(url_for("manage_users"))

    username = user.username
    db.session.delete(user)
    db.session.commit()
    flash(f"Usuario {username} eliminado.", "success")
    return redirect(url_for("manage_users"))


@app.get("/facturas/<int:invoice_id>")
@login_required
def view_invoice(invoice_id):
    invoice = db.get_or_404(Invoice, invoice_id)
    if not current_user.is_admin and invoice.owner_id != current_user.id:
        abort(404)
    return render_template("invoice_detail.html", invoice=invoice)


@app.post("/facturas/<int:invoice_id>/duplicar")
@login_required
def duplicate_invoice(invoice_id):
    source = db.get_or_404(Invoice, invoice_id)
    if not current_user.is_admin and source.owner_id != current_user.id:
        abort(404)
    duplicate = Invoice(
        number="PENDIENTE",
        customer_name=source.customer_name,
        customer_email=source.customer_email,
        customer_address=source.customer_address,
        customer_contact=source.customer_contact,
        customer_tax_id=source.customer_tax_id,
        customer_phone=source.customer_phone,
        payment_method=source.payment_method,
        seller_name=source.seller_name,
        seller_tax_id=source.seller_tax_id,
        seller_phone=source.seller_phone,
        seller_address=source.seller_address,
        issue_date=date.today(),
        currency=source.currency,
        tax_rate=source.tax_rate,
        owner_id=current_user.id,
        items=[
            InvoiceItem(
                description=item.description,
                quantity=item.quantity,
                unit_price=item.unit_price,
            )
            for item in source.items
        ],
    )
    db.session.add(duplicate)
    db.session.flush()
    duplicate.number = f"FAC-{duplicate.id:05d}"
    db.session.commit()
    flash(f"Factura {duplicate.number} duplicada. Revisa sus datos antes de usarla.", "success")
    return redirect(url_for("edit_invoice", invoice_id=duplicate.id))


def parse_decimal(value, label, minimum=None, maximum=None):
    try:
        amount = Decimal(value.strip())
    except (InvalidOperation, AttributeError):
        raise ValueError(f"{label}: escribe un número válido.")
    if not amount.is_finite():
        raise ValueError(f"{label}: escribe un número válido.")
    if minimum is not None and amount < minimum:
        raise ValueError(f"{label}: debe ser como mínimo {minimum}.")
    if maximum is not None and amount > maximum:
        raise ValueError(f"{label}: no puede superar {maximum}.")
    return amount


def seller_defaults():
    return {
        "seller_name": os.environ.get("SELLER_NAME", ""),
        "seller_tax_id": os.environ.get("SELLER_TAX_ID", ""),
        "seller_phone": os.environ.get("SELLER_PHONE", ""),
        "seller_address": os.environ.get("SELLER_ADDRESS", ""),
    }


def invoice_form_values(invoice=None):
    if request.method == "POST":
        return request.form

    defaults = {
        "seller_name": seller_defaults()["seller_name"],
        "seller_tax_id": seller_defaults()["seller_tax_id"],
        "seller_phone": seller_defaults()["seller_phone"],
        "seller_address": seller_defaults()["seller_address"],
        "customer_name": "",
        "customer_contact": "",
        "customer_email": "",
        "customer_tax_id": "",
        "customer_phone": "",
        "payment_method": "",
        "issue_date": date.today().isoformat(),
        "currency": "USD",
        "tax_rate": "0",
    }
    if invoice is not None:
        for name in defaults:
            value = getattr(invoice, name, None)
            if value is not None:
                defaults[name] = value.isoformat() if name == "issue_date" else str(value)
    return defaults


def invoice_form_items(invoice=None):
    if request.method != "POST" and invoice is not None:
        return [
            {
                "description": item.description,
                "quantity": str(item.quantity),
                "unit_price": str(item.unit_price),
            }
            for item in invoice.items
        ] or [{"description": "", "quantity": "1", "unit_price": "0"}]

    descriptions = request.form.getlist("description[]")
    quantities = request.form.getlist("quantity[]")
    unit_prices = request.form.getlist("unit_price[]")
    if not descriptions:
        return [{"description": "", "quantity": "1", "unit_price": "0"}]
    return [
        {"description": description, "quantity": quantity, "unit_price": unit_price}
        for description, quantity, unit_price in zip(
            descriptions, quantities, unit_prices
        )
    ]


def invoice_data_from_form():
    customer_name = request.form.get("customer_name", "").strip()
    if not customer_name:
        raise ValueError("Escribe el nombre de la empresa del cliente.")
    currency = request.form.get("currency", "USD").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ValueError("La moneda debe ser un código de 3 letras, por ejemplo USD.")

    descriptions = request.form.getlist("description[]")
    quantities = request.form.getlist("quantity[]")
    unit_prices = request.form.getlist("unit_price[]")
    if not (len(descriptions) == len(quantities) == len(unit_prices)):
        raise ValueError("Revisa las líneas de la factura.")

    items = []
    for index, (description, quantity_text, price_text) in enumerate(
        zip(descriptions, quantities, unit_prices), start=1
    ):
        description = description.strip()
        if not description and not quantity_text.strip() and not price_text.strip():
            continue
        if not description:
            raise ValueError(f"Línea {index}: escribe una descripción.")
        quantity = parse_decimal(
            quantity_text, f"Cantidad de la línea {index}", Decimal("0.01")
        )
        unit_price = parse_decimal(
            price_text, f"Precio de la línea {index}", Decimal("0")
        )
        items.append(
            InvoiceItem(
                description=description,
                quantity=quantity,
                unit_price=unit_price,
            )
        )
    if not items:
        raise ValueError("Agrega al menos un producto o servicio.")

    seller = {
        name: request.form.get(name, default).strip()
        for name, default in seller_defaults().items()
    }
    return {
        "customer_name": customer_name,
        "customer_email": request.form.get("customer_email", "").strip() or None,
        "customer_contact": request.form.get("customer_contact", "").strip() or None,
        "customer_tax_id": request.form.get("customer_tax_id", "").strip() or None,
        "customer_phone": request.form.get("customer_phone", "").strip() or None,
        "payment_method": request.form.get("payment_method", "").strip() or None,
        "seller_name": seller["seller_name"] or None,
        "seller_tax_id": seller["seller_tax_id"] or None,
        "seller_phone": seller["seller_phone"] or None,
        "seller_address": seller["seller_address"] or None,
        "issue_date": date.fromisoformat(request.form.get("issue_date", "")),
        "currency": currency,
        "tax_rate": parse_decimal(
            request.form.get("tax_rate", ""),
            "Impuesto (%)",
            Decimal("0"),
            Decimal("100"),
        ),
        "items": items,
    }


def render_invoice_form(invoice=None):
    return render_template(
        "new_invoice.html",
        today=date.today().isoformat(),
        invoice=invoice,
        form_values=invoice_form_values(invoice),
        item_rows=invoice_form_items(invoice),
    )


@app.route("/facturas/nueva", methods=["GET", "POST"])
@login_required
def new_invoice():
    if request.method == "POST":
        try:
            data = invoice_data_from_form()
        except (ValueError, TypeError) as error:
            flash(str(error), "error")
            return render_invoice_form()

        invoice = Invoice(number="PENDIENTE", owner_id=current_user.id, **data)
        db.session.add(invoice)
        db.session.flush()
        invoice.number = f"FAC-{invoice.id:05d}"
        db.session.commit()
        flash(f"Factura {invoice.number} creada.", "success")
        return redirect(url_for("index"))

    return render_invoice_form()


@app.route("/facturas/<int:invoice_id>/editar", methods=["GET", "POST"])
@login_required
def edit_invoice(invoice_id):
    invoice = db.get_or_404(Invoice, invoice_id)
    if not current_user.is_admin and invoice.owner_id != current_user.id:
        abort(404)
    if request.method == "POST":
        try:
            data = invoice_data_from_form()
        except (ValueError, TypeError) as error:
            flash(str(error), "error")
            return render_invoice_form(invoice)

        for name, value in data.items():
            setattr(invoice, name, value)
        db.session.commit()
        (INSTANCE_DIR / f"factura-{invoice.number}.pdf").unlink(missing_ok=True)
        flash(f"Factura {invoice.number} actualizada.", "success")
        return redirect(url_for("index"))

    return render_invoice_form(invoice)


@app.post("/facturas/<int:invoice_id>/eliminar")
@login_required
def delete_invoice(invoice_id):
    invoice = db.get_or_404(Invoice, invoice_id)
    if not current_user.is_admin and invoice.owner_id != current_user.id:
        abort(404)
    number = invoice.number
    db.session.delete(invoice)
    db.session.commit()
    (INSTANCE_DIR / f"factura-{number}.pdf").unlink(missing_ok=True)
    flash(f"Factura {number} eliminada.", "success")
    return redirect(url_for("index"))


def format_money(amount, currency):
    formatted = f"{amount:,.2f}".replace(",", "\0").replace(".", ",").replace("\0", " ")
    symbol = "$" if currency == "USD" else f"{currency} "
    return f"{symbol}{formatted}"


def format_quantity(quantity):
    return f"{quantity.normalize():f}"


app.jinja_env.globals["format_money"] = format_money
app.jinja_env.globals["format_quantity"] = format_quantity


@app.get("/facturas/<int:invoice_id>/pdf")
@login_required
def invoice_pdf(invoice_id):
    invoice = db.get_or_404(Invoice, invoice_id)
    if not current_user.is_admin and invoice.owner_id != current_user.id:
        abort(404)
    output_path = INSTANCE_DIR / f"factura-{invoice.number}.pdf"
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="Small", parent=styles["Normal"], fontSize=8, leading=10))
    styles.add(
        ParagraphStyle(
            name="TableHeader",
            parent=styles["Small"],
            textColor=colors.white,
            alignment=1,
            fontName="Helvetica-Bold",
        )
    )
    blue = colors.HexColor("#65b8ef")
    black = colors.HexColor("#111111")

    def safe_paragraph(value, style="Normal"):
        return Paragraph(escape(str(value)).replace("\n", "<br/>"), styles[style])

    def label_value(label, value):
        return Paragraph(
            f"<b>{escape(label)}</b> {escape(str(value or ''))}",
            styles["Small"],
        )

    document = SimpleDocTemplate(
        str(output_path),
        pagesize=letter,
        rightMargin=14.5 * mm,
        leftMargin=14.5 * mm,
        topMargin=16 * mm,
        bottomMargin=14 * mm,
        title=f"Factura {invoice.number}",
    )
    seller_details = [
        label_value("Nombre:", invoice.seller_name),
        label_value("Número de identificación:", invoice.seller_tax_id),
        label_value("Teléfono:", invoice.seller_phone),
        label_value("Dirección:", invoice.seller_address),
    ]
    invoice_label = Paragraph(
        f'<font color="white"><b>FACTURA No.</b></font>',
        styles["Small"],
    )
    invoice_number = Paragraph(
        f'<font color="#d52222"><b>{escape(invoice.number.removeprefix("FAC-"))}</b></font>',
        ParagraphStyle(
            name="InvoiceNumber",
            parent=styles["Small"],
            alignment=1,
            fontSize=11,
            leading=13,
        ),
    )
    invoice_box = Table([[invoice_label, invoice_number]], colWidths=[33 * mm, 36 * mm])
    invoice_box.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, 0), colors.HexColor("#00a8e8")),
                ("BACKGROUND", (1, 0), (1, 0), colors.white),
                ("BOX", (0, 0), (-1, -1), 1.2, black),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    top = Table(
        [
            [
                Table([[entry] for entry in seller_details], colWidths=[117 * mm]),
                invoice_box,
            ]
        ],
        colWidths=[118 * mm, 69 * mm],
    )
    top.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    customer_rows = [
        [Paragraph("<b>CLIENTE</b>", styles["Small"]), "", "", ""],
        [
            label_value("EMPRESA:", invoice.customer_name),
            "",
            label_value("FECHA:", invoice.issue_date.strftime("%d/%m/%Y")),
            "",
        ],
        [
            label_value("CONTACTO:", invoice.customer_contact),
            "",
            label_value("RUC:", invoice.customer_tax_id),
            "",
        ],
        [
            label_value("CORREO:", invoice.customer_email),
            "",
            label_value("FORMA DE PAGO:", invoice.payment_method),
            "",
        ],
        [
            label_value("TELÉFONO:", invoice.customer_phone),
            "",
            "",
            "",
        ],
    ]
    customer_table = Table(customer_rows, colWidths=[25 * mm, 69 * mm, 34 * mm, 59 * mm])
    customer_table.setStyle(
        TableStyle(
            [
                ("SPAN", (0, 0), (-1, 0)),
                ("SPAN", (0, 1), (1, 1)),
                ("SPAN", (2, 1), (3, 1)),
                ("SPAN", (0, 2), (1, 2)),
                ("SPAN", (2, 2), (3, 2)),
                ("SPAN", (0, 3), (1, 3)),
                ("SPAN", (2, 3), (3, 3)),
                ("SPAN", (0, 4), (1, 4)),
                ("SPAN", (2, 4), (3, 4)),
                ("GRID", (0, 0), (-1, -1), 0.8, black),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )

    item_rows = [[
        safe_paragraph("Item #", "TableHeader"),
        safe_paragraph("Cantidad", "TableHeader"),
        safe_paragraph("Descripción", "TableHeader"),
        safe_paragraph("Precio Unitario", "TableHeader"),
        safe_paragraph("Monto", "TableHeader"),
    ]]
    for item in invoice.items:
        quantity = f"{item.quantity.normalize():f}"
        item_rows.append(
            [
                str(len(item_rows)),
                quantity,
                safe_paragraph(item.description, "Small"),
                format_money(item.unit_price, invoice.currency),
                format_money(item.line_total, invoice.currency),
            ]
        )
    item_table = Table(
        item_rows,
        colWidths=[24 * mm, 23 * mm, 84 * mm, 31 * mm, 25 * mm],
        repeatRows=1,
    )
    item_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), blue),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.8, black),
                ("ALIGN", (0, 1), (1, -1), "CENTER"),
                ("ALIGN", (3, 1), (-1, -1), "RIGHT"),
                ("ALIGN", (2, 1), (2, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )

    total_rows = [["", "", "", "Sub total", format_money(invoice.subtotal, invoice.currency)]]
    if invoice.tax_rate:
        total_rows.append(
            [
                "",
                "",
                "",
                f"Impuesto ({invoice.tax_rate}%)",
                format_money(invoice.tax_amount, invoice.currency),
            ]
        )
    total_rows.append(["", "", "", "TOTAL:", format_money(invoice.total, invoice.currency)])
    signature_style = ParagraphStyle(
        name="Signature",
        parent=styles["Small"],
        textColor=colors.white,
        backColor=blue,
        borderPadding=2,
    )
    total_rows[0][0] = Paragraph("<b>Firma Autorizada:</b>", signature_style)
    footer_table = Table(total_rows, colWidths=[24 * mm, 23 * mm, 84 * mm, 31 * mm, 25 * mm])
    footer_table.setStyle(
        TableStyle(
            [
                ("SPAN", (0, 0), (2, -1)),
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
                ("GRID", (0, 0), (-1, -1), 0.8, black),
                ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("BACKGROUND", (3, -1), (-1, -1), blue),
            ]
        )
    )
    story = [
        top,
        Spacer(1, 9 * mm),
        customer_table,
        Spacer(1, 7 * mm),
        item_table,
        footer_table,
    ]
    document.build(story)
    return send_file(
        output_path,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"factura-{invoice.number}.pdf",
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)
