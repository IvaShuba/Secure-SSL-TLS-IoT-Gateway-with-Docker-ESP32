from dotenv import load_dotenv
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad, pad
from Crypto.PublicKey import ECC
from Crypto.Signature import DSS
from Crypto.Hash import SHA256
import base64
import json
import os
import re
import time
import ssl
import statistics
import asyncio
import aiosqlite
import psutil
import sensor_pb2
import aiohttp
from aiohttp import web
from datetime import datetime, timezone

# Import from custom DB module
from db import (
    DB_PATH, init_db, insert_sensor_data, delete_all_readings, 
    insert_processed_data, get_and_clear_processed_data, upload_readings_to_db, 
    update_device_status, get_sensors, get_devices, get_readings
)

load_dotenv()

# Initialize DB on startup
asyncio.run(init_db())
print("[APP] startup_event: DB ready")
main_loop = asyncio.new_event_loop()

msg_count = 0 

# === 1. CONFIGURATION FROM .ENV ===
LOCAL_BROKER = os.getenv('LOCAL_BROKER_IP', 'RasPi.local')
LOCAL_PORT = int(os.getenv('LOCAL_BROKER_PORT', 8883))
LOCAL_USER = os.getenv('LOCAL_USER', 'local_user')
LOCAL_PASS = os.getenv('LOCAL_PASS', 'local_pass')
raw_key = os.getenv('LOCAL_AES_KEY', 'key_not_found')

# Remove possible quotes and spaces, then take the first 32 bytes for AES-256
AES_KEY = raw_key.strip().replace('"', '').replace("'", "").encode('utf-8')[:32]
AES_IV  = b'\x00' * 16

# AES Encryption flag for outbound /metrics responses
EN_OUT_AES = os.getenv('EN_OUT_AES', 'False').lower() in ('true', '1', 't')

# HPmini VPN config
HPMINI_BROKER = os.getenv('HPMINI_BROKER_IP', '100.108.244.12') # Tailscale IP
HPMINI_PORT = int(os.getenv('HPMINI_PORT', 8884))
HPMINI_USER = os.getenv('HPMINI_USERNAME', 'vpn_user')
HPMINI_PASS = os.getenv('HPMINI_PASSWORD', 'vpn_pass')

# HPmini Topics
TOPIC_PUBLISH = "gateway/telemetry"
TOPIC_SUBSCRIBE_LED = "gateway/commands/led"

# === 2. GLOBAL VARIABLES ===
# Buffer for median (Potentiometer)
data_buffer = []

# Buffers for devices and sensors/led/set
devices_buffer = {}
sensors_buffer = {}

# Current sensor states
current_state = {
    "switch": 0,
    "esp_status": 0,
    "led_value": 0
}

# Daily statistics
daily_stats = {
    "max": -1.0,
    "min": 10000.0,
    "last_reset": datetime.now().day
}

# ==========================================
# 3. HPMINI BROKER LOGIC (MQTTS via Tailscale)
# ==========================================

def on_hpmini_connect(client, userdata, flags, rc):
    if rc == 0:
        print("[HPMINI] Connected to HPmini VPN Broker!")
        # Subscribe to LED commands from HPmini Dashboard
        client.subscribe(TOPIC_SUBSCRIBE_LED)
        print(f"[HPMINI] Subscribed to LED control: {TOPIC_SUBSCRIBE_LED}")
        
        # Check and send cached database records upon connection
        check_db_cache_and_send()
    else:
        print(f"[HPMINI] Connection Failed code={rc}")

def on_hpmini_message(client, userdata, msg):
    # Command from HPmini Dashboard
    try:
        payload = msg.payload.decode()
        print(f"[HPMINI] Received CMD for LED: {payload}")
        
        # Forward command to local ESP32
        if local_client.is_connected():
            local_client.publish("sensors/led/set", payload)
            print(f"[LOCAL] Forwarded to ESP32: {payload}")
        else:
            print("[ERROR] Cannot forward to ESP32: Local broker disconnected")
            
    except Exception as e:
        print(f"[ERROR] processing HPmini message: {e}")

def check_db_cache_and_send():
    # Sync cached 'processed' data to HPmini
    try:
        cached_data = asyncio.run(get_and_clear_processed_data())
        if not cached_data:
            return

        print(f"[CACHE] Found {len(cached_data)} cached records. Syncing...")
        
        for record in cached_data:
            # Re-pack and publish cached data
            payload = json.dumps({
                "sensor_id": record["sensor_id"],
                "median_val": record["avr_value"],
                "timestamp": record["timestamp"],
                "status": "Cached Data"
            })
            hpmini_client.publish(TOPIC_PUBLISH, payload)
            print(f"[CACHE] Synced record: {payload}")
            
    except Exception as e:
        print(f"[ERROR] Cache Sync: {e}")

# Setup HPmini Client (MQTTS)
hpmini_client = mqtt.Client(client_id="Gateway_VPN_Client")
hpmini_client.on_connect = on_hpmini_connect
hpmini_client.on_message = on_hpmini_message

hpmini_client.will_set(TOPIC_PUBLISH, payload=json.dumps({"status": "Gateway Offline"}), qos=1, retain=True)

# ==========================================
# 4. LOCAL BROKER LOGIC (MQTT)
# ==========================================

def on_local_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:    
        print(f"[LOCAL] Connected to Mosquitto. Code: {reason_code}")
        client.subscribe("sensors/#")
        client.subscribe("status/#")
    else:
        print(f"[LOCAL] Connection failed with code: {reason_code}")

def on_local_message(client, userdata, msg):
    try:
        # We safely push our async task from the MQTT thread to the main asyncio thread
        asyncio.run_coroutine_threadsafe(
            async_process_message(client, userdata, msg), 
            main_loop
        )
    except Exception as e:
        print(f"[ERROR] Failed to schedule task: {e}")
    
def decrypt_payload(payload_b64):
    try:
        # 1. Декодируем из Base64 в сырые байты
        # Это гарантирует, что мы получим ровно 16, 32 и т.д. байт
        encrypted_data = base64.b64decode(payload_b64)
        
        # 2. Проверка на кратность 16 (защита от мусора)
        if len(encrypted_data) % 16 != 0:
            return None

        cipher = AES.new(AES_KEY, AES.MODE_CBC, iv=b'\x00'*16)
        decrypted_bytes = cipher.decrypt(encrypted_data)
        
        # 3. Декодируем текст и чистим мусор
        decrypted_text = decrypted_bytes.decode('utf-8', errors='ignore').rstrip('\x00')
        clean_text = re.sub(r'[^a-zA-Z0-9\.\-\:]', '', decrypted_text)
        
        return clean_text
    except Exception as e:
        print(f"[CRYPTO ERROR] Decryption failed: {e}")
        return None
        
def process_incoming_float(encrypted_msg):
    # 1. Decrypt payload
    decrypted_str = decrypt_payload(encrypted_msg)
    
    if decrypted_str:
        try:
            # 2. Convert string back to float
            float_value = float(decrypted_str)
            return float_value
        except ValueError:
            print(f"[ERROR] Decrypted string '{decrypted_str}' is not a number")
    return None
    
async def async_process_message(client, userdata, msg): 
    try:
        topic_parts = msg.topic.split('/')
        if not devices_buffer:
            await update_buffers() 
    
        if len(topic_parts) == 3 and topic_parts[0] == "sensors":
            device_name = topic_parts[1]
            sensor_type = topic_parts[2]
            try:
                # decoding
                raw_data = msg.payload 
                decrypted_text = decrypt_payload(raw_data)
                if decrypted_text is None:
                    return # Stop if decryption failed
                decrypted_payload = float(decrypted_text)
                
            except Exception as e:
                print(f"[ERROR] Failed to decoding message in sensor topic: {e}")
            
            print(f"[DATA] Device: {device_name} | Sensor: {sensor_type} | Value: {decrypted_payload}")
            
            if device_name not in devices_buffer:
                print(f"[DB] New device detected: {device_name}. Adding to DB...")
                device_id = await add_new_device(device_name)
            else:
                device_id = devices_buffer[device_name]    
            
            sensor_key = (device_id, sensor_type)
    
            if sensor_key not in sensors_buffer:
                print(f"[DB] New sensor '{sensor_type}' for device {device_name}. Adding to DB...")
                sensor_id = await add_new_sensor(device_id, sensor_type)
            else:
                sensor_id = sensors_buffer[sensor_key]
            
            # 3. Now we have sensor_id, we can insert reading safely
            #print(f"[OK] Ready to save data for Sensor ID: {sensor_id} with Value: {decrypted_payload}")
            global data_buffer
            timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            data_buffer.append((sensor_id, decrypted_payload, timestamp))
            
            # 4. Check if buffer is full
            if len(data_buffer) >= 100:
                print("[BUFFER] Buffer is full! Flushing to DB...")
                
                # Copy current buffer and clear the global one 
                # to prevent race conditions during async operations
                batch_to_save = list(data_buffer)
                data_buffer.clear()
                
                # Save to DB asynchronously
                await upload_readings_to_db(batch_to_save)
            
        # Node Status
        elif len(topic_parts) == 2 and topic_parts[0] == "status":
            device_name = topic_parts[1]
            raw_payload = msg.payload.decode('utf-8').strip()
            decrypted_payload = decrypt_payload(raw_payload)
            
            print(f"[WARN] Device: {device_name} | Status: {decrypted_payload}")
            
            if not devices_buffer:
                await update_buffers() # BUGFIX: Added missing brackets
            
            if device_name not in devices_buffer:
                print(f"[DB] New device detected: {device_name}. Adding to DB...")
                device_id = await add_new_device(device_name)
            else:
                device_id = devices_buffer[device_name] 
            
            # Update status in db
            await update_device_status(device_id, decrypted_payload)
        
        else:
            print(f"[MSG] Received on {msg.topic}: {msg.payload.decode()}")

        global msg_count 
        msg_count += 1 # Increment message count for secure logging

    except Exception as e:
        print(f"[ERROR] Failed to process message in async task: {e}")

def update_min_max(val):
    # Update daily stats
    global daily_stats
    current_day = datetime.now().day
    
    if current_day != daily_stats["last_reset"]:
        daily_stats = {"max": val, "min": val, "last_reset": current_day}
        print("[STATS] New Day Reset")
    else:
        if val > daily_stats["max"]: daily_stats["max"] = val
        if val < daily_stats["min"]: daily_stats["min"] = val

local_client = mqtt.Client(CallbackAPIVersion.VERSION2,"Gateway_Logic_PasPi")
local_client.username_pw_set(LOCAL_USER, LOCAL_PASS)
local_client.tls_set(ca_certs="/app/certs/ca.crt")
local_client.tls_insecure_set(True)
local_client.on_connect = on_local_connect
local_client.on_message = on_local_message

# ==========================================
# 5. DEVICES AND SENSORS (MQTT)
# ==========================================
async def update_buffers():   
    global devices_buffer, sensors_buffer
    
    # We call async functions directly with await instead of asyncio.run()
    devices_buffer = await get_devices()
    sensors_buffer = await get_sensors()
    print("[DB] Buffers successfully updated in memory.")

async def add_new_device(device_name):
    global devices_buffer
    
    async with aiosqlite.connect(DB_PATH) as db:
        # Insert the new device into the database
        cursor = await db.execute(
            "INSERT INTO devices (device_name, device_status) VALUES (?, ?)",
            (device_name, "online")
        )
        await db.commit()
        
        # Get the actual auto-incremented ID generated by SQLite
        new_device_id = cursor.lastrowid
        
        # Update our in-memory cache with the REAL database ID
        devices_buffer[device_name] = new_device_id
        
        print(f"[DB] Registered new device: {device_name} with ID: {new_device_id}")
        return new_device_id

async def add_new_sensor(device_id, sensor_type):
    global sensors_buffer
    
    async with aiosqlite.connect(DB_PATH) as db:
        # Insert the new sensor linked to the device_id
        cursor = await db.execute(
            "INSERT INTO sensors (device_id, sensor_type) VALUES (?, ?)",
            (device_id, sensor_type)
        )
        await db.commit()
        
        # Get the actual auto-incremented ID for the sensor
        new_sensor_id = cursor.lastrowid
        
        # Create the composite key for our sensors dictionary
        sensor_key = (device_id, sensor_type)
        sensors_buffer[sensor_key] = new_sensor_id
        
        print(f"[DB] Registered sensor '{sensor_type}' for device ID {device_id} as Sensor ID: {new_sensor_id}")
        return new_sensor_id
    
# ==========================================
# 6. MAIN LOOP (PROCESS AND SEND)
# ==========================================

async def sender_task():
    """Background loop to send data every XX times by 60 seconds"""
    INTERVAL = 60 * 0.5
    print(f"[SYSTEM] Background sender started. Interval: {INTERVAL}s")
    
    while True:
        try:
            # Instead of time.sleep(1), we use async sleep
            await asyncio.sleep(INTERVAL) 
            await process_and_send()
        except Exception as e:
            print(f"[ERROR] Sender task error: {e}")

async def process_and_send():
    global data_buffer
    try:
        reading_data = await get_readings()
        if not reading_data:
            return        
    
        if hpmini_client.is_connected():
            # Create a Protobuf Batch object
            batch = sensor_pb2.Batch()
            
            # Fill readings from DB
            for record in reading_data:
                # Assuming record is a dict or tuple from your DB logic
                r = batch.readings.add()
                r.device = str(record.get("device_id", "gw")) # Map your IDs to names
                r.sensor = record.get("sensor_type", "data")
                r.value = float(record.get("value", 0))
                # Convert your timestamp string to int64 ms if needed
                r.ts = int(time.time() * 1000) 

            # Serialize to binary format
            protobuf_payload = batch.SerializeToString()
            print(protobuf_payload)
            # Publish as bytes, not JSON string
            hpmini_client.publish(TOPIC_PUBLISH, protobuf_payload)
            print(f"[HPMINI] Sent Protobuf batch, size: {len(protobuf_payload)} bytes")
            
            await delete_all_readings()
    except Exception as e:
        print(f"[ERROR] Protobuf send failed: {e}")


# ==========================================
# 7. METRICS & WEB SERVER (Prometheus Support)
# ==========================================
async def metrics_handler(request):
    """Handle Prometheus scraping request on /metrics via HTTPS"""
    readings = []
    print("[DEBUG] Metrics handler run")
    try:
        # Fetch current db rows, expecting dict-like items or tuples
        raw_data = await get_readings() 
        
        # Inverse lookup maps
        inv_devices = {v: k for k, v in devices_buffer.items()}
        inv_sensors = {v: k for k, v in sensors_buffer.items()} # returns tuple (device_id, type)
        
        if raw_data:
            for row in raw_data:
                # Handle either dict from DB module or tuple payload
                if isinstance(row, dict):
                    s_id = row.get("sensor_id")
                    val = row.get("value", 0.0)
                    ts_str = row.get("timestamp")
                else:
                    s_id, val, ts_str = row[0], row[1], row[2]
                
                # Retrieve names from IDs
                dev_id, s_type = inv_sensors.get(s_id, (None, "unknown"))
                dev_name = inv_devices.get(dev_id, "unknown")
                
                # Convert ISO string to UNIX ms
                try:
                    dt = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
                    dt = dt.replace(tzinfo=timezone.utc)
                    ts_ms = int(dt.timestamp() * 1000)
                except Exception:
                    ts_ms = int(time.time() * 1000)

                readings.append({
                    "device": dev_name,
                    "sensor": s_type,
                    "units": "raw", # Fallback since units aren't provided by MQTT
                    "value": val,
                    "ts": ts_ms
                })
        
        
    except Exception as e:
        print(f"[ERROR] Generating metrics: {e}")
    
    #print(f"[METRICS] Prepared \n{(readings)} \n readings for Prometheus")

    # Build structure
    response_dict = {
        "readings": readings,
        "gateway": {
            "gateway_id": "raspi-gw",
            "cpu": psutil.cpu_percent(),
            "ram": psutil.virtual_memory().percent,
            "uptime": time.time() - psutil.boot_time(),
            "ts": int(time.time() * 1000)
        }
    }

    # Format JSON
    response_payload = json.dumps(response_dict)
    #print(response_payload)
    
    # Optional AES256 Encryption based on EN_OUT_AES flag
    if EN_OUT_AES:
        try:
            cipher = AES.new(AES_KEY, AES.MODE_CBC, iv=AES_IV)
            padded_data = pad(response_payload.encode('utf-8'), AES.block_size)
            encrypted_payload = cipher.encrypt(padded_data)
            # Encode base64 to safely transmit via HTTP
            response_payload = base64.b64encode(encrypted_payload).decode('utf-8')
            #print(response_payload)
        except Exception as e:
            print(f"[ERROR] Failed to encrypt metrics payload: {e}")
            return web.Response(status=500, text="Encryption Error")
    
    return web.Response(text=response_payload, content_type="application/json")


async def start_https_server():
    """Start the aiohttp server bound to TLS"""
    print("[DEBUG] start_https_server")
    app = web.Application()
    app.router.add_get('/metrics', metrics_handler)
    
    # Configure TLS
    ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    try:
        # Adjust these paths to your actual certificate locations
        ssl_context.load_cert_chain('/app/certs/gateway.crt', '/app/certs/gateway.key')
    except Exception as e:
        print(f"[WARN] Could not load SSL certificates for WebServer. Starting HTTP instead: {e}")
        ssl_context = None

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8443, ssl_context=ssl_context)
    await site.start()
    print(f"[WEB] Prometheus Server listening on port 8443 (TLS: {ssl_context is not None})")

# ==========================================
# 8. SECURE LOG AND PUSH TO DB ON SERVER
# ==========================================

# Load the gateway's private key at startup
with open('/app/certs/gw_private.pem', 'rt') as f:
    gw_priv_key = ECC.import_key(f.read())

async def secure_log_loop():
    """Example loop to send secure logs every 5 minutes"""
    LOGINTERVAL = 30
    print(f"[SYSTEM] Secure log loop started. Interval: {LOGINTERVAL}s")
    
    
    while True:
        await asyncio.sleep(LOGINTERVAL)
        global msg_count
        await send_secure_report(msg_count)
        msg_count = 0 # Increment example metric for demonstration

async def send_secure_report(msg_count):
    # 1. Create the batch and fill data
    batch = sensor_pb2.Batch()
    batch.gateway.gateway_id = "raspi-gw"
    batch.gateway.ts = int(time.time() * 1000)
    
    r = batch.readings.add()
    r.device = "gateway"
    r.sensor = "msg_processed_total"
    r.value = float(msg_count)

    # 2. Serialize the payload WITHOUT the signature
    payload_to_sign = batch.SerializeToString()

    # 3. Create SHA256 hash and sign it with ECDSA
    h = SHA256.new(payload_to_sign)
    signer = DSS.new(gw_priv_key, 'fips-186-3')
    signature = signer.sign(h)

    # 4. Attach signature to the batch
    # Make sure 'bytes signature = X;' is defined in your sensor.proto
    batch.signature = signature

    # 5. Send over HTTPS to the Server
    # Use CA cert to verify the server we are sending data to
    ssl_context = ssl.create_default_context(cafile='/app/certs/ca.crt')
    # Use False if IP mismatch in Tailscale persists
    ssl_context.check_hostname = False 
    
    async with aiohttp.ClientSession() as session:
        try:
            # Send the final serialized batch (Data + Signature)
            await session.post(
                f'https://{HPMINI_BROKER}:8443/secure-ingest',
                data=batch.SerializeToString(),
                ssl=ssl_context
            )
            print("[INFO] Secure log sent to DB")
        except Exception as e:
            print(f"[ERROR] Failed to send secure log: {e}")


# ==========================================
# 9. CONNECTION & BOOT
# ==========================================

def setup_resilient_client(client, broker, port):
    """Configures auto-reconnect and async connection"""
    # Set reconnection delays: start at 1s, max 120s
    client.reconnect_delay_set(min_delay=1, max_delay=120)
    
    try:
        # connect_async doesn't block and will keep trying in background
        client.connect_async(broker, port, 60)
        # We still need loop_start() to handle the background reconnection
        client.loop_start()
        print(f"[SYSTEM] Connection queued for {broker}:{port}")
    except Exception as e:
        print(f"[CRITICAL] Could not queue connection for {broker}: {e}")

if __name__ == "__main__":
    print("Starting Gateway VPN Edition...")
    
    try:
        local_client.connect(LOCAL_BROKER, LOCAL_PORT, 60)
        local_client.loop_start()
    except Exception as e:
        print(f"[ERROR] ESP32 Connect: {e}")
        
    # Set up the loop
    asyncio.set_event_loop(main_loop)
    
    # SCHEDULE tasks BEFORE starting run_forever
    main_loop.create_task(sender_task())
    main_loop.create_task(start_https_server())
    main_loop.create_task(secure_log_loop()) 

    try:
        main_loop.run_forever()
    except KeyboardInterrupt:
        print("[SYSTEM] Stopping...")
        
        local_client.loop_stop()
        hpmini_client.loop_stop()
        main_loop.stop()