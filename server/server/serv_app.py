import os
import time
import logging
from fastapi import FastAPI, Request, HTTPException
from prometheus_client import Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST
from prometheus_client import start_http_server
from influxdb_client import InfluxDBClient, Point, WritePrecision
import immudb.client as immu
from google.protobuf.json_format import MessageToDict
from sensor_pb2 import Batch, SensorReading
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.exceptions import InvalidSignature
from dotenv import load_dotenv

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
INFLUX_ORG = os.getenv("INFLUX_ORG")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET")
influx_client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
write_api = influx_client.write_api()
print("[APP] startup_event: Influx client init")

# immudb client
immu_client = immu.ImmudbClient()
print("[APP] startup_event: immudb client init")

# Load public key for signature verification (optional)
PUBKEY_PATH = os.getenv("SECRET_VERIFY_KEY_PATH", "/certs/verify_pub.pem")
public_key = None
if os.path.exists(PUBKEY_PATH):
    with open(PUBKEY_PATH, "rb") as f:
        public_key = serialization.load_pem_public_key(f.read())
print("[APP] startup_event: public key init")

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
        if r.signature and public_key:
            try:
                payload = f"{r.device}|{r.sensor}|{r.units}|{r.value}|{r.ts}".encode()
                public_key.verify(r.signature, payload, ec.ECDSA(hashes.SHA256()))
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

@app.get("/metrics")
def metrics():
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}

