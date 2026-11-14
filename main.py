from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
import sqlite3
import os
import base64
import requests

os.makedirs("static", exist_ok=True)

app = FastAPI(title="Techno Zone Store")
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET", "ryad_super_secret")
)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

TURSO_URL = os.environ.get("TURSO_DATABASE_URL", "").replace("libsql://", "https://")
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
            return {"type": "integer", "value": "1" if val else "0"}
        if isinstance(val, int):
            return {"type": "integer", "value": str(val)}
        if isinstance(val, float):
            return {"type": "float", "value": val}
        return {"type": "text", "value": str(val)}

    def execute(self, sql, params=()):
        args = [self._format_arg(p) for p in params]
        payload = {
            "requests": [
                {"type": "execute", "stmt": {"sql": sql, "args": args}},
                {"type": "close"}
            ]
        }
        resp = requests.post(
            self.url, headers=self.headers, json=payload, timeout=20
        )
        resp.raise_for_status()
        data = resp.json()
        res = data["results"][0]

        if res["type"] == "error":
            raise Exception(res["error"]["message"])

        result = res["response"]["result"]
        cols = [c["name"] for c in result.get("cols", [])]
        rows = []

        for raw in result.get("rows", []):
            row = {}
            for name, cell in zip(cols, raw):
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

    conn = sqlite3.connect("technozone.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


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
            image_url TEXT
        )
    """)

    try:
        cur.execute("ALTER TABLE items ADD COLUMN product_id INTEGER")
    except Exception:
        pass

    try:
        cur.execute("ALTER TABLE items ADD COLUMN image_url TEXT")
    except Exception:
        pass

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
            SELECT id FROM products
            WHERE name=? AND price=? AND
            ((image_url=?) OR (image_url IS NULL AND ? IS NULL))
            LIMIT 1
        """, (name, price, image_url, image_url)).fetchone()

        if product:
            product_id = product["id"]
        else:
            cur.execute("""
                INSERT INTO products (name, price, image_url)
                VALUES (?, ?, ?)
            """, (name, price, image_url))

            product_id = cur.execute(
                "SELECT last_insert_rowid() AS id"
            ).fetchone()["id"]

        cur.execute("""
            UPDATE items SET product_id=?
            WHERE product_id IS NULL
            AND name=? AND price=?
            AND ((image_url=?) OR (image_url IS NULL AND ? IS NULL))
        """, (product_id, name, price, image_url, image_url))

    admin = cur.execute(
        "SELECT id FROM users WHERE username='admin'"
    ).fetchone()

    if not admin:
        cur.execute("""
            INSERT INTO users (username, password, is_admin)
            VALUES ('admin', '123456', 1)
        """)

    conn.commit()
    conn.close()


init_db()


def get_user(request: Request):
    return request.session.get("user")


@app.get("/")
def home(request: Request):
    return RedirectResponse(
        "/store" if get_user(request) else "/login"
    )


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
    user = conn.execute(
        "SELECT * FROM users WHERE username=? AND password=?",
        (username, password)
    ).fetchone()
    conn.close()

    if user:
        request.session["user"] = {
            "id": user["id"],
            "username": user["username"],
            "is_admin": user["is_admin"]
        }
        return RedirectResponse("/store", status_code=303)

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": "بيانات الدخول خاطئة"}
    )


@app.post("/register")
def register_action(
    request: Request,
    username: str = Form(...),
    password: str = Form(...)
):
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users (username, password) VALUES (?, ?)",
            (username, password)
        )
        conn.commit()
        conn.close()
        return RedirectResponse("/login", status_code=303)
    except Exception:
        conn.close()
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "اسم المستخدم مستخدم مسبقاً"}
        )


@app.get("/store", response_class=HTMLResponse)
def store_page(request: Request):
    user = get_user(request)
    if not user:
        return RedirectResponse("/login")

    conn = get_db()

    db_user = conn.execute(
        "SELECT * FROM users WHERE id=?", (user["id"],)
    ).fetchone()

    products = conn.execute("""
        SELECT
            p.id, p.name, p.price, p.image_url,
            COUNT(i.id) AS stock
        FROM products p
        LEFT JOIN items i
            ON i.product_id=p.id AND i.is_sold=0
        GROUP BY p.id, p.name, p.price, p.image_url
    """).fetchall()

    my_items = conn.execute("""
        SELECT i.*, p.name AS product_name
        FROM items i
        LEFT JOIN products p ON p.id=i.product_id
        WHERE i.buyer_id=?
        ORDER BY i.id DESC
    """, (user["id"],)).fetchall()

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


@app.post("/buy")
def buy_item(
    request: Request,
    product_id: int = Form(...)
):
    user = get_user(request)
    if not user:
        return RedirectResponse("/login")

    conn = get_db()

    db_user = conn.execute(
        "SELECT * FROM users WHERE id=?", (user["id"],)
    ).fetchone()

    product = conn.execute(
        "SELECT * FROM products WHERE id=?", (product_id,)
    ).fetchone()

    item = conn.execute("""
        SELECT * FROM items
        WHERE product_id=? AND is_sold=0
        ORDER BY id ASC LIMIT 1
    """, (product_id,)).fetchone()

    if (
        product and item and db_user
        and db_user["balance"] >= product["price"]
    ):
        conn.execute(
            "UPDATE users SET balance=balance-? WHERE id=?",
            (product["price"], user["id"])
        )
        conn.execute("""
            UPDATE items
            SET is_sold=1, buyer_id=?
            WHERE id=? AND is_sold=0
        """, (user["id"], item["id"]))
        conn.commit()

    conn.close()
    return RedirectResponse("/store", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    user = get_user(request)
    if not user or not user["is_admin"]:
        return RedirectResponse("/store")

    conn = get_db()

    products = conn.execute("""
        SELECT p.*,
        COUNT(i.id) AS total_codes,
        SUM(CASE WHEN i.is_sold=0 THEN 1 ELSE 0 END) AS available_codes
        FROM products p
        LEFT JOIN items i ON i.product_id=p.id
        GROUP BY p.id
        ORDER BY p.id DESC
    """).fetchall()

    users = conn.execute("""
        SELECT id, username, balance
        FROM users WHERE is_admin=0
    """).fetchall()

    conn.close()

    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={"products": products, "users": users}
    )


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

    image_url = None

    if image and image.filename:
        content = await image.read()
        if content:
            mime_type = image.content_type or "image/png"
            b64_data = base64.b64encode(content).decode("utf-8")
            image_url = f"data:{mime_type};base64,{b64_data}"

    conn = get_db()
    conn.execute("""
        INSERT INTO products (name, price, image_url)
        VALUES (?, ?, ?)
    """, (name, price, image_url))
    conn.commit()
    conn.close()

    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/add_content")
def admin_add_content(
    request: Request,
    product_id: int = Form(...),
    secret_code: str = Form(...)
):
    user = get_user(request)
    if not user or not user["is_admin"]:
        return RedirectResponse("/store")

    conn = get_db()

    product = conn.execute(
        "SELECT * FROM products WHERE id=?",
        (product_id,)
    ).fetchone()

    if product:
        conn.execute("""
            INSERT INTO items
            (name, secret_code, price, image_url, product_id)
            VALUES (?, ?, ?, ?, ?)
        """, (
            product["name"],
            secret_code,
            product["price"],
            product["image_url"],
            product_id
        ))
        conn.commit()

    conn.close()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/add_balance")
def admin_add_balance(
    request: Request,
    user_id: int = Form(...),
    amount: float = Form(...)
):
    user = get_user(request)
    if user and user["is_admin"] and amount > 0:
        conn = get_db()
        conn.execute(
            "UPDATE users SET balance=balance+? WHERE id=?",
            (amount, user_id)
        )
        conn.commit()
        conn.close()

    return RedirectResponse("/admin", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login")
