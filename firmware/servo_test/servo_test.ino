/*
 * 舵机台架自测 —— 和主固件分开，只为把「舵机 + 供电」这一件事验干净。
 *
 * 为什么要单独验：SG90 堵转电流约 700mA。如果它的 5V 是从 ESP32 板子上的
 * 5V 针取的，一带载就会把板子拉重启 —— 在主固件里的表现是「一开锁就复位」，
 * 跟软件 bug 长得一模一样，很难分。先在这儿把它验掉，把变量压到最少。
 *
 * 引脚和角度跟主固件保持一致，验完直接换回主固件，结论就能直接套用。
 *
 * 怎么判读（对着串口看）：
 *   舵机来回摆 + 串口稳定刷 = 正常
 *   摆的中途串口又打印一次「=== 舵机自测 启动 ===」 = 板子被拉重启了，供电不够
 *   舵机不摆、串口正常       = 信号线（GPIO 13）没接对，或舵机坏了
 *   一直抖 / 嗡嗡响但不摆     = 供电电流不够，或者没和板子共地
 */
#include <ESP32Servo.h>

const int PIN_SERVO = 13;        // 与主固件一致
const int ANGLE_LOCKED = 0;      // 与主固件一致
const int ANGLE_UNLOCKED = 90;   // 与主固件一致

Servo lockServo;

void setup() {
  Serial.begin(115200);
  delay(300);
  // 这行每出现一次 = 板子重启过一次。摆的过程中又冒出来，就是掉电重启。
  Serial.println("\n=== 舵机自测 启动 ===");
  Serial.printf("信号脚 GPIO %d，%d°(锁) <-> %d°(开)\n",
                PIN_SERVO, ANGLE_LOCKED, ANGLE_UNLOCKED);

  // 主固件就是这么接的，这里故意保持一致，别多加配置引入新变量
  lockServo.attach(PIN_SERVO);
  lockServo.write(ANGLE_LOCKED);
  delay(600);
}

void loop() {
  Serial.printf("[%lus] 锁 -> %d°\n", millis() / 1000, ANGLE_LOCKED);
  lockServo.write(ANGLE_LOCKED);
  delay(2000);

  Serial.printf("[%lus] 开 -> %d°\n", millis() / 1000, ANGLE_UNLOCKED);
  lockServo.write(ANGLE_UNLOCKED);
  delay(2000);
}
