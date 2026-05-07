from http import client
import os
import time
import logging
import asyncio
import aiohttp
import json
import base64
import ssl
from fastapi import FastAPI, Request, HTTPException, Response
from prometheus_client import Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST
from influxdb_client import InfluxDBClient, Point, WritePrecision
import immudb.client as immu
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from Crypto.PublicKey import ECC
from Crypto.Signature import DSS
from Crypto.Hash import SHA256
from dotenv import load_dotenv
from sensor_pb2 import Batch
import sensor_pb2 

load_dotenv()
logging.basicConfig(level=logging.INFO)
app = FastAPI()

# Prometheus metrics
REQS = Counter('server_requests_total', 'Total HTTP requests')
DECRYPT_FAIL = Counter('server_decrypt_failures_total', 'Decryption/validation failures')
LAST_TS = Gauge('server_last_ingest_timestamp', 'Last ingest timestamp')
print("[APP] startup_event: Prometheus metrics init")

# Influx client
INFLUX_URL = os.getenv("INFLUX_URL", "http://influxdb:8086")
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN", "")
INFLUX_ORG = os.getenv("INFLUX_ORG", "myorg")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "sensors")
influx_client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
write_api = influx_client.write_api()

# immudb client (Optional, configure as needed)
IMMUDB_URL  = os.getenv("IMMUDB_URL", "NOT FOUND")
IMMUDB_USER = os.getenv("IMMUDB_USER", "NOT FOUND")
IMMUDB_PASS = os.getenv("IMMUDB_PASS", "NOT FOUND")
IMMUDB_DB   = os.getenv("IMMUDB_DB", "NOT FOUND")

try:
    immu_client = immu.ImmudbClient("immudb:3322")
    print("[APP] startup_event: immudb client init")
except Exception as e:
    logging.warning(f"[APP] Immudb init failed (ignoring for now): {e}")

# ==========================================
# GATEWAY PULL LOGIC (НОВЫЙ БЛОК)
# ==========================================
GW_URL = os.getenv("GW_URL", "Lost ip!") # УКАЖИ IP ШЛЮЗА!
EN_IN_AES = os.getenv('EN_IN_AES', 'False').lower() in ('true', '1', 't')

raw_key = os.getenv('LOCAL_AES_KEY', 'key_not_found')
AES_KEY = raw_key.strip().replace('"', '').replace("'", "").encode('utf-8')[:32]
AES_IV  = b'\x00' * 16

async def fetch_gateway_data():
    """Фоновая задача, которая ходит на шлюз каждые 30 секунд"""
    # Disable SSL verification if using self-signed certs from Gateway
    ssl_context = ssl.create_default_context(cafile='/certs/ca.crt') 
    ssl_context.check_hostname = False # Если в сертификате шлюза нет SAN с IP
    connector = aiohttp.TCPConnector(ssl=ssl_context)
    
    async with aiohttp.ClientSession(connector=connector) as session:
        while True:
            try:
                logging.info(f"[POLLER] Fetching data from {GW_URL}...")
                async with session.get(GW_URL) as response:
                    if response.status == 200:
                        raw_data = await response.text()
                        
                        if EN_IN_AES:
                            try:
                                # Inverse of Gateway encryption: base64 decode -> decrypt -> unpad
                                cipher = AES.new(AES_KEY, AES.MODE_CBC, iv=AES_IV)
                                encrypted_bytes = base64.b64decode(raw_data)
                                decrypted_bytes = unpad(cipher.decrypt(encrypted_bytes), AES.block_size)
                                json_str = decrypted_bytes.decode('utf-8')
                            except Exception as e:
                                logging.error(f"[POLLER] AES Decryption failed: {e}")
                                DECRYPT_FAIL.inc()
                                json_str = None
                        else:
                            json_str = raw_data
                        
                        if json_str:
                            data = json.loads(json_str)
                            process_gateway_json(data)
                            logging.info("[POLLER] Successfully fetched and saved Gateway data.")
                    else:
                        logging.warning(f"[POLLER] Gateway returned status {response.status}")
                        
            except Exception as e:
                logging.error(f"[POLLER] Failed to reach Gateway: {e}")
            
            # Ждем 30 секунд перед следующим опросом
            await asyncio.sleep(30)

def process_gateway_json(data):
    """Парсит полученный JSON и пишет в InfluxDB"""
    readings = data.get("readings", [])
    if not readings:
        return

    for r in readings:
        ts = r.get("ts", int(time.time() * 1000))
        LAST_TS.set(ts)
        
        point = Point("sensor") \
            .tag("device", r.get("device", "unknown")) \
            .tag("sensor", r.get("sensor", "unknown")) \
            .tag("units", r.get("units", "raw")) \
            .field("value", float(r.get("value", 0.0))) \
            .time(int(ts), WritePrecision.MS)
            
        try:
            write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=point)
        except Exception as e:
            logging.exception("Influx write failed: %s", e)
            
# ==========================================
# Handlng Secure log DB
# ==========================================
with open('./gw_public.pem', 'rt') as f:
    gw_pub_key = ECC.import_key(f.read())

@app.post("/secure-ingest")
async def secure_ingest(request: Request):
    body = await request.body()
    #logging.info(f"[SECURE_INGEST] Received secure log of size {len(body)} bytes")

    # 1. Parse incoming data
    batch = sensor_pb2.Batch()
    try:
        batch.ParseFromString(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid Protobuf data")
        
    # 2. Extract signature and clear it from the object
    received_signature = batch.signature
    if not received_signature:
        raise HTTPException(status_code=400, detail="Missing signature")
        
    # Clear the field so we get the exact same bytes that the gateway signed
    batch.ClearField("signature")
    original_payload = batch.SerializeToString()
    
    # 3. Verify Signature (Authenticity & Non-repudiation)
    h = SHA256.new(original_payload)
    verifier = DSS.new(gw_pub_key, 'fips-186-3')
    
    try:
        verifier.verify(h, received_signature)
        logging.info("[SECURE_INGEST] Signature verification succeeded")
    except ValueError:
        logging.warning("[SECURE_INGEST] Signature verification failed!")
        raise HTTPException(status_code=401, detail="Signature verification failed")
        
    # 4. Write to immudb (Integrity)
    # The data is proven authentic, now write it immutably
    entry_str = f"GW:{batch.gateway.gateway_id}|TS:{batch.gateway.ts}|MSG:{batch.readings[0].value}"
    
    try:
        immu_client.set(f"log:{batch.gateway.ts}".encode(), entry_str.encode())
        logging.info("[SECURE_INGEST] Log entry written to immudb")
    except Exception as e:
        logging.error(f"[SECURE_INGEST] Failed to write to immudb: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}")
    
    try:
        immu_client.sqlExec(
            "INSERT INTO gateway_reports(ts, gateway_id, msg_count) VALUES (NOW(), @gw, @cnt)",
            params={
                "gw": batch.gateway.gateway_id, 
                "cnt": int(batch.readings[0].value)
            }
        )
    except Exception as e:
        print(f"SQL Insert Error: {e}")

    # 5. (Optional) Read back the last log entry to verify it was stored correctly
    try:
        # Метод scan в immudb-py обычно принимает prefix, seekKey, endKey, limit
        # Просто убираем reverse=True
        entries = client.scan(key=b"", desc=False, limit=10)

        if not entries:
            print("Список ключей пуст. Возможно, не та база данных?")
        else:
        
            for key, entry in entries.items():
                print(f"Key: {key}, Value: {entry.value.decode('utf-8')}")
        
        
            print(f"Найдено записей: {len(scan_result)}")
            for key in scan_result:
                # Получаем значение по ключу
                val = immu_client.get(key)
                print(f"Ключ: {key.decode()} | Значение: {val.value.decode()}")
    except Exception as e:
        print(f"Ошибка при чтении: {e}")

    return {"status": "log_secured_and_verified"}

# ==========================================
# FASTAPI ENDPOINTS & STARTUP
# ==========================================

@app.on_event("startup")
async def startup_event():
    # Запускаем фоновый опрос шлюза при старте сервера
    asyncio.create_task(fetch_gateway_data())
    try:
        # Login with your new r+w user credentials
        # Default database is usually 'defaultdb'
        immu_client.login(IMMUDB_USER, IMMUDB_PASS, database=IMMUDB_DB)
        print("[INFO] Successfully connected and authenticated to immudb")
    except Exception as e:
        print(f"[ERROR] immudb connection failed: {e}")

@app.get("/metrics")
def metrics():
    # Этот эндпоинт отдает системные метрики (REQS, LAST_TS) для Prometheus
    return Response(
        content=generate_latest(), 
        media_type=CONTENT_TYPE_LATEST
    )
    
@app.post("/ingest")
async def ingest(request: Request):
    REQS.inc()
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Empty body")
    # Try parse as Protobuf Batch
    batch = Batch()
    try:
        batch.ParseFromString(body)
    except Exception:
        # fallback: try JSON
        try:
            data = await request.json()
            # convert JSON -> write to influx/immudb (left as exercise)
            return {"status": "json_received"}
        except Exception:
            DECRYPT_FAIL.inc()
            raise HTTPException(status_code=400, detail="Invalid payload")
    # Process readings
    for r in batch.readings:
        ts = r.ts or int(time.time() * 1000)
        LAST_TS.set(ts)
        # verify signature if present
        if r.signature and gw_pub_key:
            try:
                payload = f"{r.device}|{r.sensor}|{r.units}|{r.value}|{r.ts}".encode()
                gw_pub_key.verify(r.signature, payload, ec.ECDSA(hashes.SHA256()))
            except InvalidSignature:
                DECRYPT_FAIL.inc()
                logging.warning("Invalid signature for %s/%s", r.device, r.sensor)
                continue
        # write to Influx
        point = Point("sensor") \
            .tag("device", r.device) \
            .tag("sensor", r.sensor) \
            .tag("units", r.units) \
            .field("value", float(r.value)) \
            .time(int(ts), WritePrecision.MS)
        try:
            write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=point)
        except Exception as e:
            logging.exception("Influx write failed: %s", e)
        # write to immudb (append-only)
        try:
            entry = f"{r.device}|{r.sensor}|{r.units}|{r.value}|{ts}".encode()
            immu_client.set(key=f"{r.device}:{r.sensor}:{ts}".encode(), value=entry)
        except Exception as e:
            logging.exception("immudb write failed: %s", e)
    return {"status": "ok", "count": len(batch.readings)}
