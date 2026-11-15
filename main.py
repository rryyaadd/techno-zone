from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

import sqlite3
import os
import asyncio
import base64
import requests


# ==========================================
# إعداد التطبيق
# ==========================================

os.makedirs("static", exist_ok=True)
os.makedirs("templates", exist_ok=True)

app = FastAPI(title="Techno Zone Store")

app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get(
        "SESSION_SECRET",
        "CHANGE_THIS_SESSION_SECRET"
    )
)

app.mount(
    "/static",
    StaticFiles(directory="static"),
    name="static"
)

templates = Jinja2Templates(directory="templates")

WHATSAPP_NUMBER = "967778105690"
WHATSAPP_LINK = "https://wa.me/967778105690"


# ==========================================
# إعداد Turso
# ==========================================

TURSO_URL = os.environ.get(
    "TURSO_DATABASE_URL", ""
).replace("libsql://", "https://").rstrip("/")

TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "")


class TursoCursor:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.index = 0

    def fetchone(self):
        if self.index < len(self.rows):
            row = self.rows[self.index]
            self.index += 1
            return row
        return None

    def fetchall(self):
        return self.rows


class TursoConnection:
    def __init__(self, url, token):
        self.url = url + "/v2/pipeline"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

    def _arg(self, value):
        if value is None:
            return {"type": "null"}

        if isinstance(value, bool):
            return {
                "type": "integer",
                "value": "1" if value else "0"
            }

        if isinstance(value, int):
            return {
                "type": "integer",
                "value": str(value)
            }

        if isinstance(value, float):
            return {
                "type": "float",
                "value": value
            }

        return {
            "type": "text",
            "value": str(value)
        }

    def execute(self, sql, params=()):
        payload = {
            "requests": [
                {
                    "type": "execute",
                    "stmt": {
                        "sql": sql,
                        "args": [
                            self._arg(value)
                            for value in params
                        ]
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
        item = data["results"][0]

        if item["type"] == "error":
            raise Exception(item["error"]["message"])

        result = item["response"]["result"]

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

    db = sqlite3.connect(
        "technozone.db",
        check_same_thread=False
    )

    db.row_factory = sqlite3.Row
    return db


def redirect(path):
    return RedirectResponse(
        path,
        status_code=303
    )


# ==========================================
# المستخدمون والصلاحيات
# ==========================================

def get_user(request):
    return request.session.get("user")


def is_admin(request):
    user = get_user(request)
    return bool(user and user.get("is_admin"))


def require_login(request):
    return bool(get_user(request))


# ==========================================
# إنشاء الجداول وتحديث قاعدة البيانات
# ==========================================

def init_db():
    db = get_db()
    c = db.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        balance REAL DEFAULT 0,
        is_admin INTEGER DEFAULT 0
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS products(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        price REAL NOT NULL DEFAULT 0,
        image_url TEXT,
        description TEXT,
        requires_chat INTEGER DEFAULT 0,
        requires_content INTEGER DEFAULT 1
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS items(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        secret_code TEXT NOT NULL,
        price REAL NOT NULL DEFAULT 0,
        is_sold INTEGER DEFAULT 0,
        buyer_id INTEGER,
        image_url TEXT,
        product_id INTEGER,
        sold_at TEXT
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS purchases(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        product_id INTEGER,
        product_name TEXT NOT NULL,
        price REAL NOT NULL,
        item_id INTEGER,
        secret_code TEXT,
        hidden INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now'))
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS conversations(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        product_id INTEGER,
        product_name TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        hidden INTEGER NOT NULL DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now'))
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER NOT NULL,
        sender_id INTEGER NOT NULL,
        sender_role TEXT NOT NULL,
        message TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now'))
    )
    """)

    # أعمدة إضافية لقواعد البيانات القديمة
    migrations = [
        "ALTER TABLE products ADD COLUMN image_url TEXT",
        "ALTER TABLE products ADD COLUMN description TEXT",
        "ALTER TABLE products ADD COLUMN requires_chat INTEGER DEFAULT 0",
        "ALTER TABLE products ADD COLUMN requires_content INTEGER DEFAULT 1",

        "ALTER TABLE items ADD COLUMN buyer_id INTEGER",
        "ALTER TABLE items ADD COLUMN image_url TEXT",
        "ALTER TABLE items ADD COLUMN product_id INTEGER",
        "ALTER TABLE items ADD COLUMN sold_at TEXT",

        "ALTER TABLE purchases ADD COLUMN hidden INTEGER DEFAULT 0",
        "ALTER TABLE conversations ADD COLUMN hidden INTEGER DEFAULT 0"
    ]

    for sql in migrations:
        try:
            c.execute(sql)
        except Exception:
            pass

    # ربط الأكواد القديمة بالمنتجات التي تحمل الاسم نفسه
    try:
        c.execute("""
            UPDATE items
            SET product_id = (
                SELECT products.id
                FROM products
                WHERE products.name = items.name
                ORDER BY products.id DESC
                LIMIT 1
            )
            WHERE product_id IS NULL
        """)
    except Exception as error:
        print("Product linking migration:", error)

    # نقل المشتريات القديمة إلى جدول purchases
    try:
        old_items = c.execute("""
            SELECT *
            FROM items
            WHERE is_sold=1
            AND buyer_id IS NOT NULL
        """).fetchall()

        for item in old_items:
            exists = c.execute("""
                SELECT id
                FROM purchases
                WHERE item_id=?
            """, (item["id"],)).fetchone()

            if not exists:
                c.execute("""
                    INSERT INTO purchases(
                        user_id,
                        product_id,
                        product_name,
                        price,
                        item_id,
                        secret_code
                    )
                    VALUES(?,?,?,?,?,?)
                """, (
                    item["buyer_id"],
                    item["product_id"],
                    item["name"],
                    item["price"],
                    item["id"],
                    item["secret_code"]
                ))

    except Exception as error:
        print("Purchase migration:", error)

    # إنشاء حساب المدير الافتراضي إذا لم يوجد مدير
    admin = c.execute("""
        SELECT id
        FROM users
        WHERE is_admin=1
        LIMIT 1
    """).fetchone()

    if not admin:
        old_admin = c.execute("""
            SELECT id
            FROM users
            WHERE username='admin'
        """).fetchone()

        if not old_admin:
            c.execute("""
                INSERT INTO users(
                    username,
                    password,
                    is_admin
                )
                VALUES(?,?,1)
            """, (
                "admin",
                "CHANGE_ADMIN_PASSWORD"
            ))

    db.commit()
    db.close()


init_db()
# ==========================================
# تنظيف الأكواد المباعة القديمة
# ==========================================

@app.on_event("startup")
async def cleanup_task():

    async def loop():
        while True:
            try:
                db = get_db()

                db.execute("""
                    DELETE FROM items
                    WHERE is_sold=1
                    AND sold_at IS NOT NULL
                    AND datetime(sold_at)<=datetime('now','-48 hours')
                """)

                db.commit()
                db.close()

            except Exception as error:
                print("Cleanup error:", error)

            await asyncio.sleep(3600)

    asyncio.create_task(loop())


# ==========================================
# الصفحة الرئيسية
# ==========================================

@app.get("/")
def home(request: Request):
    if get_user(request):
        return redirect("/store")

    return redirect("/login")


# ==========================================
# تسجيل الدخول
# ==========================================

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": None}
    )


@app.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...)
):
    db = get_db()

    user = db.execute("""
        SELECT *
        FROM users
        WHERE username=? AND password=?
    """, (
        username.strip(),
        password
    )).fetchone()

    db.close()

    if not user:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "بيانات الدخول خاطئة"}
        )

    request.session["user"] = {
        "id": user["id"],
        "username": user["username"],
        "is_admin": user["is_admin"]
    }

    if user["is_admin"]:
        return redirect("/admin")

    return redirect("/store")


# ==========================================
# إنشاء حساب جديد
# ==========================================

@app.post("/register")
def register(
    request: Request,
    username: str = Form(...),
    password: str = Form(...)
):
    username = username.strip()

    if len(username) < 3 or len(password) < 6:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "error": "اسم المستخدم 3 أحرف على الأقل وكلمة المرور 6 أحرف"
            }
        )

    db = get_db()

    try:
        db.execute("""
            INSERT INTO users(username, password)
            VALUES(?,?)
        """, (
            username,
            password
        ))

        db.commit()
        db.close()

        return redirect("/login")

    except Exception:
        db.close()

        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "error": "اسم المستخدم مستخدم مسبقاً"
            }
        )


# ==========================================
# تجهيز بيانات صفحة المتجر
# ==========================================

def get_store_context(request, chat_id=None):

    user = get_user(request)

    if not user:
        return {}

    db = get_db()

    db_user = db.execute("""
        SELECT *
        FROM users
        WHERE id=?
    """, (user["id"],)).fetchone()

    products = db.execute("""
        SELECT
            p.*,
            COUNT(
                CASE
                    WHEN i.is_sold=0 THEN 1
                END
            ) AS stock
        FROM products p
        LEFT JOIN items i
            ON i.product_id=p.id
        GROUP BY p.id
        ORDER BY p.id DESC
    """).fetchall()

    purchases = db.execute("""
        SELECT *
        FROM purchases
        WHERE user_id=?
        AND hidden=0
        ORDER BY id DESC
    """, (user["id"],)).fetchall()

    chats = db.execute("""
        SELECT
            c.*,
            (
                SELECT message
                FROM messages m
                WHERE m.conversation_id=c.id
                ORDER BY m.id DESC
                LIMIT 1
            ) AS last_message
        FROM conversations c
        WHERE c.user_id=?
        ORDER BY c.id DESC
    """, (user["id"],)).fetchall()

    active_chat = None
    chat_messages = []

    if chat_id:
        active_chat = db.execute("""
            SELECT *
            FROM conversations
            WHERE id=?
            AND user_id=?
        """, (
            chat_id,
            user["id"]
        )).fetchone()

        if active_chat:
            chat_messages = db.execute("""
                SELECT *
                FROM messages
                WHERE conversation_id=?
                ORDER BY id ASC
            """, (chat_id,)).fetchall()

    db.close()

    return {
        "user": db_user,
        "items": products,
        "my_items": purchases,
        "conversations": chats,
        "active_chat": active_chat,
        "chat_messages": chat_messages,
        "whatsapp_number": WHATSAPP_NUMBER,
        "whatsapp_link": WHATSAPP_LINK
    }


# ==========================================
# صفحة المتجر
# ==========================================

@app.get("/store", response_class=HTMLResponse)
def store(request: Request):

    if not get_user(request):
        return redirect("/login")

    if is_admin(request):
        return redirect("/admin")

    return templates.TemplateResponse(
        request=request,
        name="store.html",
        context=get_store_context(request)
    )


# ==========================================
# فتح محادثة العميل
# ==========================================

@app.get("/chat/{cid}", response_class=HTMLResponse)
def customer_chat(
    request: Request,
    cid: int
):

    user = get_user(request)

    if not user:
        return redirect("/login")

    if user.get("is_admin"):
        return redirect(f"/admin?chat={cid}")

    context = get_store_context(request, cid)

    if not context.get("active_chat"):
        return redirect("/store")

    return templates.TemplateResponse(
        request=request,
        name="store.html",
        context=context
    )


# ==========================================
# إرسال رسالة من العميل
# ==========================================

@app.post("/chat/{cid}/send")
def customer_send(
    request: Request,
    cid: int,
    message: str = Form(...)
):

    user = get_user(request)

    if not user or user.get("is_admin"):
        return redirect("/login")

    message = message.strip()

    if not message:
        return redirect(f"/chat/{cid}")

    db = get_db()

    chat = db.execute("""
        SELECT *
        FROM conversations
        WHERE id=?
        AND user_id=?
    """, (
        cid,
        user["id"]
    )).fetchone()

    if chat and chat["status"] == "open":

        db.execute("""
            INSERT INTO messages(
                conversation_id,
                sender_id,
                sender_role,
                message
            )
            VALUES(?,?,?,?)
        """, (
            cid,
            user["id"],
            "customer",
            message[:5000]
        ))

        db.commit()

    db.close()

    return redirect(f"/chat/{cid}")


# ==========================================
# شراء منتج
# ==========================================

@app.post("/buy")
def buy(
    request: Request,
    product_id: int = Form(...)
):

    user = get_user(request)

    if not user:
        return redirect("/login")

    if user.get("is_admin"):
        return redirect("/admin")

    db = get_db()

    try:
        buyer = db.execute("""
            SELECT *
            FROM users
            WHERE id=?
        """, (user["id"],)).fetchone()

        product = db.execute("""
            SELECT *
            FROM products
            WHERE id=?
        """, (product_id,)).fetchone()

        if not buyer or not product:
            db.close()
            return redirect("/store")

        price = float(product["price"] or 0)

        if price < 0 or float(buyer["balance"] or 0) < price:
            db.close()
            return redirect("/store")

        selected_item = None

        # إذا كان المنتج يتطلب كوداً
        if product["requires_content"]:

            selected_item = db.execute("""
                SELECT *
                FROM items
                WHERE product_id=?
                AND is_sold=0
                ORDER BY id ASC
                LIMIT 1
            """, (product_id,)).fetchone()

            if not selected_item:
                db.close()
                return redirect("/store")

            result = db.execute("""
                UPDATE items
                SET
                    is_sold=1,
                    buyer_id=?,
                    sold_at=datetime('now')
                WHERE id=?
                AND is_sold=0
            """, (
                user["id"],
                selected_item["id"]
            ))

            db.commit()

            # التأكد من عدم بيع الكود مرتين
            check_item = db.execute("""
                SELECT *
                FROM items
                WHERE id=?
                AND is_sold=1
                AND buyer_id=?
            """, (
                selected_item["id"],
                user["id"]
            )).fetchone()

            if not check_item:
                db.close()
                return redirect("/store")

        # خصم الرصيد
        db.execute("""
            UPDATE users
            SET balance=balance-?
            WHERE id=?
        """, (
            price,
            user["id"]
        ))

        # تسجيل عملية الشراء
        db.execute("""
            INSERT INTO purchases(
                user_id,
                product_id,
                product_name,
                price,
                item_id,
                secret_code
            )
            VALUES(?,?,?,?,?,?)
        """, (
            user["id"],
            product_id,
            product["name"],
            price,
            selected_item["id"] if selected_item else None,
            selected_item["secret_code"] if selected_item else None
        ))

        # إنشاء محادثة إذا كان المنتج يتطلب محادثة
        if product["requires_chat"]:

            db.execute("""
                INSERT INTO conversations(
                    user_id,
                    product_id,
                    product_name,
                    status
                )
                VALUES(?,?,?,'open')
            """, (
                user["id"],
                product_id,
                product["name"]
            ))

        db.commit()
        db.close()

        return redirect("/store")

    except Exception as error:
        print("Purchase error:", error)

        try:
            db.close()
        except Exception:
            pass

        return redirect("/store")


# ==========================================
# إخفاء عملية شراء من سجل العميل
# ==========================================

@app.post("/purchase/{purchase_id}/hide")
def hide_purchase(
    request: Request,
    purchase_id: int
):

    user = get_user(request)

    if not user:
        return redirect("/login")

    if user.get("is_admin"):
        return redirect("/admin")

    db = get_db()

    db.execute("""
        UPDATE purchases
        SET hidden=1
        WHERE id=?
        AND user_id=?
    """, (
        purchase_id,
        user["id"]
    ))

    db.commit()
    db.close()

    return redirect("/store")
    # ==========================================
# لوحة الإدارة
# ==========================================

@app.get("/admin", response_class=HTMLResponse)
def admin(
    request: Request,
    chat: int = None,
    show_hidden: int = 0,
    selected_product: int = None
):

    if not is_admin(request):
        return redirect("/login")

    db = get_db()

    products = db.execute("""
        SELECT
            p.*,
            COUNT(
                CASE
                    WHEN i.id IS NOT NULL THEN 1
                END
            ) AS total_codes,
            COUNT(
                CASE
                    WHEN i.id IS NOT NULL AND i.is_sold=0 THEN 1
                END
            ) AS available_codes
        FROM products p
        LEFT JOIN items i
            ON i.product_id=p.id
        GROUP BY p.id
        ORDER BY p.id DESC
    """).fetchall()

    users = db.execute("""
        SELECT id, username, balance
        FROM users
        WHERE is_admin=0
        ORDER BY id DESC
    """).fetchall()

    chats = db.execute("""
        SELECT
            c.*,
            u.username,
            (
                SELECT message
                FROM messages m
                WHERE m.conversation_id=c.id
                ORDER BY m.id DESC
                LIMIT 1
            ) AS last_message
        FROM conversations c
        JOIN users u
            ON u.id=c.user_id
        WHERE (?=1 OR c.hidden=0)
        ORDER BY c.id DESC
    """, (
        1 if show_hidden else 0,
    )).fetchall()

    active_chat = None
    chat_messages = []

    if chat:
        active_chat = db.execute("""
            SELECT
                c.*,
                u.username
            FROM conversations c
            JOIN users u
                ON u.id=c.user_id
            WHERE c.id=?
        """, (chat,)).fetchone()

        if active_chat:
            chat_messages = db.execute("""
                SELECT *
                FROM messages
                WHERE conversation_id=?
                ORDER BY id ASC
            """, (chat,)).fetchall()

    db.close()

    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context=
