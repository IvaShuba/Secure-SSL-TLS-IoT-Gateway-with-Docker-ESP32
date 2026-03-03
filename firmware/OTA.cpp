#include "OTA.h"

void OTA_init() {
  Serial.println("OTA module initialized");
  OTA_checkForUpdate();
}

void OTA_checkForUpdate() {
  Serial.println("Checking for firmware update...");

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi not connected");
    return;
  }

  String latestVersion = fetchLatestVersion();
  if (latestVersion == "") {
    Serial.println("Failed to fetch latest version");
    return;
  }

  Serial.println("Current Firmware Version: " + String(curFirmwareVersion));
  Serial.println("Latest Firmware Version: " + latestVersion);

  if (latestVersion != curFirmwareVersion) {
    Serial.println("New firmware available. Starting OTA update...");
    downloadAndApplyFirmware();
  } else {
    Serial.println("Device is up to date.");
  }
}

String fetchLatestVersion() {
  HTTPClient http;
  http.begin(versionUrl);

  int httpCode = http.GET();
  if (httpCode == HTTP_CODE_OK) {
    String latestVersion = http.getString();
    latestVersion.trim();
    http.end();
    return latestVersion;
  }

  http.end();
  return "";
}

void downloadAndApplyFirmware() {
  HTTPClient http;
  http.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);
  http.begin(firmwareUrl);

  int httpCode = http.GET();
  if (httpCode != HTTP_CODE_OK) {
    Serial.printf("Failed to fetch firmware. HTTP code: %d\n", httpCode);
    return;
  }

  int contentLength = http.getSize();
  WiFiClient* stream = http.getStreamPtr();

  if (startOTAUpdate(stream, contentLength)) {
    Serial.println("OTA update successful, restarting...");
    delay(2000);
    ESP.restart();
  }

  http.end();
}

bool startOTAUpdate(WiFiClient* client, int contentLength) {
  if (!Update.begin(contentLength)) {
    Serial.printf("Update begin failed: %s\n", Update.errorString());
    return false;
  }

  size_t written = Update.writeStream(*client);
  if (written != contentLength) {
    Serial.println("Write failed");
    return false;
  }

  if (!Update.end()) {
    Serial.printf("Update end failed: %s\n", Update.errorString());
    return false;
  }

  return true;
}
