# Hardware

For this project we need Raspberry Pi and ESP32. In  my case it was Raspberry Pi 2B and ESP32C6.
To reduce load on Raspberry Pi I installed Raspberry Pi OS Lite and used Ethernet LAN connection.
---
# Instalation

### 1. Docker 
Update the system:
```Code
sudo apt update && sudo apt upgrade -y
```
Install Docker:
```Code
curl -sSL https://get.docker.com | sh
```
Add the user to the Docker group (to avoid typing sudo each time):
```Code
sudo usermod -aG docker $USER
```
---
### 2. Project folder 

Create a project folder and copy there all from mqtt-gateway
```Code
mkdir ~/mqtt-gateway
cd ~/mqtt-gateway
git clone https://github.com/IvaShuba/Secure-SSL-TLS-IoT-Gateway-with-Docker-ESP32.git .
```
### 3. .env 
For gateway instalation you need ".env" which contain following variables:

```Code
# .env file

# === Setup MQTT (Local Mosquitto) ===
LOCAL_BROKER_IP=192.168.*.*
LOCAL_BROKER_PORT=8883 #if you prefer use secure connetion

# === Passwords ThingSpeak (Cloud) ===
THINGSPEAK_BROKER=mqtt3.thingspeak.com
THINGSPEAK_PORT=8883
THINGSPEAK_CLIENT_ID=***	#from your ThingSpeak account -> MQTT 
THINGSPEAK_USERNAME=****	#from your ThingSpeak account -> MQTT 
THINGSPEAK_PASSWORD=****	#from your ThingSpeak account -> MQTT 
THINGSPEAK_CHANNEL_ID=123456 	# 6-digit channel id(from channel options)
```

---
### 4. CA certificates
Now you need create a sertificates for your connection.
```Code
# 1. Go to the project folder
cd ~/mqtt-gateway

# 2. Create a folder for certificates (if not already present)
mkdir -p certs
cd certs

# 3. Generate a CA (Certification Authority) - you'll be asked to enter the data. You can press Enter, but for "Common Name," enter "MyIoTCA"
openssl req -new -x509 -days 3650 -extensions v3_ca -keyout ca.key -out ca.crt -nodes -subj "/CN=MyIoTCA"

# 4. Generate a server key
openssl genrsa -out server.key 2048

# 5. Generate a Certificate Signing Request (CSR) - ***Important: The Common Name must be "mosquitto" or the IP address of the Raspberry Pi***
openssl req -out server.csr -key server.key -new -nodes -subj "/CN=mosquitto"

# 6. Sign the server certificate
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out server.crt -days 3650
```
---

### 4. Build your compose
```Code
docker compose up -d --build
```
