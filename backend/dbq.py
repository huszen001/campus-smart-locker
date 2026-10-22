#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""locker.db 的只读查询小工具。

为什么有这个东西：Windows 不自带 sqlite3 命令行，Navicat for MySQL 又打不开 SQLite。
这里用 Python 标准库直接读，**以只读模式打开**，所以后端正在跑的时候也能查，不会抢锁。

用法：
    python dbq.py                 # 概览
    python dbq.py orders [N]      # 最近 N 条订单（默认 20）
    python dbq.py open            # 所有未结束的订单
    python dbq.py boxes           # 每个柜格的最新一单
    python dbq.py users           # 用户与绑卡（含余额）
    python dbq.py wallet [N]      # 钱包流水（默认 30）：充值 + 扣费
    python dbq.py audit [N]       # 最近 N 条审计（默认 30）
    python dbq.py sql "SELECT ..."  # 任意 SQL

本机注意：PATH 里的 python 是商店空壳，要用绝对路径，例如
    "/path/to/python.exe" dbq.py
"""

import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).with_name("locker.db")

# 控制台按 UTF-8 输出，避免中文在 Windows 上变乱码（省掉 PYTHONUTF8=1）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def query(sql, args=()):
    # mode=ro：只读打开，跟正在运行的后端互不干扰
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def show(rows, title=None):
    """把行按列宽对齐打印；空结果也明确说出来，别让人以为命令没跑。"""
    if title:
        print(f"\n=== {title} ===")
    if not rows:
        print("(无结果)")
        return
    cols = rows[0].keys()
    widths = [len(c) for c in cols]
    body = []
    for r in rows:
        # None 显示成空串，比打印 "None" 干净
        cells = ["" if r[c] is None else str(r[c]) for c in cols]
        body.append(cells)
        widths = [max(w, len(v)) for w, v in zip(widths, cells)]
    line = "  ".join(c.ljust(w) for c, w in zip(cols, widths))
    print(line)
    print("-" * len(line))
    for cells in body:
        print("  ".join(v.ljust(w) for v, w in zip(cells, widths)))


def counts():
    print(f"数据库：{DB}")
    print(f"大小：  {DB.stat().st_size} 字节\n")
    rows = []
    for (name,) in query("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        if name == "sqlite_sequence":
            continue
        n = query(f'SELECT COUNT(*) AS n FROM "{name}"')[0]["n"]
        rows.append({"表": name, "行数": n})
    show(rows)


def overview():
    counts()
    show(query("""
        SELECT id, box, action, status, phone, pickup_code, created_at
        FROM orders WHERE status IN ('pending','opened')
        ORDER BY id DESC
    """), "未结束的订单（pending / opened）")
    show(query("""
        SELECT ts, source, actor, box, action, result, detail
        FROM audit ORDER BY id DESC LIMIT 5
    """), "最近 5 条审计")
    show(query("""
        SELECT ts, phone, kind, amount_fen, balance_after_fen, note
        FROM ledger ORDER BY id DESC LIMIT 5
    """) if _has_ledger() else [], "最近 5 条钱包流水")
    print("\n提示：'python dbq.py open' 看全部未结束订单，"
          "'python dbq.py wallet' 看钱包流水，'python dbq.py sql \"...\"' 跑任意 SQL")


def _has_ledger():
    """老库在后端重启前还没有 ledger 表，overview 得能容忍这一点。"""
    return bool(query("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ledger'"))


def main():
    if not DB.exists():
        sys.exit(f"找不到 {DB} —— 后端从没启动过？")

    args = sys.argv[1:]
    cmd = args[0] if args else "overview"
    n = int(args[1]) if len(args) > 1 and args[1].isdigit() else None

    if cmd == "overview":
        overview()

    elif cmd == "orders":
        show(query("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (n or 20,)),
             f"最近 {n or 20} 条订单")

    elif cmd == "open":
        show(query("""
            SELECT id, box, action, status, phone, pickup_code, created_at
            FROM orders WHERE status IN ('pending','opened') ORDER BY id DESC
        """), "未结束的订单")

    elif cmd == "boxes":
        # 每个柜格取 id 最大的那一单；没有 orders 关联的格子不会出现
        show(query("""
            SELECT o.box, o.action, o.status, o.phone, o.pickup_code,
                   o.created_at, o.closed_at
            FROM orders o
            WHERE o.id = (SELECT MAX(id) FROM orders WHERE box = o.box)
            ORDER BY o.box
        """), "每个柜格的最新一单")

    elif cmd == "users":
        # 按余额升序：欠费的排最前，一眼能看见
        try:
            rows = query("""
                SELECT id, name, phone, card_uid, balance_fen
                FROM users ORDER BY balance_fen, id
            """)
        except sqlite3.OperationalError:
            sys.exit("users 表还没有 balance_fen 列 —— 后端启动过一次之后才会加上。")
        show(rows, "用户与绑卡"
                   "（card_uid 空 = 没绑卡；balance_fen 单位是分，负数 = 欠费冻结存件）")

    elif cmd == "wallet":
        try:
            rows = query("""
                SELECT id, ts, phone, kind, amount_fen, balance_after_fen,
                       order_id, store_id, note
                FROM ledger ORDER BY id DESC LIMIT ?
            """, (n or 30,))
        except sqlite3.OperationalError:
            # 老库还没跑过迁移（后端没重启），ledger 表还不存在
            sys.exit("还没有 ledger 表 —— 后端启动过一次之后才会建。")
        show(rows, f"钱包流水最近 {n or 30} 条"
                   "（amount_fen 正数是充值、负数是扣费，单位：分）")

    elif cmd == "audit":
        show(query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (n or 30,)),
             f"最近 {n or 30} 条审计")

    elif cmd == "sql":
        if len(args) < 2:
            sys.exit('用法：python dbq.py sql "SELECT ..."')
        sql = " ".join(args[1:])
        # 只读连接会拒掉写操作，这里给个人话提示
        try:
            show(query(sql), sql)
        except sqlite3.OperationalError as e:
            if "readonly" in str(e):
                sys.exit("这是只读连接，写不进去。要改数据请走后端的接口，别直接改库。")
            raise

    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
