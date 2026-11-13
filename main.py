from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
import sqlite3
import os
import shutil
import uuid

# التأكد من وجود مجلد الصور
if not os.path.exists("static"):
    os.makedirs("static")

app = FastAPI(title="Techno Zone Store")
app.add_middleware(SessionMiddleware, secret_key="ryad_super_secret")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

def get_db():
    conn = sqlite3.connect("technozone.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        balance REAL DEFAULT 0.0,
        is_admin BOOLEAN DEFAULT 0
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        secret_code TEXT NOT NULL,
        price REAL NOT NULL DEFAULT 0.0,
        is_sold BOOLEAN DEFAULT 0,
        buyer_id INTEGER
    )''')
    
    # تحديث قاعدة البيانات القديمة لدعم الصور (بدون حذف البيانات)
    try:
        cursor.execute("ALTER TABLE items ADD COLUMN image_url TEXT")
    except:
        pass # العمود موجود مسبقاً
        
    if not cursor.execute("SELECT id FROM users WHERE username='admin'").fetchone():
        cursor.execute("INSERT INTO users (username, password, is_admin) VALUES ('admin', '123456', 1)")
    conn.commit()
    conn.close()

init_db()

def get_user(request: Request):
    return request.session.get("user")

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return RedirectResponse(url="/store") if get_user(request) else RedirectResponse(url="/login")

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="login.html", context={"error": None})

@app.post("/login")
def login_action(request: Request, username: str = Form(...), password: str = Form(...)):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE username=? AND password=?", (username, password)).fetchone()
    conn.close()
    if user:
        request.session["user"] = {"id": user["id"], "username": user["username"], "is_admin": user["is_admin"]}
        return RedirectResponse(url="/store", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html", context={"error": "بيانات الدخول خاطئة"})

@app.post("/register")
def register_action(request: Request, username: str = Form(...), password: str = Form(...)):
    conn = get_db()
    try:
        conn.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, password))
        conn.commit()
        return RedirectResponse(url="/login", status_code=303)
    except:
        return templates.TemplateResponse(request=request, name="login.html", context={"error": "اسم المستخدم مستخدم مسبقاً"})

@app.get("/store", response_class=HTMLResponse)
def store_page(request: Request):
    user = get_user(request)
    if not user: return RedirectResponse(url="/login")
    conn = get_db()
    db_user = conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
    
    # جلب السلع مع صورها
    items = conn.execute("SELECT name, price, image_url, COUNT(id) as stock FROM items WHERE is_sold=0 GROUP BY name, price, image_url").fetchall()
    my_items = conn.execute("SELECT * FROM items WHERE buyer_id=?", (user["id"],)).fetchall()
    conn.close()
    return templates.TemplateResponse(request=request, name="store.html", context={"user": db_user, "items": items, "my_items": my_items})

@app.post("/buy")
def buy_item(request: Request, item_name: str = Form(...)):
    user = get_user(request)
    if not user: return RedirectResponse(url="/login")
    conn = get_db()
    db_user = conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
    item = conn.execute("SELECT * FROM items WHERE name=? AND is_sold=0 LIMIT 1", (item_name,)).fetchone()
    
    if item and db_user["balance"] >= item["price"]:
        conn.execute("UPDATE users SET balance = balance - ? WHERE id=?", (item["price"], user["id"]))
        conn.execute("UPDATE items SET is_sold=1, buyer_id=? WHERE id=?", (user["id"], item["id"]))
        conn.commit()
    conn.close()
    return RedirectResponse(url="/store", status_code=303)

@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    user = get_user(request)
    if not user or not user["is_admin"]: return RedirectResponse(url="/store")
    conn = get_db()
    items = conn.execute("SELECT * FROM items").fetchall()
    users = conn.execute("SELECT id, username, balance FROM users WHERE is_admin=0").fetchall()
    conn.close()
    return templates.TemplateResponse(request=request, name="admin.html", context={"items": items, "users": users})

@app.post("/admin/add_item")
async def admin_add_item(request: Request, name: str = Form(...), secret_code: str = Form(...), price: float = Form(...), image: UploadFile = File(None)):
    user = get_user(request)
    if user and user["is_admin"]:
        image_url = None
        # معالجة ورفع الصورة إن وجدت
        if image and image.filename:
            ext = image.filename.split('.')[-1]
            filename = f"{uuid.uuid4().hex}.{ext}"
            filepath = os.path.join("static", filename)
            with open(filepath, "wb") as buffer:
                shutil.copyfileobj(image.file, buffer)
            image_url = f"/static/{filename}"
            
        conn = get_db()
        conn.execute("INSERT INTO items (name, secret_code, price, image_url) VALUES (?, ?, ?, ?)", (name, secret_code, price, image_url))
        conn.commit()
        conn.close()
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/admin/add_balance")
def admin_add_balance(request: Request, user_id: int = Form(...), amount: float = Form(...)):
    user = get_user(request)
    if user and user["is_admin"]:
        conn = get_db()
        conn.execute("UPDATE users SET balance = balance + ? WHERE id=?", (amount, user_id))
        conn.commit()
        conn.close()
    return RedirectResponse(url="/admin", status_code=303)
    
@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login")
