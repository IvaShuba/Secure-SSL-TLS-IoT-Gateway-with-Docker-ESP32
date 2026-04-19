from dotenv import load_dotenv
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
import base64
import json
import os
import re
import time
import ssl
import statistics
import asyncio
import aiosqlite
from datetime import datetime, timezone
from db import (
    DB_PATH,
    init_db,
    insert_sensor_data,
    delete_all_readings,
    insert_processed_data,
    get_and_clear_processed_data,
    upload_readings_to_db,
    update_device_status,
    get_sensors,
    get_devices,
    get_readings
)
load_dotenv()
# print(f"DEBUG: Loaded user from ENV is: {os.getenv('LOCAL_USER')}")

# Initialize DB on startup
asyncio.run(init_db())
print("[APP] startup_event: DB ready")
main_loop = asyncio.new_event_loop()

# === 1. CONFIGURATION FROM .ENV ===
LOCAL_BROKER = os.getenv('LOCAL_BROKER_IP', 'RasPi.local')
LOCAL_PORT = int(os.getenv('LOCAL_BROKER_PORT', 8883))
LOCAL_USER = os.getenv('LOCAL_USER', 'local_user')
LOCAL_PASS = os.getenv('LOCAL_PASS', 'local_pass')
raw_key = os.getenv('LOCAL_AES_KEY', 'key_not_found')

# Убираем возможные кавычки и пробелы, затем берем первые 32 байта
AES_KEY = raw_key.strip().replace('"', '').replace("'", "").encode('utf-8')[:32]

# print(f"[DEBUG] AES Key length: {len(AES_KEY)} bytes")

# print(f"DEBUG: Loaded user from ENV is: {AES_KEY}")


AES_IV  = b'\x00' * 16

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

#buffer for devices and for sensors/led/set
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
#hpmini_client.username_pw_set(HPMINI_USER, HPMINI_PASS)
hpmini_client.on_connect = on_hpmini_connect
hpmini_client.on_message = on_hpmini_message
# Basic TLS config for MQTTS (port 8884)
#hpmini_client.tls_set(tls_version=ssl.PROTOCOL_TLS)

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
    
def decrypt_payload(payload_data):
    try:
        cipher = AES.new(AES_KEY, AES.MODE_CBC, iv=AES_IV)
        
        # Decrypt the 16-byte block
        decrypted_bytes = cipher.decrypt(payload_data)
        
        # DEBUG: Let's see what's inside before unpadding
        # print(f"[DEBUG] Decrypted bytes: {decrypted_bytes}")
        
        # Manual Zero-Padding removal:
        # 1. Decode to string (ignore errors to see at least something)
        # 2. rstrip('\x00') removes all trailing null bytes
        # 3. strip() removes any accidental spaces or newlines
        raw_text = decrypted_bytes.decode('utf-8', errors='ignore')
        
        # 2. Оставляем только числа, точки, знаки и буквы (для Online/Offline)
        # Этот паттерн уберет весь мусор вроде \xa8
        clean_text = re.sub(r'[^a-zA-Z0-9\.\-\:]', '', raw_text)
        # print(f"[DEBUG] clean_text: {clean_text}")
        return clean_text
    except Exception as e:
        print(f"[CRYPTO ERROR] Decryption failed: {e}")
        return None
        
def process_incoming_float(encrypted_msg):
    # 1. Расшифровываем (функция decrypt_payload из прошлых сообщений)
    decrypted_str = decrypt_payload(encrypted_msg)
    
    if decrypted_str:
        try:
            # 2. Конвертируем строку обратно во float
            float_value = float(decrypted_str)
            return float_value
        except ValueError:
            print(f"[ERROR] Decrypted string '{decrypted_str}' is not a number")
    return None
    
async def async_process_message(client, userdata, msg): 
    # print(f"[DEBUG] Received topic: {msg.topic}")
    # print(f"[DEBUG] Received payload: {msg.payload}")
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
                # print(f"[DEBUG] Raw bytes length: {len(raw_data)}")
                decrypted_text = decrypt_payload(raw_data)
                if decrypted_text is None:
                    return # Stop if decryption failed
                # raw_payload = msg.payload.decode('utf-8').strip()
                decrypted_payload = float(decrypted_text)
                
                
                
            except Exception as e:
                print(f"[ERROR] Failed to decoding message in sensor topic: {e}")
            
            print(f"[DATA] Device: {device_name} | Sensor: {sensor_type} | Value: {decrypted_payload}")
            
            # if not devices_buffer:
                # await update_buffers
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
            print(f"[OK] Ready to save data for Sensor ID: {sensor_id} with Value: {decrypted_payload}")
            global data_buffer
            timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            data_buffer.append((sensor_id, decrypted_payload, timestamp))
            # print(f"[BUFFER] Added reading. Current size: {len(data_buffer)}/100")
            
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
                await update_buffers
            
            if device_name not in devices_buffer:
                print(f"[DB] New device detected: {device_name}. Adding to DB...")
                device_id = await add_new_device(device_name)
            else:
                device_id = devices_buffer[device_name] 
            # Update status in db
            await update_device_status(device_id, decrypted_payload )
        
        else:
            print(f"[MSG] Received on {msg.topic}: {msg.payload.decode()}")
        
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
#local_client.tls_set(tls_version=ssl.PROTOCOL_TLS)

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
            # This allows the loop to process MQTT messages while waiting
            await asyncio.sleep(INTERVAL) 
            
            # Call your function (make sure it's also async or wrap it)
            # If process_and_send is a regular function:
            # process_and_send()
            # If it's async:
            await process_and_send()
            
        except Exception as e:
            print(f"[ERROR] Sender task error: {e}")


async def process_and_send():
    global data_buffer
    
    # if not data_buffer:
        # median_val = 0
        # return
    # else:
        # median_val = statistics.median(data_buffer)
    
    # JSON payload structure for new HPmini Dashboard
    
    
    # payload_dict = {
        # "median_val": median_val,
        # "switch": current_state['switch'],
        # "esp_status": current_state['esp_status'],
        # "min_val": daily_stats['min'],
        # "max_val": daily_stats['max'],
        # "status": "Gateway Online"
    # }
    
     # for row in cached_data:
            # # Re-pack and publish cached data
            # payload = json.dumps({
                # "sensor_id": record["sensor_id"],
                # "median_val": record["avr_value"],
                # "timestamp": record["timestamp"],
                # "status": "Cached Data"
            # })
            # hpmini_client.publish(TOPIC_PUBLISH, payload)
            # print(f"[CACHE] Synced record: {payload}")
    
    try:
        reading_data = await get_readings()
        if not reading_data:
            print("[INFO] No data to aggregate yet.")
            return        
    
        if hpmini_client.is_connected():
            
            for record in reading_data:
                payload = json.dumps(record)
                hpmini_client.publish(TOPIC_PUBLISH, payload)
                print(f"[HPMINI] Sent: {payload}")
                try: 
                    await delete_all_readings()
                # Drop raw readings from DB after successful send
                except Exception as e:
                    print(f"[ERROR] Cannot delete readings: {e}")
        else:
            print("[WARN] HPmini Offline. Data keeps in SQL DB.")
            # Cache to 'processed' table (assuming Sensor ID 1)
            # asyncio.run(insert_processed_data(1, median_val))
            # asyncio.run(delete_all_readings())
    except Exception as e:
        print(f"[ERROR] Cache Sync: {e}")
    # Clear memory buffer
    # data_buffer = []

# ==========================================
# 7. CONNECTION 
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
        hpmini_client.connect(HPMINI_BROKER, HPMINI_PORT, 60)
        hpmini_client.loop_start()
    except Exception as e:
        print(f"[ERROR] HPmini Connect: {e}")
    try:
        local_client.connect(LOCAL_BROKER, LOCAL_PORT, 60)
        local_client.loop_start()
    except Exception as e:
        print(f"[ERROR] ESP32 Connect: {e}")
    # Set up the loop
    asyncio.set_event_loop(main_loop)
    # SCHEDULE the sender task BEFORE starting run_forever
    main_loop.create_task(sender_task())

    try:
        main_loop.run_forever()
    except KeyboardInterrupt:
        print("[SYSTEM] Stopping...")
        client.loop_stop()
        main_loop.stop()
   
            
