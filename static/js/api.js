// Modal functions
function showCreateAgentModal(type) {
    const agentType = document.getElementById('agentType');
    const modalTitle = document.getElementById('createAgentModalTitle');

    if (agentType) agentType.value = type;
    if (modalTitle) modalTitle.textContent = `Create ${type === 'bi' ? 'BI' : 'Reasoning'} Agent`;

    new bootstrap.Modal(document.getElementById('createAgentModal')).show();
}

function showCreateConnectionModal() {
    new bootstrap.Modal(document.getElementById('createConnectionModal')).show();
}

function showUploadModal() {
    new bootstrap.Modal(document.getElementById('uploadModal')).show();
}

// CRUD functions
function createAgent() {
    const formData = {
        name: document.getElementById('agentName').value,
        description: document.getElementById('agentDescription').value,
        type: document.getElementById('agentType').value,
        connections: Array.from(document.getElementById('agentConnections').selectedOptions).map(o => o.value),
        knowledge: Array.from(document.getElementById('agentKnowledge').selectedOptions).map(o => o.value)
    };

    createAgentAPI(formData).then(data => {
        bootstrap.Modal.getInstance(document.getElementById('createAgentModal')).hide();
        alert('Agent created successfully!');
        // Refresh agents list
        fetchAgents();
    }).catch(error => {
        console.error('Error creating agent:', error);
        alert('Error creating agent. Please try again.');
    });
}

function createConnection() {
    const formData = {
        name: document.getElementById('connectionName').value,
        type: document.getElementById('dbType').value,
        server: document.getElementById('server').value,
        port: document.getElementById('port').value,
        database: document.getElementById('dbName').value,
        username: document.getElementById('username').value,
        password: document.getElementById('password').value,
        custom_packages: document.getElementById('customPackages').value
    };

    createConnectionAPI(formData).then(data => {
        bootstrap.Modal.getInstance(document.getElementById('createConnectionModal')).hide();
        alert('Database connection created successfully!');
        // Refresh connections list
        fetchConnections();
    }).catch(error => {
        console.error('Error creating connection:', error);
        alert('Error creating connection. Please try again.');
    });
}

function testConnection() {
    const formData = {
        type: document.getElementById('dbType').value,
        server: document.getElementById('server').value,
        port: document.getElementById('port').value,
        database: document.getElementById('dbName').value,
        username: document.getElementById('username').value,
        password: document.getElementById('password').value
    };

    testConnectionAPI(formData).then(data => {
        if (data.success) {
            alert('Connection test successful!');
        } else {
            alert('Connection test failed. Please check your settings.');
        }
    }).catch(error => {
        console.error('Error testing connection:', error);
        alert('Error testing connection. Please try again.');
    });
}

function uploadDocuments() {
    const files = document.getElementById('uploadFiles').files;
    const category = document.getElementById('documentCategory').value;
    const description = document.getElementById('documentDescription').value;

    const formData = new FormData();
    for (let file of files) {
        formData.append('files', file);
    }
    formData.append('category', category);
    formData.append('description', description);

    uploadDocumentsAPI(formData).then(data => {
        bootstrap.Modal.getInstance(document.getElementById('uploadModal')).hide();
        alert('Documents uploaded successfully!');
        // Refresh documents list
        fetchDocuments();
    }).catch(error => {
        console.error('Error uploading documents:', error);
        alert('Error uploading documents. Please try again.');
    });
}

// API integration functions
async function fetchAgents() {
    try {
        const response = await fetch('/api/bi-agents');
        const data = await response.json();
        // Update agents table
        return data;
    } catch (error) {
        console.error('Error fetching agents:', error);
    }
}

async function createAgentAPI(agentData) {
    const response = await fetch('/api/agents', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(agentData)
    });
    return await response.json();
}

async function sendChatMessage(agentId, message, type) {
    try {
        const response = await fetch('/api/chat', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                agent_id: agentId,
                message: message,
                type: type
            })
        });
        const data = await response.json();
        displayAgentResponse(type, data.response);
    } catch (error) {
        console.error('Error sending chat message:', error);
        displayAgentResponse(type, "Sorry, I'm having trouble connecting right now. Please try again later.");
    }
}

async function fetchConnections() {
    try {
        const response = await fetch('/api/connections');
        const data = await response.json();
        // Update connections table
        return data;
    } catch (error) {
        console.error('Error fetching connections:', error);
    }
}

async function createConnectionAPI(connectionData) {
    const response = await fetch('/api/connections', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(connectionData)
    });
    return await response.json();
}

async function testConnectionAPI(connectionData) {
    const response = await fetch('/api/connections/test', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(connectionData)
    });
    return await response.json();
}

async function uploadDocumentsAPI(formData) {
    const response = await fetch('/api/documents/upload', {
        method: 'POST',
        body: formData
    });
    return await response.json();
}

async function fetchDocuments() {
    try {
        const response = await fetch('/api/documents');
        const data = await response.json();
        // Update documents display
        return data;
    } catch (error) {
        console.error('Error fetching documents:', error);
    }
}