# app/database.py
# Connects to your MySQL on Avian Cloud (Aiven) using DATABASE_URL

import os
import mysql.connector
from mysql.connector import pooling
from urllib.parse import urlparse, unquote

pool = None


def get_pool():
    global pool
    if pool is None:
        db_url = os.getenv("DATABASE_URL", "")
        if db_url:
            # Clean the URL — remove "+pymysql" or "+mysqlconnector" if present
            clean_url = db_url.replace("mysql+pymysql://", "mysql://")
            clean_url = clean_url.replace("mysql+mysqlconnector://", "mysql://")

            parsed = urlparse(clean_url)
            config = {
                "host": parsed.hostname,
                "port": parsed.port or 3306,
                "user": unquote(parsed.username or ""),
                "password": unquote(parsed.password or ""),
                "database": parsed.path.lstrip("/"),
            }
        else:
            config = {
                "host": os.getenv("DB_HOST", "localhost"),
                "port": int(os.getenv("DB_PORT", 3306)),
                "user": os.getenv("DB_USER", "root"),
                "password": os.getenv("DB_PASSWORD", ""),
                "database": os.getenv("DB_NAME", "upskillize"),
            }

        # Aiven Cloud requires SSL
        ssl_config = {"ssl_disabled": False}

        pool = pooling.MySQLConnectionPool(
            pool_name="upskillize_pool",
            pool_size=5,
            pool_reset_session=True,
            **config,
            **ssl_config,
        )
        print(f"✅ MySQL pool created for: {config['host']}:{config['port']}/{config['database']}")
    return pool


def query(sql: str, params: tuple = ()):
    """Run a query and return rows."""
    conn = get_pool().get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        conn.commit()
        return rows
    except Exception as e:
        print(f"❌ DB error: {e}")
        raise
    finally:
        cursor.close()
        conn.close()


def execute(sql: str, params: tuple = ()):
    """Run an INSERT/UPDATE and return lastrowid."""
    conn = get_pool().get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(sql, params)
        conn.commit()
        return cursor.lastrowid
    except Exception as e:
        print(f"❌ DB error: {e}")
        raise
    finally:
        cursor.close()
        conn.close()


def test_connection() -> bool:
    try:
        rows = query("SELECT 1 as connected")
        print("✅ MySQL connection successful")
        return True
    except Exception as e:
        print(f"❌ MySQL connection FAILED: {e}")
        return False
