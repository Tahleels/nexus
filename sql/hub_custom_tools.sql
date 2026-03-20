-- Custom user-created tools for Nexus AI Hub
CREATE TABLE hub_custom_tools (
    id            INT IDENTITY(1,1) PRIMARY KEY,
    name          NVARCHAR(100) NOT NULL UNIQUE,
    display_name  NVARCHAR(200) NOT NULL,
    description   NVARCHAR(MAX),
    category      NVARCHAR(100) DEFAULT 'Custom',
    pip_packages  NVARCHAR(MAX),   -- JSON array e.g. ["requests", "pandas==2.0"]
    imports_code  NVARCHAR(MAX),   -- raw import statements
    function_code NVARCHAR(MAX),   -- body: must define def run(**kwargs)
    input_schema  NVARCHAR(MAX),   -- JSON: [{name, type, required, description}]
    output_desc   NVARCHAR(500),
    enabled       BIT DEFAULT 1,
    total_calls   INT DEFAULT 0,
    success_calls INT DEFAULT 0,
    created_by    INT,
    created_at    DATETIME2 DEFAULT GETUTCDATE(),
    updated_at    DATETIME2 DEFAULT GETUTCDATE()
);

CREATE INDEX idx_custom_tools_name    ON hub_custom_tools(name);
CREATE INDEX idx_custom_tools_enabled ON hub_custom_tools(enabled);
