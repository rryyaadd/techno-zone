from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

import sqlite3
import os
import asyncio
import requests
import uuid
from pathlib import Path


# ==========================================
# إعداد التطبيق
# ==========================================

os.makedirs("static", exist_ok=True)
os.makedirs("static/uploads", exist_ok=True)
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
# استجابات AJAX
# ==========================================

def wants_json(request: Request):
    return (
        "application/json" in request.headers.get("accept", "")
        or request.headers.get("x-requested-with") == "XMLHttpRequest"
    )


def action_response(
    request: Request,
    message="تم تنفيذ العملية بنجاح",
    redirect_url="/",
    ok=True
):
    if wants_json(request):
        return JSONResponse({
            "ok": ok,
            "message": message,
            "redirect": redirect_url
        })

    return RedirectResponse(
        redirect_url,
        status_code=303
    )


# ==========================================
# إعداد Turso
# ==========================================

TURSO_URL = os.environ.get(
    "TURSO_DATABASE_URL", ""
).replace("libsql://", "https://").rstrip("/")

TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "")


class TursoCursor:
    def __init__(self, rows=None, rowcount=0):
        self.rows = rows or []
        self.index = 0
        self.rowcount = rowcount

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

        affected = result.get("affected_row_count", 0)

        return TursoCursor(rows, affected)

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

    # ترحيل المشتريات القديمة
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
        if wants_json(request):
            return JSONResponse({
                "ok": False,
                "message": "بيانات الدخول خاطئة"
            }, status_code=401)

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

    destination = "/admin" if user["is_admin"] else "/store"

    return action_response(
        request,
        "تم تسجيل الدخول بنجاح",
        destination
    )


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
        message = "اسم المستخدم 3 أحرف على الأقل وكلمة المرور 6 أحرف"

        if wants_json(request):
            return JSONResponse({
                "ok": False,
                "message": message
            }, status_code=400)

        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": message}
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

        return action_response(
            request,
            "تم إنشاء الحساب بنجاح",
            "/login"
        )

    except Exception:
        db.close()

        message = "اسم المستخدم مستخدم مسبقاً"

        if wants_json(request):
            return JSONResponse({
                "ok": False,
                "message": message
            }, status_code=409)

        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": message}
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
                CASE WHEN i.is_sold=0 THEN 1 END
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
        AND c.hidden=0
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

    return templates.TemplateResponse(
        request=request,
        name="store.html",
        context=get_store_context(request)
    )


# ==========================================
# فتح محادثة العميل
# ==========================================

@app.get("/chat/{cid}", response_class=HTMLResponse)
def customer_chat(request: Request, cid: int):
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
        return action_response(
            request,
            "يجب تسجيل الدخول كعميل",
            "/login",
            False
        )

    message = message.strip()

    if not message:
        return action_response(
            request,
            "اكتب رسالة أولاً",
            f"/chat/{cid}",
            False
        )

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

    if not chat:
        db.close()
        return action_response(
            request,
            "المحادثة غير موجودة",
            "/store",
            False
        )

    if chat["status"] == "open":
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

    return action_response(
        request,
        "تم إرسال الرسالة",
        f"/chat/{cid}"
    )


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
        return action_response(
            request,
            "يرجى تسجيل الدخول",
            "/login",
            False
        )

    if user.get("is_admin"):
        return action_response(
            request,
            "حساب المدير لا يمكنه الشراء",
            "/store",
            False
        )

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
            return action_response(
                request,
                "المنتج غير موجود",
                "/store",
                False
            )

        price = float(product["price"] or 0)

        if price < 0:
            db.close()
            return action_response(
                request,
                "سعر المنتج غير صالح",
                "/store",
                False
            )

        if float(buyer["balance"] or 0) < price:
            db.close()
            return action_response(
                request,
                "رصيدك غير كافٍ",
                "/store",
                False
            )

        selected_item = None

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
                return action_response(
                    request,
                    "المنتج غير متوفر حالياً",
                    "/store",
                    False
                )

            db.execute("""
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
                return action_response(
                    request,
                    "تعذر إتمام عملية الشراء",
                    "/store",
                    False
                )

        db.execute("""
            UPDATE users
            SET balance=balance-?
            WHERE id=?
            AND balance>=?
        """, (
            price,
            user["id"],
            price
        ))

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

        return action_response(
            request,
            "تم شراء المنتج بنجاح",
            "/store"
        )

    except Exception as error:
        print("Purchase error:", error)

        try:
            db.close()
        except Exception:
            pass

        return action_response(
            request,
            "حدث خطأ أثناء الشراء",
            "/store",
            False
        )


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
        return action_response(
            request,
            "يرجى تسجيل الدخول",
            "/login",
            False
        )

    if user.get("is_admin"):
        return action_response(
            request,
            "غير مسموح",
            "/admin",
            False
        )

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

    return action_response(
        request,
        "تم إخفاء عملية الشراء",
        "/store"
    )


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
                CASE WHEN i.id IS NOT NULL THEN 1 END
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

    selected_product_data = None

    if selected_product:
        selected_product_data = db.execute("""
            SELECT *
            FROM products
            WHERE id=?
        """, (selected_product,)).fetchone()

    db.close()

    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={
            "products": products,
            "users": users,
            "conversations": chats,
            "active_chat": active_chat,
            "chat_messages": chat_messages,
            "show_hidden": show_hidden,
            "selected_product": selected_product_data,
            "whatsapp_number": WHATSAPP_NUMBER,
            "whatsapp_link": WHATSAPP_LINK
        }
    )


# ==========================================
# إرسال رد الإدارة
# ==========================================

@app.post("/admin/chat/{cid}/send")
def admin_send(
    request: Request,
    cid: int,
    message: str = Form(...)
):
    if not is_admin(request):
        return action_response(
            request, "غير مصرح", "/login", False
        )

    message = message.strip()

    if not message:
        return action_response(
            request, "اكتب رسالة أولاً", f"/admin?chat={cid}", False
        )

    db = get_db()

    chat = db.execute("""
        SELECT id
        FROM conversations
        WHERE id=?
    """, (cid,)).fetchone()

    if chat:
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
            get_user(request)["id"],
            "admin",
            message[:5000]
        ))

        db.commit()

    db.close()

    return action_response(
        request, "تم إرسال الرد", f"/admin?chat={cid}"
    )


# ==========================================
# تغيير حالة المحادثة
# ==========================================

@app.post("/admin/chat/{cid}/status")
def admin_chat_status(
    request: Request,
    cid: int,
    status: str = Form(...)
):
    if not is_admin(request):
        return action_response(
            request, "غير مصرح", "/login", False
        )

    if status not in ["open", "paused", "closed"]:
        return action_response(
            request, "حالة المحادثة غير صحيحة",
            f"/admin?chat={cid}", False
        )

    db = get_db()

    db.execute("""
        UPDATE conversations
        SET status=?
        WHERE id=?
    """, (
        status,
        cid
    ))

    db.commit()
    db.close()

    return action_response(
        request, "تم تحديث حالة المحادثة", f"/admin?chat={cid}"
    )


# ==========================================
# إخفاء محادثة
# ==========================================

@app.post("/admin/chat/{cid}/hide")
def admin_chat_hide(request: Request, cid: int):
    if not is_admin(request):
        return action_response(
            request, "غير مصرح", "/login", False
        )

    db = get_db()

    db.execute("""
        UPDATE conversations
        SET hidden=1
        WHERE id=?
    """, (cid,))

    db.commit()
    db.close()

    return action_response(
        request, "تم إخفاء المحادثة", "/admin"
    )


# ==========================================
# إظهار محادثة
# ==========================================

@app.post("/admin/chat/{cid}/unhide")
def admin_chat_unhide(request: Request, cid: int):
    if not is_admin(request):
        return action_response(
            request, "غير مصرح", "/login", False
        )

    db = get_db()

    db.execute("""
        UPDATE conversations
        SET hidden=0
        WHERE id=?
    """, (cid,))

    db.commit()
    db.close()

    return action_response(
        request, "تم إظهار المحادثة", "/admin?show_hidden=1"
    )


# ==========================================
# إضافة منتج مع رفع صورة
# ==========================================

@app.post("/admin/add_product")
async def add_product(
    request: Request,
    name: str = Form(...),
    price: float = Form(...),
    image_url: str = Form(""),
    description: str = Form(""),
    requires_chat: int = Form(0),
    requires_content: int = Form(1),
    image: UploadFile = File(None)
):
    if not is_admin(request):
        return action_response(
            request, "غير مصرح", "/login", False
        )

    name = name.strip()

    if not name or price < 0:
        return action_response(
            request, "اسم المنتج أو السعر غير صالح",
            "/admin", False
        )

    image_path = image_url.strip()

    if image and image.filename:
        allowed_extensions = {
            ".jpg", ".jpeg", ".png", ".webp"
        }

        extension = Path(image.filename).suffix.lower()

        if extension not in allowed_extensions:
            return action_response(
                request,
                "صيغة الصورة غير مدعومة",
                "/admin",
                False
            )

        content = await image.read()

        if len(content) > 5 * 1024 * 1024:
            return action_response(
                request,
                "حجم الصورة يجب ألا يتجاوز 5 ميجابايت",
                "/admin",
                False
            )

        filename = f"{uuid.uuid4().hex}{extension}"
        upload_dir = Path("static/uploads")
        upload_dir.mkdir(parents=True, exist_ok=True)

        file_path = upload_dir / filename

        with open(file_path, "wb") as f:
            f.write(content)

        image_path = f"/static/uploads/{filename}"

    requires_chat = 1 if requires_chat else 0
    requires_content = 1 if requires_content else 0

    db = get_db()

    db.execute("""
        INSERT INTO products(
            name,
            price,
            image_url,
            description,
            requires_chat,
            requires_content
        )
        VALUES(?,?,?,?,?,?)
    """, (
        name,
        price,
        image_path,
        description.strip(),
        requires_chat,
        requires_content
    ))

    db.commit()
    db.close()

    return action_response(
        request, "تمت إضافة المنتج بنجاح", "/admin"
    )


# ==========================================
# إضافة حسابات بصيغة الحساب وكلمة السر
# ==========================================

@app.post("/admin/add_content")
def add_content(
    request: Request,
    product_id: int = Form(...),
    secret_code: str = Form(...)
):
    if not is_admin(request):
        return action_response(
            request, "غير مصرح", "/login", False
        )

    lines = [
        line.strip()
        for line in secret_code.splitlines()
        if line.strip()
    ]

    if not lines or len(lines) % 2 != 0:
        return action_response(
            request,
            "تأكد من إدخال الحساب وكلمة السر لكل حساب",
            "/admin",
            False
        )

    accounts = []

    for i in range(0, len(lines), 2):
        email_line = lines[i]
        password_line = lines[i + 1]

        if not email_line.startswith("الحساب"):
            return action_response(
                request,
                "السطر الأول يجب أن يبدأ بكلمة الحساب",
                "/admin",
                False
            )

        if not password_line.startswith("كلمة السر"):
            return action_response(
                request,
                "السطر الثاني يجب أن يبدأ بكلمة السر",
                "/admin",
                False
            )

        email = email_line.split(":", 1)[-1].strip()
        password = password_line.split(":", 1)[-1].strip()

        if not email or not password:
            return action_response(
                request,
                "الحساب أو كلمة السر فارغة",
                "/admin",
                False
            )

        accounts.append(
            f"الحساب : {email}\nكلمة السر : {password}"
        )

    db = get_db()

    try:
        product = db.execute("""
            SELECT *
            FROM products
            WHERE id=?
        """, (product_id,)).fetchone()

        if not product or not product["requires_content"]:
            db.close()
            return action_response(
                request,
                "المنتج غير موجود أو لا يقبل المحتوى",
                "/admin",
                False
            )

        for account in accounts:
            db.execute("""
                INSERT INTO items(
                    name,
                    secret_code,
                    price,
                    is_sold,
                    product_id
                )
                VALUES(?,?,?,0,?)
            """, (
                product["name"],
                account,
                product["price"],
                product_id
            ))

        db.commit()
        db.close()

        return action_response(
            request,
            f"تمت إضافة {len(accounts)} حساب بنجاح",
            "/admin"
        )

    except Exception as error:
        print("Add content error:", error)

        try:
            db.close()
        except Exception:
            pass

        return action_response(
            request,
            "حدث خطأ أثناء إضافة الحسابات",
            "/admin",
            False
)


# ==========================================
# شحن رصيد مستخدم
# ==========================================

@app.post("/admin/add_balance")
def add_balance(
    request: Request,
    user_id: int = Form(...),
    amount: float = Form(...)
):
    if not is_admin(request):
        return action_response(
            request, "غير مصرح", "/login", False
        )

    if amount <= 0:
        return action_response(
            request, "المبلغ يجب أن يكون أكبر من صفر",
            "/admin", False
        )

    db = get_db()

    db.execute("""
        UPDATE users
        SET balance=COALESCE(balance,0)+?
        WHERE id=?
        AND is_admin=0
    """, (
        amount,
        user_id
    ))

    db.commit()
    db.close()

    return action_response(
        request, "تم شحن الرصيد بنجاح", "/admin"
    )


# ==========================================
# حذف حساب عميل نهائياً
# ==========================================

@app.post("/admin/delete_user")
def delete_user(
    request: Request,
    user_id: int = Form(...)
):
    if not is_admin(request):
        return action_response(
            request, "غير مصرح", "/login", False
        )

    db = get_db()

    try:
        customer = db.execute("""
            SELECT id, username, is_admin
            FROM users
            WHERE id=?
        """, (user_id,)).fetchone()

        if not customer:
            db.close()
            return action_response(
                request, "العميل غير موجود", "/admin", False
            )

        if customer["is_admin"]:
            db.close()
            return action_response(
                request, "لا يمكن حذف حساب المدير", "/admin", False
            )

        db.execute("""
            DELETE FROM messages
            WHERE conversation_id IN (
                SELECT id
                FROM conversations
                WHERE user_id=?
            )
        """, (user_id,))

        db.execute("""
            DELETE FROM messages
            WHERE sender_id=?
            AND sender_role='customer'
        """, (user_id,))

        db.execute("""
            DELETE FROM conversations
            WHERE user_id=?
        """, (user_id,))

        db.execute("""
            DELETE FROM purchases
            WHERE user_id=?
        """, (user_id,))

        db.execute("""
            UPDATE items
            SET buyer_id=NULL
            WHERE buyer_id=?
        """, (user_id,))

        db.execute("""
            DELETE FROM users
            WHERE id=?
            AND is_admin=0
        """, (user_id,))

        db.commit()
        db.close()

        return action_response(
            request,
            f"تم حذف حساب العميل {customer['username']} نهائياً",
            "/admin"
        )

    except Exception as error:
        print("Delete customer error:", error)

        try:
            db.close()
        except Exception:
            pass

        return action_response(
            request,
            "حدث خطأ أثناء حذف الحساب",
            "/admin",
            False
        )


# ==========================================
# حذف منتج
# ==========================================

@app.post("/admin/delete_product")
def delete_product(
    request: Request,
    product_id: int = Form(...)
):
    if not is_admin(request):
        return action_response(
            request, "غير مصرح", "/login", False
        )

    db = get_db()

    db.execute("""
        DELETE FROM items
        WHERE product_id=?
        AND is_sold=0
    """, (product_id,))

    db.execute("""
        DELETE FROM products
        WHERE id=?
    """, (product_id,))

    db.commit()
    db.close()

    return action_response(
        request, "تم حذف المنتج", "/admin"
    )


# ==========================================
# تسجيل الخروج
# ==========================================

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return redirect("/login")
