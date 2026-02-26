import paho.mqtt.client as mqtt
import json
import os
import time
import ssl
import statistics
from datetime import datetime

# === 1. CONFIG FROM .ENV ===
LOCAL_BROKER = os.getenv('LOCAL_BROKER_IP', 'mosquitto_broker')
LOCAL_PORT = int(os.getenv('LOCAL_BROKER_PORT', 8883))
BUFFER_FILE = os.getenv('BUFFER_PATH', '/data/buffer.json')

TS_BROKER = os.getenv('THINGSPEAK_BROKER', 'mqtt3.thingspeak.com')
TS_PORT = int(os.getenv('THINGSPEAK_PORT', 8883))
TS_CLIENT_ID = os.getenv('THINGSPEAK_CLIENT_ID')
TS_USER = os.getenv('THINGSPEAK_USERNAME')
TS_PASS = os.getenv('THINGSPEAK_PASSWORD')
TS_CHANNEL = os.getenv('THINGSPEAK_CHANNEL_ID')

# topics ThingSpeak
TS_TOPIC_PUBLISH = f"channels/{TS_CHANNEL}/publish"
TS_TOPIC_SUBSCRIBE_LED = f"channels/{TS_CHANNEL}/subscribe/fields/field1"

# === 2. Global variables ===
# Median buffer 
pot_buffer = []

# Current sensor states (we store the last known one)
current_state = {
    "switch": 0,       # Field 3
    "esp_status": 0,   # Field 4 (0=Offline, 1=Online)
    "led_value": 0     # Field 1 (Current state)
}

# Statistics for the day
daily_stats = {
    "max": -1.0,       # Field 6
    "min": 10000.0,    # Field 5
    "last_reset": datetime.now().day
}

# ==========================================
# 3. CLOUD LOGIC (THINGSPEAK)
# ==========================================

def on_ts_connect(client, userdata, flags, rc):
    if rc == 0:
        print(f"[CLOUD] Connected to ThingSpeak!")
        # 1. Subscribe to management LED (Field 1)
        client.subscribe(TS_TOPIC_SUBSCRIBE_LED)
        print(f"[CLOUD] Subscribed to LED control: {TS_TOPIC_SUBSCRIBE_LED}")
        
        # 2. If there was data on the flash drive, we send it
        check_usb_buffer_and_send()
    else:
        print(f"[CLOUD] Connection Failed code={rc}")

def on_ts_message(client, userdata, msg):
    """A command came from the cloud (from Field1)"""
    try:
        payload = msg.payload.decode()
        print(f"[CLOUD] Received CMD for LED: {payload}")
        
        # We forward the command to the local network to ESP32
        # (Be sure to check whether the local client is connected)
        if local_client.is_connected():
            local_client.publish("sensors/led/set", payload)
            print(f"[LOCAL] Forwarded to ESP32: {payload}")
        else:
            print("[ERROR] Cannot forward to ESP32: Local broker disconnected")
            
    except Exception as e:
        print(f"[ERROR] processing cloud message: {e}")

# Client settings ThingSpeak
ts_client = mqtt.Client(client_id=TS_CLIENT_ID)
ts_client.username_pw_set(TS_USER, TS_PASS)
ts_client.on_connect = on_ts_connect
ts_client.on_message = on_ts_message # income msg handler
ts_client.tls_set()

#LWT for the Gateway itself (if the Raspberry Pi dies)
# Note: ThingSpeak doesn't have a dedicated field for gateway status,
# write it to the meta-status
ts_client.will_set(TS_TOPIC_PUBLISH, payload="status=Gateway Offline", qos=1, retain=False)


# ==========================================
# 4. LOCAL_BROKER LOGIC (MQTT)
# ==========================================

def on_local_connect(client, userdata, flags, rc):
    print(f"[LOCAL] Connected to Mosquitto. Code: {rc}")
    # Subscribe to all topics at once
    client.subscribe([
        ("sensors/pot/data", 0),    
        ("sensors/switch/data", 0), 
        ("status/esp32", 0)         # status of ESP32 (LWT)
    ])

def on_local_message(client, userdata, msg):
    global pot_buffer, current_state, daily_stats
    
    topic = msg.topic
    payload = msg.payload.decode()
    
    try:
        # 1. Sensor data 1
        if topic == "sensors/pot/data":
            val = float(payload)
            pot_buffer.append(val)
            update_min_max(val)
            print(f"[DATA] Pot: {val}")

        # 2. Sensor data 2 - switch
        elif topic == "sensors/switch/data":
            # waiting "1" or "0"
            current_state["switch"] = int(payload)
            print(f"[DATA] Switch: {payload}")

        # 3. Status of ESP32 (LWT)
        elif topic == "status/esp32":
            if payload == "Online":
                current_state["esp_status"] = 1
            else:
                current_state["esp_status"] = 0
            print(f"[STATUS] ESP32 is {payload} ({current_state['esp_status']})")

    except Exception as e:
        print(f"[ERROR] Local msg parse: {e}")

def update_min_max(val):
    """Daily statistics update"""
    global daily_stats
    current_day = datetime.now().day
    
    # Reset at midnight
    if current_day != daily_stats["last_reset"]:
        daily_stats = {"max": val, "min": val, "last_reset": current_day}
        print("[STATS] New Day Reset")
    else:
        if val > daily_stats["max"]: daily_stats["max"] = val
        if val < daily_stats["min"]: daily_stats["min"] = val

# Setting up a local client
local_client = mqtt.Client(client_id="Gateway_Logic_Py")
local_client.tls_set(ca_certs="/app/certs/ca.crt")
local_client.tls_insecure_set(True)
local_client.on_connect = on_local_connect
local_client.on_message = on_local_message


# ==========================================
# 5. USB / OFFLINE BUFFER LOGIC
# ==========================================

def save_to_usb(payload):
    try:
        # 1. Create a folder if it doesn't exist.
        os.makedirs(os.path.dirname(BUFFER_FILE), exist_ok=True)

        entry = {"timestamp": time.time(), "payload": payload}
        
        # Write to file (mode 'a' - append)
        with open(BUFFER_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
            
        print(f"[WARN] Cloud Offline. Saved to buffer: {BUFFER_FILE}")
    except Exception as e:
        print(f"[ERROR] Buffer Write: {e}")

def check_usb_buffer_and_send():
    if not os.path.exists(BUFFER_FILE): return

    try:
        # We read all the lines
        with open(BUFFER_FILE, "r") as f:
            lines = f.readlines()
        
        if not lines: return # If the file is empty, exit.

        print(f"[BUFFER] Found {len(lines)} cached records. Syncing...")

        # We clear the file IMMEDIATELY so that new data is written to a clean file.
        open(BUFFER_FILE, 'w').close()

        for line in lines:
            if not line.strip(): continue
            try:
                record = json.loads(line)
                payload = record["payload"]
                
                # retry sending again
                ts_client.publish(TS_TOPIC_PUBLISH, payload)
                print(f"[BUFFER] Synced: {payload}")
                
                # IMPORTANT: ThingSpeak bans you if you send more than once every 15 seconds.
                # We have to wait, otherwise the data will be lost.
                time.sleep(16) 
                
            except json.JSONDecodeError:
                continue # Skipping broken lines
            except Exception as e:
                print(f"[ERROR] Publishing cached data: {e}")
                
    except Exception as e:
        print(f"[ERROR] Buffer Sync: {e}")

# ==========================================
# 6. MAIN CYCLE (COLLECTION AND DISPATCH)
# ==========================================
def process_and_send():
    global pot_buffer
    
    #If the buffer is empty but we want to send the status, we can send 0
    # But the median requires data.
    if not pot_buffer:
        median_val = 0
    else:
        median_val = statistics.median(pot_buffer)
    
    # Assembling the package:
    # field2 = Median
    # field3 = Switch
    # field4 = ESP Status (1/0)
    # field5 = Min
    # field6 = Max
    # status = Текст
    
    payload = (f"field2={median_val}&"
               f"field3={current_state['switch']}&"
               f"field4={current_state['esp_status']}&"
               f"field5={daily_stats['min']}&"
               f"field6={daily_stats['max']}&"
               f"status=Gateway Online")
    
    if ts_client.is_connected():
        ts_client.publish(TS_TOPIC_PUBLISH, payload)
        print(f"[CLOUD] Sent: Med={median_val}, ESP={current_state['esp_status']}")
    else:
        print("[WARN] Cloud Offline. Saving to USB.")
        save_to_usb(payload)
    
    # We clear the median buffer (we do not reset the switch/esp statuses, they store the last state))
    pot_buffer = []

if __name__ == "__main__":
    print("Starting Gateway v2 (LED Control + Fields)...")

    # Running clients in background threads
    try:
        ts_client.connect(TS_BROKER, TS_PORT, 60)
        ts_client.loop_start()
    except Exception as e:
        print(f"[ERROR] Cloud Connect: {e}")

    local_client.connect(LOCAL_BROKER, LOCAL_PORT, 60)
    local_client.loop_start()

    #Sending interval (default: 2 minutes)
    # For testing, set it to 30 seconds
    INTERVAL = 2 * 60 
    last_time = time.time()

    while True:
        time.sleep(1)
        if time.time() - last_time > INTERVAL:
            process_and_send()
            last_time = time.time()
