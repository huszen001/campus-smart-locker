/*
 * 校园智能寄存柜 —— ESP32 设备端（4 格：1 格真硬件 + 3 格虚拟）
 *
 * 硬件           接线
 *   SG90 舵机      信号 -> GPIO 13      （红 5V / 棕 GND，建议舵机单独 5V 供电并与板子共地）
 *   MFRC522 读卡   SCK  -> GPIO 18      （VCC 必须 3.3V，接 5V 会烧模块）
 *                  MISO -> GPIO 19
 *                  MOSI -> GPIO 23
 *                  SS   -> GPIO 21
 *                  RST  -> GPIO 22
 *   磁性门控开关    一端 -> GPIO 4，另一端 -> GND
 *   HX711 压力传感器 DT   -> GPIO 27     （VCC 必须接 3.3V，理由见下面 HX711 一节）
 *                  SCK  -> GPIO 32
 *   0.96" OLED     SCL  -> GPIO 26      （SSD1306，I2C 四针，VCC 接 3.3V）
 *                  SDA  -> GPIO 25
 *
 * ⚠️ 上面这些**只有 1 号柜格有**。2~4 号格没有舵机、没有门磁、没有秤，是固件
 *    虚拟出来的（见「柜格」一节）。读卡器和屏幕是**整个柜子共用**的：一块 RC522
 *    读一张卡，卡不会说"我在 3 号格前面"，所以"刷的是哪一格"由后端按卡主占着
 *    哪一格算出来。
 *
 * MQTT 主题（与 backend/app.py 一致）
 *   订阅  locker/{DEVICE_ID}/cmd          后端下发 {"cmd":"open","box":1,"action":"store"}
 *         locker/{DEVICE_ID}/auth_result  刷卡的授权结果
 *   发布  locker/{DEVICE_ID}/status       每 10s 心跳，**每格一条**
 *         locker/{DEVICE_ID}/event        door_opened / door_closed / card_scan / error
 *                                         （card_scan 不带 box —— 读卡器不认格）
 *
 * BOX_COUNT 必须和后端一致（后端 set BOX_COUNT=4，默认就是 4）。
 * 对不上不会报错，但会各说各话：固件报 2 号格、后端只认 1 号格，回执全被丢掉。
 *
 * 依赖库：PubSubClient、ArduinoJson(7.x)、MFRC522、ESP32Servo、U8g2
 *         （esp32 core 3.x 不再自带 Servo 库，所以要装 ESP32Servo；
 *           头文件是 ESP32Servo.h，但类名仍叫 Servo，下面代码不用改）
 */
#include <WiFi.h>
#include <PubSubClient.h>
#include <SPI.h>
#include <MFRC522.h>
#include <ESP32Servo.h>
#include <ArduinoJson.h>
#include <U8g2lib.h>

// ============================== 需要你改这里 ==============================
// 校园网如果有客户端隔离，PC 和板子会互相 ping 不通，MQTT 连不上；
// 换成普通 WPA2 个人版热点/路由器就没这个问题，PC 和板子在同一网段即可。
// ⚠️ 热点/网络换一次、或者笔记本重连一次，下面这个 IP 就会变，看后端启动横幅打的地址。
const char *WIFI_SSID = "YOUR_WIFI_SSID";
const char *WIFI_PASS = "YOUR_WIFI_PASSWORD";
const char *MQTT_HOST = "192.168.1.100";   // 改成跑 backend 那台电脑的局域网 IP
const uint16_t MQTT_PORT = 1883;
const char *MQTT_USER = "locker";          // 与 broker 的 password_file 里一致
const char *MQTT_PASS = "locker123";
const char *DEVICE_ID = "locker1";
// ==========================================================================

// ------------------------------- 引脚 -------------------------------
const int PIN_SERVO = 13;
const int PIN_DOOR = 4;    // 磁性门控开关
const int PIN_HX711_DT = 27;   // 压力传感器数据（输入）
const int PIN_HX711_SCK = 32;  // 压力传感器时钟（输出）
const int PIN_RFID_SS = 21;
const int PIN_RFID_RST = 22;
// OLED 走 I2C。ESP32 的 I2C 是引脚矩阵，SDA/SCL 能指到几乎任意 GPIO，
// 这里挑 25/26：完整双向脚、不是 strapping 脚、没被上面几样占用。
// ⚠️ 别用 34/35/36/39 —— 那几个是只读输入脚，接上去屏幕不会亮，
//    而且现象是"全黑但我什么都没接错"，很难往引脚能力上想。
const int PIN_OLED_SCL = 26;
const int PIN_OLED_SDA = 25;

// 舵机角度：装好后如果锁的开关方向反了，把这两个值对调
const int ANGLE_LOCKED = 0;
const int ANGLE_UNLOCKED = 90;

// 门磁极性：装好后如果门开关方向读反了，把这里改成 LOW
const int DOOR_OPEN_LEVEL = HIGH;  // 门开时读到 HIGH（簧片吸合拉低，INPUT_PULLUP）

const unsigned long HEARTBEAT_MS = 10000;   // 心跳间隔
const unsigned long DOOR_ALARM_MS = 30000;  // 门开超过这么久 -> 报警
// 读到"门关着"之后，先等这么久再上锁。原因见 loop 里关门分支的注释。
const unsigned long DOOR_SETTLE_MS = 400;
const unsigned long CARD_DEBOUNCE_MS = 2000;
const unsigned long VIRTUAL_DOOR_MS = 3000; // 虚拟格：开门后几秒自己"关上"
const unsigned long OLED_ROTATE_MS = 2000;  // 屏幕隔多久换一格显示

// ---------------- HX711 压力传感器：参数 ----------------
// 用压力传感器代替原来的光电，只回答"格子里有没有东西"，不称重、不上报克数。
// 驱动实现和几处"照搬 51 例程会出事"的说明在下面「HX711 压力传感器」一节。
const long ITEM_THRESHOLD_COUNTS = 8000;  // 判定"有物"的门限，按串口实测调（见 README）
long scaleZero = 0;       // 零点（去皮值）
float scaleFiltered = 0;  // 滑动平均后的当前读数
bool scaleReady = false;  // 是否取到过有效读数

// ------------------------------- 状态 -------------------------------
// ST_ERROR 目前是预留项，代码里不会进入它（后端靠超时判失败来兜"执行失败"）。
// 真要启用得先改 loop 里的关门分支：那里是 `state == ST_OPEN && !doorOpen()`，
// 一旦进了 ST_ERROR 就再也匹配不上，状态会永久卡死。别顺手打开。
enum BoxState { ST_IDLE, ST_OPEN, ST_OCCUPIED, ST_ERROR };

// ------------------------------- 柜格 -------------------------------
// 真机只给 1 号格接了硬件（舵机 / 门磁 / 压力传感器），2~4 号格是**虚拟格**：
// 状态、开门时序全由固件自己演，好让后端和页面的多格逻辑（自动分配、凭码跨格找、
// 屏幕轮放）在没有实物的情况下也能完整跑起来。后端 set BOX_COUNT=4 与之对应。
#define BOX_COUNT 4
const int REAL_BOX = 1;      // 只有这一格接了硬件，其余格的门是定时器演出来的

// 虚拟格**故意不做任何防呆**：后端说开就开，VIRTUAL_DOOR_MS 之后自己"关上"，
// 格内有无按动作记。真机的防呆来自硬件（门磁 + 秤），虚拟格没有真值可依，
// 硬编一套判据只会和真机行为分叉 —— 演示时看着像真的，一换真机全是新 bug。
struct Box {
  BoxState state;
  bool locked;
  bool item;                 // 格内有无。1 号格以秤为准，不读这里
  bool doorSeenOpen;         // 只 1 号格用：门真被打开过才认后面的关门
  unsigned long doorOpenedAt;// 只 1 号格用
  bool doorAlarmSent;        // 只 1 号格用
  unsigned long closeAt;     // 只虚拟格用：到这个时刻自动关门
};
Box boxes[BOX_COUNT];        // 下标 0 = 1 号格

String lastCardUid = "";
unsigned long lastCardAt = 0;

WiFiClient net;
PubSubClient mqtt(net);
MFRC522 rfid(PIN_RFID_SS, PIN_RFID_RST);
Servo lockServo;
// 用软件 I2C（_SW_ 而不是 _HW_）：软件模拟不挑脚、不依赖外设映射，
// 换引脚不用改配置。代价是刷新慢（全屏几十毫秒），对一块状态屏完全够用。
U8G2_SSD1306_128X64_NONAME_F_SW_I2C oled(U8G2_R0, PIN_OLED_SCL, PIN_OLED_SDA,
                                         U8X8_PIN_NONE);

const char *stateName(BoxState s) {
  switch (s) {
    case ST_IDLE: return "IDLE";
    case ST_OPEN: return "OPEN";
    case ST_OCCUPIED: return "OCCUPIED";
    default: return "ERROR";
  }
}

bool doorOpen() { return digitalRead(PIN_DOOR) == DOOR_OPEN_LEVEL; }
// 格子里有没有东西：读数离零点超过门限就算有。函数名和返回值没变，
// 所以 OLED / 心跳上报 / 关门判定那几处调用一行都不用改。
bool itemPresent() {
  if (!scaleReady) return false;   // 模块没读数，宁可判"无"，别乱报有物
  return fabsf(scaleFiltered - (float)scaleZero) > (float)ITEM_THRESHOLD_COUNTS;
}

// 下面四个是"这一格现在是什么情况"的统一入口。真硬件只有 1 号格有，
// 所以每个都得按格号分流 —— 调用方（OLED / 心跳 / 关门判定）不用知道谁是真的。
bool itemOf(int n) {
  return n == REAL_BOX ? itemPresent() : boxes[n - 1].item;
}
bool doorOpenOf(int n) {
  // 虚拟格没有门磁，就把"状态是 OPEN"当成门开着 —— 本来就是固件自己编的，
  // 编得自洽就行。
  return n == REAL_BOX ? doorOpen() : boxes[n - 1].state == ST_OPEN;
}
bool lockedOf(int n) { return boxes[n - 1].locked; }

void setLock(bool locked) {
  lockServo.write(locked ? ANGLE_LOCKED : ANGLE_UNLOCKED);
  boxes[REAL_BOX - 1].locked = locked;
  delay(400);  // 等舵机转到位
}

// ------------------------------- HX711 压力传感器 -------------------------------
// 两线时序：HX711 是 24 位 ADC，配应变片（半桥/全桥）。数据就绪时 DT 拉低，
// 主机连打 25 个时钟把读数取回来 —— 第 25 个脉冲是用来选下一轮的通道和增益的
// （25 个 = A 通道 gain 128），少打一个，下一轮读数就漂。读数在线上是偏移二进制，
// 异或 1<<23 才变成补码。
//
// 和网上那份清翔 51 例程有三处不一样，都是照搬会在 ESP32 上出事的地方：
//   ① 例程用 GPIO 2/3：2 是板载 LED + strapping 脚、3 是 UART0 的 RX，
//      会干扰启动并废掉串口。这里 DT 用 27（沿用原来光电那根线），SCK 用 32。
//   ② 例程 `while(digitalRead(DT));` 是无超时死等，模块一掉线整块板子卡死，
//      MQTT / OLED / 门磁状态机全停。这里带超时，且等待里 delay(1) 让出 CPU ——
//      空转不放 CPU 的话看门狗 5 秒喂不满会被直接重启。
//   ③ 例程用 unsigned long 存差值，读数掉到零点以下会回绕成 ~42 亿、恒大于阈值，
//      柜子从此永远认为"有物"，东西再也取不走。这里全程有符号。
//
// 为什么用"原始计数"判而不是克数：应变片的读数只有相对意义、没有绝对零点，
// 本来就得先定个基准；而既然只要"有 / 无"，基准 + 计数门限就够了，**不需要标定系数**。
// 例程里的 GapValue 430 是那个秤在那个供电电压下标出来的，换个传感器、或者激励电压
// 从 5V 变成 3.3V（本工程就是这么接的），数就对不上了。
//
// ⚠️ 供电：模块 VCC 接 3.3V，别接 5V。常见的那种红色小板把 DVDD 和 VCC 短在一起，
//    接 5V 会让 DT 输出摆到 5V 打进 ESP32（绝对最大才 ~3.6V），同时 3.3V 的 SCK
//    又够不到 5V 供电下的高电平门限。

// 读一次原始计数（有符号）。DT 一直不拉低就是没就绪，超时返回 false。
bool hx711Read(long &out) {
  unsigned long t0 = millis();
  while (digitalRead(PIN_HX711_DT) == HIGH) {
    if (millis() - t0 > 200) return false;  // 200ms 还没就绪 = 掉线或模块坏了
    delay(1);
  }

  long v = 0;
  for (int i = 0; i < 24; i++) {
    digitalWrite(PIN_HX711_SCK, HIGH);
    delayMicroseconds(1);
    v = (v << 1) | (digitalRead(PIN_HX711_DT) == HIGH ? 1 : 0);
    digitalWrite(PIN_HX711_SCK, LOW);
    delayMicroseconds(1);
  }
  // 第 25 个脉冲：选下一轮为 A 通道 gain 128。省掉它读数会乱。
  digitalWrite(PIN_HX711_SCK, HIGH);
  delayMicroseconds(1);
  digitalWrite(PIN_HX711_SCK, LOW);
  delayMicroseconds(1);

  v ^= 0x800000;                     // 偏移二进制 -> 24 位补码
  if (v & 0x800000) v -= 0x1000000;  // 符号扩展到 32 位
  out = v;
  return true;
}

// 阻塞采一组取平均。用在两个时机：开机去皮、关门后判定。
// 丢掉第一个样本：它可能是上一轮转换的结果（手还没离开 / 刚上电不稳）。
// 一次转换约 100ms（HX711 默认 10 SPS），所以整组约 500ms。
bool scaleSample(long &out) {
  long v, sum = 0;
  if (!hx711Read(v)) return false;
  for (int i = 0; i < 4; i++) {
    if (!hx711Read(v)) return false;
    sum += v;
  }
  out = sum / 4;
  return true;
}

// 把当前读数当零点。返回 false = 读不到模块（接线或供电问题）。
bool scaleTare() {
  long v;
  if (!scaleSample(v)) {
    scaleZero = 0;
    scaleReady = false;
    Serial.println("[scale] 读不到 HX711 —— 检查 DT/SCK 接线、VCC 是不是 3.3V");
    return false;
  }
  scaleZero = v;
  scaleFiltered = (float)v;
  scaleReady = true;
  Serial.printf("[scale] 去皮完成，零点 %ld（有物门限 %ld 计数）\n",
                scaleZero, ITEM_THRESHOLD_COUNTS);
  return true;
}

// 非阻塞：只在数据就绪时读一次，用滑动平均压噪声。每圈 loop 调一次。
void tickScale() {
  // 每秒往串口打一行当前读数 —— 现场定 ITEM_THRESHOLD_COUNTS 全靠它：
  // 空托盘看差值的噪声有多大，放上实物再看差多大，门限取在两者中间。
  // 读不到模块时改打 DT 的电平：**一直是高 = 没有任何东西在驱动这根线**
  // （DT 没接上 / 模块没供电 / 插错脚），这是最快能区分"接线"和"模块坏"的一条。
  static unsigned long lastPrint = 0;
  if (millis() - lastPrint >= 1000) {
    lastPrint = millis();
    if (scaleReady) {
      Serial.printf("[scale] 读数 %ld  零点 %ld  差 %ld  门限 %ld\n",
                    (long)scaleFiltered, scaleZero,
                    (long)scaleFiltered - scaleZero, ITEM_THRESHOLD_COUNTS);
    } else {
      Serial.printf("[scale] 没读数 —— DT(GPIO %d) 当前电平 %d\n",
                    PIN_HX711_DT, digitalRead(PIN_HX711_DT));
    }
  }

  if (digitalRead(PIN_HX711_DT) == HIGH) return;  // 没就绪，下一圈再看
  long v;
  if (!hx711Read(v)) return;
  if (!scaleReady) return;
  scaleFiltered += (v - scaleFiltered) / 8.0f;
}

// ------------------------------- OLED -------------------------------
// 屏幕干两件事：① 轮放各柜格的状态；② 刷卡/开门时插播一条反馈，到点退回状态页。
//
// 一块屏要照看四格，所以**轮着显示**（每 OLED_ROTATE_MS 换一格），右上角的 "n/4"
// 就是页码 —— 没有它就分不清"这屏是 2 号格"还是"1 号格变成这个状态了"。
// 哪一格刚有动作，会先停在那一格（见 doOpen），免得用户压根没看到自己那格。
//
// 先说清"设备自己知道什么"的边界：它知道 IDLE/OPEN/OCCUPIED、门磁、压力传感器，
// 但它**不知道取件码** —— 码是后端生成的，从来没下发给设备。所以屏幕显示不了
// 取件码，那要改后端协议（本次没做，见 README 的"屏幕"一节）。

char toastTop[24] = "";    // 主行：刷卡时是卡号，开门时是"n 号柜格"
char toastBot[24] = "";    // 副行：结果说明
unsigned long toastUntil = 0;
bool toastShown = false;
unsigned long lastOledDraw = 0;
unsigned long lastRotate = 0;
int oledBox = REAL_BOX;    // 屏幕正在显示哪一格
bool oledDirty = true;

void drawCentered(int y, const char *s) {
  oled.drawUTF8((128 - (int)oled.getUTF8Width(s)) / 2, y, s);
}

void oledToast(const char *top, const char *bot, unsigned long ms) {
  strncpy(toastTop, top, sizeof(toastTop) - 1);
  toastTop[sizeof(toastTop) - 1] = 0;
  strncpy(toastBot, bot, sizeof(toastBot) - 1);
  toastBot[sizeof(toastBot) - 1] = 0;
  toastUntil = millis() + ms;
  oledDirty = true;   // 不用等下一轮定时刷新，立刻显示
}

const char *stateCN(BoxState s) {
  switch (s) {
    case ST_IDLE: return "空闲";
    case ST_OPEN: return "门已开";
    case ST_OCCUPIED: return "有物品";
    default: return "故障";
  }
}

void drawOled(bool toastActive) {
  oled.clearBuffer();
  oled.setFont(u8g2_font_wqy12_t_gb2312);   // 中文字库，约 198KB flash

  if (toastActive) {
    drawCentered(24, toastTop);
    drawCentered(48, toastBot);
    oled.sendBuffer();
    return;
  }

  int n = oledBox;
  bool net = (WiFi.status() == WL_CONNECTED) && mqtt.connected();
  const char *netText = net ? "在线" : "断网";
  char head[16];
  snprintf(head, sizeof(head), "%d号柜格", n);
  oled.drawUTF8(2, 12, head);
  oled.drawUTF8(126 - (int)oled.getUTF8Width(netText), 12, netText);
  oled.drawHLine(0, 16, 128);

  // 只有一种字号，靠"白底黑字"把状态拉出来，不然整屏一样重、看不出主次
  const char *st = stateCN(boxes[n - 1].state);
  int w = (int)oled.getUTF8Width(st);
  oled.drawBox((128 - w - 20) / 2, 21, w + 20, 20);
  oled.setDrawColor(0);
  oled.drawUTF8((128 - w) / 2, 36, st);
  oled.setDrawColor(1);

  char line[32];
  snprintf(line, sizeof(line), "门%s  物%s  锁%s",
           doorOpenOf(n) ? "开" : "关", itemOf(n) ? "有" : "无",
           lockedOf(n) ? "已锁" : "未锁");
  drawCentered(58, line);

  // 页码。四格轮着显示，没有它就是一块"内容会自己变"的屏，看着像坏了。
  if (BOX_COUNT > 1) {
    char page[8];
    snprintf(page, sizeof(page), "%d/%d", n, BOX_COUNT);
    oled.drawUTF8(126 - (int)oled.getUTF8Width(page), 36, page);
  }
  oled.sendBuffer();
}

// 状态页每秒刷一次（在线/断网、门、物会自己变）；有变化立刻刷；
// 临时消息到点要马上退回状态页，不能等下一个整秒。
void tickOled() {
  unsigned long now = millis();
  bool active = now < toastUntil;

  // 轮放：没在弹消息（弹消息时不该被换掉）就换下一格
  if (!active && BOX_COUNT > 1 && now - lastRotate >= OLED_ROTATE_MS) {
    lastRotate = now;
    oledBox = oledBox % BOX_COUNT + 1;
    oledDirty = true;
  }

  bool expired = toastShown && !active;
  if (!oledDirty && !expired && now - lastOledDraw < 1000) return;
  toastShown = active;
  oledDirty = false;
  lastOledDraw = now;
  drawOled(active);
}

// ------------------------------- MQTT -------------------------------
// 心跳是**每格一条**，不是一条报文里塞四格：后端的 boxes 是按"一条状态报文 = 一格"
// 维护的，而且哪一格掉线要看它自己那条心跳断没断。
void publishStatus(int n) {
  JsonDocument doc;
  doc["box"] = n;
  doc["state"] = stateName(boxes[n - 1].state);
  doc["locked"] = lockedOf(n);
  doc["door_open"] = doorOpenOf(n);
  doc["item"] = itemOf(n);
  doc["ts"] = (uint32_t)(millis() / 1000);

  char topic[64], buf[192];
  snprintf(topic, sizeof(topic), "locker/%s/status", DEVICE_ID);
  serializeJson(doc, buf, sizeof(buf));
  mqtt.publish(topic, buf);
}

void publishAllStatus() {
  for (int n = 1; n <= BOX_COUNT; n++) publishStatus(n);
}

// n 传 0 = 这条事件不属于某一格。刷卡就是这样：RC522 是整个柜子共用的，
// 它读到的只是一张卡，"刷的是哪一格"后端自己算（见 handle_card_scan）。
void publishEvent(int n, const char *event,
                  const char *extraKey = nullptr, const char *extraVal = nullptr) {
  JsonDocument doc;
  doc["event"] = event;
  if (n > 0) doc["box"] = n;
  doc["ts"] = (uint32_t)(millis() / 1000);
  if (extraKey) doc[extraKey] = extraVal;

  char topic[64], buf[224];
  snprintf(topic, sizeof(topic), "locker/%s/event", DEVICE_ID);
  serializeJson(doc, buf, sizeof(buf));
  mqtt.publish(topic, buf);
  Serial.printf("[event] %s\n", buf);
}

void doOpen(int n, const char *action) {
  Box &b = boxes[n - 1];
  bool wantStore = (strcmp(action, "store") == 0);

  // 屏幕先停到这一格：四格轮放，不停的话用户很可能压根没看到自己那格的动静
  oledBox = n;
  oledDirty = true;

  if (n != REAL_BOX) {
    // 虚拟格：后端说开就开，VIRTUAL_DOOR_MS 之后 loop() 自己把它关上。
    // 格内有无按动作记 —— 后端本来就只在"存"之后才认为格里有东西。
    Serial.printf("[cmd] %d 号格（虚拟）开门 action=%s\n", n, action);
    b.state = ST_OPEN;
    b.locked = false;
    b.item = wantStore;
    b.closeAt = millis() + VIRTUAL_DOOR_MS;
    char top[24];
    snprintf(top, sizeof(top), "%d 号柜格", n);
    oledToast(top, wantStore ? "存件 · 柜门已开" : "取件 · 柜门已开", 6000);
    publishStatus(n);
    publishEvent(n, "door_opened");
    return;
  }

  // ---- 1 号格：真硬件 ----
  // 防呆：空柜取件、满柜存件都拒绝，并回报错误。
  // 注意这防呆只在真格上有 —— 它靠的是真实的秤和门磁，虚拟格没有真值可依。
  //
  // ⚠️ 判据用**秤的实时读数**，不用缓存的 b.state（2026-10-22 真机撞出来的）：
  // 后端「对账」把一条假存件单判失败、把格子放开之后，固件内存里的 state 还是
  // OCCUPIED —— 没有开关门，谁也不通知它。于是下一次存件被这里挡成
  // already_occupied，用户看到的是"格子解开了却存不进去，只能重启板子"。
  // 这格里有没有东西，**秤是唯一的事实**；state 只用来显示和上报。
  bool hasItem = itemPresent();
  if (wantStore && hasItem) {
    publishEvent(n, "error", "reason", "already_occupied");
    oledToast("无法开门", "这格已经有东西了", 3000);
    return;
  }
  if (!wantStore && !hasItem) {
    publishEvent(n, "error", "reason", "nothing_to_pickup");
    oledToast("无法开门", "这格是空的", 3000);
    return;
  }

  Serial.printf("[cmd] %d 号格开门 action=%s\n", n, action);
  setLock(false);
  b.state = ST_OPEN;
  b.doorOpenedAt = millis();
  b.doorAlarmSent = false;
  b.doorSeenOpen = false;
  char top[24];
  snprintf(top, sizeof(top), "%d 号柜格", n);
  oledToast(top, wantStore ? "存件 · 柜门已开" : "取件 · 柜门已开", 6000);
  publishStatus(n);
  publishEvent(n, "door_opened");
}

// 后端 auth_result 的 reason 逐个翻成人话再上屏 ——
// 屏幕是给站在柜子前面的人看的，显示 "not_bound" 等于没显示。
void oledToastAuth(const char *result, const char *reason, const char *action) {
  if (strcmp(result, "ok") == 0) {
    if (strcmp(reason, "card_bound") == 0)
      oledToast("绑卡成功", "以后刷这张卡就能开柜", 4000);
    else if (strcmp(action, "pickup") == 0)
      oledToast("已授权", "请取出物品并关门", 6000);
    else
      oledToast("已授权", "请放入物品并关门", 6000);
    return;
  }
  if (strcmp(reason, "not_bound") == 0)
    oledToast("此卡未绑定", "请先在手机页面绑卡", 4000);
  else if (strcmp(reason, "not_owner") == 0)
    oledToast("不是你的物品", "这张卡不是本柜存放人", 4000);
  else if (strcmp(reason, "device_offline") == 0)
    oledToast("设备离线", "请稍后重试", 4000);
  else if (strcmp(reason, "full") == 0)
    oledToast("柜子满了", "请稍后再试", 4000);
  else
    oledToast("已拒绝", "请联系管理员", 4000);
}

void onMqtt(char *topic, byte *payload, unsigned int len) {
  JsonDocument doc;
  if (deserializeJson(doc, payload, len)) return;

  if (strstr(topic, "/cmd")) {
    if (strcmp(doc["cmd"] | "", "open") != 0) return;
    // 格号从命令里取。以前这里一律当成 1 号格 —— 单格时看不出区别，
    // 多格之后就是"3 号格的命令开了 1 号格的门"，两边状态一起错。
    int n = doc["box"] | 0;
    if (n < 1 || n > BOX_COUNT) {
      Serial.printf("[cmd] 格号 %d 不在 1~%d 里，忽略\n", n, BOX_COUNT);
      return;
    }
    doOpen(n, doc["action"] | "store");
  } else if (strstr(topic, "/auth_result")) {
    const char *result = doc["result"] | "?";
    const char *reason = doc["reason"] | "";
    const char *action = doc["action"] | "";
    Serial.printf("[auth] %s reason=%s\n", result, reason);
    oledToastAuth(result, reason, action);
  }
}

void ensureMqtt() {
  if (mqtt.connected()) return;
  char cid[48];
  snprintf(cid, sizeof(cid), "esp32-%s", DEVICE_ID);
  Serial.print("[mqtt] 连接中…");
  if (mqtt.connect(cid, MQTT_USER, MQTT_PASS)) {
    Serial.println(" 已连接");
    char topic[64];
    snprintf(topic, sizeof(topic), "locker/%s/cmd", DEVICE_ID);
    mqtt.subscribe(topic);
    snprintf(topic, sizeof(topic), "locker/%s/auth_result", DEVICE_ID);
    mqtt.subscribe(topic);
    publishAllStatus();
  } else {
    Serial.printf(" 失败 rc=%d，2 秒后重试\n", mqtt.state());
    delay(2000);
  }
}

// ------------------------------- 刷卡 -------------------------------
void pollRfid() {
  if (!rfid.PICC_IsNewCardPresent() || !rfid.PICC_ReadCardSerial()) return;

  String uid = "";
  for (byte i = 0; i < rfid.uid.size; i++) {
    if (rfid.uid.uidByte[i] < 0x10) uid += "0";
    uid += String(rfid.uid.uidByte[i], HEX);
  }
  uid.toUpperCase();
  rfid.PICC_HaltA();

  // 同一张卡 2 秒内只报一次，避免连续触发
  if (uid == lastCardUid && millis() - lastCardAt < CARD_DEBOUNCE_MS) return;
  lastCardUid = uid;
  lastCardAt = millis();

  Serial.printf("[rfid] 卡号 %s\n", uid.c_str());
  // 先上屏"验证中"，后端 auth_result 一回来就被覆盖成真正的结果。
  // 超时 2500ms 是兜底：后端要是没回，屏幕也得自己退回状态页。
  oledToast(uid.c_str(), "验证中…", 2500);
  // 不带格号：一块 RC522 照看整个柜子，刷卡时它不知道人在哪一格前面
  publishEvent(0, "card_scan", "uid", uid.c_str());
}

// ------------------------------- 主流程 -------------------------------
void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n=== 校园智能寄存柜 启动 ===");

  // OLED 第一个起。后面 WiFi 要等最多 30 秒，屏幕得先能显示东西，
  // 不然中途出问题就是一块全黑的板子，什么都看不出来。
  oled.begin();
  oled.setFont(u8g2_font_wqy12_t_gb2312);
  oled.clearBuffer();
  oled.drawUTF8(2, 20, "系统启动中…");
  oled.sendBuffer();

  pinMode(PIN_DOOR, INPUT_PULLUP);
  // DT 用 INPUT_PULLUP：模块掉线时这根线悬空一直是高，就永远"没就绪"，
  // 于是读不到数，而不是读出一堆假值。
  pinMode(PIN_HX711_SCK, OUTPUT);
  digitalWrite(PIN_HX711_SCK, LOW);
  pinMode(PIN_HX711_DT, INPUT_PULLUP);
  scaleTare();  // 开机去皮，约 0.5 秒

  lockServo.attach(PIN_SERVO);
  setLock(true);

  SPI.begin(18, 19, 23, PIN_RFID_SS);
  rfid.PCD_Init();
  delay(200);
  byte ver = rfid.PCD_ReadRegister(MFRC522::VersionReg);
  Serial.printf("[rfid] 版本 0x%02X（读不到就检查接线和 3.3V 供电）\n", ver);

  // 顺便把读卡器版本显示在屏上：调硬件时最常卡的就是这一步。
  // 判据是"芯片答没答话"，不是"版本号是不是教科书上的值"：0x92 是正品 v2.0，
  // 但廉价克隆片报 0x82 —— 本机实测就是 0x82，刷卡完全正常。写死 0x92 会在
  // 一块好读卡器上显示"异常"。只有 0x00 / 0xFF 才代表 SPI 压根没通。
  char vline[32];
  snprintf(vline, sizeof(vline), "读卡器 0x%02X %s", ver,
           (ver == 0x00 || ver == 0xFF) ? "异常" : "正常");
  oled.clearBuffer();
  oled.drawUTF8(2, 20, vline);
  oled.sendBuffer();
  delay(900);

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  oled.clearBuffer();
  oled.drawUTF8(2, 20, "正在连接 WiFi");
  oled.drawUTF8(2, 40, WIFI_SSID);
  oled.sendBuffer();

  // 最多等 30 秒。等不到就继续往下走 —— loop() 里有重连逻辑，
  // 而卡死在 setup() 里的表现是"屏幕一动不动的黑屏"，最难看懂。
  unsigned long wifiStart = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - wifiStart < 30000) {
    delay(500);
    Serial.print(".");
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("\n[wifi] 已连接，本机 IP %s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.println("\n[wifi] 30 秒没连上，先继续跑，loop() 里会重试");
  }

  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMqtt);
  mqtt.setBufferSize(512);

  // 上电时按传感器实况定初始状态，避免断电恢复后状态错乱。
  // ⚠️ 零点就是开机这一下取的，所以**托盘上放着东西时重启，那东西会被当成零点**，
  //    之后一直报"空"。补救：把托盘清空重启，或者在串口里敲 t 重新去皮。
  // 虚拟格按"空、锁好"起：它们的状态只活在内存里，重启就没了，
  // 和后端 DB 里的占用记录对不上时以后端为准（后端才是唯一真相）。
  for (int n = 1; n <= BOX_COUNT; n++) {
    boxes[n - 1].state = ST_IDLE;
    boxes[n - 1].locked = true;
    boxes[n - 1].item = false;
  }
  // 1 号格另说：它的"有没有东西"是秤说了算，不能按"空"起
  boxes[REAL_BOX - 1].state = itemPresent() ? ST_OCCUPIED : ST_IDLE;
  boxes[REAL_BOX - 1].item = itemPresent();
  if (!scaleReady) oledToast("称重模块", "没读数，检查接线", 5000);
}

void loop() {
  // 压力传感器和屏幕都放最前面：下面 WiFi 掉线那条会 return，
  // 读数不能跟着冻住；"断网"这个状态也得靠屏幕告诉现场的人。
  tickScale();
  tickOled();

  // 串口敲 t 重新去皮（托盘清空后敲；开机时托盘上放着东西也能用它补救）
  if (Serial.available()) {
    char c = Serial.read();
    if (c == 't' || c == 'T') scaleTare();
  }

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[wifi] 掉线，重连…");
    WiFi.reconnect();
    delay(1000);
    return;
  }
  ensureMqtt();
  mqtt.loop();
  pollRfid();

  static unsigned long lastBeat = 0;
  if (millis() - lastBeat >= HEARTBEAT_MS) {
    lastBeat = millis();
    publishAllStatus();
  }

  // ------- 1 号格（真硬件）：门磁 + 秤 -------
  Box &real = boxes[REAL_BOX - 1];

  // 门真的开过了，才认后面的"关门"。舵机开锁 ≠ 门被打开 —— 少了这一关，
  // 只要下单时门正好关着，loop 下一圈（50 ms）就把它当成关门回执，订单
  // 一秒就 done，东西还没放进去，状态还会落成 IDLE（压力传感器那时读到无物）。
  // 拿跳线测时 GPIO 悬空一直读"开"，恰好把这个竞态掩盖了，接真簧片才暴露。
  if (real.state == ST_OPEN && doorOpen()) real.doorSeenOpen = true;

  // 门关了 -> 先等门停稳再上锁，然后按压力传感器读数决定落到 IDLE 还是 OCCUPIED
  //
  // 为什么不能读到"关"就立刻 setLock(true)：簧片是**渐近吸合**的，磁铁离到位
  // 还差几毫米时触点就已经闭合了，而门靠惯性还要再摆一小段；再加上越过吸合距离
  // 那一下触点本身会抖几十毫秒。loop 是 50 ms 一圈、一次采样定生死，正好能抽中
  // 抖动间隙读到"关"。上锁太早 = **锁舌顶在一个还在走的门上** —— 装上真锁体就是
  // 卡死或堵转（SG90 堵转约 700 mA，够把板子拉重启）。
  // 真机上空转舵机 + 没柜体时这两条都露不出来，是"装柜体前必须补"的一环。
  //
  // 阻塞 delay 和 setLock() 里那个 delay(400) 是同一套路数：最坏影响是 mqtt.loop()
  // 和心跳晚几百毫秒，而心跳 10 s、门开报警 30 s，余量极大。等完再确认一次门还关着
  // 才是治抖动的地方 —— 又弹开了就当没发生，下一圈重新计。
  if (real.state == ST_OPEN && real.doorSeenOpen && !doorOpen()) {
    delay(DOOR_SETTLE_MS);
    if (!doorOpen()) {
      setLock(true);  // 里面还带 delay(400) 等舵机转到位，之后门已经关实了
      // 滑动平均里可能还混着刚才手的读数，重新采一组再判；采不到就沿用上一次的值。
      long v;
      if (scaleSample(v)) scaleFiltered = (float)v;
      real.state = itemPresent() ? ST_OCCUPIED : ST_IDLE;
      oledToast("1 号柜格", real.state == ST_OCCUPIED ? "存件完成" : "取件完成",
                3000);
      publishStatus(REAL_BOX);
      // 关键：把**秤的结论**跟门磁回执一起发过去。
      // "门关了"只说明这一轮结束了，不说明操作成功 —— 成败只有秤知道。
      // 后端靠这个字段决定这单落 done 还是 failed；不带的话，"开了门又关上、
      // 但什么都没放"会被记成一次成功存件，还发一个取件码出去（格子随后卡死）。
      // 发 state 而不是裸的 item：这样两边用的是固件自己的判据，只有一个口径。
      publishEvent(REAL_BOX, "door_closed", "state", stateName(real.state));
      Serial.printf("[state] %d 号格 -> %s\n", REAL_BOX, stateName(real.state));
    }
  }

  // 门长时间不关 -> 报警一次
  if (real.state == ST_OPEN && !real.doorAlarmSent &&
      millis() - real.doorOpenedAt > DOOR_ALARM_MS) {
    real.doorAlarmSent = true;
    publishEvent(REAL_BOX, "error", "reason", "door_left_open");
  }

  // ------- 2 号及以后（虚拟格）：没有门磁，开门到点就算关上 -------
  // 真机那种"门真被打开过才认关门"在这里没有意义 —— 没有簧片可读，
  // 虚拟格开的这一刻就当门开了。
  for (int n = REAL_BOX + 1; n <= BOX_COUNT; n++) {
    Box &b = boxes[n - 1];
    if (b.state != ST_OPEN || millis() < b.closeAt) continue;
    b.state = b.item ? ST_OCCUPIED : ST_IDLE;
    b.locked = true;
    publishStatus(n);
    publishEvent(n, "door_closed", "state", stateName(b.state));
    Serial.printf("[state] %d 号格（虚拟）-> %s\n", n, stateName(b.state));
  }

  delay(50);
}
