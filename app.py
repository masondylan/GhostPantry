import os
import sqlite3
import secrets
import hashlib
import hmac
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, Depends
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

APP_VERSION = "0.1.4"
DB_PATH = Path(os.getenv("GHOSTPANTRY_DB", "/data/ghostpantry.db"))
COOKIE_SECURE_MODE = os.getenv("COOKIE_SECURE", "auto").lower()
OFF_UA = os.getenv("OPENFOODFACTS_USER_AGENT", f"GhostPantry/{APP_VERSION} (self-hosted)")
SESSION_DAYS = 30

app = FastAPI(title="GhostPantry", version=APP_VERSION)
app.mount("/static", StaticFiles(directory="static"), name="static")


def secure_cookie_for(request: Request) -> bool:
    """Choose Secure cookies automatically when behind HTTPS, or obey explicit env override."""
    if COOKIE_SECURE_MODE in {"1", "true", "yes", "on"}:
        return True
    if COOKIE_SECURE_MODE in {"0", "false", "no", "off"}:
        return False
    forwarded = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    return request.url.scheme == "https" or forwarded == "https"


def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS households (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS memberships (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
                role TEXT NOT NULL DEFAULT 'member',
                PRIMARY KEY (user_id, household_id)
            );
            CREATE TABLE IF NOT EXISTS locations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'other',
                UNIQUE(household_id, name)
            );
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                barcode TEXT UNIQUE,
                name TEXT NOT NULL,
                brand TEXT,
                image_url TEXT,
                default_unit TEXT DEFAULT 'item',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS inventory_lots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
                location_id INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
                product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                quantity REAL NOT NULL DEFAULT 1,
                unit TEXT NOT NULL DEFAULT 'item',
                expires_on TEXT,
                purchased_on TEXT,
                price REAL,
                notes TEXT,
                created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_lots_household ON inventory_lots(household_id);
            CREATE INDEX IF NOT EXISTS idx_lots_expiry ON inventory_lots(expires_on);
            CREATE TABLE IF NOT EXISTS shopping_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
                product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
                name TEXT NOT NULL,
                quantity REAL NOT NULL DEFAULT 1,
                unit TEXT NOT NULL DEFAULT 'item',
                checked INTEGER NOT NULL DEFAULT 0,
                created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )


@app.on_event("startup")
def startup():
    init_db()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$", 2)
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


def current_user(request: Request):
    token = request.cookies.get("ghostpantry_session")
    if not token:
        raise HTTPException(401, "Not signed in")
    with db() as c:
        row = c.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.expires_at>?",
            (token, datetime.utcnow().isoformat()),
        ).fetchone()
    if not row:
        raise HTTPException(401, "Session expired")
    return dict(row)


def require_admin(user=Depends(current_user)):
    if not user["is_admin"]:
        raise HTTPException(403, "Administrator access required")
    return user


def household_access(c, user_id: int, household_id: int, admin=False):
    if admin:
        return
    row = c.execute(
        "SELECT 1 FROM memberships WHERE user_id=? AND household_id=?", (user_id, household_id)
    ).fetchone()
    if not row:
        raise HTTPException(403, "You do not have access to this household")


class SetupIn(BaseModel):
    username: str = Field(min_length=2, max_length=40)
    password: str = Field(min_length=8, max_length=200)
    household_name: str = Field(default="My House", min_length=1, max_length=80)


class LoginIn(BaseModel):
    username: str
    password: str


class HouseholdIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class UserIn(BaseModel):
    username: str = Field(min_length=2, max_length=40)
    password: str = Field(min_length=8, max_length=200)
    household_ids: list[int] = []
    is_admin: bool = False


class InventoryIn(BaseModel):
    household_id: int
    location_id: int
    barcode: Optional[str] = None
    name: str = Field(min_length=1, max_length=180)
    brand: Optional[str] = None
    image_url: Optional[str] = None
    quantity: float = Field(default=1, gt=0)
    unit: str = Field(default="item", min_length=1, max_length=30)
    expires_on: Optional[str] = None
    purchased_on: Optional[str] = None
    price: Optional[float] = None
    notes: Optional[str] = None


class ConsumeIn(BaseModel):
    amount: float = Field(default=1, gt=0)


class QuantityUpdateIn(BaseModel):
    quantity: float = Field(gt=0)
    unit: Optional[str] = Field(default=None, min_length=1, max_length=30)


class MoveIn(BaseModel):
    location_id: int


class ShoppingIn(BaseModel):
    household_id: int
    name: str = Field(min_length=1, max_length=180)
    quantity: float = Field(default=1, gt=0)
    unit: str = Field(default="item", min_length=1, max_length=30)


@app.get("/api/status")
def status():
    with db() as c:
        count = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
    return {"version": APP_VERSION, "first_run": count == 0}


@app.post("/api/setup")
def setup(data: SetupIn, response: Response, request: Request):
    with db() as c:
        if c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]:
            raise HTTPException(409, "GhostPantry has already been configured")
        cur = c.execute(
            "INSERT INTO users(username,password_hash,is_admin) VALUES(?,?,1)",
            (data.username.strip(), hash_password(data.password)),
        )
        user_id = cur.lastrowid
        cur = c.execute("INSERT INTO households(name) VALUES(?)", (data.household_name.strip(),))
        hid = cur.lastrowid
        c.execute("INSERT INTO memberships(user_id,household_id,role) VALUES(?,?,?)", (user_id, hid, "owner"))
        for name, kind in [("Pantry", "pantry"), ("Refrigerator", "fridge"), ("Freezer", "freezer")]:
            c.execute("INSERT INTO locations(household_id,name,kind) VALUES(?,?,?)", (hid, name, kind))
        token = secrets.token_urlsafe(32)
        c.execute("INSERT INTO sessions(token,user_id,expires_at) VALUES(?,?,?)", (token, user_id, (datetime.utcnow()+timedelta(days=SESSION_DAYS)).isoformat()))
    response.set_cookie("ghostpantry_session", token, httponly=True, samesite="lax", secure=secure_cookie_for(request), max_age=SESSION_DAYS*86400)
    return {"ok": True}


@app.post("/api/login")
def login(data: LoginIn, response: Response, request: Request):
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (data.username.strip(),)).fetchone()
        if not row or not verify_password(data.password, row["password_hash"]):
            raise HTTPException(401, "Invalid username or password")
        token = secrets.token_urlsafe(32)
        c.execute("DELETE FROM sessions WHERE expires_at<=?", (datetime.utcnow().isoformat(),))
        c.execute("INSERT INTO sessions(token,user_id,expires_at) VALUES(?,?,?)", (token, row["id"], (datetime.utcnow()+timedelta(days=SESSION_DAYS)).isoformat()))
    response.set_cookie("ghostpantry_session", token, httponly=True, samesite="lax", secure=secure_cookie_for(request), max_age=SESSION_DAYS*86400)
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get("ghostpantry_session")
    if token:
        with db() as c:
            c.execute("DELETE FROM sessions WHERE token=?", (token,))
    response.delete_cookie("ghostpantry_session")
    return {"ok": True}


@app.get("/api/me")
def me(user=Depends(current_user)):
    with db() as c:
        if user["is_admin"]:
            hs = c.execute("SELECT id,name FROM households ORDER BY name").fetchall()
        else:
            hs = c.execute("SELECT h.id,h.name FROM memberships m JOIN households h ON h.id=m.household_id WHERE m.user_id=? ORDER BY h.name", (user["id"],)).fetchall()
    return {"id": user["id"], "username": user["username"], "is_admin": bool(user["is_admin"]), "households": [dict(x) for x in hs]}


@app.post("/api/households")
def add_household(data: HouseholdIn, user=Depends(require_admin)):
    with db() as c:
        try:
            cur = c.execute("INSERT INTO households(name) VALUES(?)", (data.name.strip(),))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "A household with that name already exists")
        hid = cur.lastrowid
        c.execute("INSERT OR IGNORE INTO memberships(user_id,household_id,role) VALUES(?,?,?)", (user["id"], hid, "owner"))
        for name, kind in [("Pantry", "pantry"), ("Refrigerator", "fridge"), ("Freezer", "freezer")]:
            c.execute("INSERT INTO locations(household_id,name,kind) VALUES(?,?,?)", (hid, name, kind))
    return {"id": hid, "name": data.name.strip()}


@app.get("/api/households/{household_id}/locations")
def locations(household_id: int, user=Depends(current_user)):
    with db() as c:
        household_access(c, user["id"], household_id, bool(user["is_admin"]))
        rows = c.execute("SELECT id,name,kind FROM locations WHERE household_id=? ORDER BY CASE kind WHEN 'pantry' THEN 1 WHEN 'fridge' THEN 2 WHEN 'freezer' THEN 3 ELSE 4 END,name", (household_id,)).fetchall()
    return [dict(x) for x in rows]


@app.get("/api/users")
def users(user=Depends(require_admin)):
    with db() as c:
        rows = c.execute("SELECT id,username,is_admin,created_at FROM users ORDER BY username").fetchall()
        out=[]
        for r in rows:
            hs = c.execute("SELECT h.id,h.name FROM memberships m JOIN households h ON h.id=m.household_id WHERE m.user_id=? ORDER BY h.name", (r["id"],)).fetchall()
            d=dict(r); d["is_admin"]=bool(d["is_admin"]); d["households"]=[dict(x) for x in hs]; out.append(d)
    return out


@app.post("/api/users")
def add_user(data: UserIn, user=Depends(require_admin)):
    with db() as c:
        try:
            cur=c.execute("INSERT INTO users(username,password_hash,is_admin) VALUES(?,?,?)", (data.username.strip(), hash_password(data.password), 1 if data.is_admin else 0))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Username already exists")
        uid=cur.lastrowid
        for hid in data.household_ids:
            if not c.execute("SELECT 1 FROM households WHERE id=?", (hid,)).fetchone():
                raise HTTPException(400, f"Unknown household {hid}")
            c.execute("INSERT INTO memberships(user_id,household_id,role) VALUES(?,?,?)", (uid,hid,"member"))
    return {"id":uid,"username":data.username.strip()}


def validate_date(value: Optional[str]):
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise HTTPException(400, "Date must use YYYY-MM-DD")


@app.get("/api/product/{barcode}")
async def product_lookup(barcode: str, user=Depends(current_user)):
    barcode = "".join(ch for ch in barcode if ch.isdigit())
    if not barcode:
        raise HTTPException(400, "Invalid barcode")
    with db() as c:
        row = c.execute("SELECT * FROM products WHERE barcode=?", (barcode,)).fetchone()
        if row:
            d=dict(row); d["source"]="local"; return d
    url=f"https://world.openfoodfacts.org/api/v2/product/{barcode}.json"
    params={"fields":"code,product_name,brands,image_front_small_url,image_front_url,quantity"}
    try:
        async with httpx.AsyncClient(timeout=8, headers={"User-Agent": OFF_UA}) as client:
            r=await client.get(url, params=params)
            r.raise_for_status()
            payload=r.json()
    except Exception as e:
        raise HTTPException(502, f"Product lookup service unavailable: {type(e).__name__}")
    if payload.get("status") != 1:
        return {"barcode":barcode,"found":False,"source":"openfoodfacts"}
    p=payload.get("product") or {}
    return {
        "barcode": barcode,
        "found": True,
        "name": p.get("product_name") or "",
        "brand": p.get("brands") or "",
        "image_url": p.get("image_front_small_url") or p.get("image_front_url") or "",
        "package_quantity": p.get("quantity") or "",
        "source": "openfoodfacts",
    }


@app.post("/api/inventory")
def add_inventory(data: InventoryIn, user=Depends(current_user)):
    exp=validate_date(data.expires_on)
    purchased=validate_date(data.purchased_on) or date.today().isoformat()
    with db() as c:
        household_access(c, user["id"], data.household_id, bool(user["is_admin"]))
        loc=c.execute("SELECT 1 FROM locations WHERE id=? AND household_id=?", (data.location_id,data.household_id)).fetchone()
        if not loc: raise HTTPException(400,"Location does not belong to that household")
        barcode=(data.barcode or "").strip() or None
        product=None
        if barcode:
            product=c.execute("SELECT * FROM products WHERE barcode=?",(barcode,)).fetchone()
        if not product:
            cur=c.execute("INSERT INTO products(barcode,name,brand,image_url,default_unit) VALUES(?,?,?,?,?)", (barcode,data.name.strip(),(data.brand or "").strip(),(data.image_url or "").strip(),data.unit.strip()))
            pid=cur.lastrowid
        else:
            pid=product["id"]
            c.execute("UPDATE products SET name=?,brand=?,image_url=?,default_unit=? WHERE id=?", (data.name.strip(),(data.brand or "").strip(),(data.image_url or "").strip(),data.unit.strip(),pid))
        cur=c.execute("INSERT INTO inventory_lots(household_id,location_id,product_id,quantity,unit,expires_on,purchased_on,price,notes,created_by) VALUES(?,?,?,?,?,?,?,?,?,?)", (data.household_id,data.location_id,pid,data.quantity,data.unit.strip(),exp,purchased,data.price,(data.notes or "").strip(),user["id"]))
    return {"id":cur.lastrowid,"ok":True}


@app.get("/api/inventory")
def inventory(household_id: int, location_id: Optional[int]=None, user=Depends(current_user)):
    with db() as c:
        household_access(c,user["id"],household_id,bool(user["is_admin"]))
        sql="""SELECT l.id,l.quantity,l.unit,l.expires_on,l.purchased_on,l.price,l.notes,
                      p.id product_id,p.barcode,p.name,p.brand,p.image_url,
                      loc.id location_id,loc.name location_name,loc.kind location_kind
               FROM inventory_lots l JOIN products p ON p.id=l.product_id JOIN locations loc ON loc.id=l.location_id
               WHERE l.household_id=?"""
        args=[household_id]
        if location_id:
            sql += " AND l.location_id=?"; args.append(location_id)
        sql += " ORDER BY CASE WHEN l.expires_on IS NULL THEN 1 ELSE 0 END,l.expires_on,p.name"
        rows=c.execute(sql,args).fetchall()
    today=date.today()
    out=[]
    for r in rows:
        d=dict(r)
        d["days_until_expiry"]=(date.fromisoformat(d["expires_on"])-today).days if d["expires_on"] else None
        out.append(d)
    return out


@app.post("/api/inventory/{lot_id}/consume")
def consume(lot_id:int,data:ConsumeIn,user=Depends(current_user)):
    with db() as c:
        row=c.execute("SELECT * FROM inventory_lots WHERE id=?",(lot_id,)).fetchone()
        if not row: raise HTTPException(404,"Inventory item not found")
        household_access(c,user["id"],row["household_id"],bool(user["is_admin"]))
        remaining=row["quantity"]-data.amount
        if remaining <= 0:
            c.execute("DELETE FROM inventory_lots WHERE id=?",(lot_id,))
            remaining=0
        else:
            c.execute("UPDATE inventory_lots SET quantity=? WHERE id=?",(remaining,lot_id))
    return {"ok":True,"remaining":remaining}


@app.post("/api/inventory/{lot_id}/quantity")
def update_quantity(lot_id:int,data:QuantityUpdateIn,user=Depends(current_user)):
    with db() as c:
        row=c.execute("SELECT * FROM inventory_lots WHERE id=?",(lot_id,)).fetchone()
        if not row: raise HTTPException(404,"Inventory item not found")
        household_access(c,user["id"],row["household_id"],bool(user["is_admin"]))
        unit=(data.unit or row["unit"]).strip()
        c.execute("UPDATE inventory_lots SET quantity=?,unit=? WHERE id=?",(data.quantity,unit,lot_id))
    return {"ok":True,"quantity":data.quantity,"unit":unit}


@app.post("/api/inventory/{lot_id}/shopping")
def add_inventory_to_shopping(lot_id:int,user=Depends(current_user)):
    with db() as c:
        row=c.execute("""SELECT l.household_id,l.unit,p.id product_id,p.name
                         FROM inventory_lots l JOIN products p ON p.id=l.product_id
                         WHERE l.id=?""",(lot_id,)).fetchone()
        if not row: raise HTTPException(404,"Inventory item not found")
        household_access(c,user["id"],row["household_id"],bool(user["is_admin"]))
        existing=c.execute("""SELECT id,quantity FROM shopping_items
                              WHERE household_id=? AND checked=0
                                AND (product_id=? OR (product_id IS NULL AND name=? COLLATE NOCASE))
                              ORDER BY id LIMIT 1""",
                           (row["household_id"],row["product_id"],row["name"])).fetchone()
        if existing:
            new_qty=existing["quantity"]+1
            c.execute("UPDATE shopping_items SET quantity=?,unit=? WHERE id=?",
                      (new_qty,row["unit"],existing["id"]))
            sid=existing["id"]
            added=False
        else:
            cur=c.execute("""INSERT INTO shopping_items(household_id,product_id,name,quantity,unit,created_by)
                             VALUES(?,?,?,?,?,?)""",
                          (row["household_id"],row["product_id"],row["name"],1,row["unit"],user["id"]))
            sid=cur.lastrowid
            new_qty=1
            added=True
    return {"ok":True,"shopping_id":sid,"quantity":new_qty,"added":added}


@app.post("/api/inventory/{lot_id}/move")
def move(lot_id:int,data:MoveIn,user=Depends(current_user)):
    with db() as c:
        row=c.execute("SELECT * FROM inventory_lots WHERE id=?",(lot_id,)).fetchone()
        if not row: raise HTTPException(404,"Inventory item not found")
        household_access(c,user["id"],row["household_id"],bool(user["is_admin"]))
        loc=c.execute("SELECT 1 FROM locations WHERE id=? AND household_id=?",(data.location_id,row["household_id"])).fetchone()
        if not loc: raise HTTPException(400,"Invalid location")
        c.execute("UPDATE inventory_lots SET location_id=? WHERE id=?",(data.location_id,lot_id))
    return {"ok":True}


@app.get("/api/dashboard")
def dashboard(household_id:int,user=Depends(current_user)):
    with db() as c:
        household_access(c,user["id"],household_id,bool(user["is_admin"]))
        total=c.execute("SELECT COALESCE(SUM(quantity),0) n FROM inventory_lots WHERE household_id=?",(household_id,)).fetchone()["n"]
        lots=c.execute("""SELECT l.id,l.quantity,l.unit,l.expires_on,p.name,p.brand,p.image_url,loc.name location_name
                          FROM inventory_lots l JOIN products p ON p.id=l.product_id JOIN locations loc ON loc.id=l.location_id
                          WHERE l.household_id=? AND l.expires_on IS NOT NULL ORDER BY l.expires_on LIMIT 20""",(household_id,)).fetchall()
        shop=c.execute("SELECT COUNT(*) n FROM shopping_items WHERE household_id=? AND checked=0",(household_id,)).fetchone()["n"]
    today=date.today(); expiring=[]; expired=0; soon=0
    for r in lots:
        d=dict(r); days=(date.fromisoformat(d["expires_on"])-today).days; d["days_until_expiry"]=days
        if days<0: expired+=1
        if days<=7: soon+=1
        if days<=14: expiring.append(d)
    return {"total_quantity":total,"expiring_7_days":soon,"expired":expired,"shopping_open":shop,"expiring":expiring}


@app.get("/api/shopping")
def shopping(household_id:int,user=Depends(current_user)):
    with db() as c:
        household_access(c,user["id"],household_id,bool(user["is_admin"]))
        rows=c.execute("SELECT * FROM shopping_items WHERE household_id=? ORDER BY checked,name",(household_id,)).fetchall()
    return [dict(x) for x in rows]


@app.post("/api/shopping")
def shopping_add(data:ShoppingIn,user=Depends(current_user)):
    with db() as c:
        household_access(c,user["id"],data.household_id,bool(user["is_admin"]))
        cur=c.execute("INSERT INTO shopping_items(household_id,name,quantity,unit,created_by) VALUES(?,?,?,?,?)",(data.household_id,data.name.strip(),data.quantity,data.unit.strip(),user["id"]))
    return {"id":cur.lastrowid,"ok":True}


@app.post("/api/shopping/{item_id}/toggle")
def shopping_toggle(item_id:int,user=Depends(current_user)):
    with db() as c:
        row=c.execute("SELECT * FROM shopping_items WHERE id=?",(item_id,)).fetchone()
        if not row: raise HTTPException(404,"Shopping item not found")
        household_access(c,user["id"],row["household_id"],bool(user["is_admin"]))
        c.execute("UPDATE shopping_items SET checked=? WHERE id=?",(0 if row["checked"] else 1,item_id))
    return {"ok":True}


@app.delete("/api/shopping/{item_id}")
def shopping_delete(item_id:int,user=Depends(current_user)):
    with db() as c:
        row=c.execute("SELECT * FROM shopping_items WHERE id=?",(item_id,)).fetchone()
        if not row: raise HTTPException(404,"Shopping item not found")
        household_access(c,user["id"],row["household_id"],bool(user["is_admin"]))
        c.execute("DELETE FROM shopping_items WHERE id=?",(item_id,))
    return {"ok":True}


@app.get("/health")
def health():
    return {"ok":True,"version":APP_VERSION}


@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.get("/{path:path}")
def spa(path: str):
    if path.startswith("api/"):
        raise HTTPException(404)
    return FileResponse("static/index.html")
