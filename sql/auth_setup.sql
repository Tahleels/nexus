-- =============================================================
-- AUTH SETUP SCRIPT  |  SQL Server (SSMS)
-- Run this ONCE against your application database.
-- Fill in the database name below before executing.
-- =============================================================

USE [nexus];
GO

-- -------------------------------------------------------------
-- 1. ROLES
-- -------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'roles')
BEGIN
    CREATE TABLE roles (
        id          INT IDENTITY(1,1) PRIMARY KEY,
        name        NVARCHAR(50)  NOT NULL UNIQUE,   -- admin | dev | user
        description NVARCHAR(255) NULL,
        created_at  DATETIME2     NOT NULL DEFAULT GETUTCDATE()
    );

    INSERT INTO roles (name, description) VALUES
        ('admin', 'Full access including user management'),
        ('dev',   'Access to all APIs except user management'),
        ('user',  'Chat and dashboard access only');
END
GO

-- -------------------------------------------------------------
-- 2. USERS
-- -------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'users')
BEGIN
    CREATE TABLE users (
        id            INT IDENTITY(1,1) PRIMARY KEY,
        username      NVARCHAR(100) NOT NULL UNIQUE,
        email         NVARCHAR(255) NOT NULL UNIQUE,
        password_hash NVARCHAR(255) NOT NULL,          -- bcrypt hash
        role_id       INT           NOT NULL,
        is_active     BIT           NOT NULL DEFAULT 1,
        created_by    INT           NULL,              -- FK to users.id
        created_at    DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
        last_login    DATETIME2     NULL,
        CONSTRAINT fk_users_role    FOREIGN KEY (role_id)    REFERENCES roles(id),
        CONSTRAINT fk_users_creator FOREIGN KEY (created_by) REFERENCES users(id)
    );
END
GO

-- -------------------------------------------------------------
-- 3. SESSIONS  (server-side session store)
-- -------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'user_sessions')
BEGIN
    CREATE TABLE user_sessions (
        id         INT IDENTITY(1,1) PRIMARY KEY,
        user_id    INT           NOT NULL,
        token      NVARCHAR(255) NOT NULL UNIQUE,      -- secure random token
        ip_address NVARCHAR(45)  NULL,
        user_agent NVARCHAR(512) NULL,
        created_at DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
        expires_at DATETIME2     NOT NULL,
        is_active  BIT           NOT NULL DEFAULT 1,
        CONSTRAINT fk_sessions_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    );
END
GO

-- -------------------------------------------------------------
-- 4. DEFAULT ADMIN USER
-- password = Admin@123  (bcrypt hash — change after first login!)
-- Hash generated with: bcrypt.hashpw(b'Admin@123', bcrypt.gensalt(12))
-- -------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM users WHERE username = 'admin')
BEGIN
    DECLARE @admin_role_id INT = (SELECT id FROM roles WHERE name = 'admin');

    INSERT INTO users (username, email, password_hash, role_id, is_active)
    VALUES (
        'admin',
        'admin@yourdomain.com',
        '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQyCgLuJhKFZs9VwFzaZpJD7O',
        @admin_role_id,
        1
    );
END
GO

-- -------------------------------------------------------------
-- 5. INDEXES  (improves query speed at scale)
-- -------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_users_username')
    CREATE INDEX IX_users_username ON users(username);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_users_email')
    CREATE INDEX IX_users_email ON users(email);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_sessions_token')
    CREATE INDEX IX_sessions_token ON user_sessions(token);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_sessions_user_id')
    CREATE INDEX IX_sessions_user_id ON user_sessions(user_id);
GO

PRINT '✅ Auth tables created successfully.';
PRINT '   Default admin login → username: admin  |  password: Admin@123';
PRINT '   IMPORTANT: Change the admin password immediately after first login.';
GO

-- +++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
-- =============================================================
-- MIGRATION: agent_assignments table
-- Run this ONCE in SSMS after auth_setup.sql has been executed.
-- =============================================================

USE [nexus];   -- ← change this to your database name
GO

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'agent_assignments')
BEGIN
    CREATE TABLE agent_assignments (
        id          INT IDENTITY(1,1) PRIMARY KEY,
        user_id     INT           NOT NULL,
        agent_name  NVARCHAR(255) NOT NULL,
        assigned_by INT           NOT NULL,
        assigned_at DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT fk_aa_user     FOREIGN KEY (user_id)     REFERENCES users(id) ON DELETE CASCADE,
        CONSTRAINT fk_aa_assigner FOREIGN KEY (assigned_by) REFERENCES users(id),
        CONSTRAINT uq_aa_user_agent UNIQUE (user_id, agent_name)
    );

    CREATE INDEX IX_aa_user_id    ON agent_assignments(user_id);
    CREATE INDEX IX_aa_agent_name ON agent_assignments(agent_name);

    PRINT '✅ agent_assignments table created successfully.';
END
ELSE
BEGIN
    PRINT '⚠️  agent_assignments table already exists — skipping.';
END
GO

-- +++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
-- =============================================================
-- MIGRATION: token_usage table
-- Run after auth_setup.sql and migration_agent_assignments.sql
-- =============================================================

USE [nexus];   -- ← same database as before
GO

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'token_usage')
BEGIN
    CREATE TABLE token_usage (
        id            INT IDENTITY(1,1) PRIMARY KEY,
        user_id       INT            NOT NULL,
        used_at       DATETIME2      NOT NULL DEFAULT GETUTCDATE(),
        tokens_used   INT            NOT NULL,
        call_type     NVARCHAR(50)   NOT NULL DEFAULT 'chat',
        agent_name    NVARCHAR(255)  NULL,
        question      NVARCHAR(1000) NULL,
        input_tokens  INT            NOT NULL DEFAULT 0,
        output_tokens INT            NOT NULL DEFAULT 0,
        model         NVARCHAR(100)  NULL,
        agent_id      NVARCHAR(36)   NULL,
        CONSTRAINT fk_tu_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    );

    CREATE INDEX IX_tu_user_date  ON token_usage(user_id, used_at);
    CREATE INDEX IX_tu_call_type  ON token_usage(call_type);
END
GO

PRINT '✅ token_usage table created.';
GO

-- +++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++


-- =============================================================
-- MIGRATION: add call_type to token_usage
-- Run this if you already ran migration_token_usage.sql.
-- If you haven't run it yet, use the combined version below instead.
-- =============================================================

USE [nexus];
GO

-- Safe: only adds the column if it doesn't exist yet
IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE  object_id = OBJECT_ID('token_usage') AND name = 'call_type'
)
BEGIN
    ALTER TABLE token_usage
    ADD call_type NVARCHAR(50) NOT NULL DEFAULT 'chat';

    -- index helps the admin dashboard query "group by call_type"
    CREATE INDEX IX_tu_call_type ON token_usage(call_type);

    PRINT '✅ call_type column added to token_usage.';
END
ELSE
    PRINT '⚠️  call_type already exists — skipped.';
GO

GO