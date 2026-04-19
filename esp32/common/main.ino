#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <PubSubClient.h>
#include "time.h"
#include "config.h"
#include <HTTPUpdate.h>
#include <ESPmDNS.h>
#include "OTA.h"
#include "AESLib.h"


// WIFI setup
const char* ssid = SECRET_SSID;
const char* password = SECRET_PASS;

AESLib aes;
byte key[] = SECRET_AES;


// Raspberry Pi MQTT
const char* mqttServer = "RasPi.local";
const int mqttPort = 8883;  //  SSL port
const char* rootCACertificate = SECRET_CERT;
const char* mqttNodeName = SECRET_NODE_NAME;
const char* mqttUserName = SECRET_USERNAME;
const char* mqttUserPass = SECRET_UPASSWORD;


// ===== PINS =====
const int ledPin = 21;
const int analogPin = 5;
const int digitalPin = 19;

// ===== MQTT =====
WiFiClientSecure secureClient;
PubSubClient mqttClient(secureClient);

//----- firmware version and updates
const char* firmwareUrl = SECRET_FIRMWARE;
const char* versionUrl = SECRET_VERSION;

const char* curFirmwareVersion = "0.4";
const unsigned long updateCheckInterval = 1 * 60 * 1000;  //once a hour
unsigned long lstUdateCheck = 0;

// ===== incouming msgs from RASPBERRY PI =====
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

// ===== msg encryption =====
String encryptPayload(String msg) {
  uint16_t msgLen = msg.length();
  
  // buffer
  char encryptedBuffer[256]; 
  byte aes_iv[16] = {0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0};
  // clean it
  memset(encryptedBuffer, 0, sizeof(encryptedBuffer));
  // encrypt
  aes.encrypt((byte*)msg.c_str(), msgLen, (byte*)encryptedBuffer, key, 256, aes_iv);
  //return 
  return String(encryptedBuffer);
}

// ===== reconnect =====
void reconnect() {
  while (!mqttClient.connected()) {
    Serial.print("[MQTT] Connecting to Gateway at ");
    Serial.print(mqttServer);
    Serial.print("... ");

    // connection to Raspi MQTT
    String encryptedOffline = encryptPayload("Offline");
    String encryptedOnline = encryptPayload("Online");
    char will_topic[40];
    snprintf(will_topic, sizeof(will_topic), "status/%s", mqttNodeName);
    if (mqttClient.connect(mqttNodeName,mqttUserName, mqttUserPass, will_topic, 1, true, encryptedOffline.c_str())) {
      Serial.println("SUCCESS!");
      mqttClient.publish(will_topic, encryptedOnline.c_str(), true);
      // Subscribe to some topics?
      //mqttClient.subscribe("sensors/led/set");
      //Serial.println("[MQTT] Subscribed to sensors/led/set");
    } else {
      Serial.print("FAILED, rc=");
      Serial.print(mqttClient.state());
      Serial.println(" (try again in 5 seconds)");

      // Debug error
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
  ledcAttach(ledPin, 5000, 8);
  ledcWrite(ledPin, 0);

  // sensors init
  pinMode(analogPin, INPUT);
  pinMode(digitalPin, INPUT);

  // connect Wi-Fi
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

  IPAddress mqttIP;
  if (MDNS.queryHost("RasPi", mqttIP)) {
    Serial.print("[mDNS] Resolved: ");
    Serial.println(mqttIP);
    mqttClient.setServer(mqttIP, mqttPort);
  } else {
    Serial.println("[mDNS] Resolution failed, using fallback IP");
    mqttClient.setServer("192.168.0.162", mqttPort);  // fallback
  }



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
    OTA_checkForUpdate();  // ← update check
  }

  if (!mqttClient.connected()) {
    reconnect();
  }
  mqttClient.loop();

  // data push every 5 sec
  static unsigned long lastMsg = 0;
  unsigned long now = millis();

  if (now - lastMsg > 5000) {
    lastMsg = now;
    int analogValue = analogReadMilliVolts(analogPin);
    int digitalValue = digitalRead(digitalPin);
    float tempValue = (analogValue - 500) / 10;
    char analog_topic[35];
    snprintf(analog_topic, sizeof(analog_topic), "sensors/%s/temperature", mqttNodeName);

    String enc_analog = encryptPayload(String(tempValue,2));
    mqttClient.publish(analog_topic, enc_analog.c_str());

    char digital_topic[35];
    snprintf(digital_topic, sizeof(digital_topic), "sensors/%s/digital", mqttNodeName);
    String enc_digital = encryptPayload(String(digitalValue));
    mqttClient.publish(digital_topic, String(enc_digital).c_str());

    // serial notify
    Serial.printf("Sent: temperature=%f, digital=%d\n", tempValue, digitalValue);
  }
}