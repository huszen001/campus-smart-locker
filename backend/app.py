"""
校园智能寄存柜 —— 后端服务 (Flask + MQTT + SQLite)

数据流：
    手机(微信扫一扫) --HTTP--> 本服务 --MQTT--> broker --MQTT--> ESP32
    ESP32 --MQTT(status/event)--> broker --MQTT--> 本服务

订单状态机（等门磁回执才落库，不靠"发出去就当成功"）：
    pending --收到 door_opened--> opened --收到 door_closed--> done
       |                             |
       +--- 超时无回执 ---------------+---> failed

两条开门入口，共用同一套授权与订单逻辑：
    扫码：存件填手机号 / 取件凭 6 位取件码
    刷卡：卡 UID 查绑定用户；该格有物品时，只有卡主就是占用者才能取

运行：
    python app.py            监听 0.0.0.0:5000
"""
import contextlib
import io
import json
import os
import random
import re
import socket
import sqlite3
import threading
import time
from collections import namedtuple
from datetime import datetime
from pathlib import Path

import paho.mqtt.client as mqtt
import segno
from flask import Flask, abort, jsonify, render_template, request, send_file

# ---------------------------------- 配置 ----------------------------------
MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "locker")
MQTT_PASS = os.environ.get("MQTT_PASS", "locker123")
DEVICE_ID = "locker1"
# 柜格数。**真机只装了 1 号格的硬件**（舵机 / 门磁 / 秤），2 号及以后的格由固件
# 虚拟出来（状态、开门时序都自己演），所以这里大于 1 是正常状态，不是配错 ——
# 屏幕上那几格会显示"离线"是它真的没心跳，不是页面坏了。
# 要退回单格：set BOX_COUNT=1
BOX_COUNT = max(1, int(os.environ.get("BOX_COUNT", "4")))
# 真机只给这一格接了硬件（舵机 / 门磁 / 秤），和固件里的 REAL_BOX 是同一个值。
# 只有这一格的 item 是**物理事实**；2 号及以后是固件按后端指令自己编的，
# 拿它对账等于自己骗自己 —— 所以对账只在这一格做（见 check_reconcile）。
REAL_BOX = max(1, min(BOX_COUNT, int(os.environ.get("REAL_BOX", "1"))))
HTTP_PORT = 5000
DB_PATH = Path(__file__).with_name("locker.db")

# 设备每 10s 发一次心跳，所以 30s 没动静就算掉线
OFFLINE_AFTER = int(os.environ.get("OFFLINE_AFTER", "30"))
# 命令下发后多久没等到 door_opened 回执就判失败
OPEN_ACK_TIMEOUT = int(os.environ.get("OPEN_ACK_TIMEOUT", "15"))
# 对账：设备连续报"和 DB 里的占用记录对不上"多久，才认定 DB 那条是脏的。
# 用**时间**而不是次数：门关那一下读数会跳，短时间内的几次心跳可能都撞在这个
# 窗口里，按次数算会误杀一张好单。心跳 10s 一次，30s 约等于连续 3 次。
RECONCILE_AFTER = int(os.environ.get("RECONCILE_AFTER", "30"))
# opened 之后多久还没关门就判失败（设备自己的 door_left_open 事件是 30s，这里是兜底）
OPEN_CLOSE_TIMEOUT = int(os.environ.get("OPEN_CLOSE_TIMEOUT", "60"))

# ---- 计费（规则见 README「扣费」一节）----
# 下面这四个常数就是全部计费规则。演示时把 FREE_SECONDS / UNIT_SECONDS 调小
# （例如都设成 6），逻辑一条不改，只是跑得快 —— 这是能现场把扣费演给老师看的关键。
FREE_SECONDS = int(os.environ.get("FREE_SECONDS", "1800"))          # 存件后 30 分钟免费
UNIT_SECONDS = max(1, int(os.environ.get("UNIT_SECONDS", "3600")))  # 每满 1 小时为一个计费单位
UNIT_FEE_FEN = int(os.environ.get("UNIT_FEE_FEN", "100"))           # 每计费单位 1 元
MAX_FEE_FEN = int(os.environ.get("MAX_FEE_FEN", "1000"))            # 单次封顶 10 元
# 老库升级到"有余额"这一版时，给已有用户发的启动余额（只在第一次加列时发）
START_BALANCE_FEN = int(os.environ.get("START_BALANCE_FEN", "5000"))
# 用户端模拟支付的单次上限。不是业务规则，纯粹是防手滑打出六个零。
PAY_MAX_FEN = 50000                                                 # ¥500

TOPIC_CMD = f"locker/{DEVICE_ID}/cmd"
TOPIC_STATUS = f"locker/{DEVICE_ID}/status"
TOPIC_EVENT = f"locker/{DEVICE_ID}/event"
TOPIC_AUTH_RESULT = f"locker/{DEVICE_ID}/auth_result"

# 订单状态
PENDING, OPENED, DONE, FAILED = "pending", "opened", "done", "failed"

PHONE_RE = re.compile(r"^1\d{10}$")
# 绑卡窗口：页面点了「绑定校园卡」之后，多久之内刷的那张未绑定卡算绑给它
BIND_WINDOW = int(os.environ.get("BIND_WINDOW", "45"))

# 监控台口令。监控台要列全部订单，而订单里有取件码 —— 取件码能开柜，
# 不挡的话局域网里谁都能捞走别人的码，整套取件码授权就白做了。
# 用环境变量覆盖：set ADMIN_KEY=你的口令
ADMIN_KEY = os.environ.get("ADMIN_KEY", "locker-admin")

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
# debug=False 时 Flask 默认把模板缓存住，改了 templates/*.html 不重启看不到效果。
# 课设要反复调页面，这里打开自动重载（代价只是每次渲染多做一次 mtime 检查）。
app.config["TEMPLATES_AUTO_RELOAD"] = True

# 柜格实时状态：来自设备心跳，放内存即可（设备一上线就会重发）
# last_seen 是**服务端收到消息的时刻**，用它判在线；绝不能用报文里的 ts
# —— 固件发的是 millis()/1000（开机秒数），模拟器发的是 Unix 秒，两者语义不同。
boxes = {
    n: {"box": n, "state": "UNKNOWN", "locked": None, "door_open": None,
        "item": None, "battery": None, "ts": 0, "last_seen": 0.0}
    for n in range(1, BOX_COUNT + 1)
}
boxes_lock = threading.Lock()

# 绑卡要分两步走：后端收到 card_scan 时并不知道这张卡是谁的（没有登录态），
# 所以得由页面先"认领"（在这里登记手机号），用户再刷卡，两边配对。
# 单格原型，同时只允许一个绑卡请求。
bind_wait = {"phone": None, "until": 0.0}
bind_lock = threading.Lock()

# 真机格的对账状态。只在 MQTT 回调线程里读写，不需要锁。
#   kind  : 当前对不上的类型（ghost = DB 说有物、秤说没有；unregistered = 反过来）
#   since : 这一轮矛盾是从什么时候开始的（用来算够不够 RECONCILE_AFTER）
#   fired : 这一轮已经处理/报过没有 —— 不置位的话"有东西没登记"会每 30s 刷一条审计
reconcile = {"kind": None, "since": 0.0, "fired": False}


PRICING = {"free": FREE_SECONDS, "unit": UNIT_SECONDS,
           "fee": UNIT_FEE_FEN, "max": MAX_FEE_FEN}


def human_secs(s: int) -> str:
    """把秒数说成人话。演示时把阈值压到几秒，页面上也不会写着"30 分钟"骗人。"""
    if s % 3600 == 0:
        return f"{s // 3600} 小时"
    if s % 60 == 0:
        return f"{s // 60} 分钟"
    return f"{s} 秒"


def admin_denied(html=False):
    """监控台 + 订单/日志接口的口令闸门。通过则返回 None。

    口令从 ?k=xxx 或请求头 X-Admin-Key 取。**扫码页不查口令** —— 它是给
    所有人用的，而且它只碰 /api/boxes 和单条 /api/order/<id>，捞不到别人的东西。
    """
    key = request.args.get("k") or request.headers.get("X-Admin-Key", "")
    if key == ADMIN_KEY:
        return None
    if html:
        # 这里绝不能提示口令是什么 —— 那等于把钥匙挂在门上：
        # 任何人访问一下 / 就拿到口令了，这道闸门白设。
        return ("<meta charset='utf-8'><body style='font:15px/1.7 sans-serif;padding:40px'>"
                "<h3>需要口令</h3><p>监控台地址后面要带上口令："
                "<code>/?k=你的口令</code></p>"
                "<p style='color:#888'>口令不在这里显示，问管理员要。</p></body>"), 401
    return jsonify({"ok": False, "msg": "需要口令：地址后加 ?k=<口令>"}), 401


def take_bind_wait():
    """取出并清空待绑手机号；过期或没人认领则返回 None。

    无论成不成都清空 —— 一张卡只能配一次，配对完就作废。
    """
    with bind_lock:
        phone, until = bind_wait["phone"], bind_wait["until"]
        bind_wait["phone"], bind_wait["until"] = None, 0.0
    return phone if (phone and time.time() < until) else None


def lan_ip() -> str:
    """取本机局域网 IP，让二维码在手机上能打开。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------- 数据库 ----------------------------------
def db():
    # isolation_level=None 关掉隐式事务，改由下面 tx() 显式控制
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")     # 请求线程/事件线程/sweeper 三拨写者
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


@contextlib.contextmanager
def conn():
    """普通语句用这个：自动提交（autocommit），用完一定关连接。

    旧写法 `with sqlite3.connect(...)` 只提交、不关闭，sweeper 每 2 秒写一次会持续漏连接。
    """
    c = db()
    try:
        yield c
    finally:
        c.close()


@contextlib.contextmanager
def tx():
    """需要"检查 + 写入"原子完成时用这个。"""
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        yield c
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    finally:
        c.close()


def init_db():
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            phone      TEXT UNIQUE,
            card_uid   TEXT UNIQUE
        );
        CREATE TABLE IF NOT EXISTS orders (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            box         INTEGER NOT NULL,
            phone       TEXT,
            action      TEXT NOT NULL,     -- store / pickup
            status      TEXT NOT NULL,     -- pending / opened / done / failed
            pickup_code TEXT,              -- 存件时生成，取件凭它
            created_at  TEXT NOT NULL,
            closed_at   TEXT
        );
        CREATE TABLE IF NOT EXISTS audit (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            ts       TEXT NOT NULL,
            source   TEXT NOT NULL,        -- wechat / card / device / system
            actor    TEXT,
            box      INTEGER,
            action   TEXT NOT NULL,
            result   TEXT NOT NULL,        -- ok / deny
            detail   TEXT
        );
        -- 钱包流水。金额一律用"分"（整数），永远不要浮点。
        CREATE TABLE IF NOT EXISTS ledger (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            ts                TEXT NOT NULL,
            phone             TEXT,
            kind              TEXT NOT NULL,     -- charge 扣费 / recharge 充值
            amount_fen        INTEGER NOT NULL,  -- 充值为正、扣费为负（有符号）
            balance_after_fen INTEGER NOT NULL,
            order_id          INTEGER,           -- 扣费对应哪条取件单
            store_id          INTEGER,           -- 对应哪条存件单
            note              TEXT
        );
        -- 一条取件单只允许扣一次。进程在"改状态"和"扣费"之间被杀时靠它兜底。
        CREATE UNIQUE INDEX IF NOT EXISTS ux_ledger_charge_order
            ON ledger(order_id) WHERE kind='charge';
        """)
        # 两个演示用户。只 seed 一个的话，"别人拿不走你的东西"这一幕演不出来。
        # ⚠️ 必须排在下面"发启动余额"之前 —— 全新库上 users 是空表，
        # 余额那条 UPDATE 会扫到零行，种子用户就成 0 元了。
        c.execute("INSERT OR IGNORE INTO users (name, phone, card_uid) VALUES (?,?,?)",
                  ("演示学生", "13800138000", "04A12B3C4D"))
        c.execute("INSERT OR IGNORE INTO users (name, phone, card_uid) VALUES (?,?,?)",
                  ("张三", "13900139000", "11223344AA"))

        # 老库升级：pickup_code / store_id 都是后加的
        cols = {r["name"] for r in c.execute("PRAGMA table_info(orders)")}
        if "pickup_code" not in cols:
            c.execute("ALTER TABLE orders ADD COLUMN pickup_code TEXT")
        if "store_id" not in cols:
            # 取件单指回它的存件单，计费才知道东西放了多久。
            # 不能拿 box+phone 反推：原型只有一格，box 恒为 1，毫无区分力。
            c.execute("ALTER TABLE orders ADD COLUMN store_id INTEGER")

        ucols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
        if "balance_fen" not in ucols:
            # NOT NULL DEFAULT 0：新注册用户直接是 0，也不用满世界防 NULL
            c.execute("ALTER TABLE users ADD COLUMN balance_fen INTEGER NOT NULL DEFAULT 0")
            # 只在这一刻发启动余额 —— 写在 if 里，重启不会把花掉的余额重置
            c.execute("UPDATE users SET balance_fen=?", (START_BALANCE_FEN,))


def audit(source, actor, box, action, result, detail=""):
    with conn() as c:
        c.execute(
            "INSERT INTO audit (ts, source, actor, box, action, result, detail)"
            " VALUES (?,?,?,?,?,?,?)",
            (now_str(), source, actor, box, action, result, detail),
        )
    print(f"[audit] {source}/{actor} box={box} {action} -> {result} {detail}")


# ---------------------------------- 计费 ----------------------------------
def parse_ts(s):
    """'YYYY-MM-DD HH:MM:SS' -> epoch 秒；空值/坏值返回 None。"""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return None


def compute_fee(seconds):
    """按免费时长 / 计费单位 / 单位价 / 封顶 算出费用，单位"分"。

    全整数运算，不碰浮点。恰好等于 FREE_SECONDS 算免费（边界取"不超过"）。
    """
    if seconds is None or seconds <= FREE_SECONDS:
        return 0
    billable = seconds - FREE_SECONDS
    units = (billable + UNIT_SECONDS - 1) // UNIT_SECONDS   # 不足一个单位按一个单位算
    return min(units * UNIT_FEE_FEN, MAX_FEE_FEN)


def accrued_fee(store_row, until=None):
    """一条存件单"到现在为止"累计的费用（分）。

    监控台靠它显示实时跳动的费用 —— 那也是演示时最直观的一幕。
    """
    start = parse_ts(store_row["closed_at"])
    if start is None:
        return 0
    end = until if until is not None else time.time()
    return compute_fee(max(0, int(end - start)))


def settle_pickup(pickup_oid):
    """取件完成后扣一次费。返回 dict（fee/phone/balance）或 None。

    必须在 advance(...DONE) 成功之后调用。内部自成一个 tx()。
    ⚠️ 绝不能在 tx() 里调 audit()/conn()/db() —— 它们会另开一条连接，
    而这里正握着写锁，等于自己等自己。
    """
    if not pickup_oid:
        return None
    with tx() as c:
        p = c.execute("SELECT * FROM orders WHERE id=?", (pickup_oid,)).fetchone()
        if p is None or p["action"] != "pickup" or p["status"] != DONE:
            return None
        # 幂等：这条取件单已经扣过就直接回（sweeper 补账和事件线程可能同时到）
        if c.execute("SELECT 1 FROM ledger WHERE kind='charge' AND order_id=?",
                     (pickup_oid,)).fetchone():
            return None
        if p["store_id"] is None:
            return None
        store = c.execute("SELECT * FROM orders WHERE id=?", (p["store_id"],)).fetchone()
        if store is None or store["action"] != "store" or store["closed_at"] is None:
            return None
        start, end = parse_ts(store["closed_at"]), parse_ts(p["closed_at"])
        if start is None or end is None:
            return None
        secs = int(end - start)
        if secs < 0:
            return None

        # 费用 0 也落一条流水：这样"已结算"就是明确的，
        # sweeper 的补账扫描靠"有没有 charge 流水"判断，不会反复重扣。
        fee = compute_fee(secs)
        phone = store["phone"]
        row = c.execute("SELECT COALESCE(balance_fen,0) AS b FROM users WHERE phone=?",
                        (phone,)).fetchone()
        if row is None:
            return None
        new_bal = row["b"] - fee          # 允许扣成负数：东西必须能取出来
        c.execute("UPDATE users SET balance_fen=? WHERE phone=?", (new_bal, phone))
        c.execute(
            "INSERT INTO ledger (ts, phone, kind, amount_fen, balance_after_fen,"
            " order_id, store_id, note) VALUES (?,?,?,?,?,?,?,?)",
            (p["closed_at"] or now_str(), phone, "charge", -fee, new_bal,
             p["id"], store["id"], f"寄存 {secs}s"),
        )
        return {"fee": fee, "phone": phone, "balance": new_bal}


def recharge(phone, amount_fen, note=""):
    """管理员充值。返回新余额（分）；参数不对抛 ValueError。"""
    if amount_fen <= 0:
        raise ValueError("充值金额必须是正整数（单位：分）")
    with tx() as c:
        row = c.execute("SELECT COALESCE(balance_fen,0) AS b FROM users WHERE phone=?",
                        (phone,)).fetchone()
        if row is None:
            raise ValueError("该手机号还没注册")
        new_bal = row["b"] + amount_fen
        c.execute("UPDATE users SET balance_fen=? WHERE phone=?", (new_bal, phone))
        c.execute(
            "INSERT INTO ledger (ts, phone, kind, amount_fen, balance_after_fen, note)"
            " VALUES (?,?,?,?,?,?)",
            (now_str(), phone, "recharge", amount_fen, new_bal, note),
        )
        return new_bal


def parse_amount_fen(body):
    """页面收「元」，后端一律收「分」—— 换算只在这一处发生。

    返回 (分, 错误信息)，两者必有一个是 None。
    """
    try:
        fen = int(round(float(body.get("amount", 0)) * 100))
    except (TypeError, ValueError):
        return None, "金额填个数字"
    if fen <= 0:
        return None, "充值金额要大于 0"
    return fen, None


def order_fee(c, row):
    """一条订单该显示的费用。返回 (费用分, 是否还在实时累计)。

    - 取件单：已结算就取那笔流水；还没结算回 None（前端继续等下一轮轮询）。
    - 存件单：已经取走了就取流水；还占着格子才显示实时累计 ——
      不加这道判断的话，取走的单会跟着钟一直涨。
    第二个返回值给前端区分"¥1.00"和"≈¥1.00（还在涨）" ——
    演示时费用当场上跳，那一幕要看得出来。
    """
    if row["action"] == "pickup":
        r = c.execute("SELECT amount_fen FROM ledger WHERE kind='charge' AND order_id=?",
                      (row["id"],)).fetchone()
        return ((-r["amount_fen"]) if r else None), False
    r = c.execute("SELECT amount_fen FROM ledger WHERE kind='charge' AND store_id=?",
                  (row["id"],)).fetchone()
    if r:
        return -r["amount_fen"], False
    occ = occupant(c, row["box"])
    if occ is not None and occ["id"] == row["id"]:
        return accrued_fee(row), True
    return None, False


# ---------------------------------- 订单 ----------------------------------
def advance(box: int, from_status: str, to_status: str, closed: bool = False):
    """条件 UPDATE（compare-and-set）：只有订单仍是 from_status 才推进。

    sweeper 线程和 MQTT 事件线程会同时想改同一条订单，无条件写会互相覆盖。
    返回被推进的 order id；没推进任何东西则返回 None（说明已被对方改过 / 迟到回执）。
    """
    with conn() as c:
        row = c.execute(
            "SELECT id FROM orders WHERE box=? AND status=? ORDER BY id DESC LIMIT 1",
            (box, from_status)).fetchone()
        if row is None:
            return None
        sql = ("UPDATE orders SET status=?, closed_at=? WHERE id=? AND status=?"
               if closed else "UPDATE orders SET status=? WHERE id=? AND status=?")
        args = ([to_status, now_str(), row["id"], from_status] if closed
                else [to_status, row["id"], from_status])
        return row["id"] if c.execute(sql, args).rowcount else None


def active_order(c, box: int):
    """该格正在进行的订单（还没落定），没有则 None。"""
    return c.execute(
        "SELECT * FROM orders WHERE box=? AND status IN (?,?) ORDER BY id DESC LIMIT 1",
        (box, PENDING, OPENED)).fetchone()


def occupant(c, box: int):
    """该格当前被谁占着：取最后一条 done 的订单，且它必须是 store。

    必须连 action 一起看 —— 只看 phone 的话，"A 存入 → A 取走"之后
    最近一条 done 仍然带 phone=A，会被误判成 A 还占着。
    """
    row = c.execute(
        "SELECT * FROM orders WHERE box=? AND status=? ORDER BY id DESC LIMIT 1",
        (box, DONE)).fetchone()
    return row if (row is not None and row["action"] == "store") else None


def box_of_code(c, code: str):
    """凭取件码找出东西在哪一格；没有则返回 None。

    扫柜体**总码**取件时页面没有格号，只能凭码自己找 —— 这正是取件码要全局唯一的
    原因（见 gen_pickup_code：一个码用过就不再复用）。

    不能直接 `SELECT box FROM orders WHERE pickup_code=?` ——"东西还在不在"只有
    occupant() 一个口径。直接查的话，一个早就取走的存件单还会命中，于是给用户
    开一次空柜门。宁可多查几次 occupant()，也不要第二个口径。
    """
    for n in range(1, BOX_COUNT + 1):
        occ = occupant(c, n)
        if occ is not None and occ["pickup_code"] == code:
            return n
    return None


class BoxUnavailable(Exception):
    """选不到格 / 格被占 / 设备离线 —— 提前失败，由调用方在**事务外**审计。

    做成异常而不是返回值，是为了让 create_order 全程待在 tx() 里：
    一旦在里面 audit() 就会开第二条连接，在持写锁期间等 busy_timeout，自死锁。
    """

    def __init__(self, msg: str, status: int = 409):
        super().__init__(msg)
        self.msg = msg
        self.status = status


# 一次成功的占格：订单号、落在哪一格、发给用户的取件码（取件单没有码）
NewOrder = namedtuple("NewOrder", "id box code")


def gen_pickup_code(c) -> str:
    """生成一个没被任何存件单用过的 6 位取件码。

    ⚠️ 必须在持有 BEGIN IMMEDIATE 的事务里调用 —— 查和插之间有窗口的话，
    两个并发存件会生成同一个码，而取件是"凭码跨格找"，重码就成了真歧义。
    查全表而不是只查还占着格的：一个码用过了就不再复用，规则最简单。
    """
    while True:
        code = f"{random.randint(0, 999999):06d}"
        row = c.execute(
            "SELECT 1 FROM orders WHERE pickup_code=? AND action='store' LIMIT 1",
            (code,)).fetchone()
        if row is None:
            return code


def pick_free_box(c, online=None) -> int:
    """挑一个空闲柜格：既没有进行中的单，也没有还放在里面的东西。

    必须在 tx() 里调用 —— "挑格"和"占格"之间留窗口的话，两个并发存件会挑到同一格。
    online 是**开事务之前**取的在线格快照：device_online() 要拿 boxes_lock，
    持着 DB 写锁再去等进程锁会把锁序搞乱。
    """
    free = [n for n in range(1, BOX_COUNT + 1)
            if active_order(c, n) is None and occupant(c, n) is None]
    if not free:
        raise BoxUnavailable("没有空闲柜格，请稍后再试")
    if online is not None:
        live = [n for n in free if n in online]
        if not live:
            # 有空格但设备不在线。不硬选 —— 真机只接了 1 号格时，
            # 2 号格在 DB 上写着空，选了它也只会让固件开错门。
            raise BoxUnavailable("设备离线，暂时无法存件", 503)
        free = live
    # 取号最小的那个：一是"从前往后填"符合直觉，二是**真机只接了 1 号格** ——
    # 优先用 1 号格，演示时动的就是那个真有舵机、真有门磁的格子。
    # 想让活摊开（比如挑占用次数最少的那格），改这一行就够了。
    return min(free)


def create_order(box, action: str, actor: str, store_id=None, online=None) -> NewOrder:
    """原子地"确认这一格能用 + 插入 pending 订单"，返回 NewOrder。

    检查和插入必须在同一个 tx() 里：否则两个并发请求会都读到空闲，各发一条
    开门命令（TOCTOU）。box 传 None 表示自动挑一个空闲格。
    store_id 只有取件单才带：指回它的存件单，是后面算费用时唯一可靠的时间起点。
    """
    with tx() as c:
        if box is None:
            box = pick_free_box(c, online)
        elif active_order(c, box) is not None:
            raise BoxUnavailable("该格有正在进行的操作，请稍候")

        occ = occupant(c, box)
        if action == "store":
            # 这道检查原来在 api_open 里、用的是另一条连接，等于没锁上
            if occ is not None:
                raise BoxUnavailable("柜格里还有东西，请先取走")
        else:
            # 读到写之间东西可能已经被别的码取走了，这里再确认一次
            if occ is None or occ["id"] != store_id:
                raise BoxUnavailable("物品已不在格内")

        code = gen_pickup_code(c) if action == "store" else None
        cur = c.execute(
            "INSERT INTO orders (box, phone, action, status, pickup_code, store_id, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (box, actor, action, PENDING, code, store_id, now_str()),
        )
        return NewOrder(cur.lastrowid, box, code)


def publish_open(box, action: str, actor: str, source: str, store_id=None, online=None):
    """下发开门命令。必须先落库再 publish —— 设备可能毫秒级回 door_opened，
    若此时订单还没写，事件线程就找不到它是谁的。

    返回 (NewOrder, None, 200) 或 (None, 错误信息, 状态码)。
    """
    try:
        order = create_order(box, action, actor, store_id, online)
    except BoxUnavailable as e:
        audit(source, actor, box, action, "deny", e.msg)
        return None, e.msg, e.status

    payload = {"cmd": "open", "box": order.box, "action": action,
               "order": order.id, "ts": int(time.time())}
    info = mqtt_client.publish(TOPIC_CMD, json.dumps(payload))
    if info.rc != mqtt.MQTT_ERR_SUCCESS:
        # broker 没连上，消息已经丢了，别让订单干等到超时
        with conn() as c:
            c.execute("UPDATE orders SET status=? WHERE id=?", (FAILED, order.id))
        audit(source, actor, order.box, action, "deny", f"MQTT 下发失败 rc={info.rc}")
        return None, "设备通信不可用，请稍后重试", 503

    audit(source, actor, order.box, action, "ok", f"已下发开门命令 order#{order.id}")
    print(f"[cmd] -> {TOPIC_CMD} {payload}")
    return order, None, 200


def auth_result(result: str, box: int, action=None, reason=None):
    payload = {"result": result, "box": box}
    if action:
        payload["action"] = action
    if reason:
        payload["reason"] = reason
    mqtt_client.publish(TOPIC_AUTH_RESULT, json.dumps(payload))


# ---------------------------------- MQTT ----------------------------------
mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="locker-backend")


def device_online(box: int) -> bool:
    with boxes_lock:
        seen = boxes[box]["last_seen"]
    return seen > 0 and (time.time() - seen) < OFFLINE_AFTER


# ------------------------------- 真机格对账 -------------------------------
# 为什么要有这一段：后端的 occupant() 只认 orders 表，设备的 state 只活在内存里，
# 两边一旦分家就**互相堵死** —— 后端说"格里还有东西"不让存、固件说"这格是空的"
# 不让取（nothing_to_pickup），谁也解不开，只能人去改库。2026-10-16 就是这么卡的。
# 对账的作用不是"从源头不产生分歧"（那是 door_closed 带 state 干的事），
# 而是**兜住所有已经分家的局面，让系统自己爬出来**。
def check_reconcile(box: int, item):
    """拿秤的读数和 DB 里的占用记录对账，连续矛盾够久就把脏的那条判失败。

    只在真机格上做（调用方保证）—— 别的格的 item 是固件编的，没有对账价值。
    """
    now = time.time()
    # 掉线时读数不动了，不能拿旧读数判案；正在操作的一格也不能动，
    # 免得把一单正在进行中的订单判死。
    if item is None or not device_online(box):
        reconcile.update(kind=None, since=0.0, fired=False)
        return
    with conn() as c:
        if active_order(c, box) is not None:
            reconcile.update(kind=None, since=0.0, fired=False)
            return
        occ = occupant(c, box)

    if occ is not None and not item:
        kind = "ghost"          # DB 说里面有东西，秤说没有 -> 那条存件单是假的
    elif occ is None and item:
        kind = "unregistered"   # 秤说有东西，DB 里没记录 -> 有东西没登记
    else:
        reconcile.update(kind=None, since=0.0, fired=False)
        return

    if reconcile["kind"] != kind:
        reconcile.update(kind=kind, since=now, fired=False)
        return
    if reconcile["fired"] or now - reconcile["since"] < RECONCILE_AFTER:
        return
    reconcile["fired"] = True

    if kind == "ghost":
        drop_ghost_order(box, occ)
    else:
        audit("system", "reconcile", box, "unregistered_item", "deny",
              f"秤连续 {RECONCILE_AFTER}s 报格内有物，但 DB 里没有对应的存件单 —— "
              f"东西进柜没记上账，需要人工核对")


def drop_ghost_order(box: int, occ):
    """把一条"DB 说有物、秤严格说没有"的存件单判失败 —— 这就是 1 号格卡死的解药。

    occupant() 只认 done 的存件单，把它改成 failed，格子立刻恢复空闲，
    存和取两边同时解开。带 status=DONE 条件，改不到就报 0 行，不会误伤别的单。

    UPDATE 在 tx() 里，audit() 必须在 tx() 之外 —— audit() 会另开一条连接，
    握着写锁再调它就是自己等自己（同 settle_pickup 的注意事项）。
    """
    with tx() as c:
        n = c.execute("UPDATE orders SET status=? WHERE id=? AND status=?",
                      (FAILED, occ["id"], DONE)).rowcount
    if n:
        audit("system", "reconcile", box, "drop_ghost_order", "deny",
              f"order#{occ['id']} {DONE}->{FAILED}；秤连续 {RECONCILE_AFTER}s 报格内无物，"
              f"判为假存件，柜格已解锁")
        print(f"[reconcile] {box} 号格：订单 #{occ['id']} 判为假存件 -> {FAILED}，柜格解锁")


def on_connect(client, userdata, flags, reason_code, properties=None):
    print(f"[mqtt] 已连接 {MQTT_HOST}:{MQTT_PORT} ({reason_code})")
    client.subscribe([(TOPIC_STATUS, 0), (TOPIC_EVENT, 0)])


def on_message(client, userdata, msg):
    try:
        data = json.loads(msg.payload.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        print(f"[mqtt] 报文解析失败 {msg.topic}: {e}")
        return

    box = data.get("box")

    if msg.topic == TOPIC_STATUS:
        if box in boxes:
            with boxes_lock:
                boxes[box].update(data)
                # 在线只看心跳。last_seen 存服务端时刻，而且必须放在 update 之后覆盖，
                # 否则会被报文里那个语义不同的 ts 顶掉（固件发开机秒数，模拟器发 Unix 秒）。
                # 事件消息故意不刷新它 —— 否则一条 card_scan 就能让已死的设备"续命"。
                boxes[box]["last_seen"] = time.time()
            # 对账放在 boxes_lock **之外**：device_online() 要拿 boxes_lock、
            # occupant() 要开 DB 连接，持着这把锁再去做这两件事会把锁序搞乱
            # （同 pick_free_box 那条"在线快照必须在开事务之前取"的理由）。
            if box == REAL_BOX:
                check_reconcile(box, data.get("item"))

    elif msg.topic == TOPIC_EVENT:
        if data.get("event") == "card_scan":
            # 不带格号：RC522 是柜体级的，读到的只是一张卡，"刷哪一格"由后端算
            handle_card_scan(data.get("uid", ""))
        else:
            handle_device_event(data)


def door_close_verdict(box: int, verdict):
    """门关上之后，这一单到底成没成？返回 (是否成功, 说明)。

    verdict 是固件随 door_closed 一起发来的格内状态（"OCCUPIED"/"IDLE"），
    是**固件自己按秤算出来的结论** —— 后端直接采信它，两边保持一个口径。
    没有这个字段 = 老固件（或 device_sim 的某些路径），退回老行为：门关了就算成功。

    ⚠️ 这里只解决"门关了但动手没动成"这一类假成功。判断本身准不准取决于固件
    那次采样，秤的余量并不宽（见 README「物品检测」一节）。
    """
    if verdict is None:
        return True, ""
    with conn() as c:
        row = active_order(c, box)
    if row is None or row["status"] != OPENED:
        # 没有"开着"的单子（迟到回执之类），交给 advance() 自己的 CAS 去判
        return True, ""
    if row["action"] == "store":
        return (verdict == "OCCUPIED",
                f"门关了但秤报格内为空 —— 东西没放进去，存件单 #{row['id']} 不算数")
    return (verdict == "IDLE",
            f"门关了但秤报格内还有东西 —— 东西没拿走，取件单 #{row['id']} 不算数")


def handle_device_event(data):
    """把门磁回执落到订单上 —— 决策点⑤ 就是这一段。"""
    box = data.get("box")
    event = data.get("event")

    # 格号无效就整条丢掉。原来这里是 `data.get("box", 1)` —— 单格时看不出区别，
    # 多格之后等于"3 号格的门开了、记到 1 号格头上"：两格的订单一起错，
    # 而审计表上还挺好看。宁可丢一条回执让它超时失败，也不要记到别人头上。
    if box not in boxes:
        audit("device", "device", None, event or "event", "deny",
              f"报文里的 box 不是有效柜格：{json.dumps(data, ensure_ascii=False)}")
        return

    if event == "door_opened":
        oid = advance(box, PENDING, OPENED)
        audit("device", "device", box, "door_opened", "ok",
              f"order#{oid} {PENDING}->{OPENED}" if oid else "迟到回执，无对应订单")

    elif event == "door_closed":
        # 先问秤：门关了，但这一轮到底动没动成？
        ok, why = door_close_verdict(box, data.get("state"))
        if not ok:
            oid = advance(box, OPENED, FAILED)
            audit("device", "device", box, "door_closed", "deny",
                  (f"order#{oid} {OPENED}->{FAILED}；{why}" if oid
                   else f"{why}（无对应订单，迟到回执）"))
            print(f"[door_closed] box={box} {why}")
        else:
            oid = advance(box, OPENED, DONE, closed=True)
            # 取件的收费就挂在"门关上"这一刻 —— 状态变成 done 之后、在 tx 之外结算。
            # settle_pickup 对存件单会直接返回 None，所以这里不用先判断 action。
            charged = settle_pickup(oid) if oid else None
            detail = f"order#{oid} {OPENED}->{DONE}" if oid else "迟到回执，无对应订单"
            if charged:
                detail += f"；计费 {charged['fee']} 分，余额 {charged['balance']} 分"
            audit("device", "device", box, "door_closed", "ok", detail)
            if charged:
                audit("system", "billing", box, "charge", "ok",
                      f"order#{oid} 扣 {charged['fee']} 分（{charged['phone']}），"
                      f"余额 {charged['balance']} 分")

    elif event == "error":
        reason = data.get("reason", "")
        src = OPENED
        oid = advance(box, OPENED, FAILED)
        if oid is None:
            src = PENDING
            oid = advance(box, PENDING, FAILED)
        audit("device", "device", box, f"error/{reason}", "deny",
              f"order#{oid} {src}->{FAILED}" if oid else "无对应订单")

    else:
        audit("system", "device", box, event or "event", "ok",
              json.dumps(data, ensure_ascii=False))


def handle_card_scan(uid: str):
    """刷卡开柜。和扫码走同一套授权，但**格是后端算出来的，不是报文里带的**。

    柜体上的 RC522 是所有格公用的：它读到的只是一张卡，卡不会说"我在 3 号格前"。
    所以"刷哪一格"由后端定 —— 这张卡的主人占着哪一格就取哪一格；哪一格都没占，
    就是存件，格从在线的空格里挑（和扫总码那条路完全一样，见 pick_free_box）。

    顺带一个好处：原来那条"卡主不是占用者"的拒绝分支整条消失了 —— 格是按
    卡主算出来的，就不可能算出别人占着的格。
    """
    with conn() as c:
        user = c.execute("SELECT * FROM users WHERE card_uid=?", (uid,)).fetchone()
        # 卡主占着哪一格。判"占着"用 occupant()，和别处同一个口径。
        box = occ = None
        if user is not None:
            for n in range(1, BOX_COUNT + 1):
                o = occupant(c, n)
                if o is not None and o["phone"] == user["phone"]:
                    box, occ = n, o
                    break

    if user is None:
        # 没绑过的卡：如果页面上正好有人点了「绑定校园卡」，这张卡就绑给他。
        # 反过来，已经绑给别人的卡走不到这里（user 查得到），也就偷不走。
        owner = take_bind_wait()
        if owner:
            with conn() as c:
                c.execute("UPDATE users SET card_uid=? WHERE phone=?", (uid, owner))
            audit("card", owner, None, "bind_card", "ok", f"卡 {uid} 已绑定到 {owner}")
            auth_result("ok", None, reason="card_bound")
            print(f"[bind] 卡 {uid} -> {owner}")
            return
        audit("card", uid, None, "card_scan", "deny", "卡未绑定")
        auth_result("deny", None, reason="not_bound")
        return

    action = "pickup" if box is not None else "store"

    # 欠费冻结：东西可以取走（取件那条路不拦），但不能再往里存
    if action == "store" and (user["balance_fen"] or 0) < 0:
        audit("card", uid, box, "card_scan", "deny",
              f"欠费 {user['balance_fen']} 分，拒绝存件")
        auth_result("deny", box, action="store", reason="balance")
        return

    # 在线格快照，开事务之前取（理由见 pick_free_box）
    online = {n for n in boxes if device_online(n)}
    if not online:
        audit("card", uid, box, "card_scan", "deny", "设备离线")
        auth_result("deny", box, reason="device_offline")
        return
    if box is not None and box not in online:
        audit("card", uid, box, "card_scan", "deny", "设备离线")
        auth_result("deny", box, reason="device_offline")
        return

    if action == "store":
        # 预检一次"还有没有空格"：真正挑格仍然在 create_order 的事务里。
        # 这一步只是为了别先说"已授权，请放入物品"、然后门一声不响 ——
        # 屏幕上那句谎话比多查一次库难看得多。
        with conn() as c:
            try:
                pick_free_box(c, online)
            except BoxUnavailable as e:
                audit("card", uid, None, "store", "deny", e.msg)
                auth_result("deny", None, action="store", reason="full")
                return

    # 刷卡存件也要发码（现在由 create_order 统一生成）：不然监控台的取件码列
    # 空着，而且东西只能再用这张卡取（朋友拿码取不走）。取件时不需要码
    # —— 卡本身就是凭证。
    # 多格之后 publish_open 可能挑到别的格（box 传 None 时），所以"存进了哪一格"
    # 是它算出来的：这一句要在 publish_open **之前**报 —— 授权结果告诉人"允许了"，
    # 门磁回执随后才证明门真开了。
    auth_result("ok", box, action=action)
    publish_open(box, action, user["phone"], "card",
                 store_id=occ["id"] if action == "pickup" else None, online=online)


# ---------------------------------- 超时兜底 ----------------------------------
def sweeper():
    """干等着的订单没人来推进就会被永远卡住：pending 等不到 door_opened、
    opened 等不到 door_closed。这个线程负责把它们判失败。

    门开着不关这一路，优先靠设备自己发的 error/door_left_open 事件（固件 30s 触发），
    这里只是兜底。
    """
    while True:
        time.sleep(2)
        try:
            with conn() as c:
                rows = c.execute(
                    "SELECT id, box, action, status, created_at FROM orders"
                    " WHERE status IN (?,?)", (PENDING, OPENED)).fetchall()
            for r in rows:
                age = time.time() - datetime.strptime(
                    r["created_at"], "%Y-%m-%d %H:%M:%S").timestamp()
                limit = OPEN_ACK_TIMEOUT if r["status"] == PENDING else OPEN_CLOSE_TIMEOUT
                if age <= limit:
                    continue
                oid = advance(r["box"], r["status"], FAILED)
                if oid:
                    audit("system", "sweeper", r["box"], r["action"], "deny",
                          f"order#{oid} {r['status']}->{FAILED}（{int(age)}s 无回执）")

            # 补账：进程若在"改成 done"和"扣费"之间被杀，这条取件单就白取走了。
            # 判据是"done 了但流水里没有它那一笔" —— 所以 settle_pickup 连 0 元
            # 也要落一条流水，否则免费的单会被这里反复重算。
            # 子查询要显式滤掉 NULL：NOT IN 里只要有一个 NULL，整张表都会被判为假。
            with conn() as c:
                owed = c.execute(
                    "SELECT id FROM orders WHERE action='pickup' AND status=?"
                    " AND store_id IS NOT NULL"
                    " AND id NOT IN (SELECT order_id FROM ledger"
                    "                WHERE kind='charge' AND order_id IS NOT NULL)",
                    (DONE,)).fetchall()
            for r in owed:
                info = settle_pickup(r["id"])
                if info:
                    audit("system", "sweeper", None, "charge", "ok",
                          f"补扣 order#{r['id']} {info['fee']} 分（{info['phone']}），"
                          f"余额 {info['balance']} 分")
        except Exception as e:                      # 兜底线程绝不能死
            print(f"[sweeper] {e}")


# ---------------------------------- 路由 ----------------------------------
@app.route("/")
def page_monitor():
    if (deny := admin_denied(html=True)):
        return deny
    return render_template("index.html", device=DEVICE_ID, box_count=BOX_COUNT,
                           pricing=PRICING, human=human_secs)


@app.route("/scan")
def page_scan():
    """微信扫一扫打开的页面。

    带 `?box=N` 是扫**柜格贴**的码：锁定到那一格，存件就进那一格。
    不带 box 是扫**柜体总码**：存件时格由后端挑，取件时凭码自己找格。

    格号非法一律 404，不再回落到 1 号格 —— 贴纸旧了/格号写错了，就得当场看出来。
    回落到 1 号格意味着"扫 3 号格的码、1 号格的门开了"，而用户完全看不出来
    （页面上写着 3 号），东西放进去就和别人的混了。
    """
    raw = request.args.get("box")
    box = None
    if raw is not None:
        try:
            box = int(raw)
        except ValueError:
            abort(404)
        if box not in boxes:
            abort(404)
    return render_template("scan.html", box=box, pricing=PRICING, human=human_secs)


@app.route("/poster")
def page_posters():
    """柜贴索引：总码一张 + 每格一张。这是打印柜贴的入口。"""
    return render_template("posters.html", boxes=sorted(boxes))


@app.route("/poster/<int:box>")
def page_poster(box):
    """单格柜贴：打印出来贴到对应柜门上的那张二维码。

    跟监控台分开两页是有意的 —— 监控台是管理员盯状态用的，柜贴是要贴在
    柜子上的，使用者、使用时机都不同，而且混在监控台里没法直接打印。
    不查口令：柜贴上就是二维码和柜格号，没有任何敏感信息。
    """
    if box not in boxes:
        abort(404)
    return render_template("poster.html", box=box,
                           url=f"http://{lan_ip()}:{HTTP_PORT}/scan?box={box}")


@app.route("/poster/cabinet")
def page_poster_cabinet():
    """柜体总码：只有一张，贴在柜体上（不是某一格的门上）。

    扫它不锁定格号 —— 存件时后端挑一个空格，取件时凭码找格。
    """
    return render_template("poster.html", box=None,
                           url=f"http://{lan_ip()}:{HTTP_PORT}/scan")


def _qr(url: str):
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="png", scale=8, border=2)
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@app.route("/qr/<int:box>.png")
def qr_png(box):
    if box not in boxes:
        abort(404)
    return _qr(f"http://{lan_ip()}:{HTTP_PORT}/scan?box={box}")


@app.route("/qr/cabinet.png")
def qr_cabinet_png():
    return _qr(f"http://{lan_ip()}:{HTTP_PORT}/scan")


@app.route("/api/boxes")
def api_boxes():
    now = time.time()
    with boxes_lock:
        out = []
        for b in boxes.values():
            d = {k: v for k, v in b.items() if k != "last_seen"}
            # 在线是现算的：心跳停了就自动显示离线，不用额外线程
            d["online"] = b["last_seen"] > 0 and (now - b["last_seen"]) < OFFLINE_AFTER
            out.append(d)
    return jsonify(out)


@app.route("/api/orders")
def api_orders():
    if (deny := admin_denied()):
        return deny
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM orders ORDER BY id DESC LIMIT 20").fetchall()
        # 在连接还开着的时候算费用（order_fee 要再查 ledger 和占用者）
        out = []
        for r in rows:
            d = dict(r)
            d["fee_fen"], d["fee_live"] = order_fee(c, r)
            out.append(d)
    return jsonify(out)


@app.route("/api/order/<int:oid>")
def api_order(oid):
    """单查一条订单的状态，给扫码页盯自己那一单用。

    故意只回非敏感字段：扫码页是所有人都能打开的，不能让它顺手把
    别人的手机号和取件码捞走 —— 那等于把取件码授权整个绕过去。
    费用可以给：它只说明"这一单花了多少钱"，泄漏不了谁存的、还剩多少钱。
    """
    with conn() as c:
        row = c.execute(
            "SELECT id, box, action, status, closed_at FROM orders WHERE id=?",
            (oid,)).fetchone()
        if row is None:
            return jsonify({"ok": False, "msg": "没有这条订单"}), 404
        fee, _ = order_fee(c, row)
    return jsonify({**dict(row), "fee_fen": fee})


@app.route("/api/logs")
def api_logs():
    if (deny := admin_denied()):
        return deny
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM audit ORDER BY id DESC LIMIT 50").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/register", methods=["POST"])
def api_register():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    phone = (body.get("phone") or "").strip()

    if not name:
        return jsonify({"ok": False, "msg": "请填姓名"}), 400
    if len(name) > 20:
        return jsonify({"ok": False, "msg": "姓名太长了"}), 400
    if not PHONE_RE.match(phone):
        return jsonify({"ok": False, "msg": "请输入正确的 11 位手机号"}), 400

    try:
        with conn() as c:
            c.execute("INSERT INTO users (name, phone) VALUES (?,?)", (name, phone))
    except sqlite3.IntegrityError:
        # phone 上有 UNIQUE 约束，撞了就是已经注册过
        return jsonify({"ok": False, "msg": "该手机号已经注册过了"}), 409

    audit("wechat", phone, None, "register", "ok", f"新用户 {name}")
    return jsonify({"ok": True, "msg": f"{name}，注册成功", "phone": phone})


@app.route("/api/user")
def api_user():
    """给页面看这个手机号注册了没、绑了哪张卡。"""
    phone = (request.args.get("phone") or "").strip()
    with conn() as c:
        row = c.execute(
            "SELECT name, phone, card_uid, balance_fen FROM users WHERE phone=?",
            (phone,)).fetchone()
    if row is None:
        return jsonify({"registered": False})
    return jsonify({"registered": True, **dict(row)})


@app.route("/api/recharge", methods=["POST"])
def api_recharge():
    """管理员代充。监控台用 —— 学生欠费了但人不在，或者要补偿一笔。

    学生自己充值的入口是 `/api/pay`。两个都走 `recharge()`，
    只是流水上的 note 不一样，好区分钱是怎么进来的。
    """
    if (deny := admin_denied()):
        return deny
    body = request.get_json(silent=True) or {}
    phone = (body.get("phone") or "").strip()
    amount_fen, err = parse_amount_fen(body)
    if err:
        return jsonify({"ok": False, "msg": err}), 400

    try:
        balance = recharge(phone, amount_fen, note="管理员充值")
    except ValueError as e:
        return jsonify({"ok": False, "msg": str(e)}), 400

    audit("system", "admin", None, "recharge", "ok",
          f"{phone} 充值 {amount_fen} 分，余额 {balance} 分")
    return jsonify({"ok": True, "balance_fen": balance})


@app.route("/api/pay", methods=["POST"])
def api_pay():
    """用户自助充值 —— **模拟支付**，没有接任何真实支付通道。

    ⚠️ 这里**没有任何身份校验**：知道手机号就能给它充值。扫码页本来就是
    谁都能打开、谁都能填任意手机号的（`/api/open` 也只认手机号），
    所以这一版并没有比原来更弱。但真实上线必须换成支付平台的异步回调
    + 签名校验，绝不能长这样 —— 钱是从客户端说充多少就充多少的。

    课设里这样做的价值在于：**欠费的用户能在手机上自己走出来**，
    不用去找管理员。不然"欠费冻结"就成了一个死循环。
    """
    body = request.get_json(silent=True) or {}
    phone = (body.get("phone") or "").strip()
    if not PHONE_RE.match(phone):
        return jsonify({"ok": False, "msg": "请输入正确的 11 位手机号"}), 400
    amount_fen, err = parse_amount_fen(body)
    if err:
        return jsonify({"ok": False, "msg": err}), 400
    if amount_fen > PAY_MAX_FEN:
        return jsonify({"ok": False, "msg": f"单次最多充 ¥{PAY_MAX_FEN // 100}"}), 400

    try:
        balance = recharge(phone, amount_fen, note="模拟支付")
    except ValueError as e:
        return jsonify({"ok": False, "msg": str(e)}), 400

    audit("wechat", phone, None, "recharge", "ok",
          f"模拟支付 {amount_fen} 分，余额 {balance} 分")
    return jsonify({"ok": True, "balance_fen": balance})


@app.route("/api/ledger")
def api_ledger():
    """钱包流水，监控台用。手机号可选，不传就回所有人的最近 30 条。"""
    if (deny := admin_denied()):
        return deny
    phone = (request.args.get("phone") or "").strip()
    with conn() as c:
        if phone:
            rows = c.execute(
                "SELECT * FROM ledger WHERE phone=? ORDER BY id DESC LIMIT 30",
                (phone,)).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM ledger ORDER BY id DESC LIMIT 30").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/stats")
def api_stats():
    """运营看板那几个数。全是只读聚合，没有"读-改-写"，所以用 conn() 就够。

    "今天"按 `substr(时间串, 1, 10)` 比。时间串是 now_str() 写的
    'YYYY-MM-DD HH:MM:SS'，直接截前 10 位当日期最省事，也不用赌 SQLite 的
    date() 认不认这个格式（认，但那是个隐含约定；写 substr 就没这层顾虑）。

    ⚠️ 平均寄存时长取的是"**取件单关门时刻 - 存件单关门时刻**"，不是 created_at：
    计费用的就是这两个时刻（见 elapsed_seconds），看板必须和计费同一个口径，
    否则页面上"平均 3 分钟"和"这一单扣了 ¥3"会互相打架。
    """
    if (deny := admin_denied()):
        return deny
    today = now_str()[:10]
    with conn() as c:
        revenue = c.execute(
            "SELECT COALESCE(-SUM(amount_fen), 0) FROM ledger"
            " WHERE kind='charge' AND substr(ts,1,10)=?", (today,)).fetchone()[0]
        stores = c.execute(
            "SELECT COUNT(*) FROM orders"
            " WHERE action='store' AND substr(created_at,1,10)=?", (today,)).fetchone()[0]
        # 封顶单：扣到了单次上限的那些。它们说明"有人放了一整天"，
        # 是调定价时最该看的数 —— 封顶越多，越说明这个价定得偏低。
        capped = c.execute(
            "SELECT COUNT(*) FROM ledger WHERE kind='charge' AND amount_fen<=?"
            " AND substr(ts,1,10)=?", (-MAX_FEE_FEN, today)).fetchone()[0]
        # 当前在柜数用 occupant() 数，不另写一条 SQL —— "东西还在里面"只有那一个口径
        in_use = sum(1 for n in boxes if occupant(c, n) is not None)
        spans = c.execute(
            "SELECT p.closed_at AS p_at, s.closed_at AS s_at FROM orders p"
            "  JOIN orders s ON s.id = p.store_id"
            " WHERE p.action='pickup' AND p.status=? AND substr(p.closed_at,1,10)=?",
            (DONE, today)).fetchall()

    secs = []
    for r in spans:
        try:
            a = datetime.strptime(r["s_at"], "%Y-%m-%d %H:%M:%S")
            b = datetime.strptime(r["p_at"], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue          # 老库里的 closed_at 可能是空的，跳过就好
        secs.append(max(0, int((b - a).total_seconds())))

    return jsonify({
        "date": today,
        "revenue_fen": revenue,                          # 今日营收（分）
        "stores": stores,                                # 今日寄存次数
        "in_use": in_use,                                # 当前在柜数
        "box_count": BOX_COUNT,
        "avg_seconds": (sum(secs) // len(secs)) if secs else None,   # 平均寄存时长
        "settled": len(secs),                            # 参与平均的样本数
        "capped": capped,                                # 今日封顶单数
    })


@app.route("/api/bind/start", methods=["POST"])
def api_bind_start():
    """页面点「绑定校园卡」：先认领手机号，再让用户刷卡完成配对。"""
    body = request.get_json(silent=True) or {}
    phone = (body.get("phone") or "").strip()
    if not PHONE_RE.match(phone):
        return jsonify({"ok": False, "msg": "请输入正确的 11 位手机号"}), 400

    with conn() as c:
        row = c.execute("SELECT name, card_uid FROM users WHERE phone=?",
                        (phone,)).fetchone()
    if row is None:
        return jsonify({"ok": False, "msg": "该手机号还没注册"}), 403
    if row["card_uid"]:
        # 换卡得先把旧卡解绑，课设不做这个入口，走数据库
        return jsonify({"ok": False, "msg": f"已经绑了 {row['card_uid']}，换卡请联系管理员"}), 409

    with bind_lock:
        bind_wait["phone"] = phone
        bind_wait["until"] = time.time() + BIND_WINDOW

    audit("wechat", phone, None, "bind_start", "ok", f"{BIND_WINDOW}s 内等刷卡")
    return jsonify({"ok": True, "msg": f"请在 {BIND_WINDOW} 秒内刷一次卡",
                    "window": BIND_WINDOW})


@app.route("/api/open", methods=["POST"])
def api_open():
    body = request.get_json(silent=True) or {}
    # 不传 box = 扫的是柜体上的**总码**，哪一格由后端挑（见 pick_free_box）。
    # 传了就必须是个真存在的格号，不再静默回落 —— 用户扫 3 号格却开了 1 号格的门，
    # 他放进去的东西会躺在别人的格里，事后谁都说不清。
    box = body.get("box")
    if box is not None:
        try:
            box = int(box)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "msg": "box 参数不对"}), 400
        if box not in boxes:
            return jsonify({"ok": False, "msg": "没有这个柜格"}), 400

    action = body.get("action", "store")
    phone = (body.get("phone") or "").strip()
    code = (body.get("code") or "").strip()

    if action not in ("store", "pickup"):
        return jsonify({"ok": False, "msg": "action 只能是 store / pickup"}), 400

    # 在线格快照，在**开事务之前**取：device_online() 要拿 boxes_lock，
    # 而 create_order 在事务里握着 DB 写锁 —— 持写锁再等进程锁是锁序倒置。
    online = {n for n in boxes if device_online(n)}
    if box is not None and box not in online:
        return jsonify({"ok": False, "msg": "设备未连接，请稍后再试"}), 503

    if action == "store":
        if not phone:
            return jsonify({"ok": False, "msg": "请先填手机号"}), 400
        with conn() as c:
            user = c.execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()
        if user is None:
            audit("wechat", phone, box, "store", "deny", "手机号未注册")
            return jsonify({"ok": False, "msg": "该手机号未注册，无法存件"}), 403
        if (user["balance_fen"] or 0) < 0:
            # 欠费只冻结"存"，不冻结"取" —— 否则东西被扣在柜里，比欠费更糟
            audit("wechat", phone, box, "store", "deny",
                  f"欠费 {user['balance_fen']} 分")
            return jsonify({"ok": False, "msg": "账户欠费，请在「我的」页充值后再存件"}), 402

        # "格子里有没有东西"和"取件码取哪个"都收在 create_order 的那个事务里做。
        # 不拦的话命令会发出去、设备拒绝开锁，用户先看到"柜门已打开"、
        # 订单随后才变 failed —— 体验很怪。
        # 带 online 进去：自动挑格时只挑**在线**的空格，不然真机只接了 1 号格，
        # 后端会把 2 号格分出去，命令发出去没人接，用户干等到超时。
        res, err, status = publish_open(box, "store", phone, "wechat", online=online)
        if res is None:
            return jsonify({"ok": False, "msg": err}), status
        # 措辞是"正在开门"而不是"已打开"：命令刚发出去，门磁回执还没回来，
        # 这时说"已打开"就是在替设备撒谎（设备可能根本没开）。
        # 回 box 是给"柜体总码"那条路用的：格号是后端挑的，页面得知道挑中了哪个。
        return jsonify({"ok": True, "msg": "正在开门，请放入物品后关好门",
                        "order": res.id, "box": res.box, "code": res.code})

    # ---- 取件：凭取件码，不看是谁来取 ----
    with conn() as c:
        if box is None:
            # 扫总码时页面上没有格号，凭码自己找格 —— 这正是"码"存在的意义：
            # 用户不需要知道东西在哪一格，记个码就行。
            box = box_of_code(c, code)
            if box is None:
                # 没有这个码，或者它那一单东西早取走了。两种情况都回 403 同一句话：
                # 分开说等于告诉别人哪些码是真的（存件码只有 6 位，本来就该少给信息）。
                audit("wechat", phone or "-", None, "pickup", "deny", "没有柜格放着这个码")
                return jsonify({"ok": False, "msg": "取件码不对"}), 403
        occ = occupant(c, box)

    if occ is None:
        audit("wechat", phone or "-", box, "pickup", "deny", "柜格是空的")
        return jsonify({"ok": False, "msg": "柜格里没有东西"}), 409
    if code != occ["pickup_code"]:
        audit("wechat", phone or "-", box, "pickup", "deny", "取件码不对")
        return jsonify({"ok": False, "msg": "取件码不对"}), 403
    # 总码那条路上格号是刚解出来的，上面那道在线检查漏了它，这里补上
    if box not in online:
        return jsonify({"ok": False, "msg": "设备未连接，请稍后再试"}), 503

    # 带上 occ["id"]：这条取件单得知道它取的是哪一单存件，才可能算钱
    res, err, status = publish_open(box, "pickup", occ["phone"], "wechat",
                                    store_id=occ["id"], online=online)
    if res is None:
        return jsonify({"ok": False, "msg": err}), status
    return jsonify({"ok": True, "msg": "正在开门，请取出物品后关好门",
                    "order": res.id, "box": box})


def main():
    init_db()
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
    mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
    # connect_async：broker 还没起来也不影响本服务启动，paho 会自己重连
    mqtt_client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
    mqtt_client.loop_start()
    print(f"[mqtt] 正在连接 broker {MQTT_HOST}:{MQTT_PORT}（未启动会自动重试）")

    threading.Thread(target=sweeper, daemon=True).start()

    print(f"\n  监控页   http://{lan_ip()}:{HTTP_PORT}/?k={ADMIN_KEY}   ← 要口令")
    print(f"  扫码页   http://{lan_ip()}:{HTTP_PORT}/scan          ← 柜体总码（自动挑格）")
    print(f"           http://{lan_ip()}:{HTTP_PORT}/scan?box=1    ← 1 号柜格的码")
    print(f"  柜贴     http://{lan_ip()}:{HTTP_PORT}/poster        ← Ctrl+P 打印贴柜子上")
    print(f"  柜格数   {BOX_COUNT}"
          + ("（真机只有 1 号格接了硬件，其余格由固件虚拟，显示离线是正常）"
             if BOX_COUNT > 1 else "") + "\n")
    app.run(host="0.0.0.0", port=HTTP_PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
