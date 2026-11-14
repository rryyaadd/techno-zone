from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

import sqlite3
import os
import base64
import requests


# =========================
# إعداد التطبيق
# =========================

os.makedirs("static", exist_ok=True)

app = FastAPI(title="Techno Zone Store")

app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET", "CHANGE_THIS_SESSION_SECRET")
)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# =========================
# إعداد Turso
# =========================

TURSO_URL = os.environ.get(
    "TURSO_DATABASE_URL", ""
).replace("libsql://", "https://").rstrip("/")

TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "")


class TursoCursor:
    def __init__(self, rows=None):
        self._rows = rows or []
        self._index = 0

    def fetchone(self):
        if self._index < len(self._rows):
            row = self._rows[self._index]
            self._index += 1
            return row
        return None

    def fetchall(self):
        return self._rows


class TursoConnection:
    def __init__(self, url, token):
        self.url = f"{url}/v2/pipeline"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

    def _format_arg(self, val):
        if val is None:
            return {"type": "null"}

        if isinstance(val, bool):
            return {
                "type": "integer",
                "value": "1" if val else "0"
            }

        if isinstance(val, int):
            return {"type": "integer", "value": str(val)}

        if isinstance(val, float):
            return {"type": "float", "value": val}

        return {"type": "text", "value": str(val)}

    def execute(self, sql, params=()):
        args = [self._format_arg(p) for p in params]

        payload = {
            "requests": [
                {
                    "type": "execute",
                    "stmt": {
                        "sql": sql,
                        "args": args
                    }
                },
                {"type": "close"}
            ]
        }

        response = requests.post(
            self.url,
            headers=self.headers,
            json=payload,
            timeout=30
        )

        response.raise_for_status()

        data = response.json()
        result_response = data["results"][0]

        if result_response["type"] == "error":
            raise Exception(
                result_response["error"]["message"]
            )

        result = result_response["response"]["result"]

        columns = [
            column["name"]
            for column in result.get("cols", [])
        ]

        rows = []

        for raw_row in result.get("rows", []):
            row = {}

            for name, cell in zip(columns, raw_row):
                value = cell.get("value")

                if cell["type"] == "integer" and value is not None:
                    value = int(value)

                elif cell["type"] == "float" and value is not None:
                    value = float(value)

                row[name] = value

            rows.append(row)

        return TursoCursor(rows)

    def cursor(self):
        return self

    def commit(self):
        pass

    def close(self):
        pass


def get_db():
    if TURSO_URL and TURSO_TOKEN:
        return TursoConnection(TURSO_URL, TURSO_TOKEN)

    conn = sqlite3.connect(
        "technozone.db",
        check_same_thread=False
    )

    conn.row_factory = sqlite3.Row
    return conn


# =========================
# تهيئة قاعدة البيانات
# =========================

def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            balance REAL DEFAULT 0.0,
            is_admin INTEGER DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price REAL NOT NULL DEFAULT 0,
            image_url TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            secret_code TEXT NOT NULL,
            price REAL NOT NULL DEFAULT 0,
            is_sold INTEGER DEFAULT 0,
            buyer_id INTEGER,
            image_url TEXT,
            product_id INTEGER
        )
    """)

    # التأكد من وجود الأعمدة في قواعد البيانات القديمة

    try:
        cur.execute(
            "ALTER TABLE items ADD COLUMN product_id INTEGER"
        )
    except Exception:
        pass

    try:
        cur.execute(
            "ALTER TABLE items ADD COLUMN image_url TEXT"
        )
    except Exception:
        pass

    # ترحيل السلع القديمة إلى جدول products

    old_groups = cur.execute("""
        SELECT DISTINCT name, price, image_url
        FROM items
        WHERE product_id IS NULL
    """).fetchall()

    for old in old_groups:
        name = old["name"]
        price = old["price"]
        image_url = old["image_url"]

        product = cur.execute("""
            SELECT id
            FROM products
            WHERE name = ?
            AND price = ?
            AND (
                image_url = ?
                OR (image_url IS NULL AND ? IS NULL)
            )
            LIMIT 1
        """, (
            name,
            price,
            image_url,
            image_url
        )).fetchone()

        if product:
            product_id = product["id"]

        else:
            inserted = cur.execute("""
                INSERT INTO products (name, price, image_url)
                VALUES (?, ?, ?)
                RETURNING id
            """, (
                name,
                price,
                image_url
            )).fetchone()

            product_id = inserted["id"]

        cur.execute("""
            UPDATE items
            SET product_id = ?
            WHERE product_id IS NULL
            AND name = ?
            AND price = ?
            AND (
                image_url = ?
                OR (image_url IS NULL AND ? IS NULL)
            )
        """, (
            product_id,
            name,
            price,
            image_url,
            image_url
        ))

    # إنشاء حساب الإدارة فقط إذا لم يكن موجوداً

    admin = cur.execute("""
        SELECT id
        FROM users
        WHERE is_admin = 1
        LIMIT 1
    """).fetchone()

    if not admin:
        existing_admin = cur.execute("""
            SELECT id
            FROM users
            WHERE username = 'admin'
        """).fetchone()

        if not existing_admin:
            cur.execute("""
                INSERT INTO users
                (username, password, is_admin)
                VALUES (?, ?, 1)
            """, (
                "admin",
                "CHANGE_ADMIN_PASSWORD"
            ))

    conn.commit()
    conn.close()


init_db()


# =========================
# المستخدم والجلسات
# =========================

def get_user(request: Request):
    return request.session.get("user")


@app.get("/")
def home(request: Request):
    if get_user(request):
        return RedirectResponse("/store")

    return RedirectResponse("/login")


# =========================
# تسجيل الدخول
# =========================

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": None}
    )


@app.post("/login")
def login_action(
    request: Request,
    username: str = Form(...),
    password: str = Form(...)
):
    conn = get_db()

    user = conn.execute("""
        SELECT *
        FROM users
        WHERE username = ?
        AND password = ?
    """, (
        username,
        password
    )).fetchone()

    conn.close()

    if user:
        request.session["user"] = {
            "id": user["id"],
            "username": user["username"],
            "is_admin": user["is_admin"]
        }

        return RedirectResponse(
            "/store",
            status_code=303
        )

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": "بيانات الدخول خاطئة"}
    )


# =========================
# إنشاء حساب
# =========================

@app.post("/register")
def register_action(
    request: Request,
    username: str = Form(...),
    password: str = Form(...)
):
    conn = get_db()

    try:
        conn.execute("""
            INSERT INTO users (username, password)
            VALUES (?, ?)
        """, (
            username,
            password
        ))

        conn.commit()
        conn.close()

        return RedirectResponse(
            "/login",
            status_code=303
        )

    except Exception:
        conn.close()

        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "error": "اسم المستخدم مستخدم مسبقاً"
            }
        )


# =========================
# صفحة المتجر
# =========================

@app.get("/store", response_class=HTMLResponse)
def store_page(request: Request):
    user = get_user(request)

    if not user:
        return RedirectResponse("/login")

    conn = get_db()

    db_user = conn.execute("""
        SELECT *
        FROM users
        WHERE id = ?
    """, (
        user["id"],
    )).fetchone()

    products = conn.execute("""
        SELECT
            p.id,
            p.name,
            p.price,
            p.image_url,
            COUNT(
                CASE WHEN i.is_sold = 0 THEN 1 END
            ) AS stock
        FROM products p
        LEFT JOIN items i
            ON i.product_id = p.id
        GROUP BY
            p.id,
            p.name,
            p.price,
            p.image_url
        ORDER BY p.id DESC
    """).fetchall()

    my_items = conn.execute("""
        SELECT
            i.*,
            p.name AS product_name
        FROM items i
        LEFT JOIN products p
            ON p.id = i.product_id
        WHERE i.buyer_id = ?
        ORDER BY i.id DESC
    """, (
        user["id"],
    )).fetchall()

    conn.close()

    return templates.TemplateResponse(
        request=request,
        name="store.html",
        context={
            "user": db_user,
            "items": products,
            "my_items": my_items
        }
    )


# =========================
# شراء كود
# =========================

@app.post("/buy")
def buy_item(
    request: Request,
    product_id: int = Form(...)
):
    user = get_user(request)

    if not user:
        return RedirectResponse("/login")

    conn = get_db()

    try:
        db_user = conn.execute("""
            SELECT *
            FROM users
            WHERE id = ?
        """, (
            user["id"],
        )).fetchone()

        product = conn.execute("""
            SELECT *
            FROM products
            WHERE id = ?
        """, (
            product_id,
        )).fetchone()

        item = conn.execute("""
            SELECT *
            FROM items
            WHERE product_id = ?
            AND is_sold = 0
            ORDER BY id ASC
            LIMIT 1
        """, (
            product_id,
        )).fetchone()

        if not db_user or not product or not item:
            return RedirectResponse(
                "/store",
                status_code=303
            )

        if db_user["balance"] < product["price"]:
            return RedirectResponse(
                "/store",
                status_code=303
            )

        # تحديث حالة الكود

        conn.execute("""
            UPDATE items
            SET is_sold = 1,
                buyer_id = ?
            WHERE id = ?
            AND is_sold = 0
        """, (
            user["id"],
            item["id"]
        ))

        # خصم قيمة السلعة

        conn.execute("""
            UPDATE users
            SET balance = balance - ?
            WHERE id = ?
        """, (
            product["price"],
            user["id"]
        ))

        conn.commit()

    finally:
        conn.close()

    return RedirectResponse(
        "/store",
        status_code=303
    )


# =========================
# لوحة الإدارة
# =========================

@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    user = get_user(request)

    if not user or not user["is_admin"]:
        return RedirectResponse("/store")

    conn = get_db()

    products = conn.execute("""
        SELECT
            p.*,
            COUNT(i.id) AS total_codes,
            SUM(
                CASE WHEN i.is_sold = 0 THEN 1 ELSE 0 END
            ) AS available_codes
        FROM products p
        LEFT JOIN items i
            ON i.product_id = p.id
        GROUP BY p.id
        ORDER BY p.id DESC
    """).fetchall()

    users = conn.execute("""
        SELECT
            id,
            username,
            balance
        FROM users
        WHERE is_admin = 0
        ORDER BY id DESC
    """).fetchall()

    conn.close()

    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={
            "products": products,
            "users": users
        }
    )


# =========================
# إضافة سلعة جديدة
# =========================

@app.post("/admin/add_product")
async def admin_add_product(
    request: Request,
    name: str = Form(...),
    price: float = Form(...),
    image: UploadFile = File(None)
):
    user = get_user(request)

    if not user or not user["is_admin"]:
        return RedirectResponse("/store")

    name = name.strip()

    if not name or price < 0:
        return RedirectResponse(
            "/admin",
            status_code=303
        )

    image_url = None

    if image and image.filename:
        content = await image.read()

        if content:
            mime_type = image.content_type or "image/png"

            encoded = base64.b64encode(
                content
            ).decode("utf-8")

            image_url = (
                f"data:{mime_type};base64,{encoded}"
            )

    conn = get_db()

    try:
        conn.execute("""
            INSERT INTO products
            (name, price, image_url)
            VALUES (?, ?, ?)
        """, (
            name,
            price,
            image_url
        ))

        conn.commit()

    finally:
        conn.close()

    return RedirectResponse(
        "/admin",
        status_code=303
    )


# =========================
# إضافة كود إلى سلعة
# =========================

@app.post("/admin/add_content")
def admin_add_content(
    request: Request,
    product_id: int = Form(...),
    secret_code: str = Form(...)
):
    user = get_user(request)

    if not user or not user["is_admin"]:
        return RedirectResponse("/store")

    secret_code = secret_code.strip()

    if not secret_code:
        return RedirectResponse(
            "/admin",
            status_code=303
        )

    conn = get_db()

    try:
        product = conn.execute("""
            SELECT *
            FROM products
            WHERE id = ?
        """, (
            product_id,
        )).fetchone()

        if product:
            conn.execute("""
                INSERT INTO items
                (
                    name,
                    secret_code,
                    price,
                    image_url,
                    product_id,
                    is_sold
                )
                VALUES (?, ?, ?, ?, ?, 0)
            """, (
                product["name"],
                secret_code,
                product["price"],
                product["image_url"],
                product_id
            ))

            conn.commit()

    finally:
        conn.close()

    return RedirectResponse(
        "/admin",
        status_code=303
    )


# =========================
# شحن رصيد المستخدم
# =========================

@app.post("/admin/add_balance")
def admin_add_balance(
    request: Request,
    user_id: int = Form(...),
    amount: float = Form(...)
):
    user = get_user(request)

    if not user or not user["is_admin"]:
        return RedirectResponse("/store")

    if amount <= 0:
        return RedirectResponse(
            "/admin",
            status_code=303
        )

    conn = get_db()

    try:
        conn.execute("""
            UPDATE users
            SET balance = balance + ?
            WHERE id = ?
            AND is_admin = 0
        """, (
            amount,
            user_id
        ))

        conn.commit()

    finally:
        conn.close()

    return RedirectResponse(
        "/admin",
        status_code=303
    )


# =========================
# تسجيل الخروج
# =========================

@app.get("/logout")
def logout(request: Request):
    request.session.clear()

    return RedirectResponse(
        "/login",
        status_code=303
    )
