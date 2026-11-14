from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
import sqlite3, os, asyncio, base64, requests

os.makedirs("static", exist_ok=True)
app = FastAPI(title="Techno Zone Store")
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("SESSION_SECRET", "CHANGE_THIS_SESSION_SECRET"))
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

TURSO_URL = os.environ.get("TURSO_DATABASE_URL", "").replace("libsql://", "https://").rstrip("/")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "")

class TursoCursor:
    def __init__(self, rows=None): self.rows, self.i = rows or [], 0
    def fetchone(self):
        if self.i < len(self.rows):
            row = self.rows[self.i]; self.i += 1; return row
        return None
    def fetchall(self): return self.rows

class TursoConnection:
    def __init__(self, url, token):
        self.url = url + "/v2/pipeline"
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    def _arg(self, x):
        if x is None: return {"type": "null"}
        if isinstance(x, bool): return {"type": "integer", "value": "1" if x else "0"}
        if isinstance(x, int): return {"type": "integer", "value": str(x)}
        if isinstance(x, float): return {"type": "float", "value": x}
        return {"type": "text", "value": str(x)}
    def execute(self, sql, params=()):
        payload = {"requests":[{"type":"execute","stmt":{"sql":sql,"args":[self._arg(x) for x in params]}},{"type":"close"}]}
        r = requests.post(self.url, headers=self.headers, json=payload, timeout=30); r.raise_for_status()
        item = r.json()["results"][0]
        if item["type"] == "error": raise Exception(item["error"]["message"])
        result = item["response"]["result"]
        cols = [c["name"] for c in result.get("cols", [])]
        rows = []
        for raw in result.get("rows", []):
            row = {}
            for name, cell in zip(cols, raw):
                val = cell.get("value")
                if cell["type"] == "integer" and val is not None: val = int(val)
                elif cell["type"] == "float" and val is not None: val = float(val)
                row[name] = val
            rows.append(row)
        return TursoCursor(rows)
    def cursor(self): return self
    def commit(self): pass
    def close(self): pass

def get_db():
    if TURSO_URL and TURSO_TOKEN: return TursoConnection(TURSO_URL, TURSO_TOKEN)
    db = sqlite3.connect("technozone.db", check_same_thread=False); db.row_factory = sqlite3.Row
    return db

def init_db():
    db = get_db(); c = db.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, password TEXT NOT NULL, balance REAL DEFAULT 0, is_admin INTEGER DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, price REAL NOT NULL DEFAULT 0, image_url TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, secret_code TEXT NOT NULL, price REAL NOT NULL DEFAULT 0, is_sold INTEGER DEFAULT 0, buyer_id INTEGER, image_url TEXT, product_id INTEGER, sold_at TEXT)""")
    for sql in ["ALTER TABLE items ADD COLUMN product_id INTEGER", "ALTER TABLE items ADD COLUMN image_url TEXT", "ALTER TABLE items ADD COLUMN sold_at TEXT"]:
        try: c.execute(sql)
        except Exception: pass
    c.execute("""CREATE TABLE IF NOT EXISTS conversations(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, product_id INTEGER, product_name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', hidden INTEGER NOT NULL DEFAULT 0, created_at TEXT DEFAULT (datetime('now')))""")
    c.execute("""CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER NOT NULL, sender_id INTEGER NOT NULL, sender_role TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT DEFAULT (datetime('now')))""")
    c.execute("UPDATE items SET sold_at=datetime('now') WHERE is_sold=1 AND sold_at IS NULL")
    old = c.execute("SELECT DISTINCT name,price,image_url FROM items WHERE product_id IS NULL").fetchall()
    for x in old:
        p = c.execute("""SELECT id FROM products WHERE name=? AND price=? AND (image_url=? OR (image_url IS NULL AND ? IS NULL)) LIMIT 1""",(x["name"],x["price"],x["image_url"],x["image_url"])).fetchone()
        if p: pid = p["id"]
        else:
            c.execute("INSERT INTO products(name,price,image_url) VALUES(?,?,?)",(x["name"],x["price"],x["image_url"]))
            pid = c.execute("SELECT id FROM products ORDER BY id DESC LIMIT 1").fetchone()["id"]
        c.execute("""UPDATE items SET product_id=? WHERE product_id IS NULL AND name=? AND price=? AND (image_url=? OR (image_url IS NULL AND ? IS NULL))""",(pid,x["name"],x["price"],x["image_url"],x["image_url"]))
    admin = c.execute("SELECT id FROM users WHERE is_admin=1 LIMIT 1").fetchone()
    if not admin and not c.execute("SELECT id FROM users WHERE username='admin'").fetchone():
        c.execute("INSERT INTO users(username,password,is_admin) VALUES(?,?,1)",("admin","CHANGE_ADMIN_PASSWORD"))
    db.commit(); db.close()

init_db()

def get_user(req): return req.session.get("user")
def is_admin(req):
    u = get_user(req); return bool(u and u.get("is_admin"))
def redirect(path): return RedirectResponse(path, status_code=303)
def put_message(db, cid, uid, role, msg):
    db.execute("INSERT INTO messages(conversation_id,sender_id,sender_role,message) VALUES(?,?,?,?)",(cid,uid,role,msg[:5000]))

@app.on_event("startup")
async def cleanup_task():
    async def loop():
        while True:
            try:
                db=get_db(); db.execute("DELETE FROM items WHERE is_sold=1 AND sold_at IS NOT NULL AND datetime(sold_at)<=datetime('now','-48 hours')"); db.commit(); db.close()
            except Exception as e: print("cleanup error:",e)
            await asyncio.sleep(3600)
    asyncio.create_task(loop())

@app.get("/")
def home(request: Request): return redirect("/store" if get_user(request) else "/login")

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request=request,name="login.html",context={"error":None})

@app.post("/login")
def login(request: Request, username:str=Form(...), password:str=Form(...)):
    db=get_db(); u=db.execute("SELECT * FROM users WHERE username=? AND password=?",(username,password)).fetchone(); db.close()
    if not u: return templates.TemplateResponse(request=request,name="login.html",context={"error":"بيانات الدخول خاطئة"})
    request.session["user"]={"id":u["id"],"username":u["username"],"is_admin":u["is_admin"]}
    return redirect("/store")

@app.post("/register")
def register(request:Request, username:str=Form(...), password:str=Form(...)):
    db=get_db()
    try:
        db.execute("INSERT INTO users(username,password) VALUES(?,?)",(username,password)); db.commit(); db.close()
        return redirect("/login")
    except Exception:
        db.close(); return templates.TemplateResponse(request=request,name="login.html",context={"error":"اسم المستخدم مستخدم مسبقاً"})

def get_store_context(request, chat_id=None):
    u=get_user(request); db=get_db()
    dbu=db.execute("SELECT * FROM users WHERE id=?",(u["id"],)).fetchone()
    products=db.execute("""SELECT p.id,p.name,p.price,p.image_url,COUNT(CASE WHEN i.is_sold=0 THEN 1 END) stock FROM products p LEFT JOIN items i ON i.product_id=p.id GROUP BY p.id ORDER BY p.id DESC""").fetchall()
    purchases=db.execute("""SELECT i.*,p.name product_name FROM items i LEFT JOIN products p ON p.id=i.product_id WHERE i.buyer_id=? ORDER BY i.id DESC""",(u["id"],)).fetchall()
    chats=db.execute("""SELECT c.*,(SELECT message FROM messages m WHERE m.conversation_id=c.id ORDER BY m.id DESC LIMIT 1) last_message FROM conversations c WHERE c.user_id=? ORDER BY c.id DESC""",(u["id"],)).fetchall()
    active=None; msgs=[]
    if chat_id:
        active=db.execute("SELECT * FROM conversations WHERE id=? AND user_id=?",(chat_id,u["id"])).fetchone()
        if active: msgs=db.execute("SELECT * FROM messages WHERE conversation_id=? ORDER BY id",(chat_id,)).fetchall()
    db.close()
    return {"user":dbu,"items":products,"my_items":purchases,"conversations":chats,"active_chat":active,"chat_messages":msgs}

@app.get("/store",response_class=HTMLResponse)
def store(request:Request):
    if not get_user(request): return redirect("/login")
    return templates.TemplateResponse(request=request,name="store.html",context=get_store_context(request))

@app.get("/chat/{cid}",response_class=HTMLResponse)
def customer_chat(request:Request,cid:int):
    u=get_user(request)
    if not u: return redirect("/login")
    if u.get("is_admin"): return redirect(f"/admin?chat={cid}")
    ctx=get_store_context(request,cid)
    if not ctx["active_chat"]: return redirect("/store")
    return templates.TemplateResponse(request=request,name="store.html",context=ctx)

@app.post("/chat/{cid}/send")
def customer_send(request:Request,cid:int,message:str=Form(...)):
    u=get_user(request)
    if not u or u.get("is_admin"): return redirect("/login")
    db=get_db(); chat=db.execute("SELECT * FROM conversations WHERE id=? AND user_id=?",(cid,u["id"])).fetchone()
    if chat and chat["status"]=="open" and message.strip():
        put_message(db,cid,u["id"],"customer",message.strip()); db.commit()
    db.close(); return redirect(f"/chat/{cid}")

@app.post("/buy")
def buy(request:Request,product_id:int=Form(...)):
    u=get_user(request)
    if not u: return redirect("/login")
    db=get_db()
    try:
        buyer=db.execute("SELECT * FROM users WHERE id=?",(u["id"],)).fetchone()
        product=db.execute("SELECT * FROM products WHERE id=?",(product_id,)).fetchone()
        item=db.execute("SELECT * FROM items WHERE product_id=? AND is_sold=0 ORDER BY id LIMIT 1",(product_id,)).fetchone()
        if not buyer or not product or not item or buyer["balance"]<product["price"]: return redirect("/store")
        db.execute("UPDATE items SET is_sold=1,buyer_id=?,sold_at=datetime('now') WHERE id=? AND is_sold=0",(u["id"],item["id"]))
        db.execute("UPDATE users SET balance=balance-? WHERE id=?",(product["price"],u["id"]))
        db.execute("INSERT INTO conversations(user_id,product_id,product_name,status) VALUES(?,?,?,'open')",(u["id"],product_id,product["name"]))
        db.commit()
    finally: db.close()
    return redirect("/store")

@app.get("/admin",response_class=HTMLResponse)
def admin(request:Request,chat:int=None,show_hidden:int=0,selected_product:int=None):
    if not is_admin(request): return redirect("/store")
    db=get_db()
    products=db.execute("""SELECT p.*,COUNT(i.id) total_codes,SUM(CASE WHEN i.is_sold=0 THEN 1 ELSE 0 END) available_codes FROM products p LEFT JOIN items i ON i.product_id=p.id GROUP BY p.id ORDER BY p.id DESC""").fetchall()
    users=db.execute("SELECT id,username,balance FROM users WHERE is_admin=0 ORDER BY id DESC").fetchall()
    chats=db.execute("""SELECT c.*,u.username,(SELECT message FROM messages m WHERE m.conversation_id=c.id ORDER BY m.id DESC LIMIT 1) last_message FROM conversations c JOIN users u ON u.id=c.user_id WHERE (?=1 OR c.hidden=0) ORDER BY c.id DESC""",(show_hidden,)).fetchall()
    active=None; msgs=[]
    if chat:
        active=db.execute("SELECT c.*,u.username FROM conversations c JOIN users u ON u.id=c.user_id WHERE c.id=?",(chat,)).fetchone()
        if active: msgs=db.execute("SELECT * FROM messages WHERE conversation_id=? ORDER BY id",(chat,)).fetchall()
    db.close()
    return templates.TemplateResponse(request=request,name="admin.html",context={"products":products,"users":users,"conversations":chats,"active_chat":active,"chat_messages":msgs,"show_hidden":show_hidden,"selected_product":selected_product})

@app.post("/admin/chat/{cid}/send")
def admin_send(request:Request,cid:int,message:str=Form(...)):
    u=get_user(request)
    if not u or not u.get("is_admin"): return redirect("/login")
    db=get_db(); chat=db.execute("SELECT * FROM conversations WHERE id=?",(cid,)).fetchone()
    if chat and chat["status"]!="closed" and message.strip():
        put_message(db,cid,u["id"],"admin",message.strip()); db.commit()
    db.close(); return redirect(f"/admin?chat={cid}")

@app.post("/admin/chat/{cid}/status")
def chat_status(request:Request,cid:int,status:str=Form(...)):
    if not is_admin(request): return redirect("/login")
    if status not in ("open","paused","closed"): return redirect("/admin")
    db=get_db(); db.execute("UPDATE conversations SET status=? WHERE id=?",(status,cid)); db.commit(); db.close()
    return redirect(f"/admin?chat={cid}")

@app.post("/admin/chat/{cid}/hide")
def chat_hide(request:Request,cid:int):
    if not is_admin(request): return redirect("/login")
    db=get_db(); db.execute("UPDATE conversations SET hidden=1 WHERE id=?",(cid,)); db.commit(); db.close(); return redirect("/admin")

@app.post("/admin/chat/{cid}/unhide")
def chat_unhide(request:Request,cid:int):
    if not is_admin(request): return redirect("/login")
    db=get_db(); db.execute("UPDATE conversations SET hidden=0 WHERE id=?",(cid,)); db.commit(); db.close(); return redirect("/admin?show_hidden=1")

@app.post("/admin/add_product")
async def add_product(request:Request,name:str=Form(...),price:float=Form(...),image:UploadFile=File(None)):
    if not is_admin(request): return redirect("/store")
    image_url=None
    if image and image.filename:
        data=await image.read()
        if data: image_url=f"data:{image.content_type or 'image/png'};base64,"+base64.b64encode(data).decode()
    db=get_db(); db.execute("INSERT INTO products(name,price,image_url) VALUES(?,?,?)",(name.strip(),price,image_url)); db.commit(); db.close()
    return redirect("/admin")

@app.post("/admin/add_content")
def add_content(request:Request,product_id:int=Form(...),secret_code:str=Form(...)):
    if not is_admin(request): return redirect("/store")
    db=get_db(); p=db.execute("SELECT * FROM products WHERE id=?",(product_id,)).fetchone()
    if p and secret_code.strip():
        db.execute("INSERT INTO items(name,secret_code,price,image_url,product_id,is_sold) VALUES(?,?,?,?,?,0)",(p["name"],secret_code.strip(),p["price"],p["image_url"],product_id)); db.commit()
    db.close(); return redirect(f"/admin?selected_product={product_id}")

@app.post("/admin/add_balance")
def add_balance(request:Request,user_id:int=Form(...),amount:float=Form(...)):
    if not is_admin(request): return redirect("/store")
    if amount>0:
        db=get_db(); db.execute("UPDATE users SET balance=balance+? WHERE id=? AND is_admin=0",(amount,user_id)); db.commit(); db.close()
    return redirect("/admin")

@app.post("/admin/delete_product")
def delete_product(request:Request,product_id:int=Form(...)):
    if not is_admin(request): return redirect("/store")
    db=get_db(); db.execute("DELETE FROM items WHERE product_id=?",(product_id,)); db.execute("DELETE FROM products WHERE id=?",(product_id,)); db.commit(); db.close()
    return redirect("/admin")

@app.post("/admin/delete_user")
def delete_user(request:Request,user_id:int=Form(...)):
    u=get_user(request)
    if not u or not u.get("is_admin") or user_id==u["id"]: return redirect("/admin")
    db=get_db()
    ids=db.execute("SELECT id FROM conversations WHERE user_id=?",(user_id,)).fetchall()
    for x in ids: db.execute("DELETE FROM messages WHERE conversation_id=?",(x["id"],))
    db.execute("DELETE FROM conversations WHERE user_id=?",(user_id,))
    db.execute("DELETE FROM items WHERE buyer_id=? AND is_sold=1",(user_id,))
    db.execute("DELETE FROM users WHERE id=? AND is_admin=0",(user_id,))
    db.commit(); db.close(); return redirect("/admin")

@app.get("/logout")
def logout(request:Request):
    request.session.clear(); return redirect("/login")
