#!/usr/bin/env python3
# ARYAN PREMIUM STORE - Telegram digital products + FamGateway
# Configure BOT_TOKEN and OWNER_ID as environment variables. Never paste API keys in source code.
import os, time, sqlite3, logging, threading, secrets, shutil
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse
import requests
import telebot
from telebot import types

BOT_TOKEN = os.getenv("BOT_TOKEN", "9186356344:AAG-LdpbGB8fxPLMDLZ94Ev9m2ASlUeXZKE").strip()
OWNER_ID = 8781077406  # Aryan admin/owner Telegram ID
DB_PATH = os.getenv("DB_PATH", "core.db")
BASE_GATEWAY_URL = "https://famgateway.in"
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing. Set your NEW BotFather token in Environment Variables.")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("aryan-premium")
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
http = requests.Session()
states = {}
polling = set()
lock = threading.Lock()

# ---------------- DATABASE ----------------
def connect():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c

def init_db():
    with connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          user_id INTEGER PRIMARY KEY, name TEXT DEFAULT '', username TEXT DEFAULT '',
          balance REAL NOT NULL DEFAULT 0, banned INTEGER NOT NULL DEFAULT 0,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP, last_seen TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS products(
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, description TEXT DEFAULT '',
          price REAL NOT NULL, file_id TEXT NOT NULL, file_type TEXT NOT NULL,
          stock INTEGER DEFAULT -1, featured INTEGER DEFAULT 0, active INTEGER DEFAULT 1,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS orders(
          id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT UNIQUE NOT NULL, user_id INTEGER NOT NULL,
          product_id INTEGER, amount REAL NOT NULL, kind TEXT NOT NULL, status TEXT DEFAULT 'pending',
          utr TEXT DEFAULT '', coupon TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          paid_at TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS transactions(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, amount REAL NOT NULL,
          kind TEXT NOT NULL, reference TEXT UNIQUE NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS coupons(
          code TEXT PRIMARY KEY, discount REAL NOT NULL, uses_left INTEGER DEFAULT -1,
          active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS reviews(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, product_id INTEGER,
          stars INTEGER, comment TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS purchases(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
          order_id TEXT UNIQUE NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS favorites(
          user_id INTEGER NOT NULL, product_id INTEGER NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY(user_id, product_id));
        CREATE TABLE IF NOT EXISTS tickets(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, message TEXT NOT NULL,
          status TEXT DEFAULT 'open', admin_reply TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS notifications(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, message TEXT NOT NULL,
          seen INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        """)
        defaults = {
          "api_url": BASE_GATEWAY_URL, "api_key": "", "channel": "",
          "store_name": "ARYAN PREMIUM STORE", "welcome": "Welcome to our premium digital store!",
          "support": "https://t.me/BOTS_UPDATE_07", "maintenance": "0"
        }
        for k,v in defaults.items():
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",(k,v))
        # Safe additive migrations for existing databases.
        cols={r[1] for r in c.execute("PRAGMA table_info(orders)").fetchall()}
        if "qr_chat_id" not in cols: c.execute("ALTER TABLE orders ADD COLUMN qr_chat_id INTEGER")
        if "qr_message_id" not in cols: c.execute("ALTER TABLE orders ADD COLUMN qr_message_id INTEGER")
        if "checkout_url" not in cols: c.execute("ALTER TABLE orders ADD COLUMN checkout_url TEXT DEFAULT ''")
init_db()

def get_setting(k, default=""):
    with connect() as c:
        r=c.execute("SELECT value FROM settings WHERE key=?",(k,)).fetchone()
    return r["value"] if r else default

def put_setting(k,v):
    with connect() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(k,str(v)))

def admin(uid): return int(uid)==OWNER_ID

def money(v):
    try:
        x=Decimal(str(v).strip())
        if not x.is_finite() or x<=0 or x>Decimal("1000000"): return None
        return float(x.quantize(Decimal("0.01")))
    except (InvalidOperation,ValueError,TypeError): return None

def save_user(u):
    with connect() as c:
        c.execute("""INSERT INTO users(user_id,name,username) VALUES(?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET name=excluded.name,username=excluded.username,last_seen=CURRENT_TIMESTAMP""",
        (u.id,u.first_name or "",u.username or ""))

def banned(uid):
    with connect() as c:
        r=c.execute("SELECT banned FROM users WHERE user_id=?",(uid,)).fetchone()
    return bool(r and r["banned"])

def allowed(uid):
    return admin(uid) or (not banned(uid) and get_setting("maintenance","0")!="1")

def esc(s):
    return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

# ---------------- MENUS ----------------
def user_menu():
    k=types.InlineKeyboardMarkup(row_width=2)
    k.add(types.InlineKeyboardButton("🛍️ Products",callback_data="products"),
          types.InlineKeyboardButton("⭐ Featured",callback_data="featured"))
    k.add(types.InlineKeyboardButton("🔎 Search",callback_data="search"),
          types.InlineKeyboardButton("👛 Wallet",callback_data="wallet"))
    k.add(types.InlineKeyboardButton("💎 Add Balance",callback_data="wallet_add"))
    k.add(types.InlineKeyboardButton("🧾 My Orders",callback_data="orders"),
          types.InlineKeyboardButton("📥 My Downloads",callback_data="downloads"))
    k.add(types.InlineKeyboardButton("❤️ Wishlist",callback_data="favorites"),
          types.InlineKeyboardButton("💳 Transactions",callback_data="transactions"))
    k.add(types.InlineKeyboardButton("🔔 Notifications",callback_data="notifications"),
          types.InlineKeyboardButton("👤 Profile",callback_data="profile"))
    k.add(types.InlineKeyboardButton("🎟️ Offers",callback_data="offers"),
          types.InlineKeyboardButton("🆘 Help Ticket",callback_data="ticket"))
    k.add(types.InlineKeyboardButton("💬 Support",url=get_setting("support","https://t.me/BOTS_UPDATE_07")))
    return k

def admin_menu():
    k=types.InlineKeyboardMarkup(row_width=2)
    items=[
      ("➕ Add Product","a_add"),("📦 Manage Products","a_products"),
      ("🔑 Payment API Setup","a_api"),("📢 Force Join Setup","a_channel"),
      ("👥 Ban / Unban User","a_ban"),("📊 Sales & Statistics","a_stats"),
      ("🎟️ Coupon Manager","a_coupon"),("📣 Broadcast","a_broadcast"),
      ("🧾 Recent Orders","a_orders"),("⚙️ Store Settings","a_settings"),
      ("💰 Wallet Credit/Debit","a_balance"),("➕ Add User Balance","a_addbalance"),
      ("👥 User Lookup","a_users"),("⏳ Pending Payments","a_pending"),
      ("💾 Backup Database","a_backup"),("🔧 Maintenance Mode","a_maintenance"),
      ("📨 Notify User","a_notify"),("⭐ Reviews","a_reviews")
    ]
    for label,data in items: k.add(types.InlineKeyboardButton(label,callback_data=data))
    k.add(types.InlineKeyboardButton("🏠 User Menu",callback_data="home"))
    return k

def join_ok(uid):
    ch=get_setting("channel","").strip()
    if not ch: return True
    try:
        m=bot.get_chat_member(ch,uid)
        return m.status in ("creator","administrator","member")
    except Exception as e:
        log.warning("Membership check error: %s",e)
        return False

def join_prompt(chat):
    ch=get_setting("channel","").strip()
    k=types.InlineKeyboardMarkup()
    if ch: k.add(types.InlineKeyboardButton("📢 Join Channel",url="https://t.me/"+ch.lstrip("@")))
    k.add(types.InlineKeyboardButton("✅ Verify Membership",callback_data="verify"))
    bot.send_message(chat,"🔐 Join our channel and verify to continue.",reply_markup=k)

def require_user(chat,uid):
    if not allowed(uid):
        bot.send_message(chat,"⛔ Access unavailable. You are banned or the store is in maintenance mode.")
        return False
    if not admin(uid) and not join_ok(uid):
        join_prompt(chat); return False
    return True

def ask(chat, prompt, state):
    states[chat]=state
    bot.send_message(chat,prompt+"\n\nSend /cancel to cancel.")

# ---------------- FAMGATEWAY ----------------
def gateway():
    key=get_setting("api_key").strip()
    url=get_setting("api_url",BASE_GATEWAY_URL).strip().rstrip("/")
    p=urlparse(url)
    if not key: raise ValueError("Admin has not added FamGateway API Key in Admin Panel.")
    if p.scheme!="https" or not p.netloc: raise ValueError("API URL must be a valid HTTPS URL.")
    return key,url

def create_gateway_order(amount,customer):
    key,url=gateway()
    r=http.post(url+"/api/create-order",
      headers={"X-Api-Key":key,"Content-Type":"application/json"},
      json={"amount":round(float(amount),2),"customer_name":str(customer)[:80]},
      timeout=20)
    r.raise_for_status()
    j=r.json(); d=j.get("data",j)
    if j.get("status")!="success" or not d.get("order_id") or not d.get("checkout_url"):
        raise RuntimeError(j.get("message","FamGateway did not return a valid order."))
    return d

def verify_gateway_order(oid):
    key,url=gateway()
    r=http.get(url+"/api/verify-order.php",headers={"X-Api-Key":key},
               params={"order_id":oid},timeout=15)
    r.raise_for_status()
    return r.json()

def create_order_record(oid,uid,pid,amount,kind,coupon=""):
    with connect() as c:
        c.execute("INSERT INTO orders(order_id,user_id,product_id,amount,kind,coupon) VALUES(?,?,?,?,?,?)",
                  (oid,uid,pid,amount,kind,coupon))

def deliver(uid,pid,oid):
    with connect() as c:
        p=c.execute("SELECT * FROM products WHERE id=?",(pid,)).fetchone()
    if not p:
        bot.send_message(uid,f"Payment received, but product is unavailable. Contact support. Order: <code>{esc(oid)}</code>")
        return
    cap=f"🎉 <b>Purchase Successful!</b>\n📦 {esc(p['name'])}\n🧾 <code>{esc(oid)}</code>"
    try:
        typ,fid=p["file_type"],p["file_id"]
        if typ=="document": bot.send_document(uid,fid,caption=cap)
        elif typ=="photo": bot.send_photo(uid,fid,caption=cap)
        elif typ=="video": bot.send_video(uid,fid,caption=cap)
        elif typ=="audio": bot.send_audio(uid,fid,caption=cap)
        elif typ=="text": bot.send_message(uid,cap+"\n\n"+esc(fid))
        else: bot.send_message(uid,cap+"\nPlease contact support for delivery.")
    except Exception:
        log.exception("Delivery failed: %s",oid)
        bot.send_message(uid,f"Payment verified, delivery failed. Contact support with Order ID <code>{esc(oid)}</code>.")

def send_product_file(uid,pid,caption="📥 Your purchased file"):
    with connect() as c:
        p=c.execute("SELECT * FROM products WHERE id=?",(pid,)).fetchone()
    if not p:
        bot.send_message(uid,"This product file is no longer available. Please contact support.")
        return
    typ,fid=p["file_type"],p["file_id"]
    cap=f"{caption}\\n📦 {esc(p['name'])}"
    if typ=="document": bot.send_document(uid,fid,caption=cap)
    elif typ=="photo": bot.send_photo(uid,fid,caption=cap)
    elif typ=="video": bot.send_video(uid,fid,caption=cap)
    elif typ=="audio": bot.send_audio(uid,fid,caption=cap)
    else: bot.send_message(uid,cap+"\\n\\n"+esc(fid))

def delete_payment_qr(oid):
    """Remove the QR/payment card from Telegram after success, expiry, or user cancel."""
    try:
        with connect() as c:
            row=c.execute("SELECT qr_chat_id,qr_message_id FROM orders WHERE order_id=?",(oid,)).fetchone()
        if row and row["qr_chat_id"] and row["qr_message_id"]:
            bot.delete_message(row["qr_chat_id"],row["qr_message_id"])
    except Exception as e:
        log.info("Could not delete payment QR for %s: %s",oid,e)

def gateway_payload(payload):
    """FamGateway responses may wrap fields in data; normalize without trusting client input."""
    if not isinstance(payload,dict): return {}
    inner=payload.get("data")
    return inner if isinstance(inner,dict) else payload

def fulfill(oid,verified):
    v=gateway_payload(verified)
    status=str(verified.get("status",v.get("status",""))).lower()
    if status not in ("success","paid","completed"): return False
    with connect() as c:
        row=c.execute("SELECT * FROM orders WHERE order_id=?",(oid,)).fetchone()
        if not row or row["status"]=="success": return False
        try: paid=float(v.get("amount",verified.get("amount",0)))
        except (ValueError,TypeError): return False
        if abs(paid-float(row["amount"]))>0.02:
            log.error("Payment amount mismatch: %s expected=%s received=%s",oid,row["amount"],paid); return False
        utr=str(v.get("utr") or v.get("transaction_id") or verified.get("utr") or oid)
        # Use the unique order ID as ledger reference: repeated UTRs from a gateway
        # must not prevent a legitimate separate order from being credited.
        c.execute("INSERT OR IGNORE INTO transactions(user_id,amount,kind,reference) VALUES(?,?,?,?)",
                  (row["user_id"],row["amount"],row["kind"],"fam-order:"+oid))
        c.execute("UPDATE orders SET status='success',utr=?,paid_at=CURRENT_TIMESTAMP WHERE order_id=? AND status!='success'",(utr,oid))
        if row["kind"]=="wallet":
            c.execute("UPDATE users SET balance=balance+? WHERE user_id=?",(row["amount"],row["user_id"]))
        elif row["kind"]=="product":
            prod=c.execute("SELECT stock FROM products WHERE id=?",(row["product_id"],)).fetchone()
            if prod and prod["stock"]>0: c.execute("UPDATE products SET stock=stock-1 WHERE id=?",(row["product_id"],))
            c.execute("INSERT OR IGNORE INTO purchases(user_id,product_id,order_id) VALUES(?,?,?)",(row["user_id"],row["product_id"],oid))
        uid,pid,kind,amount=row["user_id"],row["product_id"],row["kind"],float(row["amount"])
    delete_payment_qr(oid)
    if kind=="product": deliver(uid,pid,oid)
    else:
        with connect() as c: bal=c.execute("SELECT balance FROM users WHERE user_id=?",(uid,)).fetchone()
        bot.send_message(uid,f"✅ <b>Payment verified automatically!</b>\n₹{amount:.2f} added to your wallet.\nNew balance: ₹{float(bal['balance']):.2f}\nOrder: <code>{esc(oid)}</code>")
    return True

def poll_payment(oid):
    try:
        for _ in range(75):
            time.sleep(4)
            with connect() as c:
                row=c.execute("SELECT status,user_id FROM orders WHERE order_id=?",(oid,)).fetchone()
            if not row or row["status"]!="pending": return
            try:
                v=verify_gateway_order(oid)
                normalized=gateway_payload(v)
                status=str(v.get("status",normalized.get("status",""))).lower()
                if status in ("success","paid","completed"):
                    fulfill(oid,v); return
                if status in ("expired","failed","cancelled"):
                    with connect() as c: c.execute("UPDATE orders SET status=? WHERE order_id=? AND status='pending'",(status,oid))
                    delete_payment_qr(oid)
                    bot.send_message(row["user_id"],f"⌛ Payment session {status}. Please create a new order if you have not paid.")
                    return
            except Exception as e: log.warning("Payment polling failed for %s: %s",oid,e)
        with connect() as c: c.execute("UPDATE orders SET status='expired' WHERE order_id=? AND status='pending'",(oid,))
        delete_payment_qr(oid)
        try: bot.send_message(row["user_id"],"⌛ Payment window expired. If money was deducted, tap Check Payment or contact support with your Order ID.")
        except Exception: pass
    finally:
        with lock: polling.discard(oid)

def start_payment(chat,uid,amount,kind,pid=None,coupon=""):
    d=create_gateway_order(amount, f"Telegram {uid}")
    oid=str(d["order_id"]); payable=float(d.get("payable_amount",amount))
    create_order_record(oid,uid,pid,payable,kind,coupon)
    kb=types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("💳 Pay Securely",url=d["checkout_url"]))
    kb.add(types.InlineKeyboardButton("🔄 Check Payment",callback_data="check:"+oid),
           types.InlineKeyboardButton("❌ Cancel / Remove QR",callback_data="cancelpay:"+oid))
    msg=(f"💎 <b>SECURE PAYMENT</b>\n━━━━━━━━━━━━━━━━\nAmount: <b>₹{payable:.2f}</b>\n"
         f"Order ID: <code>{esc(oid)}</code>\n\nPayment complete hote hi auto verification hoga aur wallet instantly update hoga.\n"
         "QR/payment card successful verification ke baad automatically delete ho jayega.")
    sent=None; qr=d.get("qr_url")
    if qr:
        try: sent=bot.send_photo(chat,qr,caption=msg,reply_markup=kb)
        except Exception: sent=bot.send_message(chat,msg,reply_markup=kb)
    else: sent=bot.send_message(chat,msg,reply_markup=kb)
    with connect() as c:
        c.execute("UPDATE orders SET qr_chat_id=?,qr_message_id=?,checkout_url=? WHERE order_id=?",
                  (chat,sent.message_id,d["checkout_url"],oid))
    with lock:
        if oid not in polling:
            polling.add(oid); threading.Thread(target=poll_payment,args=(oid,),daemon=True).start()

# ---------------- PRODUCT / COUPON HELPERS ----------------
def list_products(chat,featured=False):
    with connect() as c:
        if featured:
            rows=c.execute("SELECT * FROM products WHERE active=1 AND featured=1 ORDER BY id DESC").fetchall()
        else:
            rows=c.execute("SELECT * FROM products WHERE active=1 ORDER BY id DESC").fetchall()
    if not rows:
        bot.send_message(chat,"No products available yet."); return
    k=types.InlineKeyboardMarkup()
    for p in rows:
        stock="∞" if p["stock"]<0 else str(p["stock"])
        k.add(types.InlineKeyboardButton(f"{'⭐ ' if p['featured'] else ''}{p['name']} | ₹{p['price']:.2f} | Stock {stock}",
                                         callback_data=f"p:{p['id']}"))
    bot.send_message(chat,"🛍️ <b>ARYAN PREMIUM CATALOG</b>",reply_markup=k)

def show_product(chat,pid):
    with connect() as c: p=c.execute("SELECT * FROM products WHERE id=? AND active=1",(pid,)).fetchone()
    if not p: bot.send_message(chat,"Product not found."); return
    if p["stock"]==0: bot.send_message(chat,"❌ This product is currently out of stock."); return
    k=types.InlineKeyboardMarkup()
    k.add(types.InlineKeyboardButton("🛒 Buy Now",callback_data=f"buy:{pid}"))
    k.add(types.InlineKeyboardButton("❤️ Add to Wishlist",callback_data=f"favadd:{pid}"))
    k.add(types.InlineKeyboardButton("⭐ Rate Product",callback_data=f"rate:{pid}"))
    bot.send_message(chat,f"📦 <b>{esc(p['name'])}</b>\n\n{esc(p['description'])}\n\n"
                     f"💰 Price: ₹{p['price']:.2f}\n📦 Stock: {'Unlimited' if p['stock']<0 else p['stock']}",
                     reply_markup=k)

def buy_product(chat,uid,pid):
    with connect() as c: p=c.execute("SELECT * FROM products WHERE id=? AND active=1",(pid,)).fetchone()
    if not p or p["stock"]==0: bot.send_message(chat,"Product unavailable or out of stock."); return
    states[chat]={"mode":"buy_coupon","pid":pid}
    k=types.InlineKeyboardMarkup()
    k.add(types.InlineKeyboardButton("Skip Coupon",callback_data=f"payfull:{pid}"))
    bot.send_message(chat,"🎟️ Enter coupon code, or tap Skip Coupon.",reply_markup=k)

def discounted(pid,code):
    with connect() as c:
        p=c.execute("SELECT price FROM products WHERE id=?",(pid,)).fetchone()
        cp=c.execute("SELECT * FROM coupons WHERE code=? AND active=1",(code.upper(),)).fetchone()
    if not p: return None,None
    price=float(p["price"])
    if code and cp and (cp["uses_left"]!=0):
        price=max(1,round(price*(100-float(cp["discount"]))/100,2))
        return price,cp["code"]
    return price,""

# ---------------- COMMANDS ----------------
@bot.message_handler(commands=["start"])
def start(m):
    save_user(m.from_user)
    if not require_user(m.chat.id,m.from_user.id): return
    bot.send_message(m.chat.id,f"✨ <b>{esc(get_setting('store_name'))}</b>\n\n{esc(get_setting('welcome'))}",
                     reply_markup=user_menu())

@bot.message_handler(commands=["admin", "adminpanel"])
def admin_cmd(m):
    if admin(m.from_user.id):
        states.pop(m.chat.id, None)
        bot.send_message(m.chat.id,"🛡️ <b>ARYAN PREMIUM ADMIN PANEL</b>\\n\\nChoose an admin option:",reply_markup=admin_menu())
    else:
        bot.reply_to(m, "⛔ Admin only.")

# Also open the admin panel when the owner sends plain text: admin / Admin / ADMIN.
# This handler is registered before the general text/state handler below.
@bot.message_handler(func=lambda m: bool(m.text) and m.text.strip().lower() == "admin", content_types=["text"])
def plain_admin_cmd(m):
    if admin(m.from_user.id):
        states.pop(m.chat.id, None)
        bot.send_message(m.chat.id,"🛡️ <b>ARYAN PREMIUM ADMIN PANEL</b>\\n\\nChoose an admin option:",reply_markup=admin_menu())
    else:
        bot.reply_to(m, "⛔ Admin only.")

@bot.message_handler(commands=["cancel"])
def cancel(m):
    states.pop(m.chat.id,None)
    bot.reply_to(m,"❎ Action cancelled.")

@bot.message_handler(commands=["backup"])
def backup_cmd(m):
    if not admin(m.from_user.id): return
    send_backup(m.chat.id)

def send_backup(chat):
    try:
        with connect() as c: c.execute("PRAGMA wal_checkpoint(FULL)")
        dest=f"aryan_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        shutil.copy2(DB_PATH,dest)
        with open(dest,"rb") as f: bot.send_document(chat,f,caption="🗄️ Database backup")
        os.remove(dest)
    except Exception as e: bot.send_message(chat,f"Backup failed: {esc(e)}")

# ---------------- CALLBACK ROUTER ----------------
@bot.callback_query_handler(func=lambda q: True)
def callback(q):
    uid,chat,data=q.from_user.id,q.message.chat.id,q.data
    try: bot.answer_callback_query(q.id)
    except Exception: pass
    if data=="verify":
        if join_ok(uid): bot.send_message(chat,"✅ Verified. Use /start.")
        else: bot.answer_callback_query(q.id,"Please join the channel first.",show_alert=True)
        return
    if not require_user(chat,uid): return
    if data.startswith("a_"):
        if not admin(uid): bot.answer_callback_query(q.id,"Admin only",show_alert=True); return
        admin_action(chat,data); return
    if data=="home": bot.send_message(chat,"🏠 Main Menu",reply_markup=user_menu())
    elif data=="products": list_products(chat)
    elif data=="featured": list_products(chat,True)
    elif data=="search": ask(chat,"🔎 Send product name or keyword:","search")
    elif data=="wallet":
        with connect() as c: r=c.execute("SELECT balance FROM users WHERE user_id=?",(uid,)).fetchone()
        bal=float(r["balance"]) if r else 0
        k=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("➕ Add Balance",callback_data="wallet_add"))
        bot.send_message(chat,f"👛 Wallet Balance: <b>₹{bal:.2f}</b>",reply_markup=k)
    elif data=="wallet_add": ask(chat,"Enter amount to add to wallet:","wallet_amount")
    elif data=="profile":
        with connect() as c: r=c.execute("SELECT * FROM users WHERE user_id=?",(uid,)).fetchone()
        bot.send_message(chat,f"👤 <b>Profile</b>\nID: <code>{uid}</code>\nName: {esc(r['name'] if r else '')}\nBalance: ₹{float(r['balance']) if r else 0:.2f}")
    elif data=="orders":
        with connect() as c: rows=c.execute("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 15",(uid,)).fetchall()
        if not rows: bot.send_message(chat,"No orders yet.")
        else:
            lines=["🧾 <b>Purchase History</b>"]
            for r in rows: lines.append(f"• <code>{esc(r['order_id'])}</code> | ₹{r['amount']:.2f} | {r['kind']} | {r['status']}")
            bot.send_message(chat,"\n".join(lines))
    elif data=="downloads":
        with connect() as c:
            rows=c.execute("""SELECT p.id,p.name,MAX(x.created_at) bought FROM purchases x
                              JOIN products p ON p.id=x.product_id WHERE x.user_id=?
                              GROUP BY p.id,p.name ORDER BY bought DESC LIMIT 30""",(uid,)).fetchall()
        if not rows: bot.send_message(chat,"📥 Abhi aapki koi purchased download nahi hai.")
        else:
            k=types.InlineKeyboardMarkup()
            for r in rows:
                k.add(types.InlineKeyboardButton("📥 "+r["name"],callback_data=f"download:{r['id']}"))
            bot.send_message(chat,"📥 <b>Your Purchased Downloads</b>\\nKisi product par tap karke file dobara le sakte ho.",reply_markup=k)
    elif data.startswith("download:"):
        pid=int(data.split(":")[1])
        with connect() as c:
            own=c.execute("SELECT 1 FROM purchases WHERE user_id=? AND product_id=? LIMIT 1",(uid,pid)).fetchone()
        if own: send_product_file(uid,pid)
        else: bot.send_message(chat,"❌ Ye file aapke account me purchased nahi hai.")
    elif data=="favorites":
        with connect() as c:
            rows=c.execute("""SELECT p.id,p.name,p.price FROM favorites f JOIN products p ON p.id=f.product_id
                              WHERE f.user_id=? AND p.active=1 ORDER BY f.created_at DESC""",(uid,)).fetchall()
        if not rows: bot.send_message(chat,"❤️ Wishlist empty hai. Product page se Add to Wishlist karo.")
        else:
            k=types.InlineKeyboardMarkup()
            for r in rows: k.add(types.InlineKeyboardButton(f"{r['name']} — ₹{r['price']:.2f}",callback_data=f"p:{r['id']}"))
            bot.send_message(chat,"❤️ <b>Your Wishlist</b>",reply_markup=k)
    elif data.startswith("favadd:"):
        pid=int(data.split(":")[1])
        with connect() as c: c.execute("INSERT OR IGNORE INTO favorites(user_id,product_id) VALUES(?,?)",(uid,pid))
        bot.send_message(chat,"❤️ Product wishlist me add ho gaya.")
    elif data=="transactions":
        with connect() as c: rows=c.execute("SELECT amount,kind,created_at FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT 20",(uid,)).fetchall()
        if not rows: bot.send_message(chat,"Abhi transaction history empty hai.")
        else:
            lines=["💳 <b>Wallet Transaction History</b>"]
            for r in rows: lines.append(f"• ₹{r['amount']:.2f} | {esc(r['kind'])} | {esc(r['created_at'])}")
            bot.send_message(chat,"\\n".join(lines))
    elif data=="notifications":
        with connect() as c:
            rows=c.execute("SELECT id,message FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 20",(uid,)).fetchall()
            c.execute("UPDATE notifications SET seen=1 WHERE user_id=?",(uid,))
        if not rows: bot.send_message(chat,"🔔 Koi new notification nahi hai.")
        else: bot.send_message(chat,"🔔 <b>Your Notifications</b>\\n\\n"+"\\n\\n".join("• "+esc(r["message"]) for r in rows))
    elif data=="offers":
        with connect() as c: rows=c.execute("SELECT code,discount,uses_left FROM coupons WHERE active=1 AND uses_left!=0 ORDER BY code").fetchall()
        if not rows: bot.send_message(chat,"Abhi koi active offer nahi hai.")
        else: bot.send_message(chat,"🎟️ <b>Active Offers</b>\\n\\n"+"\\n".join(f"• <code>{esc(r['code'])}</code> — {r['discount']:g}% OFF"+(" (Unlimited)" if r["uses_left"]<0 else f" ({r['uses_left']} uses left)") for r in rows))
    elif data=="ticket":
        ask(chat,"🆘 Apni problem detail me bhejo. Support ticket create ho jayega:","ticket")
    elif data.startswith("p:"): show_product(chat,int(data.split(":")[1]))
    elif data.startswith("buy:"): buy_product(chat,uid,int(data.split(":")[1]))
    elif data.startswith("payfull:"): checkout_product(chat,uid,int(data.split(":")[1]),"")
    elif data.startswith("cancelpay:"):
        oid=data.split(":",1)[1]
        with connect() as c: r=c.execute("SELECT user_id,status FROM orders WHERE order_id=?",(oid,)).fetchone()
        if not r or (r["user_id"]!=uid and not admin(uid)):
            bot.send_message(chat,"Order not found.")
        elif r["status"]=="success":
            delete_payment_qr(oid); bot.send_message(chat,"✅ Payment already verified. QR removed.")
        else:
            delete_payment_qr(oid)
            # Do not mark the order failed here: background verification continues so
            # a payment made just before tapping cancel is still credited safely.
            bot.send_message(chat,"❎ Payment screen/QR removed. Background verification is still active for a short time; if you already paid, wallet will update automatically.")
    elif data.startswith("check:"):
        oid=data.split(":",1)[1]
        with connect() as c: r=c.execute("SELECT * FROM orders WHERE order_id=?",(oid,)).fetchone()
        if not r or (r["user_id"]!=uid and not admin(uid)): bot.send_message(chat,"Order not found.")
        elif r["status"]=="success": bot.send_message(chat,"✅ Already paid.")
        else:
            try:
                v=verify_gateway_order(oid)
                if fulfill(oid,v): bot.send_message(chat,"✅ Payment verified and processed.")
                else:
                    vv=gateway_payload(v); status=v.get("status",vv.get("status","pending"))
                    bot.send_message(chat,f"Payment status: {esc(status)}")
            except Exception as e: bot.send_message(chat,f"Verification error: {esc(e)}")
    elif data.startswith("rate:"):
        pid=int(data.split(":")[1]); states[chat]={"mode":"review","pid":pid}
        bot.send_message(chat,"Send rating as 1-5 followed by a short review (example: 5 Great product).")
    elif data.startswith("rmprod:"):
        if admin(uid):
            pid=int(data.split(":")[1])
            with connect() as c: c.execute("UPDATE products SET active=0 WHERE id=?",(pid,))
            bot.send_message(chat,"Product hidden from store.")
    elif data.startswith("stock:"):
        if admin(uid):
            pid=int(data.split(":")[1]); ask(chat,f"Enter new stock for product #{pid} (-1 means unlimited):",f"stock:{pid}")
    elif data.startswith("feature:"):
        if admin(uid):
            pid=int(data.split(":")[1])
            with connect() as c: c.execute("UPDATE products SET featured=1-featured WHERE id=?",(pid,))
            bot.send_message(chat,"Featured status toggled.")
    elif data.startswith("paycoupon:"):
        pid=int(data.split(":")[1]); st=states.get(chat,{})
        code=st.get("coupon","")
        checkout_product(chat,uid,pid,code)
    elif data.startswith("usecoupon:"):
        pid=int(data.split(":")[1]); st=states.get(chat,{})
        checkout_product(chat,uid,pid,st.get("coupon",""))

def checkout_product(chat,uid,pid,code):
    amount,coupon=discounted(pid,code)
    if amount is None: bot.send_message(chat,"Product not found."); return
    try:
        start_payment(chat,uid,amount,"product",pid,coupon)
        if coupon:
            with connect() as c: c.execute("UPDATE coupons SET uses_left=uses_left-1 WHERE code=? AND uses_left>0",(coupon,))
    except Exception as e:
        log.exception("Checkout failed")
        bot.send_message(chat,f"❌ Payment setup failed: {esc(e)}")

# ---------------- ADMIN ACTIONS ----------------
def admin_action(chat,data):
    if data=="a_add":
        states[chat]={"mode":"add_name"}
        bot.send_message(chat,"➕ Send product name.")
    elif data=="a_products":
        with connect() as c: rows=c.execute("SELECT id,name,price,active,featured,stock FROM products ORDER BY id DESC").fetchall()
        if not rows: bot.send_message(chat,"No products yet."); return
        for p in rows:
            k=types.InlineKeyboardMarkup(row_width=2)
            k.add(types.InlineKeyboardButton("⭐ Feature",callback_data=f"feature:{p['id']}"),
                  types.InlineKeyboardButton("📦 Stock",callback_data=f"stock:{p['id']}"),
                  types.InlineKeyboardButton("🗑 Hide",callback_data=f"rmprod:{p['id']}"))
            bot.send_message(chat,f"#{p['id']} {esc(p['name'])} | ₹{p['price']:.2f} | active={p['active']} | stock={p['stock']}",reply_markup=k)
    elif data=="a_api":
        ask(chat,"Send FamGateway API Key (stored privately in SQLite; never sent to users):","api_key")
    elif data=="a_channel":
        ask(chat,"Send force-join channel username, e.g. @YourChannel. Send NONE to disable:","channel")
    elif data=="a_ban":
        ask(chat,"Send user ID and action: BAN 123456789 or UNBAN 123456789","ban")
    elif data=="a_stats":
        with connect() as c:
            users=c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
            products=c.execute("SELECT COUNT(*) n FROM products WHERE active=1").fetchone()["n"]
            sales=c.execute("SELECT COALESCE(SUM(amount),0) n FROM orders WHERE status='success'").fetchone()["n"]
            orders=c.execute("SELECT COUNT(*) n FROM orders WHERE status='success'").fetchone()["n"]
            today=c.execute("SELECT COALESCE(SUM(amount),0) n FROM orders WHERE status='success' AND date(paid_at)=date('now','localtime')").fetchone()["n"]
        bot.send_message(chat,f"📊 <b>Store Analytics</b>\nUsers: {users}\nActive Products: {products}\nSuccessful Orders: {orders}\nLifetime Sales: ₹{sales:.2f}\nToday's Sales: ₹{today:.2f}")
    elif data=="a_coupon":
        ask(chat,"Send coupon as CODE DISCOUNT_PERCENT USES (example SAVE10 10 100; -1 uses = unlimited):","coupon")
    elif data=="a_broadcast":
        ask(chat,"Send broadcast message to all users:","broadcast")
    elif data=="a_orders":
        with connect() as c: rows=c.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 20").fetchall()
        if not rows: bot.send_message(chat,"No orders yet.")
        for r in rows: bot.send_message(chat,f"Order <code>{esc(r['order_id'])}</code>\nUser: {r['user_id']}\n₹{r['amount']:.2f} | {r['kind']} | {r['status']}\nUTR: {esc(r['utr'])}")
    elif data=="a_settings":
        ask(chat,"Send STORE_NAME | WELCOME_MESSAGE | SUPPORT_URL (separate with |):","settings")
    elif data=="a_balance":
        ask(chat,"Send CREDIT user_id amount OR DEBIT user_id amount","balance")
    elif data=="a_addbalance":
        ask(chat,"➕ Send user_id amount to add wallet balance (example: 123456789 100)","addbalance")
    elif data=="a_users":
        with connect() as c: rows=c.execute("SELECT user_id,name,username,balance,banned FROM users ORDER BY last_seen DESC LIMIT 20").fetchall()
        if not rows: bot.send_message(chat,"No users registered yet.")
        else:
            lines=["👥 <b>Recent Users</b>"]
            for u in rows: lines.append(f"{u['user_id']} | {esc(u['name'])} | @{esc(u['username'])} | ₹{u['balance']:.2f} | {'BANNED' if u['banned'] else 'Active'}")
            bot.send_message(chat,"\n".join(lines))
    elif data=="a_pending":
        with connect() as c: rows=c.execute("SELECT order_id,user_id,amount,kind,created_at FROM orders WHERE status='pending' ORDER BY id DESC LIMIT 20").fetchall()
        if not rows: bot.send_message(chat,"✅ No pending payments.")
        else:
            lines=["⏳ <b>Pending Payments</b>"]
            kb=types.InlineKeyboardMarkup(row_width=1)
            for r in rows:
                lines.append(f"<code>{esc(r['order_id'])}</code> | {r['user_id']} | ₹{r['amount']:.2f} | {r['kind']} | {r['created_at']}")
                kb.add(types.InlineKeyboardButton("🔄 Verify "+str(r['order_id'])[:24],callback_data="check:"+r['order_id']))
            bot.send_message(chat,"\n".join(lines),reply_markup=kb)
    elif data=="a_backup": send_backup(chat)
    elif data=="a_maintenance":
        val="0" if get_setting("maintenance","0")=="1" else "1"; put_setting("maintenance",val)
        bot.send_message(chat,"Maintenance mode is now "+("ON" if val=="1" else "OFF"))
    elif data=="a_notify":
        ask(chat,"Send user_id | message to send a targeted notification:","notify")
    elif data=="a_reviews":
        with connect() as c: rows=c.execute("SELECT * FROM reviews ORDER BY id DESC LIMIT 15").fetchall()
        if not rows: bot.send_message(chat,"No reviews yet.")
        for r in rows: bot.send_message(chat,f"Product #{r['product_id']} | User {r['user_id']} | {r['stars']}⭐\n{esc(r['comment'])}")

# ---------------- MESSAGE STATE HANDLER ----------------
@bot.message_handler(content_types=["text","document","photo","video","audio"])
def messages(m):
    uid,chat=m.from_user.id,m.chat.id
    if m.text and m.text.startswith("/"): return
    save_user(m.from_user)
    st=states.get(chat)
    if not st:
        return
    mode=st.get("mode") if isinstance(st,dict) else st
    if mode and mode.startswith("stock:"):
        if not admin(uid): states.pop(chat,None); return
        try:
            n=int(m.text.strip())
            if n < -1: raise ValueError()
            pid=int(mode.split(":")[1])
            with connect() as c: c.execute("UPDATE products SET stock=? WHERE id=?",(n,pid))
            bot.reply_to(m,"✅ Stock updated.")
        except Exception: bot.reply_to(m,"Enter -1 or a non-negative whole number.")
        states.pop(chat,None); return
    if mode=="wallet_amount":
        amt=money(m.text)
        if amt is None: bot.reply_to(m,"Invalid amount. Enter a positive amount."); return
        states.pop(chat,None)
        try: start_payment(chat,uid,amt,"wallet")
        except Exception as e: bot.reply_to(m,f"Payment setup error: {esc(e)}")
        return
    if mode=="search":
        term=m.text.strip()
        with connect() as c: rows=c.execute("SELECT id,name,price FROM products WHERE active=1 AND name LIKE ? ORDER BY id DESC LIMIT 30",("%"+term+"%",)).fetchall()
        states.pop(chat,None)
        if not rows: bot.reply_to(m,"No matching products found.")
        else:
            k=types.InlineKeyboardMarkup()
            for p in rows: k.add(types.InlineKeyboardButton(f"{p['name']} — ₹{p['price']:.2f}",callback_data=f"p:{p['id']}"))
            bot.reply_to(m,"🔎 Search Results:",reply_markup=k)
        return
    if mode=="review":
        try:
            parts=m.text.split(maxsplit=1); stars=int(parts[0])
            if stars<1 or stars>5: raise ValueError()
            comment=parts[1] if len(parts)>1 else ""
            with connect() as c: c.execute("INSERT INTO reviews(user_id,product_id,stars,comment) VALUES(?,?,?,?)",(uid,st["pid"],stars,comment[:500]))
            bot.reply_to(m,"⭐ Thank you for your review!")
        except Exception: bot.reply_to(m,"Format: 1-5 followed by review.")
        states.pop(chat,None); return
    if mode=="ticket":
        msg=(m.text or "").strip()
        if not msg: bot.reply_to(m,"Please apni problem text me bhejo."); return
        with connect() as c:
            cur=c.execute("INSERT INTO tickets(user_id,message) VALUES(?,?)",(uid,msg[:1500]))
            tid=cur.lastrowid
        states.pop(chat,None)
        bot.reply_to(m,f"✅ Ticket #{tid} create ho gaya. Admin aapko reply karega.")
        try: bot.send_message(OWNER_ID,f"🆘 <b>New Support Ticket #{tid}</b>\\nUser: <code>{uid}</code>\\n{esc(msg[:1500])}\\nReply format: /reply {tid} your message")
        except Exception: log.exception("Ticket admin notification failed")
        return
    if not admin(uid) and mode not in ("wallet_amount","search","review","buy_coupon"):
        return
    if mode=="api_key":
        put_setting("api_key",m.text.strip()); states.pop(chat,None)
        ask(chat,"Now send API Base URL (normally https://famgateway.in):","api_url")
    elif mode=="api_url":
        url=m.text.strip().rstrip("/")
        if not url.startswith("https://"): bot.reply_to(m,"Use a valid https:// URL."); return
        put_setting("api_url",url); states.pop(chat,None)
        bot.reply_to(m,"✅ FamGateway API Key and URL saved in admin settings.")
    elif mode=="channel":
        val="" if m.text.strip().upper()=="NONE" else m.text.strip()
        put_setting("channel",val); states.pop(chat,None)
        bot.reply_to(m,"✅ Force-join channel updated. Make the bot an admin in that channel so Telegram can verify membership.")
    elif mode=="add_name":
        states[chat]={"mode":"add_price","name":m.text.strip()[:100]}
        bot.reply_to(m,"Send product price in INR.")
    elif mode=="add_price":
        amt=money(m.text)
        if amt is None: bot.reply_to(m,"Invalid price. Send a positive number."); return
        states[chat].update({"mode":"add_desc","price":amt})
        bot.reply_to(m,"Send product description (or - for none).")
    elif mode=="add_desc":
        states[chat].update({"mode":"add_stock","description":"" if m.text.strip()=="-" else m.text[:800]})
        bot.reply_to(m,"Send stock quantity (-1 for unlimited).")
    elif mode=="add_stock":
        try:
            n=int(m.text.strip())
            if n < -1: raise ValueError()
        except Exception: bot.reply_to(m,"Enter -1 or 0 or a positive whole number."); return
        states[chat]["stock"]=n; states[chat]["mode"]="add_file"
        bot.reply_to(m,"Now upload the product file, photo, video, audio, or send text content.")
    elif mode=="add_file":
        fid,typ=None,None
        if m.document: fid,typ=m.document.file_id,"document"
        elif m.photo: fid,typ=m.photo[-1].file_id,"photo"
        elif m.video: fid,typ=m.video.file_id,"video"
        elif m.audio: fid,typ=m.audio.file_id,"audio"
        elif m.text: fid,typ=m.text,"text"
        if not fid: bot.reply_to(m,"Upload a supported file or send text."); return
        data=states.pop(chat)
        with connect() as c:
            c.execute("INSERT INTO products(name,description,price,file_id,file_type,stock) VALUES(?,?,?,?,?,?)",
                      (data["name"],data["description"],data["price"],fid,typ,data["stock"]))
        bot.reply_to(m,f"✅ <b>Product Added</b>\n{esc(data['name'])}\nPrice: ₹{data['price']:.2f}\nStock: {data['stock']}")
    elif mode=="ban":
        try:
            action,target=m.text.split()
            target=int(target)
            if action.upper() not in ("BAN","UNBAN"): raise ValueError()
            with connect() as c:
                c.execute("UPDATE users SET banned=? WHERE user_id=?",(1 if action.upper()=="BAN" else 0,target))
            bot.reply_to(m,f"✅ {action.upper()} applied to {target}.")
        except Exception: bot.reply_to(m,"Format: BAN 123456789 or UNBAN 123456789")
        states.pop(chat,None)
    elif mode=="coupon":
        try:
            code,disc,uses=m.text.split()
            disc=float(disc); uses=int(uses)
            if not 0<disc<100 or uses==0 or uses < -1: raise ValueError()
            with connect() as c:
                c.execute("INSERT OR REPLACE INTO coupons(code,discount,uses_left,active) VALUES(?,?,?,1)",(code.upper(),disc,uses))
            bot.reply_to(m,"✅ Coupon saved.")
            states.pop(chat,None)
        except Exception: bot.reply_to(m,"Format: CODE DISCOUNT_PERCENT USES (uses -1 for unlimited)")
    elif mode=="broadcast":
        with connect() as c: users=c.execute("SELECT user_id FROM users WHERE banned=0").fetchall()
        sent=failed=0
        for u in users:
            try: bot.send_message(u["user_id"],m.text); sent+=1
            except Exception: failed+=1
        states.pop(chat,None); bot.reply_to(m,f"📣 Broadcast complete. Sent: {sent}, Failed: {failed}")
    elif mode=="settings":
        try:
            name,welcome,support=[x.strip() for x in m.text.split("|",2)]
            if not support.startswith("https://"): raise ValueError()
            put_setting("store_name",name[:80]); put_setting("welcome",welcome[:500]); put_setting("support",support)
            states.pop(chat,None); bot.reply_to(m,"✅ Store settings updated.")
        except Exception: bot.reply_to(m,"Format: STORE_NAME | WELCOME_MESSAGE | https://support-link")
    elif mode=="addbalance":
        try:
            target_s,amount_s=m.text.split(); target=int(target_s); amount=money(amount_s)
            if amount is None: raise ValueError()
            with connect() as c:
                exists=c.execute("SELECT 1 FROM users WHERE user_id=?",(target,)).fetchone()
                if not exists: raise ValueError("User not registered")
                c.execute("UPDATE users SET balance=balance+? WHERE user_id=?",(amount,target))
                c.execute("INSERT INTO transactions(user_id,amount,kind,reference) VALUES(?,?,?,?)",(target,amount,"admin_credit","admin:add:"+secrets.token_hex(12)))
                bal=c.execute("SELECT balance FROM users WHERE user_id=?",(target,)).fetchone()["balance"]
            bot.reply_to(m,f"✅ ₹{amount:.2f} added. User new balance: ₹{bal:.2f}")
            try: bot.send_message(target,f"💎 Admin added ₹{amount:.2f} to your wallet. New balance: ₹{bal:.2f}")
            except Exception: pass
            states.pop(chat,None)
        except Exception as e: bot.reply_to(m,f"Format: user_id amount (example: 123456789 100). {esc(e)}")
    elif mode=="balance":
        try:
            action,target,amount=m.text.split(); target=int(target); amount=money(amount)
            if action.upper() not in ("CREDIT","DEBIT") or amount is None: raise ValueError()
            with connect() as c:
                c.execute("UPDATE users SET balance=MAX(0,balance+?) WHERE user_id=?",
                          (amount if action.upper()=="CREDIT" else -amount,target))
                c.execute("INSERT INTO transactions(user_id,amount,kind,reference) VALUES(?,?,?,?)",
                          (target,amount,"admin_"+action.lower(),"admin:"+secrets.token_hex(10)))
            bot.reply_to(m,"✅ Wallet updated.")
            bot.send_message(target,f"Wallet update: {action.upper()} ₹{amount:.2f}")
            states.pop(chat,None)
        except Exception: bot.reply_to(m,"Format: CREDIT 123456789 100 OR DEBIT 123456789 50")
    elif mode=="notify":
        try:
            target,msg=m.text.split("|",1); target=int(target.strip())
            with connect() as c: c.execute("INSERT INTO notifications(user_id,message) VALUES(?,?)",(target,msg.strip()))
            bot.send_message(target,msg.strip()); bot.reply_to(m,"✅ Notification sent.")
            states.pop(chat,None)
        except Exception: bot.reply_to(m,"Format: user_id | message")
    elif mode=="buy_coupon":
        pid=st["pid"]; code=m.text.strip().upper()
        amount,coupon=discounted(pid,code)
        if not coupon:
            bot.reply_to(m,"Coupon invalid/expired. Continue without coupon using the button below.")
            k=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("Continue without coupon",callback_data=f"payfull:{pid}"))
            bot.send_message(chat,"Choose:",reply_markup=k); states.pop(chat,None); return
        states.pop(chat,None)
        checkout_product(chat,uid,pid,coupon)

# ---------------- ADMIN PANEL COMMAND ----------------
@bot.message_handler(commands=["reply"])
def reply_ticket(m):
    if not admin(m.from_user.id): return
    try:
        _,tid,msg=m.text.split(maxsplit=2); tid=int(tid)
        with connect() as c:
            row=c.execute("SELECT user_id FROM tickets WHERE id=?",(tid,)).fetchone()
            if not row: bot.reply_to(m,"Ticket not found."); return
            c.execute("UPDATE tickets SET status='replied',admin_reply=? WHERE id=?",(msg[:1500],tid))
            c.execute("INSERT INTO notifications(user_id,message) VALUES(?,?)",(row["user_id"],f"Support ticket #{tid}: {msg[:1500]}"))
        bot.send_message(row["user_id"],f"💬 <b>Support Reply — Ticket #{tid}</b>\\n{esc(msg[:1500])}")
        bot.reply_to(m,"✅ Reply sent.")
    except Exception: bot.reply_to(m,"Format: /reply TICKET_ID your message")

@bot.message_handler(commands=["tickets"])
def tickets_cmd(m):
    if not admin(m.from_user.id): return
    with connect() as c: rows=c.execute("SELECT id,user_id,message FROM tickets WHERE status='open' ORDER BY id DESC LIMIT 20").fetchall()
    if not rows: bot.reply_to(m,"No open tickets."); return
    for r in rows:
        bot.send_message(m.chat.id,f"🆘 Ticket #{r['id']} | User <code>{r['user_id']}</code>\\n{esc(r['message'])}\\nReply: /reply {r['id']} your message")

@bot.message_handler(commands=["panel"])
def panel(m):
    if admin(m.from_user.id): bot.send_message(m.chat.id,"🛡️ <b>Admin Panel</b>",reply_markup=admin_menu())

log.info("ARYAN PREMIUM STORE starting")
bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
