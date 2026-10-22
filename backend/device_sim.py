"""
设备模拟器 —— 在没有 ESP32 实物时假扮一个柜子，用来联调后端。

    python device_sim.py                     正常跑，等后端下发开门命令（这就是"柜子"）
    python device_sim.py --boxes 1           只假扮一格（配合后端 set BOX_COUNT=1）
    python device_sim.py --card 04A12B3C4D   模拟刷一次校园卡（只发事件，不发命令）
    python device_sim.py --door-hold 5       开门后 5 秒自动关门（默认 3 秒）

    演示失败路径用：
    python device_sim.py --drop-open         收到开门命令但不回 door_opened（触发 15s 超时）
    python device_sim.py --stuck-open        开了门就不关了（30s 后上报 door_left_open）

它和真机走完全一样的 MQTT 主题和报文格式，所以后端不用改一行。

**一个进程假扮的是整个柜子，不是一格**：一台 ESP32 上跑一块固件，心跳和事件里都带
格号，一个进程报 N 格。真机只有 1 号格接了舵机 / 门磁 / 秤，2~4 号格是固件虚拟出来的
—— 这个模拟器把那些虚拟格也一并演了，所以默认就是 4 格（和后端 BOX_COUNT 默认一致）。

注意 `--card` 和常驻模式的分工：**刷卡只是注入一条 card_scan 事件，自己不扮演柜子**。
真正开门/关门的是那个常驻的 `device_sim.py`（或真机）—— 刷卡本来就得有读卡器在场。
两者同时跑不会互相打架：`--card` 那条路**不订阅 cmd**、也不起心跳线程，发一条事件就退出，
所以不会抢着去开门、让每个回执都重复一遍。
"""
import argparse
import json
import os
import random
import threading
import time

import paho.mqtt.client as mqtt

MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "locker")
MQTT_PASS = os.environ.get("MQTT_PASS", "locker123")
DEVICE_ID = "locker1"
TOPIC_CMD = f"locker/{DEVICE_ID}/cmd"
TOPIC_STATUS = f"locker/{DEVICE_ID}/status"
TOPIC_EVENT = f"locker/{DEVICE_ID}/event"

BOX_COUNT = 4
# 每格一份状态：IDLE(锁闭·空) / OPEN(门开) / OCCUPIED(锁闭·有物) / ERROR
# 在 main() 里按 --boxes 建好 —— 这里先留空，免得模块被 import 时多出几格来
boxes = {}
# client_id 带上 pid：不然 `--card` 那个一次性实例会和正在跑的主模拟器撞车，
# broker 会把先来的那个踢下线（MQTT 规定 client_id 相同即顶掉旧连接）。
client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                     client_id=f"locker-sim-{os.getpid()}")
door_hold = 3
drop_open = False     # 不回复 door_opened
stuck_open = False    # 不关门
state_lock = threading.Lock()


def say_status(box):
    with state_lock:
        payload = dict(boxes[box])
    payload["battery"] = random.randint(4900, 5100)
    payload["ts"] = int(time.time())
    client.publish(TOPIC_STATUS, json.dumps(payload))


def say_event(box, name, **extra):
    payload = {"event": name, "box": box, "ts": int(time.time()), **extra}
    client.publish(TOPIC_EVENT, json.dumps(payload))
    print(f"[sim] {box} 号格 事件 {name} {extra if extra else ''}".rstrip())


def do_open(box, action):
    """开门 -> 停一会儿 -> 关门，并按动作更新格内物品。

    每一格各起一个线程，所以"3 号格还开着门"不影响 4 号格接命令。
    """
    if drop_open:
        print(f"[sim] --drop-open：收到 {box} 号格开门命令，故意不回复 door_opened")
        return

    with state_lock:
        boxes[box].update(state="OPEN", locked=False, door_open=True)
    say_status(box)
    say_event(box, "door_opened")

    if stuck_open:
        print(f"[sim] --stuck-open：{box} 号格门不关了，30 秒后上报 door_left_open")
        time.sleep(30)
        say_event(box, "error", reason="door_left_open")
        return

    time.sleep(door_hold)

    with state_lock:
        boxes[box]["item"] = (action == "store")
        boxes[box].update(state="OCCUPIED" if boxes[box]["item"] else "IDLE",
                          locked=True, door_open=False)
    say_status(box)
    # 带上 state：和真固件同一个报文体。不带的话后端会退回"门关了就算成功"，
    # ① 那条判定在兜底方案下就静默失效了。
    say_event(box, "door_closed", state=boxes[box]["state"])
    print(f"[sim] {box} 号格 {action} 完成，当前状态 {boxes[box]['state']}")


def on_connect(c, userdata, flags, reason_code, properties=None):
    print(f"[sim] 已连接 broker ({reason_code})，假扮 {BOX_COUNT} 格，等待开门命令…")
    c.subscribe(TOPIC_CMD, 0)


def on_message(c, userdata, msg):
    data = json.loads(msg.payload.decode())
    if data.get("cmd") != "open":
        return
    # 格号从命令里取。多格之后后端一定会带 —— 以前这里随手丢掉、一律当成 1 号格，
    # 3 号格的命令会把 1 号格的门"开"一遍，两边状态一起对不上。
    box = data.get("box")
    if not isinstance(box, int) or not (1 <= box <= BOX_COUNT):
        print(f"[sim] 命令里的柜格号不对：{box!r}（本进程只假扮 1~{BOX_COUNT}），忽略")
        return
    threading.Thread(target=do_open, args=(box, data.get("action", "store")),
                     daemon=True).start()


def heartbeat():
    while True:
        for box in boxes:
            say_status(box)
        time.sleep(10)


def main():
    global door_hold, drop_open, stuck_open, BOX_COUNT, boxes
    p = argparse.ArgumentParser()
    p.add_argument("--card", help="模拟刷一次校园卡，值为卡号 UID")
    p.add_argument("--boxes", type=int, default=4,
                   help="假扮几格（要和后端的 BOX_COUNT 一致，默认 4）")
    p.add_argument("--door-hold", type=int, default=3, help="开门后多少秒关门")
    p.add_argument("--drop-open", action="store_true",
                   help="收到开门命令但不回 door_opened（演示 pending 超时）")
    p.add_argument("--stuck-open", action="store_true",
                   help="开门后不关门（演示 opened 卡死 / 门久开报警）")
    a = p.parse_args()
    door_hold = a.door_hold
    drop_open = a.drop_open
    stuck_open = a.stuck_open
    BOX_COUNT = max(1, a.boxes)
    boxes = {n: {"box": n, "state": "IDLE", "locked": True,
                 "door_open": False, "item": False}
             for n in range(1, BOX_COUNT + 1)}

    client.username_pw_set(MQTT_USER, MQTT_PASS)

    if a.card:
        # 纯注入器：不订阅 cmd、不扮演柜子，发完事件就退。
        # 真开门的是常驻的 device_sim.py / 真机，这里抢着开门只会让回执重复一遍。
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
        client.loop_start()
        time.sleep(0.5)
        # 不带格号：读卡器是整个柜子公用的，它读到的只是一张卡 ——
        # "刷哪一格"由后端按卡主占着哪一格算出来。
        client.publish(TOPIC_EVENT, json.dumps(
            {"event": "card_scan", "uid": a.card, "ts": int(time.time())}))
        print(f"[sim] 已模拟刷卡 {a.card}")
        time.sleep(1)                       # 等这条 publish 真发出去
        client.loop_stop()
        return

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
    client.loop_start()
    time.sleep(0.5)

    threading.Thread(target=heartbeat, daemon=True).start()
    print("[sim] Ctrl+C 退出")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        client.loop_stop()


if __name__ == "__main__":
    main()
