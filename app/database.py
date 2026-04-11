# app/database.py
# Connects to your MySQL on Aiven Cloud using DATABASE_URL

import os
import pymysql
import pymysql.cursors
from urllib.parse import urlparse, unquote


def get_connection():
    db_url = os.getenv("DATABASE_URL", "")
    if db_url:
        clean_url = db_url.replace("mysql+pymysql://", "mysql://")
        clean_url = clean_url.replace("mysql+mysqlconnector://", "mysql://")
        parsed = urlparse(clean_url)
        config = {
            "host": parsed.hostname,
            "port": parsed.port or 3306,
            "user": unquote(parsed.username or ""),
            "password": unquote(parsed.password or ""),
            "database": parsed.path.lstrip("/"),
            "cursorclass": pymysql.cursors.DictCursor,
        }
    else:
        config = {
            "host": os.getenv("DB_HOST", "localhost"),
            "port": int(os.getenv("DB_PORT", 3306)),
            "user": os.getenv("DB_USER", "root"),
            "password": os.getenv("DB_PASSWORD", ""),
            "database": os.getenv("DB_NAME", "upskillize"),
            "cursorclass": pymysql.cursors.DictCursor,
        }
    return pymysql.connect(**config)


def query(sql: str, params: tuple = ()):
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()
    except Exception as e:
        print(f"❌ DB error: {e}")
        raise
    finally:
        conn.close()


def execute(sql: str, params: tuple = ()):
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            conn.commit()
            return cursor.lastrowid
    except Exception as e:
        print(f"❌ DB error: {e}")
        raise
    finally:
        conn.close()


def test_connection() -> bool:
    try:
        query("SELECT 1 as connected")
        print("✅ MySQL connection successful")
        return True
    except Exception as e:
        print(f"❌ MySQL connection FAILED: {e}")
        return False