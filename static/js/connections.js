// static/js/connections.js

class ConnectionManager {
    constructor() {
        this.modal = null;
        this.isEditMode = false;
        this.currentConnectionName = null;

        this.initializeEventListeners();
    }

    initializeEventListeners() {
        document.getElementById('testConnectionBtn')?.addEventListener('click', () => {
            this.testConnectionFromForm();
        });

        document.getElementById('createConnectionBtn')?.addEventListener('click', () => {
            this.saveConnection();
        });

        document.getElementById('dbType')?.addEventListener('change', (e) => {
            this.updatePortPlaceholder(e.target.value);
        });
    }

    updatePortPlaceholder(dbType) {
        const portInput = document.getElementById('port');
        if (portInput) {
            portInput.placeholder = `Default: ${this.getDefaultPort(dbType)}`;
        }
    }

    showCreateConnectionModal() {
        this.isEditMode = false;
        this.currentConnectionName = null;

        document.getElementById('createConnectionForm').reset();
        document.querySelector('#createConnectionModal .modal-title').textContent = 'Create Database Connection';
        document.getElementById('createConnectionBtn').textContent = 'Create Connection';
        document.getElementById('connectionName').readOnly = false;

        const dbType = document.getElementById('dbType').value;
        if (dbType) this.updatePortPlaceholder(dbType);

        this.modal = new bootstrap.Modal(document.getElementById('createConnectionModal'));
        this.modal.show();
    }

    showEditConnectionModal(connectionName) {
        this.isEditMode = true;
        this.currentConnectionName = connectionName;

        fetch('/api/connections')
            .then(r => r.json())
            .then(connections => {
                const connection = connections.find(c => c.name === connectionName);
                if (!connection) {
                    this.showNotification('Connection not found', 'error');
                    return;
                }

                document.getElementById('connectionName').value = connection.name;
                document.getElementById('dbType').value = connection.type;
                document.getElementById('server').value = connection.server;
                document.getElementById('port').value = connection.port;
                document.getElementById('username').value = connection.username;
                document.getElementById('password').value = '';
                document.getElementById('dbName').value = connection.database || '';

                this.updatePortPlaceholder(connection.type);

                document.querySelector('#createConnectionModal .modal-title').textContent = 'Edit Database Connection';
                document.getElementById('createConnectionBtn').textContent = 'Save Changes';

                this.modal = new bootstrap.Modal(document.getElementById('createConnectionModal'));
                this.modal.show();
            })
            .catch(() => this.showNotification('Error loading connection data', 'error'));
    }

    saveConnection() {
        const connectionData = {
            name:     document.getElementById('connectionName').value.trim(),
            type:     document.getElementById('dbType').value,
            server:   document.getElementById('server').value.trim(),
            port:     document.getElementById('port').value || String(this.getDefaultPort(document.getElementById('dbType').value)),
            username: document.getElementById('username').value.trim(),
            password: document.getElementById('password').value,
            database: document.getElementById('dbName').value.trim()
        };

        // In edit mode password is optional (keep existing if blank)
        const passwordRequired = !this.isEditMode;

        if (!connectionData.name || !connectionData.type || !connectionData.server ||
            !connectionData.username || (passwordRequired && !connectionData.password) || !connectionData.database) {
            this.showNotification('Please fill in all required fields' + (this.isEditMode ? ' (leave password blank to keep existing)' : ''), 'error');
            return;
        }

        let url, method, body;

        if (this.isEditMode) {
            url = '/api/connections';
            method = 'PUT';
            body = { ...connectionData, _old_name: this.currentConnectionName };
        } else {
            url = '/api/connections';
            method = 'POST';
            body = connectionData;
        }

        fetch(url, {
            method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        })
        .then(r => {
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            return r.json();
        })
        .then(data => {
            if (data.status === 'success') {
                this.showNotification(this.isEditMode ? 'Connection updated successfully!' : 'Connection saved successfully!', 'success');
                this.modal.hide();
                setTimeout(() => location.reload(), 1000);
            } else {
                this.showNotification('Error: ' + data.message, 'error');
            }
        })
        .catch(err => this.showNotification('Error saving connection: ' + err.message, 'error'));
    }

    // Test using form values (called from modal Test button)
    testConnectionFromForm() {
        const connectionData = {
            name:     document.getElementById('connectionName').value.trim(),
            type:     document.getElementById('dbType').value,
            server:   document.getElementById('server').value.trim(),
            port:     parseInt(document.getElementById('port').value) || this.getDefaultPort(document.getElementById('dbType').value),
            username: document.getElementById('username').value.trim(),
            password: document.getElementById('password').value,
            database: document.getElementById('dbName').value.trim()
        };

        // In edit mode, if password is blank, test by name (uses stored password)
        if (this.isEditMode && !connectionData.password) {
            this.testExistingConnectionByName(this.currentConnectionName);
            return;
        }

        if (!connectionData.name || !connectionData.type || !connectionData.server ||
            !connectionData.username || !connectionData.password || !connectionData.database) {
            this.showNotification('Please fill in all required fields to test', 'error');
            return;
        }

        this._runTest(connectionData);
    }

    // Test a saved connection by name (called from table row Test button)
    testExistingConnectionByName(connectionName) {
        this.showNotification(`Testing "${connectionName}"...`, 'info');
        this._runTest({ name: connectionName });
    }

    _runTest(payload) {
        this.showNotification('Testing connection...', 'info');
        fetch('/api/connections/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        })
        .then(r => {
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            return r.json();
        })
        .then(data => {
            if (data.success) {
                this.showNotification(`✓ ${data.message}`, 'success');
            } else {
                this.showNotification(`✗ ${data.message}`, 'error');
            }
        })
        .catch(err => this.showNotification('Error testing connection: ' + err.message, 'error'));
    }

    getDefaultPort(dbType) {
        const ports = { postgresql: 5432, mysql: 3306, mssql: 1433, oracle: 1521, mongodb: 27017 };
        return ports[dbType] || 5432;
    }

    deleteConnection(connectionName) {
        if (!confirm(`Are you sure you want to delete connection "${connectionName}"?`)) return;

        fetch('/api/connections', {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: connectionName })
        })
        .then(r => {
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            return r.json();
        })
        .then(data => {
            if (data.status === 'success') {
                this.showNotification('Connection deleted successfully!', 'success');
                setTimeout(() => location.reload(), 1000);
            } else {
                this.showNotification('Error: ' + data.message, 'error');
            }
        })
        .catch(err => this.showNotification('Error deleting connection: ' + err.message, 'error'));
    }

    showNotification(message, type = 'info') {
        const alertClass = type === 'success' ? 'alert-success' : type === 'error' ? 'alert-danger' : 'alert-info';
        const el = document.createElement('div');
        el.className = `alert ${alertClass} alert-dismissible fade show position-fixed`;
        el.style.cssText = 'top: 20px; right: 20px; z-index: 1060; min-width: 300px;';
        el.innerHTML = `${message}<button type="button" class="btn-close" data-bs-dismiss="alert"></button>`;
        document.body.appendChild(el);
        setTimeout(() => { if (el.parentNode) el.remove(); }, 5000);
    }
}

document.addEventListener('DOMContentLoaded', function () {
    window.connectionManager = new ConnectionManager();

    window.showCreateConnectionModal = () => window.connectionManager.showCreateConnectionModal();
    window.editConnection            = (name) => window.connectionManager.showEditConnectionModal(name);
    window.deleteConnection          = (name) => window.connectionManager.deleteConnection(name);
    window.testExistingConnection    = (name) => window.connectionManager.testExistingConnectionByName(name);
    window.testConnection            = () => window.connectionManager.testConnectionFromForm();
    window.createConnection          = () => window.connectionManager.saveConnection();
});
