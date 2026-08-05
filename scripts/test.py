import os
import pyodbc

# =========================
# DATABASE CONNECTION
# =========================

DB_SERVER = os.getenv("DB_SERVER", "localhost")
DB_PORT = os.getenv("DB_PORT", "1433")
DB_DATABASE = os.getenv("DB_DATABASE", "")
DB_USERNAME = os.getenv("DB_USERNAME", "")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_DRIVER = os.getenv("DB_DRIVER", "ODBC Driver 17 for SQL Server")

# =========================
# CONNECT TO SQL SERVER
# =========================

try:
    conn = pyodbc.connect(
        f"DRIVER={{{DB_DRIVER}}};"
        f"SERVER={DB_SERVER},{DB_PORT};"
        f"DATABASE={DB_DATABASE};"
        f"UID={DB_USERNAME};"
        f"PWD={DB_PASSWORD};"
        f"TrustServerCertificate=yes;"
    )

    cursor = conn.cursor()

    print("Connected to SQL Server successfully!")

    # =========================
    # MIGRATION QUERIES
    # =========================

    queries = [

        # tool_name
        """
        IF NOT EXISTS (
            SELECT 1
            FROM sys.columns
            WHERE object_id = OBJECT_ID('hub_jobs')
            AND name = 'tool_name'
        )
        BEGIN
            ALTER TABLE hub_jobs
            ADD tool_name NVARCHAR(100) NULL;
        END
        """,

        # tool_params_json
        """
        IF NOT EXISTS (
            SELECT 1
            FROM sys.columns
            WHERE object_id = OBJECT_ID('hub_jobs')
            AND name = 'tool_params_json'
        )
        BEGIN
            ALTER TABLE hub_jobs
            ADD tool_params_json NVARCHAR(MAX) NULL;
        END
        """,

        # tool_env_vars_json
        """
        IF NOT EXISTS (
            SELECT 1
            FROM sys.columns
            WHERE object_id = OBJECT_ID('hub_jobs')
            AND name = 'tool_env_vars_json'
        )
        BEGIN
            ALTER TABLE hub_jobs
            ADD tool_env_vars_json NVARCHAR(MAX) NULL;
        END
        """,

        # run_count
        """
        IF NOT EXISTS (
            SELECT 1
            FROM sys.columns
            WHERE object_id = OBJECT_ID('hub_jobs')
            AND name = 'run_count'
        )
        BEGIN
            ALTER TABLE hub_jobs
            ADD run_count INT NOT NULL DEFAULT 0;
        END
        """,

        # last_run_at
        """
        IF NOT EXISTS (
            SELECT 1
            FROM sys.columns
            WHERE object_id = OBJECT_ID('hub_jobs')
            AND name = 'last_run_at'
        )
        BEGIN
            ALTER TABLE hub_jobs
            ADD last_run_at DATETIME2 NULL;
        END
        """,

        # last_run_status
        """
        IF NOT EXISTS (
            SELECT 1
            FROM sys.columns
            WHERE object_id = OBJECT_ID('hub_jobs')
            AND name = 'last_run_status'
        )
        BEGIN
            ALTER TABLE hub_jobs
            ADD last_run_status NVARCHAR(20) NULL;
        END
        """,

        # last_run_output
        """
        IF NOT EXISTS (
            SELECT 1
            FROM sys.columns
            WHERE object_id = OBJECT_ID('hub_jobs')
            AND name = 'last_run_output'
        )
        BEGIN
            ALTER TABLE hub_jobs
            ADD last_run_output NVARCHAR(MAX) NULL;
        END
        """
    ]

    # =========================
    # EXECUTE QUERIES
    # =========================

    for index, query in enumerate(queries, start=1):
        print(f"Executing query {index}...")
        cursor.execute(query)

    conn.commit()

    print("Migration applied successfully!")
    print("Columns added to hub_jobs:")
    print("- tool_name")
    print("- tool_params_json")
    print("- tool_env_vars_json")
    print("- run_count")
    print("- last_run_at")
    print("- last_run_status")
    print("- last_run_output")

except pyodbc.Error as e:
    print("SQL Server Error:")
    print(e)

except Exception as e:
    print("Unexpected Error:")
    print(e)

finally:
    try:
        cursor.close()
        conn.close()
        print("Database connection closed.")
    except:
        pass