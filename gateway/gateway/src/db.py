import aiosqlite
import sqlite3
from typing import Dict, Any

# Path to local database
DB_PATH = "/data/sensor_data.db"

async def init_db():
    print("[DB] init_db() called, DB_PATH =", DB_PATH)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_name VARCHAR(30) NOT NULL,
            device_type VARCHAR(30),
            location VARCHAR(100),
            device_status VARCHAR(15) DEFAULT 'not connected'
        );

        CREATE TABLE IF NOT EXISTS sensors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id INTEGER,
            sensor_type VARCHAR(30),
            unit VARCHAR(10),
            min_value INTEGER DEFAULT 0,
            max_value INTEGER DEFAULT 90,
            FOREIGN KEY(device_id) REFERENCES devices(id)
        );

        CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sensor_id INTEGER,
            reading_value DOUBLE,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(sensor_id) REFERENCES sensors(id)
        );

        CREATE TABLE IF NOT EXISTS processed (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sensor_id INTEGER,
            avr_value DOUBLE,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(sensor_id) REFERENCES sensors(id)
        );
        """)
        await db.commit()
    print("[DB] init_db() finished")

async def insert_sensor_data(sensor_id, reading_value):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        # Используем параметризованный запрос для защиты и корректности
        cursor.execute(
            "INSERT INTO readings (sensor_id, reading_value) VALUES (?, ?)",
            (sensor_id, reading_value)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB ERROR] Failed to insert: {e}")

async def delete_all_readings():
    # Drop readings after successful send to HPmini
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM readings")
        await db.commit()
    print("[DB] All readings dropped.")

async def insert_processed_data(sensor_id: int, avr_value: float):
    # Cache median/average values on HPmini disconnect
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO processed (sensor_id, avr_value) 
            VALUES (?, ?)
        """, (sensor_id, avr_value))
        await db.commit()
    print(f"[DB] Cached processed data: Sensor {sensor_id}, Value {avr_value}")

async def get_and_clear_processed_data():
    # Retrieve cached data and clear the processed table
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id, sensor_id, avr_value, timestamp FROM processed") as cursor:
            rows = await cursor.fetchall()
        
        if rows:
            await db.execute("DELETE FROM processed")
            await db.commit()
            
    return [dict(row) for row in rows]
    
async def get_devices():
    try:    
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT id, device_name, device_status FROM devices") as cursor:
                rows = await cursor.fetchall()
                return {row[1]: row[0] for row in rows}
    except Exception as e:
        print(f"[ERROR] Failed to grab devices from DB: {e}")

async def get_readings():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT 
                s.sensor_type, 
                d.device_name, 
                datetime((strftime('%s', r.timestamp) / 600) * 600, 'unixepoch') AS interval_start,
                r.sensor_id,
                AVG(r.reading_value) AS avg_value, 
                COUNT(*) AS readings_count      
            FROM readings r
            JOIN sensors s ON r.sensor_id = s.id
            JOIN devices d ON s.device_id = d.id
            GROUP BY interval_start, r.sensor_id
            ORDER BY interval_start DESC, r.sensor_id;
            """) as cursor:
            rows = await cursor.fetchall()
    return [
        {
            "sensor_id": row[3],
            "device_name": row[1],
            "type": row[0],
            "value": round(row[4], 2), # Using AVG(reading_value) rounded to 2 decimals
            "note": "average",
            "timestamp": row[2]
        }
        for row in rows
    ]
            
async def get_sensors():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, device_id, sensor_type FROM sensors") as cursor:
            rows = await cursor.fetchall()
            return {(row[1], row[2]): row[0] for row in rows}

async def update_device_status(device_id, status):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("UPDATE devices SET device_status=? WHERE id=? ",(status, device_id)) as cursor:
            await db.commit()
    print(f"[DB] Device status updated: Device {device_id}, status: {status}")
    
    
async def upload_readings_to_db(batch):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            # Optimize SQLite for batch operations
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute("PRAGMA synchronous=NORMAL;")
            
            # This inserts all 100 records in a single transaction!
            await db.executemany(
                "INSERT INTO readings (sensor_id, reading_value, timestamp) VALUES (?, ?, ?)",
                batch
            )
            await db.commit()
            
        print(f"[DB] Successfully saved {len(batch)} readings to database.")
        
    except Exception as e:
        print(f"[ERROR] Failed to save batch to DB: {e}")