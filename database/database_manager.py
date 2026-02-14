# database_manager.py
"""database_manager.py — Manages user-configured external database connections
(the customer SQL Server/PostgreSQL/MySQL/Oracle/SQLite/MongoDB instances that
BI agents query), as distinct from the app's own SQL Server (see app_db.py).

DatabaseConnectionManager (exposed as the module-level singleton ``db_manager``)
persists connection definitions in the ``app_database_connections`` table
(via app_db.get_app_db()), tests raw driver connectivity, and maintains a
SQLAlchemy engine pool (one engine per connection name, lazily built and
cached) used by NLQEngine and the BI chat/execute-sql routes in app.py to
actually run generated SQL.
"""
import json
import os
import threading
from pathlib import Path
import importlib
from typing import Dict, Tuple, Optional
from app_db import get_app_db

class DatabaseConnectionManager:
    """Stores/tests user database connections and maintains a SQLAlchemy engine pool keyed by connection name."""

    def __init__(self):
        """Initialise the empty engine pool and the per-db-type driver/connect-string table."""
        # SQLAlchemy engine pool — one engine per connection name, reused across queries
        self._engine_pool: Dict = {}
        self._engine_lock = threading.Lock()  # guards _engine_pool reads/writes

        # Database drivers mapping
        self.DB_DRIVERS = {
            'postgresql': {
                'module': 'psycopg2',
                'connect_string': "host='{server}' port={port} dbname='{database}' user='{username}' password='{password}'",
                'test_query': 'SELECT 1'
            },
            'mysql': {
                'module': 'pymysql',
                'connect_string': "host='{server}', port={port}, "
                "user='{username}', password='{password}', database='{database}'",
                'test_query': 'SELECT 1'
            },
            'mssql': {
                'module': 'pyodbc',
                'connect_string': "DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={server},{port};DATABASE={database};UID={username};PWD={password}",
                'test_query': 'SELECT 1'
            },
            'oracle': {
                'module': 'cx_Oracle',
                'connect_string': "{username}/{password}@{server}:{port}/{database}",
                'test_query': 'SELECT 1 FROM DUAL'
            },
            'sqlite': {
                'module': 'sqlite3',
                'connect_string': "{database}",
                'test_query': 'SELECT 1'
            },
            'mongodb': {
                'module': 'pymongo',
                'connect_string': "mongodb://{username}:{password}@{server}:{port}/{database}",
                'test_method': 'server_info'  # MongoDB uses a different validation method
            }
        }
    
    def load_connections(self):
        """Load all connections from the database."""
        try:
            with get_app_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, name, type, server, port, username, password, db_name, status
                    FROM app_database_connections
                    ORDER BY id
                """)
                rows = cursor.fetchall()
                return [
                    {
                        "id":       r[0], "name":     r[1],
                        "type":     r[2], "server":   r[3],
                        "port":     r[4], "username": r[5],
                        "password": r[6], "database": r[7],
                        "status":   r[8],
                    }
                    for r in rows
                ]
        except Exception as e:
            print(f"Error loading connections: {e}")
            return []

    def save_connections(self, connections):
        """Replace all connections in the database (full overwrite)."""
        try:
            with get_app_db() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM app_database_connections")
                for c in connections:
                    cursor.execute("""
                        INSERT INTO app_database_connections
                            (name, type, server, port, username, password, db_name, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, c.get("name",""), c.get("type",""),
                        c.get("server",""), str(c.get("port","")),
                        c.get("username",""), c.get("password",""),
                        c.get("database",""), c.get("status","disconnected"))
                conn.commit()
            return True
        except Exception as e:
            print(f"Error saving connections: {e}")
            return False
    
    def add_connection(self, connection_data):
        """Add a new connection"""
        connections = self.load_connections()
        
        # Check if connection name already exists
        if any(conn['name'] == connection_data['name'] for conn in connections):
            return False, "Connection name already exists"
        
        # Add new connection
        new_connection = {
            'name': connection_data['name'],
            'type': connection_data['type'],
            'server': connection_data['server'],
            'port': connection_data['port'],
            'username': connection_data['username'],
            'password': connection_data['password'],
            'database': connection_data.get('database', ''),
            'status': 'disconnected'
        }
        
        connections.append(new_connection)
        success = self.save_connections(connections)
        
        if success:
            return True, "Connection saved successfully"
        else:
            return False, "Failed to save connection"
    
    def update_connection(self, old_name, connection_data):
        """Update an existing connection. If password is empty, keep the existing one."""
        connections = self.load_connections()
        idx = next((i for i, c in enumerate(connections) if c['name'] == old_name), None)
        if idx is None:
            return False, f"Connection '{old_name}' not found"

        new_name = connection_data.get('name', old_name)
        # Name change collision check
        if new_name != old_name and any(c['name'] == new_name for c in connections):
            return False, f"Connection name '{new_name}' already exists"

        existing = connections[idx]
        connections[idx] = {
            'name':     new_name,
            'type':     connection_data.get('type',     existing['type']),
            'server':   connection_data.get('server',   existing['server']),
            'port':     connection_data.get('port',     existing['port']),
            'username': connection_data.get('username', existing['username']),
            'password': connection_data.get('password') or existing['password'],
            'database': connection_data.get('database', existing['database']),
            'status':   existing['status'],
        }

        if new_name != old_name:
            self.dispose_engine(old_name)

        success = self.save_connections(connections)
        return (True, "Connection updated successfully") if success else (False, "Failed to save changes")

    def delete_connection(self, connection_name):
        """Delete a connection by name"""
        connections = self.load_connections()
        initial_count = len(connections)

        connections = [conn for conn in connections if conn['name'] != connection_name]

        if len(connections) < initial_count:
            self.dispose_engine(connection_name)
            success = self.save_connections(connections)
            if success:
                return True, "Connection deleted successfully"
            else:
                return False, "Failed to save changes"
        else:
            return False, "Connection not found"
    
    def get_connection(self, connection_name):
        """Get a specific connection by name"""
        connections = self.load_connections()
        for conn in connections:
            if conn['name'] == connection_name:
                return conn
        return None
    
    def test_connection(self, connection_data: Dict) -> Tuple[bool, str]:
        """
        Test database connection with actual credentials
        Returns: (success: bool, message: str)
        """
        db_type = connection_data.get('type', '').lower()
        
        if db_type not in self.DB_DRIVERS:
            return False, f"Unsupported database type: {db_type}"
        
        driver_info = self.DB_DRIVERS[db_type]
        module_name = driver_info['module']
        
        try:
            # Try to import the required module
            module = importlib.import_module(module_name)
        except ImportError:
            return False, f"Required driver '{module_name}' is not installed. Please install it using: pip install {module_name}"
        
        try:
            if db_type == 'mongodb':
                return self._test_mongodb_connection(module, connection_data, driver_info)
            else:
                return self._test_sql_connection(module, connection_data, driver_info, db_type)
                
        except Exception as e:
            return False, f"Connection failed: {str(e)}"
    
    # In database_manager.py - update the _test_sql_connection method
    def _test_sql_connection(self, module, connection_data: Dict, driver_info: Dict, db_type: str) -> Tuple[bool, str]:
        """Test connection for SQL databases"""
        # Prepare connection parameters
        params = {
            'server': connection_data.get('server', ''),
            'port': connection_data.get('port', ''),
            'username': connection_data.get('username', ''),
            'password': connection_data.get('password', ''),
            'database': connection_data.get('database', '')
        }
        
        # Set default ports if not provided
        if not params['port']:
            default_ports = {
                'postgresql': '5432',
                'mysql': '3306',
                'mssql': '1433',
                'oracle': '1521'
            }
            params['port'] = default_ports.get(db_type, '')
        
        try:
            # Create connection string and connect
            if db_type == 'mssql':
                # Check for ODBC driver - try Driver 18 first, then 17, then any ODBC driver
                try:
                    drivers = module.drivers()
                    print(f"Available ODBC drivers: {drivers}")
                    
                    # Try to find the best available driver
                    driver = None
                    if 'ODBC Driver 18 for SQL Server' in drivers:
                        driver = 'ODBC Driver 18 for SQL Server'
                    elif 'ODBC Driver 17 for SQL Server' in drivers:
                        driver = 'ODBC Driver 17 for SQL Server'
                    else:
                        # Use any ODBC driver that contains "SQL Server"
                        sql_server_drivers = [d for d in drivers if 'SQL Server' in d]
                        if sql_server_drivers:
                            driver = sql_server_drivers[0]
                    
                    if not driver:
                        return False, "No suitable ODBC Driver found for SQL Server. Available drivers: " + ", ".join(drivers)
                    
                    print(f"Using ODBC driver: {driver}")
                    
                    # Build connection string
                    conn_str = f"DRIVER={{{driver}}};SERVER={params['server']},{params['port']};DATABASE={params['database']};UID={params['username']};PWD={params['password']};TrustServerCertificate=yes"
                    conn = module.connect(conn_str)
                    
                except ImportError:
                    return False, "pyodbc not installed. Please install it with: pip install pyodbc"
                    
            elif db_type == 'sqlite':
                # SQLite connection
                conn = module.connect(params['database'])
            else:
                # PostgreSQL, MySQL connections
                conn_str = driver_info['connect_string'].format(**params)
                conn = module.connect(conn_str)
            
            # Test the connection
            cursor = conn.cursor()
            cursor.execute(driver_info['test_query'])
            result = cursor.fetchone()
            
            # Close connection
            cursor.close()
            conn.close()
            
            return True, f"✅ Connection test successful! (Using {driver})"
            
        except module.Error as e:
            error_msg = str(e)
            if 'IM002' in error_msg or 'data source name not found' in error_msg.lower():
                available_drivers = module.drivers() if 'module' in locals() else []
                return False, f"ODBC Driver issue. Available drivers: {available_drivers}. Error: {error_msg}"
            elif 'Login failed' in error_msg:
                return False, "❌ Login failed. Please check your username and password."
            elif 'Cannot open database' in error_msg:
                return False, f"❌ Cannot open database '{params['database']}'. Please check the database name."
            else:
                return False, f"❌ Database error: {error_msg}"
        except Exception as e:
            return False, f"❌ Connection failed: {str(e)}"
    
    def _test_mongodb_connection(self, module, connection_data: Dict, driver_info: Dict) -> Tuple[bool, str]:
        """Test connection for MongoDB"""
        params = {
            'server': connection_data.get('server', ''),
            'port': connection_data.get('port', '27017'),
            'username': connection_data.get('username', ''),
            'password': connection_data.get('password', ''),
            'database': connection_data.get('database', '')
        }
        
        try:
            # Create MongoDB connection string
            conn_str = driver_info['connect_string'].format(**params)
            client = module.MongoClient(conn_str, serverSelectionTimeoutMS=5000)
            
            # Test the connection
            client.admin.command('ismaster')
            client.close()
            
            return True, "MongoDB connection test successful!"
            
        except Exception as e:
            return False, f"MongoDB connection failed: {str(e)}"
    
    def test_connection_by_name(self, connection_name: str) -> Tuple[bool, str]:
        """Test an existing connection by name"""
        connection = self.get_connection(connection_name)
        if not connection:
            return False, f"Connection '{connection_name}' not found"

        return self.test_connection(connection)

    # ------------------------------------------------------------------
    # SQLAlchemy engine pool
    # ------------------------------------------------------------------

    def _build_sa_engine(self, connection: Dict):
        """Build a SQLAlchemy engine for the given connection config."""
        from sqlalchemy import create_engine as _sa_create
        from urllib.parse import quote_plus as _qp
        db_type = connection['type']

        if db_type == 'mssql':
            import pyodbc as _pyodbc
            drivers = _pyodbc.drivers()
            driver = None
            for candidate in ('ODBC Driver 17 for SQL Server', 'ODBC Driver 18 for SQL Server'):
                if candidate in drivers:
                    driver = candidate
                    break
            if not driver:
                avail = [d for d in drivers if 'SQL Server' in d]
                driver = avail[0] if avail else None
            if not driver:
                raise ValueError(f"No SQL Server ODBC driver found. Available: {drivers}")
            conn_str = (
                f"DRIVER={{{driver}}};"
                f"SERVER={connection['server']},{connection.get('port', '1433')};"
                f"DATABASE={connection['database']};"
                f"UID={connection['username']};"
                f"PWD={connection['password']};"
                "TrustServerCertificate=yes;"
            )
            url = f"mssql+pyodbc:///?odbc_connect={_qp(conn_str)}"
        elif db_type == 'postgresql':
            url = (
                f"postgresql://{_qp(connection['username'])}:{_qp(connection['password'])}"
                f"@{connection['server']}:{connection.get('port', '5432')}/{connection['database']}"
            )
        elif db_type == 'mysql':
            url = (
                f"mysql+pymysql://{_qp(connection['username'])}:{_qp(connection['password'])}"
                f"@{connection['server']}:{connection.get('port', '3306')}/{connection['database']}"
            )
        elif db_type == 'sqlite':
            url = f"sqlite:///{connection['database']}"
        else:
            raise ValueError(f"Unsupported database type for engine pool: {db_type}")

        return _sa_create(url, pool_pre_ping=True, pool_recycle=3600)

    def get_engine(self, connection_name: str):
        """Return a cached SQLAlchemy engine, creating it on first call."""
        with self._engine_lock:
            if connection_name not in self._engine_pool:
                connection = self.get_connection(connection_name)
                if not connection:
                    raise ValueError(f"Connection '{connection_name}' not found")
                self._engine_pool[connection_name] = self._build_sa_engine(connection)
            return self._engine_pool[connection_name]

    def dispose_engine(self, connection_name: str) -> None:
        """Dispose and remove the cached engine (call on connection delete)."""
        with self._engine_lock:
            if connection_name in self._engine_pool:
                try:
                    self._engine_pool[connection_name].dispose()
                except Exception:
                    pass
                del self._engine_pool[connection_name]

    def get_connection_object(self, connection):
        """Get a database connection object"""
        db_type = connection['type']
        
        if db_type not in self.DB_DRIVERS:
            raise ValueError(f"Unsupported database type: {db_type}")
        
        driver_info = self.DB_DRIVERS[db_type]
        module_name = driver_info['module']
        
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            raise ImportError(f"Required driver '{module_name}' is not installed")
        
        # Prepare connection parameters
        params = {
            'server': connection.get('server', ''),
            'port': connection.get('port', ''),
            'username': connection.get('username', ''),
            'password': connection.get('password', ''),
            'database': connection.get('database', '')
        }
        
        # Set default ports if not provided
        if not params['port']:
            default_ports = {
                'postgresql': '5432',
                'mysql': '3306',
                'mssql': '1433',
                'oracle': '1521'
            }
            params['port'] = default_ports.get(db_type, '')
        
        try:
            if db_type == 'mssql':
                import pyodbc
                drivers = pyodbc.drivers()
                
                driver = None
                if 'ODBC Driver 18 for SQL Server' in drivers:
                    driver = 'ODBC Driver 18 for SQL Server'
                elif 'ODBC Driver 17 for SQL Server' in drivers:
                    driver = 'ODBC Driver 17 for SQL Server'
                else:
                    sql_server_drivers = [d for d in drivers if 'SQL Server' in d]
                    if sql_server_drivers:
                        driver = sql_server_drivers[0]
                
                if not driver:
                    raise ValueError(f"No suitable ODBC Driver found for SQL Server. Available drivers: {drivers}")
                
                conn_str = f"DRIVER={{{driver}}};SERVER={params['server']},{params['port']};DATABASE={params['database']};UID={params['username']};PWD={params['password']};TrustServerCertificate=yes"
                return pyodbc.connect(conn_str)
                
            elif db_type == 'sqlite':
                return module.connect(params['database'])
            else:
                conn_str = driver_info['connect_string'].format(**params)
                return module.connect(conn_str)
                
        except Exception as e:
            raise Exception(f"Failed to connect to database: {str(e)}")
    
    # In database_manager.py - Add these methods
    def get_table_schema(self, connection, table_name):
        """Get detailed table schema including full schema.table support"""
        try:
            conn = self.get_connection_object(connection)
            cursor = conn.cursor()

            print(f"🔧 Getting schema for table: {table_name}")

            db_type = connection['type']

            # ---------------------------
            # Parse schema.table input
            # ---------------------------
            if "." in table_name:
                schema, tbl = table_name.split(".", 1)
            else:
                # Default schema based on DB type
                schema = "dbo" if db_type == "mssql" else None
                tbl = table_name

            # ---------------------------
            # MSSQL VERSION
            # ---------------------------
            if db_type == 'mssql':

                cursor.execute("""
                    SELECT 
                        COLUMN_NAME,
                        DATA_TYPE,
                        IS_NULLABLE,
                        CHARACTER_MAXIMUM_LENGTH
                    FROM INFORMATION_SCHEMA.COLUMNS 
                    WHERE TABLE_NAME = ?
                    AND TABLE_SCHEMA = ?
                    ORDER BY ORDINAL_POSITION
                """, (tbl, schema))

                rows = cursor.fetchall()

                columns = []
                for row in rows:
                    col_name, col_type, nullable, max_len = row

                    column_info = {
                        'name': col_name,
                        'type': col_type,
                        'nullable': (nullable == "YES"),
                        'max_length': max_len,
                        'type_display': f"{col_type}({max_len})" if max_len else col_type
                    }
                    columns.append(column_info)

                print(f"✅ Found {len(columns)} columns for {schema}.{tbl}")
                return {'columns': columns}

            # ---------------------------
            # POSTGRESQL VERSION
            # ---------------------------
            elif db_type == "postgresql":

                cursor.execute("""
                    SELECT 
                        column_name,
                        data_type,
                        is_nullable,
                        character_maximum_length
                    FROM information_schema.columns
                    WHERE table_name = %s
                    AND (%s IS NULL OR table_schema = %s)
                    ORDER BY ordinal_position
                """, (tbl, schema, schema))

                rows = cursor.fetchall()

                columns = []
                for row in rows:
                    col_name, col_type, nullable, max_len = row

                    column_info = {
                        'name': col_name,
                        'type': col_type,
                        'nullable': (nullable == "YES"),
                        'max_length': max_len,
                        'type_display': f"{col_type}({max_len})" if max_len else col_type
                    }
                    columns.append(column_info)

                return {'columns': columns}

            # ---------------------------
            # MYSQL VERSION
            # ---------------------------
            elif db_type == "mysql":

                cursor.execute("""
                    SELECT 
                        COLUMN_NAME,
                        COLUMN_TYPE,
                        IS_NULLABLE,
                        CHARACTER_MAXIMUM_LENGTH
                    FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_NAME = %s
                    AND (%s IS NULL OR TABLE_SCHEMA = %s)
                    ORDER BY ORDINAL_POSITION
                """, (tbl, schema, schema))

                rows = cursor.fetchall()

                columns = []
                for row in rows:
                    col_name, col_type, nullable, max_len = row

                    column_info = {
                        'name': col_name,
                        'type': col_type,
                        'nullable': (nullable == "YES"),
                        'max_length': max_len,
                        'type_display': col_type
                    }
                    columns.append(column_info)

                return {'columns': columns}

            # ---------------------------
            # SQLITE VERSION
            # ---------------------------
            elif db_type == "sqlite":

                # SQLite does not have schemas, so ignore schema
                cursor.execute(f"PRAGMA table_info('{tbl}')")
                rows = cursor.fetchall()

                columns = []
                for row in rows:
                    # PRAGMA table_info fields
                    # cid | name | type | notnull | dflt_value | pk
                    col_name = row[1]
                    col_type = row[2]
                    nullable = (row[3] == 0)

                    column_info = {
                        'name': col_name,
                        'type': col_type,
                        'nullable': nullable,
                        'max_length': None,
                        'type_display': col_type
                    }
                    columns.append(column_info)

                return {'columns': columns}

            else:
                print(f"❌ Unsupported database type: {db_type}")
                return {'columns': []}

        except Exception as e:
            print(f"❌ Error getting table schema for {table_name}: {e}")
            return {'columns': [], 'error': str(e)}



    def get_sample_data(self, connection, table_name, selected_columns=None, limit=5):
        """Get sample data from table - works for schema.table and all DB types"""
        try:
            conn = self.get_connection_object(connection)
            cursor = conn.cursor()

            print(f"🔧 Getting sample data for table: {table_name}")
            print(f"🔧 Selected columns: {selected_columns}")

            # --- Parse schema.table format ---
            if "." in table_name:
                schema, tbl = table_name.split(".", 1)
                full_table = f"{schema}.{tbl}"
            else:
                full_table = table_name  # assume dbo/table if schema not supplied

            # --- Build SELECT column list safely ---
            if selected_columns and len(selected_columns) > 0:
                columns_str = ", ".join([f"[{col}]" for col in selected_columns])
            else:
                columns_str = "*"

            # --- Build query based on database type ---
            db_type = connection['type']

            if db_type == 'mssql':
                # SQL Server MUST have TOP directly after SELECT
                query = f"SELECT TOP {limit} {columns_str} FROM {full_table}"

            elif db_type in ("mysql", "postgresql"):
                query = f"SELECT {columns_str} FROM {full_table} LIMIT {limit}"

            elif db_type == "sqlite":
                query = f"SELECT {columns_str} FROM {full_table} LIMIT {limit}"

            else:
                print(f"❌ Unsupported DB type for sample data: {db_type}")
                return []

            print(f"🔧 Executing query: {query}")
            cursor.execute(query)

            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()

            sample_data = []
            for row in rows:
                sample_data.append(dict(zip(columns, row)))

            print(f"✅ Found {len(sample_data)} sample rows for table {table_name}")
            return sample_data

        except Exception as e:
            print(f"❌ Error getting sample data for {table_name}: {e}")
            return []


# Create a global instance
db_manager = DatabaseConnectionManager()