#pragma once
#include <WiFi.h>
#include <HTTPClient.h>
#include <Update.h>

// Переменные из main.ino
extern const char* firmwareUrl;
extern const char* versionUrl;
extern const char* curFirmwareVersion;

// Публичные функции
void OTA_init();
void OTA_checkForUpdate();

// Приватные функции (но должны быть объявлены!)
String fetchLatestVersion();
void downloadAndApplyFirmware();
bool startOTAUpdate(WiFiClient* client, int contentLength);
