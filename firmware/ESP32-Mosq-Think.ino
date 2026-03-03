#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <PubSubClient.h>
#include "time.h"
#include "config.h"
#include <HTTPUpdate.h>
#include "OTA.h"

// WIFI setup
const char* ssid = SECRET_SSID;
const char* password = SECRET_PASS;

// Raspberry Pi MQTT
const char* mqttServer = "192.168.0.162";
const int mqttPort = 8883;  // Порт для SSL
const char* rootCACertificate = SECRET_CERT;

// ===== PINS =====
const int ledPin = 21;
const int potPin = 5;
const int switchPin = 19;

// ===== MQTT =====
WiFiClientSecure secureClient;
PubSubClient mqttClient(secureClient);

//----- firmware version and updates
const char* firmwareUrl = SECRET_FIRMWARE;
const char* versionUrl = SECRET_VERSION;

const char* curFirmwareVersion = "0.3";
const unsigned long updateCheckInterval = 1 * 60 * 1000; //once a hour
unsigned long lstUdateCheck = 0;

// ===== ОБРАБОТКА СООБЩЕНИЙ ОТ RASPBERRY PI =====
void callback(char* topic, byte* payload, unsigned int length) {
  String message = "";
  for (int i = 0; i < length; i++) {
    message += (char)payload[i];
  }
  Serial.print("[MQTT] Message arrived [");
  Serial.print(topic);
  Serial.print("]: ");
  Serial.println(message);

  // get led val from Raspberry Pi from ThingSpeak
  if (String(topic) == "sensors/led/set") {
    int brightness = message.toInt();
    // range 0-255
    if (brightness < 0) brightness = 0;
    if (brightness > 255) brightness = 255;

    ledcWrite(ledPin, brightness);
    Serial.printf("[LED] Set to: %d\n", brightness);
  }
}

// ===== ПОДКЛЮЧЕНИЕ =====
void reconnect() {
  while (!mqttClient.connected()) {
    Serial.print("[MQTT] Connecting to Gateway at ");
    Serial.print(mqttServer);
    Serial.print("... ");

    // connection to Raspi MQTT
    if (mqttClient.connect("ESP32_ID", "status/esp32", 1, true, "Offline")) {
      Serial.println("SUCCESS!");
      mqttClient.publish("status/esp32", "Online", true);
      // Subscribe to LED update
      mqttClient.subscribe("sensors/led/set");
      Serial.println("[MQTT] Subscribed to sensors/led/set");
    } else {
      Serial.print("FAILED, rc=");
      Serial.print(mqttClient.state());
      Serial.println(" (try again in 5 seconds)");

      // Расшифровка ошибок для удобства
      if (mqttClient.state() == -2) Serial.println("Reason: Network unreachable (Check IP / Firewall on Pi)");

      delay(5000);
    }
  }
}

void setup() {
  Serial.begin(115200);
  delay(1000);

  // init LED
  pinMode(ledPin, OUTPUT);
  // Для ESP32 Arduino Core 3.0+:
  ledcAttach(ledPin, 5000, 8);
  ledcWrite(ledPin, 0);

  // sensors init
  pinMode(potPin, INPUT);
  pinMode(switchPin, INPUT);  // Или INPUT_PULLUP, если нужно

  // Подключение к Wi-Fi
  Serial.print("\n[WiFi] Connecting to ");
  Serial.println(ssid);
  WiFi.begin(ssid, password);

  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("\n[WiFi] Connected!");
  Serial.print("IP: ");
  Serial.println(WiFi.localIP());
/*
  // --- TIME CHECK ---
  Serial.print("Setting time using NTP");
  configTime(0, 0, "pool.ntp.org");  // Синхронизация с интернетом
  time_t now = time(nullptr);
  while (now < 24 * 3600) {  // Ждем, пока время не станет больше 1970 года
    Serial.print(".");
    delay(100);
    now = time(nullptr);
  }
  struct tm timeinfo;
  gmtime_r(&now, &timeinfo);
  Serial.print("\nCurrent time: ");
  Serial.println(asctime(&timeinfo));
  // --------------------------------
*/

  // === init SSL ===
  secureClient.setCACert(rootCACertificate);
  //secureClient.setInsecure();
  secureClient.setHandshakeTimeout(30);

  mqttClient.setServer(mqttServer, mqttPort);
  mqttClient.setCallback(callback);


  // init OTA + check
  OTA_init();
}

void loop() {
  if (millis() - lstUdateCheck > updateCheckInterval) { 
    lstUdateCheck = millis(); 
    OTA_checkForUpdate(); // ← вызываем проверку обновлений 
  }

  if (!mqttClient.connected()) {
    reconnect();
  }
  mqttClient.loop();

  // Отправляем данные каждые 2 секунды (в локальной сети можно часто)
  static unsigned long lastMsg = 0;
  unsigned long now = millis();

  if (now - lastMsg > 5000) {
    lastMsg = now;

    int potValue = analogRead(potPin);
    int switchValue = digitalRead(switchPin);

    // Публикуем в топики, которые слушает Raspberry Pi
    mqttClient.publish("sensors/pot/data", String(potValue).c_str());
    mqttClient.publish("sensors/switch/data", String(switchValue).c_str());

    // Для отладки в мониторе
    Serial.printf("Sent: Pot=%d, Switch=%d\n", potValue, switchValue);
  }
}