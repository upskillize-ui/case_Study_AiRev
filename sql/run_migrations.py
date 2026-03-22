# sql/run_migrations.py
import os, sys, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from app.database import test_connection, get_pool


def run():
    print("Starting database migrations...")

    if not test_connection():
        print("Cannot connect to database. Check your .env file.")
        sys.exit(1)

    sql_file = os.path.join(os.path.dirname(__file__), "migrations.sql")
    with open(sql_file, "r", encoding="utf-8") as f:
        sql_content = f.read()

    # Remove comment lines
    lines = []
    for line in sql_content.split("\n"):
        if line.strip().startswith("--"):
            continue
        lines.append(line)
    clean_sql = "\n".join(lines)

    # Split by semicolon
    statements = [s.strip() for s in clean_sql.split(";") if s.strip() and len(s.strip()) > 10]

    pool = get_pool()
    success = 0

    for stmt in statements:
        conn = pool.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(stmt)
            conn.commit()
            match = re.search(r"(?:CREATE TABLE|INSERT INTO)\s+(?:IF NOT EXISTS\s+)?(\w+)", stmt, re.IGNORECASE)
            name = match.group(1) if match else "done"
            print(f"   OK: {name}")
            success += 1
            cursor.close()
        except Exception as e:
            err = str(e)
            if "already exists" in err.lower() or "1050" in err or "Duplicate" in err or "1062" in err:
                print(f"   SKIPPED (already exists)")
            else:
                print(f"   ERROR: {err[:120]}")
        finally:
            conn.close()

    print(f"\nDone: {success} statements executed")

    # Verify
    print("\nVerifying tables...")
    conn = pool.get_connection()
    cursor = conn.cursor(dictionary=True)
    for t in ["case_studies", "case_study_submissions", "student_performance_tracker", "mentor_reports", "ai_review_logs"]:
        try:
            cursor.execute(f"SELECT COUNT(*) as count FROM {t}")
            rows = cursor.fetchall()
            print(f"   OK: {t} - {rows[0]['count']} rows")
        except:
            print(f"   MISSING: {t}")
    cursor.close()
    conn.close()
    print("\nDatabase is ready!")


if __name__ == "__main__":
    run()


