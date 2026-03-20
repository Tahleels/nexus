"""
agent_manager.py — CRUD and schema-introspection layer for BI (NLQ) agents.

A "BI agent" (app_agents row) is a named binding of:
  - a target database connection (by name, resolved via database_manager.db_manager)
  - a set of selected schema.table names and, per table, selected columns
  - an AI-generated schema_context (table/column descriptions, inferred purpose,
    sample values) used to ground nlq_engine's SQL generation
  - optional semantic_rules (per-table natural-language constraints injected into
    the SQL prompt by nlq_engine.hybrid_schema_pruning)

AgentManager (this module) owns:
  - load_agents/save_agents/create_agent/get_agent/update_agent/delete_agent —
    full-table-overwrite persistence against app_agents (SQL Server)
  - get_database_tables/get_table_columns/get_database_schema — SQLAlchemy
    `inspect()`-based introspection of the *target* database (not app DB),
    used while building/editing an agent in the UI
  - generate_schema_context/analyze_table_structure — dynamic, heuristic column
    purpose inference (name/type/sample-value pattern matching) plus an
    AI-generated narrative summary (generate_ai_schema_analysis, OpenAI
    gpt-3.5-turbo) describing the schema's business domain and likely metrics

Creating or updating an agent kicks off two side effects in the background:
  1. precompute_agent_embeddings() (nlq_engine) — eagerly embeds every selected
     table's description text and persists it to app_schema_embedding_cache so
     the agent's first real NLQ query doesn't pay a cold-encode cost.
  2. Schema-graph invalidation (delete_agent_graph) — forces nlq_engine's
     schema-graph join-path layer to rebuild from the new schema_context on
     next use.

The module-level `agent_manager` instance is the conventional import target.
"""
import json
import os
from typing import Dict, List, Optional
from database_manager import db_manager
from app_db import get_app_db
import threading
from nlq_engine import NLQEngine, precompute_agent_embeddings

try:
    from schema_graph import get_schema_graph as _get_schema_graph
except ImportError:
    def _get_schema_graph():  # noqa: E302
        """Fallback used when schema_graph.py cannot be imported — always returns None."""
        return None
import pandas as pd
from sqlalchemy import create_engine, text, inspect
import openai
import datetime



class AgentManager:
    """Loads, saves, and introspects BI agents and their target-database schemas.

    Agents are persisted as a flat list in app_agents; all mutation methods
    (create/update/delete) read the full list, modify it, and write it back
    via save_agents() (full overwrite, not row-level SQL DML).
    """

    def __init__(self):
        """No state to initialize — all methods are stateless wrappers around app_db/db_manager."""
        pass

    def load_agents(self):
        """Load all agents from the database."""
        try:
            with get_app_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT name, description, database_connection,
                           selected_tables, selected_columns, schema_context,
                           created_at, created_by, semantic_rules
                    FROM app_agents
                    ORDER BY id
                """)
                rows = cursor.fetchall()
                agents = []
                for r in rows:
                    agents.append({
                        "name":                r[0],
                        "description":         r[1] or "",
                        "database_connection": r[2] or "",
                        "selected_tables":     json.loads(r[3]) if r[3] else [],
                        "selected_columns":    json.loads(r[4]) if r[4] else {},
                        "schema_context":      json.loads(r[5]) if r[5] else None,
                        "created_at":          str(r[6]) if r[6] else "",
                        "created_by":          r[7] or 0,
                        "semantic_rules":      json.loads(r[8]) if r[8] else {},
                    })
                return agents
        except Exception as e:
            print(f"Error loading agents: {e}")
            return []

    def save_agents(self, agents):
        """Replace all agents in the database (full overwrite)."""
        try:
            with get_app_db() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM app_agents")
                for a in agents:
                    cursor.execute("""
                        INSERT INTO app_agents
                            (name, description, database_connection,
                             selected_tables, selected_columns, schema_context,
                             created_at, created_by, semantic_rules)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        a.get("name", ""), a.get("description", ""),
                        a.get("database_connection", ""),
                        json.dumps(a.get("selected_tables", [])),
                        json.dumps(a.get("selected_columns", {})),
                        json.dumps(a.get("schema_context")) if a.get("schema_context") else None,
                        a.get("created_at", datetime.datetime.utcnow().isoformat()),
                        a.get("created_by") or 0,
                        json.dumps(a.get("semantic_rules")) if a.get("semantic_rules") else None)
                conn.commit()
            return True
        except Exception as e:
            print(f"Error saving agents: {e}")
            return False
    
    def create_agent(self, agent_data):
        """Create a new agent"""
        agents = self.load_agents()
        
        # Check if agent name already exists
        if any(agent['name'] == agent_data['name'] for agent in agents):
            return False, "Agent name already exists"
        
        agents.append(agent_data)
        success = self.save_agents(agents)

        if success:
            threading.Thread(
                target=precompute_agent_embeddings,
                args=(agent_data,),
                daemon=True,
            ).start()
            return True, "Agent created successfully"
        else:
            return False, "Failed to create agent"
    
    def get_agent(self, agent_name):
        """Get a specific agent by name"""
        agents = self.load_agents()
        for agent in agents:
            if agent['name'] == agent_name:
                return agent
        return None
    
    def delete_agent(self, agent_name):
        """Delete an agent by name"""
        agents = self.load_agents()
        initial_count = len(agents)

        agents = [agent for agent in agents if agent['name'] != agent_name]

        if len(agents) < initial_count:
            success = self.save_agents(agents)
            if success:
                try:
                    graph = _get_schema_graph()
                    if graph is not None and getattr(graph, "enabled", False):
                        graph.delete_agent_graph(agent_name)
                except Exception as _ge:
                    print(f"Schema graph cleanup failed: {_ge}")
                return True, "Agent deleted successfully"
            else:
                return False, "Failed to save changes"
        else:
            return False, "Agent not found"

    def update_agent(self, agent_name, updated_data):
        """Update an existing agent's fields (tables, columns, description, etc.)"""
        agents = self.load_agents()
        for i, agent in enumerate(agents):
            if agent['name'] == agent_name:
                # Preserve created_at and name; overwrite everything else
                updated_data['name'] = agent_name
                updated_data['created_at'] = agent.get('created_at', updated_data.get('created_at'))
                agents[i] = updated_data
                success = self.save_agents(agents)
                if success:
                    # Re-embed updated schema immediately in background
                    threading.Thread(
                        target=precompute_agent_embeddings,
                        args=(agents[i],),
                        daemon=True,
                    ).start()
                    # Invalidate schema graph so it rebuilds on next query
                    try:
                        graph = _get_schema_graph()
                        if graph is not None and getattr(graph, "enabled", False):
                            graph.delete_agent_graph(agent_name)
                    except Exception as _ge:
                        print(f"Schema graph invalidation failed: {_ge}")
                    return True, "Agent updated successfully"
                else:
                    return False, "Failed to save changes"
        return False, "Agent not found"

    def _create_engine(self, connection):
        """Create SQLAlchemy engine from connection data"""
        db_type = connection['type']
        
        if db_type == 'postgresql':
            url = f"postgresql://{connection['username']}:{connection['password']}@{connection['server']}:{connection['port']}/{connection['database']}"
        elif db_type == 'mysql':
            url = f"mysql+pymysql://{connection['username']}:{connection['password']}@{connection['server']}:{connection['port']}/{connection['database']}"
        elif db_type == 'mssql':
            # Try to find the best available ODBC driver
            import pyodbc
            drivers = pyodbc.drivers()
            
            if 'ODBC Driver 17 for SQL Server' in drivers:
                driver = 'ODBC Driver 17 for SQL Server'
            # elif 'ODBC Driver 18 for SQL Server' in drivers:
            #     driver = 'ODBC Driver 18 for SQL Server'
            else:
                raise ValueError(f"No suitable SQL Server driver found. Available: {drivers}")
            
            if not driver:
                raise ValueError(f"No suitable ODBC Driver found for SQL Server. Available drivers: {drivers}")
            
            print(f"Using ODBC driver for schema: {driver}")
            
            # Enhanced SQL Server connection string
            from urllib.parse import quote_plus

            conn_str = (
                f"DRIVER={{{driver}}};"
                f"SERVER={connection['server']},{connection['port']};"
                f"DATABASE={connection['database']};"
                f"UID={connection['username']};"
                f"PWD={connection['password']};"
                "TrustServerCertificate=yes;"
            )

            connection_url = f"mssql+pyodbc:///?odbc_connect={quote_plus(conn_str)}"

            engine = create_engine(connection_url)
            
        elif db_type == 'sqlite':
            url = f"sqlite:///{connection['database']}"
        else:
            raise ValueError(f"Unsupported database type: {db_type}")
        
        print(f"Creating engine for {db_type}: {connection['server']}")
        
        # Create engine with connection timeouts
        return engine

    def get_database_tables(self, connection_name):
        """Return FULL schema.table names for all tables (MSSQL, MySQL, PostgreSQL)"""
        connection = db_manager.get_connection(connection_name)
        if not connection:
            return None, "Connection not found"

        print(f"🔧 Fetching table names for: {connection_name}")

        engine = None
        try:
            engine = self._create_engine(connection)

            # test connection
            with engine.connect() as test_conn:
                test_conn.execute(text("SELECT 1"))

            inspector = inspect(engine)

            tables = []

            # Get all schemas first
            schemas = inspector.get_schema_names()
            print(f"🔧 Schemas found: {schemas}")

            for schema in schemas:

                # Skip system schemas
                if schema.lower() in ["information_schema", "sys"]:
                    continue

                schema_tables = inspector.get_table_names(schema=schema)

                for t in schema_tables:
                    # Add schema.table format
                    tables.append(f"{schema}.{t}")

            print(f"✅ Found {len(tables)} tables across all schemas")

            return [{'name': t} for t in tables], None

        except Exception as e:
            print(f"❌ Error fetching tables: {e}")
            return None, str(e)

        finally:
            if engine:
                engine.dispose()



    def get_table_columns(self, connection_name, table_names):
        """Return column/PK/FK details for each of table_names via SQLAlchemy inspect().

        Args:
            connection_name: Name of a saved connection (resolved via db_manager).
            table_names: List of 'table' or 'schema.table' names to introspect.

        Returns:
            Tuple of (table_details, error). table_details is a list of dicts with
            name/columns/primary_keys/foreign_keys (or an 'error' key per-table on
            failure); error is a top-level error string, or None on success.
        """
        connection = db_manager.get_connection(connection_name)
        if not connection:
            return None, "Connection not found"

        print(f"🔧 Fetching columns for {len(table_names)} tables: {table_names}")

        engine = None
        try:
            engine = self._create_engine(connection)
            inspector = inspect(engine)

            table_details = []

            for table_name in table_names:
                print(f"🔧 Processing table: {table_name}")

                # --- FIX: parse schema.table ---
                if "." in table_name:
                    schema, tbl = table_name.split(".", 1)
                else:
                    schema = "dbo"
                    tbl = table_name

                try:
                    columns = inspector.get_columns(tbl, schema=schema)
                    primary_keys = inspector.get_pk_constraint(tbl, schema=schema)
                    foreign_keys = inspector.get_foreign_keys(tbl, schema=schema)

                    table_info = {
                        'name': table_name,
                        'columns': [],
                        'primary_keys': primary_keys.get('constrained_columns', []),
                        'foreign_keys': foreign_keys
                    }

                    for column in columns:
                        table_info['columns'].append({
                            'name': column['name'],
                            'type': str(column['type']),
                            'nullable': column['nullable'],
                            'default': column.get('default')
                        })

                    print(f"✅ Processed table {table_name} with {len(columns)} columns")
                    table_details.append(table_info)

                except Exception as table_error:
                    print(f"❌ Error processing table {table_name}: {table_error}")
                    table_details.append({
                        'name': table_name,
                        'columns': [],
                        'error': str(table_error)
                    })

            return table_details, None

        except Exception as e:
            print(f"❌ Error fetching columns: {e}")
            return None, str(e)

        finally:
            if engine:
                engine.dispose()


    def get_database_schema(self, connection_name, selected_tables=None):
        """Get database schema with full schema.table names"""
        connection = db_manager.get_connection(connection_name)
        if not connection:
            return None, "Connection not found"
        
        print(f"🔧 Fetching schema for: {connection_name}")
        print(f"🔧 Selected tables parameter: {selected_tables}")
        
        engine = None
        try:
            engine = self._create_engine(connection)
            
            # Test connection first
            with engine.connect() as test_conn:
                test_conn.execute(text("SELECT 1"))
            
            inspector = inspect(engine)

            # -------- FIX STARTS HERE -------- #
            print("🔧 Loading schemas...")
            schemas = inspector.get_schema_names()
            print(f"🔧 Schemas found: {schemas}")

            tables = []

            for schema in schemas:
                if schema.lower() in ["information_schema", "sys"]:
                    continue

                schema_tables = inspector.get_table_names(schema=schema)

                for t in schema_tables:
                    tables.append(f"{schema}.{t}")

            print(f"✅ Found {len(tables)} tables across all schemas")

            # If user selected specific tables → return detailed columns
            if selected_tables and len(selected_tables) > 0:
                print(f"🔧 Fetching columns for {len(selected_tables)} tables: {selected_tables}")
                result = self._get_tables_with_columns(inspector, selected_tables)
                print(f"🔧 Returning {len(result)} tables WITH columns")
                return result, None
            
            # Default: return schema-qualified names
            print(f"🔧 Returning {len(tables)} schema-qualified tables for initial load")
            return [{'name': t} for t in tables], None

            # -------- FIX ENDS HERE -------- #

        except Exception as e:
            print(f"❌ Error fetching schema: {e}")
            return None, f"Error fetching schema: {str(e)}"
        finally:
            if engine:
                engine.dispose()


    def _get_tables_with_columns(self, inspector, table_names):
        """Get detailed column information for specific tables"""
        table_details = []
        
        for table_name in table_names:
            try:
                print(f"🔧 Processing table columns: {table_name}")
                
                # Get columns for this table
                if "." in table_name:
                    schema, tbl = table_name.split(".", 1)
                else:
                    schema = "dbo"
                    tbl = table_name

                columns = inspector.get_columns(tbl, schema=schema)

                
                table_info = {
                    'name': table_name,
                    'columns': []
                }
                
                for column in columns:
                    column_info = {
                        'name': column['name'],
                        'type': str(column['type']),
                        'nullable': column['nullable'],
                        'default': column.get('default')
                    }
                    table_info['columns'].append(column_info)
                
                table_details.append(table_info)
                print(f"✅ Processed table {table_name} with {len(columns)} columns")
                
            except Exception as table_error:
                print(f"❌ Error processing table {table_name}: {table_error}")
                # Still add the table but with error info
                table_details.append({
                    'name': table_name,
                    'columns': [],
                    'error': str(table_error)
                })
                continue
        
        return table_details
    
    def generate_schema_context(self, connection_name, selected_tables, selected_columns):
        """Build the full schema_context blob stored on an agent: per-table column analysis
        (analyze_table_structure) plus an AI-generated narrative (generate_ai_schema_analysis)
        describing business domain, relationships, and likely metrics.

        Args:
            connection_name: Target database connection name.
            selected_tables: List of table names to analyze.
            selected_columns: Dict of table_name -> list of selected column names.

        Returns:
            Tuple of (schema_context, error). schema_context contains 'tables' (per-table
            analysis), 'ai_analysis' text, and '_schema_in_tokens'/'_schema_out_tokens'
            token counts for the AI call; error is a string on failure, else None.
        """
        try:
            print(f"🔧 Starting schema context generation for: {connection_name}")
            print(f"🔧 Selected tables: {selected_tables}")
            print(f"🔧 Selected columns: {selected_columns}")
            
            # Get connection details
            connection = db_manager.get_connection(connection_name)
            if not connection:
                error_msg = f"Connection '{connection_name}' not found"
                print(f"❌ {error_msg}")
                return None, error_msg
            
            print(f"✅ Found connection: {connection['name']}")
            
            schema_context = {
                "database_type": connection.get('type', 'unknown'),
                "tables": [],
                "relationships": [],
                "business_context": "",
                "generated_at": datetime.datetime.now().isoformat()
            }
            
            # Analyze each selected table
            for table_name in selected_tables:
                print(f"🔧 Analyzing table: {table_name}")
                table_analysis = self.analyze_table_structure(connection, table_name, selected_columns.get(table_name, []))
                schema_context["tables"].append(table_analysis)
                print(f"✅ Analyzed table: {table_name}")

            # Generate AI-powered context
            print("🔧 Generating AI analysis...")
            ai_context, _in_tok, _out_tok = self.generate_ai_schema_analysis(schema_context)
            schema_context["ai_analysis"]      = ai_context
            schema_context["_schema_in_tokens"]  = _in_tok
            schema_context["_schema_out_tokens"] = _out_tok
            print("✅ AI analysis completed")

            return schema_context, None
            
        except Exception as e:
            error_msg = f"Error generating schema context: {str(e)}"
            print(f"❌ {error_msg}")
            import traceback
            traceback.print_exc()
            return None, error_msg
    def _generate_table_description_dynamic(self, table_name, column_analysis):
        """Generate table description based on actual column analysis"""
        if not column_analysis:
            return f"Table: {table_name}"
        
        key_columns = [col for col in column_analysis if 'identifier' in col['inferred_purpose'].lower()]
        main_columns = [col for col in column_analysis if col not in key_columns][:3]
        
        if key_columns and main_columns:
            return f"Table {table_name} with {key_columns[0]['name']} as identifier and columns like {', '.join(col['name'] for col in main_columns)}"
        elif column_analysis:
            return f"Table {table_name} with columns: {', '.join(col['name'] for col in column_analysis[:3])}"
        else:
            return f"Table: {table_name}"

    def _infer_table_purpose_dynamic(self, table_name, column_analysis):
        """Dynamically infer table purpose from actual columns"""
        purposes = set()
        
        for col in column_analysis:
            purpose = col.get('inferred_purpose', '').lower()
            
            if any(keyword in purpose for keyword in ['identifier', 'key']):
                purposes.add('Data relationships')
            elif any(keyword in purpose for keyword in ['date', 'time']):
                purposes.add('Temporal tracking')
            elif any(keyword in purpose for keyword in ['amount', 'price', 'total', 'monetary']):
                purposes.add('Financial data')
            elif any(keyword in purpose for keyword in ['user', 'customer', 'agent']):
                purposes.add('User management')
            elif any(keyword in purpose for keyword in ['config', 'setting']):
                purposes.add('System configuration')
            elif any(keyword in purpose for keyword in ['status', 'type', 'category']):
                purposes.add('Classification')
            elif any(keyword in purpose for keyword in ['email', 'phone', 'address']):
                purposes.add('Contact information')
        
        # Add table name based inference
        table_lower = table_name.lower()
        if 'agent' in table_lower:
            purposes.add('Agent management')
        if 'user' in table_lower:
            purposes.add('User management')
        if 'config' in table_lower:
            purposes.add('Configuration')
        
        return ', '.join(sorted(purposes)) if purposes else 'Business data storage'

    def analyze_table_structure(self, connection, table_name, selected_columns):
        """Dynamically analyze table structure using actual schema and sample data"""
        try:
            print(f"🔧 Analyzing table structure: {table_name}")
            print(f"🔧 Selected columns: {selected_columns}")
            
            # Get table schema
            table_schema = db_manager.get_table_schema(connection, table_name)
            
            # Get sample data for selected columns only
            sample_data = db_manager.get_sample_data(
                connection, table_name, 
                selected_columns=selected_columns, 
                limit=5
            )
            
            print(f"🔧 Found {len(table_schema.get('columns', []))} columns in schema")
            print(f"🔧 Found {len(sample_data)} sample rows")
            
            # Analyze each column dynamically
            column_analysis = []
            for column in table_schema.get('columns', []):
                # Only process selected columns
                if not selected_columns or column['name'] in selected_columns:
                    # Extract actual sample values from the data
                    sample_values = self.extract_column_samples(sample_data, column['name'])
                    
                    # Dynamically infer purpose based on actual data
                    inferred_purpose = self.infer_column_purpose_dynamic(
                        column['name'], 
                        column['type'], 
                        sample_values
                    )
                    
                    column_info = {
                        "name": column['name'],
                        "type": column.get('type_display', column['type']),
                        "sample_values": sample_values,
                        "inferred_purpose": inferred_purpose
                    }
                    column_analysis.append(column_info)
                    
                    print(f"🔧 Analyzed column {column['name']}: {inferred_purpose}")
            
            # Create dynamic table description based on actual columns
            table_description = self._generate_table_description_dynamic(table_name, column_analysis)
            table_purpose = self._infer_table_purpose_dynamic(table_name, column_analysis)
            
            result = {
                "table_name": table_name,
                "description": table_description,
                "columns": column_analysis,
                "sample_row_count": len(sample_data),
                "estimated_purpose": table_purpose
            }
            
            print(f"✅ Dynamic table analysis complete")
            return result
            
        except Exception as e:
            error_msg = f"Error analyzing table {table_name}: {str(e)}"
            print(f"❌ {error_msg}")
            import traceback
            traceback.print_exc()
            return {
                "table_name": table_name,
                "error": error_msg,
                "columns": []
            }

    def generate_ai_schema_analysis(self, schema_context):
        """Use AI to analyze the schema and generate meaningful context.

        Returns (ai_text, input_tokens, output_tokens).
        """
        try:
            prompt = self.build_schema_analysis_prompt(schema_context)

            from llm_providers.factory import get_provider
            from llm_providers.errors import LLMConfigError, LLMError

            # Pinned to OpenAI explicitly (not get_provider()'s DEFAULT_LLM_PROVIDER
            # fallback) — "gpt-3.5-turbo" below is an OpenAI-only model id, so
            # this must not silently start targeting Bedrock if the global
            # default provider changes.
            try:
                provider = get_provider("openai")
            except LLMConfigError:
                return "OpenAI API key not configured. Please set OPENAI_API_KEY environment variable.", 0, 0

            response = provider.chat(
                system="You are a data analysis expert. Analyze database schemas and provide meaningful business context.",
                messages=[{"role": "user", "content": prompt}],
                model="gpt-3.5-turbo",
                temperature=0.1,
                max_tokens=1000
            )

            ai_text  = response.content
            _in_tok  = int(response.usage.prompt_tokens or 0)
            _out_tok = int(response.usage.completion_tokens or 0)
            return ai_text, _in_tok, _out_tok

        except ImportError:
            return "OpenAI library not installed. Please install with: pip install openai", 0, 0
        except LLMError as e:
            return f"AI analysis unavailable: {str(e)}", 0, 0
        except Exception as e:
            return f"AI analysis unavailable: {str(e)}", 0, 0

    def build_schema_analysis_prompt(self, schema_context):
        """Build a comprehensive prompt for AI analysis"""
        print(f"🔧 Building AI prompt with schema context")

        tables_info = []

        for table in schema_context["tables"]:
            if "error" not in table:
                columns_info = []
                for col in table.get("columns", []):
                    samples_text = f" [Samples: {', '.join(col['sample_values'][:2])}]" if col['sample_values'] else ""
                    columns_info.append(f"- {col['name']} ({col['type']}): {col['inferred_purpose']}{samples_text}")

                tables_info.append(f"""
    Table: {table['table_name']}
    Description: {table['description']}
    Business Purpose: {table['estimated_purpose']}
    Sample Rows: {table['sample_row_count']}
    Columns:
    {chr(10).join(columns_info)}
                """)

        prompt = f"""
    Analyze this database schema and sample data to provide:

    1. BUSINESS DOMAIN: What business domain this likely serves based on table names, columns, and sample data
    2. DATA RELATIONSHIPS: How the tables and columns might relate to each other
    3. KEY BUSINESS METRICS: What important metrics could be calculated from this data
    4. COMMON BUSINESS QUESTIONS: What typical questions users might ask about this data
    5. DATA INSIGHTS: Any interesting patterns or insights from the column purposes and sample values

    Database Type: {schema_context['database_type']}

    Tables Analyzed:
    {chr(10).join(tables_info)}

    Provide specific, actionable analysis based on the actual column names, data types, and sample values shown above.
    Focus on what this data can tell us about the business operations and user needs.
    """

        print(f"🔧 AI Prompt length: {len(prompt)} characters")
        return prompt

    def _generate_table_description(self, table_name, df):
        """Generate AI-powered description for a table"""
        # This is a simplified version - you can enhance this with actual AI
        sample_summary = []
        for col in df.columns:
            if df[col].dtype in ['object', 'string']:
                unique_vals = df[col].unique()[:3]
                sample_summary.append(f"{col}: {', '.join(map(str, unique_vals))}...")
            else:
                sample_summary.append(f"{col}: numeric data")
        
        description = f"Table '{table_name}' contains sample data with columns showing: {'; '.join(sample_summary)}"
        return description
    def extract_column_samples(self, sample_data, column_name):
        """Dynamically extract sample values from actual data"""
        try:
            samples = []
            for row in sample_data:
                if column_name in row and row[column_name] is not None:
                    value = str(row[column_name])
                    # Clean and truncate very long values
                    if len(value) > 100:
                        value = value[:97] + '...'
                    samples.append(value)
                    if len(samples) >= 3:  # Get max 3 samples
                        break
            return samples
        except Exception as e:
            print(f"⚠️ Error extracting samples for {column_name}: {e}")
            return []

    def infer_column_purpose_dynamic(self, column_name, column_type, sample_values):
        """Dynamically infer column purpose based on name, type, and actual data"""
        column_name_lower = column_name.lower()
        column_type_str = str(column_type).lower()
        
        # Analyze sample values for patterns
        sample_analysis = self._analyze_sample_values(sample_values)
        
        # Name-based inference
        name_based_purpose = self._infer_from_column_name(column_name_lower)
        if name_based_purpose:
            return name_based_purpose
        
        # Data-based inference from actual samples
        data_based_purpose = self._infer_from_sample_data(sample_analysis, column_type_str)
        if data_based_purpose:
            return data_based_purpose
        
        # Type-based fallback
        return self._infer_from_column_type(column_type_str)

    def _analyze_sample_values(self, sample_values):
        """Analyze sample values for patterns"""
        if not sample_values:
            return {"empty": True}
        
        analysis = {
            "count": len(sample_values),
            "has_numeric": False,
            "has_dates": False,
            "has_emails": False,
            "has_uuids": False,
            "has_booleans": False,
            "unique_count": len(set(sample_values)),
            "avg_length": sum(len(str(v)) for v in sample_values) / len(sample_values) if sample_values else 0
        }
        
        for value in sample_values:
            value_str = str(value).lower()
            
            # Check for numeric patterns
            if value_str.replace('.', '').replace('-', '').isdigit():
                analysis["has_numeric"] = True
            
            # Check for date patterns
            if any(pattern in value_str for pattern in ['202', '201', '200', '199', '-01-', '-12-']):
                analysis["has_dates"] = True
            
            # Check for email patterns
            if '@' in value_str and '.' in value_str:
                analysis["has_emails"] = True
            
            # Check for UUID patterns
            if len(value_str) == 36 and '-' in value_str:
                analysis["has_uuids"] = True
            
            # Check for boolean patterns
            if value_str in ['true', 'false', 'yes', 'no', '1', '0']:
                analysis["has_booleans"] = True
        
        return analysis

    def _infer_from_column_name(self, column_name_lower):
        """Infer purpose from column name patterns"""
        patterns = {
            'id': 'Unique identifier',
            'name': 'Name or title',
            'title': 'Title or heading',
            'description': 'Description or details',
            'date': 'Date information',
            'time': 'Time information',
            'created': 'Creation timestamp',
            'updated': 'Last modification timestamp',
            'email': 'Email address',
            'phone': 'Phone number',
            'address': 'Physical address',
            'price': 'Monetary value',
            'amount': 'Numerical quantity',
            'total': 'Total amount',
            'status': 'Status indicator',
            'type': 'Classification type',
            'category': 'Category classification',
            'role': 'Role or permission level',
            'config': 'Configuration settings',
            'user': 'User reference',
            'customer': 'Customer reference',
            'agent': 'Agent reference'
        }
        
        for pattern, purpose in patterns.items():
            if pattern in column_name_lower:
                return purpose
        
        return None

    def _infer_from_sample_data(self, sample_analysis, column_type):
        """Infer purpose from actual sample data"""
        if sample_analysis.get("empty"):
            return "No sample data available"
        
        if sample_analysis["has_emails"]:
            return "Email addresses"
        
        if sample_analysis["has_dates"]:
            return "Date/time information"
        
        if sample_analysis["has_numeric"] and sample_analysis["avg_length"] < 10:
            return "Numerical values"
        
        if sample_analysis["has_uuids"]:
            return "Unique identifiers"
        
        if sample_analysis["has_booleans"]:
            return "Boolean flags"
        
        if sample_analysis["unique_count"] == sample_analysis["count"]:
            return "Unique values"
        
        return "Textual data"

    def _infer_from_column_type(self, column_type):
        """Fallback inference based on column type"""
        if any(t in column_type for t in ['int', 'numeric', 'decimal', 'float']):
            return 'Numerical data'
        elif any(t in column_type for t in ['varchar', 'text', 'char', 'string']):
            return 'Text data'
        elif any(t in column_type for t in ['date', 'time', 'datetime']):
            return 'Date/time data'
        elif any(t in column_type for t in ['bool', 'bit']):
            return 'Boolean data'
        else:
            return 'General data'

# Create a global instance
agent_manager = AgentManager()