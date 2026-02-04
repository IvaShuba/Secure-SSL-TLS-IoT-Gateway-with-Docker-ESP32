# Secure SSL/TLS IoT Gateway: ESP32 to Cloud via Docker

## 🚀 Overview
This project implements a robust, secure, and fault-tolerant IoT system. It bridges low-power edge devices (ESP32) with cloud analytics (ThingSpeak) using a local Gateway (Raspberry Pi). Unlike basic IoT demos, this project focuses on Enterprise-grade security (TLS/SSL encryption), Containerization (Docker), and Reliability (Offline data buffering).

---

## 🏗 System Architecture
The system is designed as a three-layer architecture: **Edge**, **Fog (Gateway)**, and **Cloud**.

graph LR
    A[ESP32 Sensor]
    B(Mosquitto Broker)
    C[Python Gateway]
    D((ThingSpeak Cloud))
    E[(Offline Buffer)]

    A -- MQTT/TLS --> B
    B --> C
    C -- HTTPS --> D
    C -- Save JSON --> E


### **Edge Layer**
- ESP32 collects analog sensor data  
- Controls actuators via MQTT  

### **Gateway Layer**
A Raspberry Pi running a Dockerized stack (Mosquitto Broker + Python Controller) orchestrates traffic, buffers data during network outages, and performs protocol conversion.

### **Cloud Layer**
ThingSpeak provides data visualization and historical logging.

---

## 🔒 Communication & Security

Security was the primary focus of this implementation.  
The system does **not** transmit plain text data.

- **Protocol:** MQTT v3.1.1 over TLS 1.2 (Port 8883)  
- **Certificate Management:** Custom PKI (Public Key Infrastructure) with a self-signed Root CA  
- **Server Validation:** ESP32 strictly validates the Gateway's identity using the Root CA certificate  
- **Time Synchronization:** NTP ensures certificate validity periods are checked correctly  
  (prevents replay attacks or expired certificate usage)

---

## ⭐ Key Features

- **Full Dockerization:** All gateway services (Broker, Logic, OTA Server) run in isolated containers using Docker Compose  
- **Offline Buffering:** Gateway caches sensor data locally (JSON buffer) and bulk-uploads it when internet returns  
- **Bi-directional Control:** Real-time LED brightness control via Cloud Dashboard  
- **OTA Ready:** Infrastructure prepared for Over-The-Air firmware updates via a dedicated HTTP server container  

---

## 🐳 Containerization & Deployment

Instead of "State Machine Logic" (which is simple here), the focus is on DevOps aspects.  
The gateway services are defined in `docker-compose.yml`:

- **mqtt_broker:** Eclipse Mosquitto with strict ACLs and SSL listeners  
- **gateway_logic:** Python 3.9 script using paho-mqtt and requests; handles business logic and buffering  
- **ota_server:** Lightweight HTTP server for serving binary firmware files  

---

## 🔧 Hardware Highlights

- **Microcontroller:** ESP32-WROOM-32 (NodeMCU)  
- **Gateway:** Raspberry Pi 4 Model B (Raspberry Pi OS Lite)  
- **Sensors:** Rotary Potentiometer (10k), Tactile Switch  
- **Actuators:** LED (PWM controlled)  

---

## 📄 Example Log Output

Proof of successful TLS Handshake and Time Sync:
```Logs
[WiFi] Connected! IP: 192.168.0.163
Setting time using NTP...
Current time: Sat Feb 04 14:35:10 2026

[MQTT] Connecting to Gateway at 192.168.0.162... 
[SSL] Verifying Server Certificate... OK
[MQTT] Connected! SUCCESS!

Sent: Pot=1841, Switch=1
[MQTT] Message arrived [sensors/led/set]: 80
[LED] Set to: 80
```

---

## 🎓 Skills Demonstrated
- **Embedded C++:** Async MQTT, Hardware Interrupts, PWM, WiFi Security.
- **Network Security:** SSL/TLS Certificates generation (OpenSSL), Chain of Trust.
- **DevOps:** Docker, Docker Compose, Linux Networking (iptables resolution).
- **Backend Development:** Python scripting for asynchronous I/O and API integration.


---

## 📜 License
This project is licensed under the MIT License - see the LICENSE file for details.
