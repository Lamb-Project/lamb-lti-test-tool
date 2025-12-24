"""
LTI 1.1 Test Platform
A local development tool for testing LTI 1.1 tool integrations.
"""

import sqlite3
import hashlib
import hmac
import base64
import urllib.parse
import uuid
import time
import json
from datetime import datetime
from typing import Optional
from contextlib import contextmanager

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import uvicorn

app = FastAPI(title="LTI 1.1 Test Platform")

# Database setup
DB_PATH = "lti_platform.db"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initialize the database with all required tables."""
    conn = get_db()
    cursor = conn.cursor()
    
    # Tool Servers table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tool_servers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            domain TEXT NOT NULL,
            port INTEGER NOT NULL,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Tools table (one tool per server, but with its own config)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tools (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tool_server_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            launch_path TEXT NOT NULL DEFAULT '/lti/launch',
            consumer_key TEXT NOT NULL,
            consumer_secret TEXT NOT NULL,
            custom_params TEXT,
            description TEXT,
            launch_url_override TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (tool_server_id) REFERENCES tool_servers(id) ON DELETE CASCADE
        )
    """)
    
    # Courses table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS courses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            code TEXT NOT NULL UNIQUE,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Users table (students and teachers)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            role TEXT NOT NULL CHECK(role IN ('student', 'teacher')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Course enrollments
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS enrollments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            FOREIGN KEY (course_id) REFERENCES courses(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(course_id, user_id)
        )
    """)
    
    # Course-Tool associations
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS course_tools (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id INTEGER NOT NULL,
            tool_id INTEGER NOT NULL,
            resource_link_id TEXT NOT NULL,
            resource_link_title TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (course_id) REFERENCES courses(id) ON DELETE CASCADE,
            FOREIGN KEY (tool_id) REFERENCES tools(id) ON DELETE CASCADE,
            UNIQUE(course_id, tool_id)
        )
    """)
    
    # Launch logs
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS launch_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_tool_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            launch_params TEXT NOT NULL,
            signed_params TEXT NOT NULL,
            oauth_signature TEXT NOT NULL,
            launched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (course_tool_id) REFERENCES course_tools(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    
    # Grade results (outcomes)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS grade_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_tool_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            sourced_id TEXT NOT NULL,
            score REAL,
            raw_xml TEXT,
            received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (course_tool_id) REFERENCES course_tools(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    
    conn.commit()
    conn.close()


def seed_demo_data():
    """Create demo courses with students and teachers if none exist."""
    conn = get_db()
    cursor = conn.cursor()
    
    # Check if we already have data
    cursor.execute("SELECT COUNT(*) FROM courses")
    if cursor.fetchone()[0] > 0:
        conn.close()
        return
    
    # Create demo users - 2 teachers
    teachers = [
        ("Dr. Alice Smith", "alice.smith@example.edu", "teacher"),
        ("Prof. Bob Johnson", "bob.johnson@example.edu", "teacher"),
    ]
    
    # 4 students
    students = [
        ("Charlie Brown", "charlie.brown@example.edu", "student"),
        ("Diana Prince", "diana.prince@example.edu", "student"),
        ("Edward Norton", "edward.norton@example.edu", "student"),
        ("Fiona Green", "fiona.green@example.edu", "student"),
    ]
    
    for name, email, role in teachers + students:
        cursor.execute(
            "INSERT INTO users (name, email, role) VALUES (?, ?, ?)",
            (name, email, role)
        )
    
    # Create demo courses
    courses = [
        ("Introduction to Python", "CS101", "Learn the basics of Python programming"),
        ("Web Development", "WEB201", "Build modern web applications"),
        ("Data Science Fundamentals", "DS301", "Introduction to data analysis and machine learning"),
    ]
    
    for name, code, description in courses:
        cursor.execute(
            "INSERT INTO courses (name, code, description) VALUES (?, ?, ?)",
            (name, code, description)
        )
    
    # Enroll all users in all courses
    cursor.execute("SELECT id FROM users")
    user_ids = [row[0] for row in cursor.fetchall()]
    
    cursor.execute("SELECT id FROM courses")
    course_ids = [row[0] for row in cursor.fetchall()]
    
    for course_id in course_ids:
        for user_id in user_ids:
            cursor.execute(
                "INSERT INTO enrollments (course_id, user_id) VALUES (?, ?)",
                (course_id, user_id)
            )
    
    conn.commit()
    conn.close()


# OAuth 1.0a Implementation
def generate_oauth_signature(method: str, url: str, params: dict, consumer_secret: str) -> str:
    """Generate OAuth 1.0a signature for LTI launch."""
    # Sort parameters
    sorted_params = sorted(params.items())
    
    # Create parameter string
    param_string = "&".join(
        f"{urllib.parse.quote(str(k), safe='')}"
        f"={urllib.parse.quote(str(v), safe='')}"
        for k, v in sorted_params
    )
    
    # Create signature base string
    signature_base = "&".join([
        method.upper(),
        urllib.parse.quote(url, safe=''),
        urllib.parse.quote(param_string, safe='')
    ])
    
    # Debug: Print signature base string (helps debug mismatches)
    print(f"DEBUG OAuth - Method: {method}")
    print(f"DEBUG OAuth - URL: {url}")
    print(f"DEBUG OAuth - Signature Base String (first 500 chars): {signature_base[:500]}...")
    
    # Create signing key (consumer_secret + "&" + token_secret, but token_secret is empty for LTI)
    signing_key = f"{urllib.parse.quote(consumer_secret, safe='')}&"
    
    # Generate HMAC-SHA1 signature
    hashed = hmac.new(
        signing_key.encode('utf-8'),
        signature_base.encode('utf-8'),
        hashlib.sha1
    )
    signature = base64.b64encode(hashed.digest()).decode('utf-8')
    
    print(f"DEBUG OAuth - Generated Signature: {signature}")
    
    return signature


def build_lti_launch_params(
    tool: dict,
    course: dict,
    user: dict,
    resource_link_id: str,
    resource_link_title: str,
    launch_url: str,
    outcomes_url: str,
    custom_params: dict = None
) -> dict:
    """Build the complete LTI 1.1 launch parameters.
    
    IMPORTANT: launch_url must be EXACTLY the URL the tool will see when it
    receives the request. This is critical for OAuth signature verification.
    """
    
    # Determine LTI role
    if user['role'] == 'teacher':
        lti_role = "Instructor"
    else:
        lti_role = "Learner"
    
    # Generate unique identifiers
    sourced_id = base64.b64encode(
        f"{course['id']}:{resource_link_id}:{user['id']}".encode()
    ).decode()
    
    timestamp = str(int(time.time()))
    nonce = str(uuid.uuid4())
    
    # Debug logging
    print(f"DEBUG: Building LTI params for launch_url = {launch_url}")
    print(f"DEBUG: Consumer key = {tool['consumer_key']}")
    print(f"DEBUG: Timestamp = {timestamp}, Nonce = {nonce}")
    
    params = {
        # LTI Required
        "lti_message_type": "basic-lti-launch-request",
        "lti_version": "LTI-1p0",
        
        # OAuth
        "oauth_consumer_key": tool['consumer_key'],
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": timestamp,
        "oauth_nonce": nonce,
        "oauth_version": "1.0",
        "oauth_callback": "about:blank",
        
        # Resource
        "resource_link_id": resource_link_id,
        "resource_link_title": resource_link_title,
        
        # Context (Course)
        "context_id": str(course['id']),
        "context_label": course['code'],
        "context_title": course['name'],
        "context_type": "CourseSection",
        
        # User
        "user_id": str(user['id']),
        "lis_person_name_given": user['name'].split()[0],
        "lis_person_name_family": " ".join(user['name'].split()[1:]) or user['name'],
        "lis_person_name_full": user['name'],
        "lis_person_contact_email_primary": user['email'],
        "roles": lti_role,
        
        # Outcomes service
        "lis_outcome_service_url": outcomes_url,
        "lis_result_sourcedid": sourced_id,
        
        # Launch presentation
        "launch_presentation_locale": "en-US",
        "launch_presentation_document_target": "iframe",
        
        # Tool consumer info
        "tool_consumer_instance_guid": "lti-test-platform.local",
        "tool_consumer_instance_name": "LTI Test Platform",
        "tool_consumer_instance_description": "Local LTI 1.1 Testing Environment",
        "tool_consumer_info_product_family_code": "lti-test-platform",
        "tool_consumer_info_version": "1.0",
    }
    
    # Add custom parameters
    if custom_params:
        for key, value in custom_params.items():
            param_key = f"custom_{key}" if not key.startswith("custom_") else key
            params[param_key] = value
    
    # Generate OAuth signature
    signature = generate_oauth_signature("POST", launch_url, params, tool['consumer_secret'])
    params["oauth_signature"] = signature
    
    return params


# Initialize database on startup
@app.on_event("startup")
async def startup():
    init_db()
    seed_demo_data()


# HTML Template (embedded for simplicity)
def get_base_template():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>LTI 1.1 Test Platform</title>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=Space+Grotesk:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-primary: #0a0a0f;
            --bg-secondary: #12121a;
            --bg-tertiary: #1a1a25;
            --accent: #6366f1;
            --accent-hover: #818cf8;
            --success: #10b981;
            --warning: #f59e0b;
            --danger: #ef4444;
            --text-primary: #f8fafc;
            --text-secondary: #94a3b8;
            --text-muted: #64748b;
            --border: #2d2d3a;
            --radius: 8px;
        }
        
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: 'Space Grotesk', sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            min-height: 100vh;
            line-height: 1.6;
        }
        
        .container {
            max-width: 1400px;
            margin: 0 auto;
            padding: 2rem;
        }
        
        header {
            background: linear-gradient(135deg, var(--bg-secondary) 0%, var(--bg-tertiary) 100%);
            border-bottom: 1px solid var(--border);
            padding: 1.5rem 2rem;
            margin-bottom: 2rem;
        }
        
        header h1 {
            font-size: 1.75rem;
            font-weight: 700;
            background: linear-gradient(135deg, var(--accent) 0%, var(--accent-hover) 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        
        header p {
            color: var(--text-secondary);
            font-size: 0.9rem;
            margin-top: 0.25rem;
        }
        
        nav {
            display: flex;
            gap: 0.5rem;
            margin-top: 1rem;
            flex-wrap: wrap;
        }
        
        nav a {
            padding: 0.5rem 1rem;
            background: var(--bg-tertiary);
            color: var(--text-secondary);
            text-decoration: none;
            border-radius: var(--radius);
            font-size: 0.85rem;
            font-weight: 500;
            transition: all 0.2s;
            border: 1px solid var(--border);
        }
        
        nav a:hover, nav a.active {
            background: var(--accent);
            color: white;
            border-color: var(--accent);
        }
        
        .card {
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 1.5rem;
            margin-bottom: 1.5rem;
        }
        
        .card-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 1rem;
            padding-bottom: 1rem;
            border-bottom: 1px solid var(--border);
        }
        
        .card-title {
            font-size: 1.1rem;
            font-weight: 600;
        }
        
        .grid {
            display: grid;
            gap: 1.5rem;
        }
        
        .grid-2 { grid-template-columns: repeat(auto-fit, minmax(400px, 1fr)); }
        .grid-3 { grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); }
        
        table {
            width: 100%;
            border-collapse: collapse;
        }
        
        th, td {
            text-align: left;
            padding: 0.75rem;
            border-bottom: 1px solid var(--border);
        }
        
        th {
            color: var(--text-secondary);
            font-weight: 500;
            font-size: 0.85rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }
        
        td {
            font-size: 0.9rem;
        }
        
        tr:hover {
            background: var(--bg-tertiary);
        }
        
        .btn {
            display: inline-flex;
            align-items: center;
            gap: 0.5rem;
            padding: 0.5rem 1rem;
            border-radius: var(--radius);
            font-size: 0.85rem;
            font-weight: 500;
            text-decoration: none;
            border: none;
            cursor: pointer;
            transition: all 0.2s;
            font-family: inherit;
        }
        
        .btn-primary {
            background: var(--accent);
            color: white;
        }
        
        .btn-primary:hover {
            background: var(--accent-hover);
        }
        
        .btn-secondary {
            background: var(--bg-tertiary);
            color: var(--text-secondary);
            border: 1px solid var(--border);
        }
        
        .btn-secondary:hover {
            background: var(--border);
            color: var(--text-primary);
        }
        
        .btn-success {
            background: var(--success);
            color: white;
        }
        
        .btn-danger {
            background: var(--danger);
            color: white;
        }
        
        .btn-sm {
            padding: 0.35rem 0.75rem;
            font-size: 0.8rem;
        }
        
        .form-group {
            margin-bottom: 1rem;
        }
        
        label {
            display: block;
            margin-bottom: 0.5rem;
            color: var(--text-secondary);
            font-size: 0.85rem;
            font-weight: 500;
        }
        
        input, textarea, select {
            width: 100%;
            padding: 0.75rem;
            background: var(--bg-tertiary);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            color: var(--text-primary);
            font-size: 0.9rem;
            font-family: inherit;
            transition: border-color 0.2s;
        }
        
        input:focus, textarea:focus, select:focus {
            outline: none;
            border-color: var(--accent);
        }
        
        textarea {
            min-height: 100px;
            resize: vertical;
        }
        
        .badge {
            display: inline-block;
            padding: 0.25rem 0.5rem;
            border-radius: 4px;
            font-size: 0.75rem;
            font-weight: 500;
            text-transform: uppercase;
        }
        
        .badge-teacher {
            background: rgba(99, 102, 241, 0.2);
            color: var(--accent-hover);
        }
        
        .badge-student {
            background: rgba(16, 185, 129, 0.2);
            color: var(--success);
        }
        
        .code-block {
            background: var(--bg-primary);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 1rem;
            overflow-x: auto;
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.8rem;
            max-height: 400px;
            overflow-y: auto;
        }
        
        .code-block pre {
            white-space: pre-wrap;
            word-wrap: break-word;
        }
        
        .tabs {
            display: flex;
            gap: 0.25rem;
            margin-bottom: 1rem;
            border-bottom: 1px solid var(--border);
        }
        
        .tab {
            padding: 0.75rem 1.25rem;
            background: transparent;
            color: var(--text-secondary);
            border: none;
            cursor: pointer;
            font-size: 0.9rem;
            font-weight: 500;
            font-family: inherit;
            border-bottom: 2px solid transparent;
            margin-bottom: -1px;
            transition: all 0.2s;
        }
        
        .tab:hover {
            color: var(--text-primary);
        }
        
        .tab.active {
            color: var(--accent);
            border-bottom-color: var(--accent);
        }
        
        .tab-content {
            display: none;
        }
        
        .tab-content.active {
            display: block;
        }
        
        .alert {
            padding: 1rem;
            border-radius: var(--radius);
            margin-bottom: 1rem;
        }
        
        .alert-info {
            background: rgba(99, 102, 241, 0.1);
            border: 1px solid var(--accent);
            color: var(--accent-hover);
        }
        
        .alert-success {
            background: rgba(16, 185, 129, 0.1);
            border: 1px solid var(--success);
            color: var(--success);
        }
        
        .user-selector {
            display: flex;
            gap: 0.5rem;
            flex-wrap: wrap;
            margin-bottom: 1rem;
        }
        
        .user-card {
            padding: 0.75rem 1rem;
            background: var(--bg-tertiary);
            border: 2px solid var(--border);
            border-radius: var(--radius);
            cursor: pointer;
            transition: all 0.2s;
        }
        
        .user-card:hover {
            border-color: var(--accent);
        }
        
        .user-card.selected {
            border-color: var(--accent);
            background: rgba(99, 102, 241, 0.1);
        }
        
        .user-card .name {
            font-weight: 500;
            font-size: 0.9rem;
        }
        
        .user-card .role {
            font-size: 0.75rem;
            color: var(--text-muted);
        }
        
        .launch-frame {
            width: 100%;
            height: 600px;
            border: 1px solid var(--border);
            border-radius: var(--radius);
            background: white;
        }
        
        .empty-state {
            text-align: center;
            padding: 3rem;
            color: var(--text-muted);
        }
        
        .empty-state h3 {
            margin-bottom: 0.5rem;
            color: var(--text-secondary);
        }
        
        .flex {
            display: flex;
            gap: 1rem;
            align-items: center;
        }
        
        .flex-between {
            justify-content: space-between;
        }
        
        .mt-1 { margin-top: 0.5rem; }
        .mt-2 { margin-top: 1rem; }
        .mb-1 { margin-bottom: 0.5rem; }
        .mb-2 { margin-bottom: 1rem; }
        
        .text-muted { color: var(--text-muted); }
        .text-success { color: var(--success); }
        .text-danger { color: var(--danger); }
        
        .score {
            font-size: 1.25rem;
            font-weight: 600;
        }
        
        .inline-form {
            display: flex;
            gap: 0.5rem;
            align-items: flex-end;
        }
        
        .inline-form .form-group {
            margin-bottom: 0;
            flex: 1;
        }
        
        .modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0, 0, 0, 0.8);
            z-index: 1000;
            align-items: center;
            justify-content: center;
        }
        
        .modal.active {
            display: flex;
        }
        
        .modal-content {
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 2rem;
            max-width: 600px;
            width: 90%;
            max-height: 90vh;
            overflow-y: auto;
        }
        
        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 1.5rem;
        }
        
        .modal-close {
            background: none;
            border: none;
            color: var(--text-muted);
            font-size: 1.5rem;
            cursor: pointer;
        }
        
        .modal-close:hover {
            color: var(--text-primary);
        }
    </style>
</head>
<body>
    <header>
        <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 1rem;">
            <div>
                <h1>🚀 LTI 1.1 Test Platform</h1>
                <p>Local development environment for testing LTI tool integrations</p>
            </div>
            <a href="https://lamb-project.org" target="_blank" rel="noopener" style="display: block;">
                <img src="https://lamb-project.org/images/lamb_1.png" alt="LAMB Project" style="height: 60px; width: auto;">
            </a>
        </div>
        <nav>
            <a href="/" class="{{'active' if active_page == 'dashboard' else ''}}">Dashboard</a>
            <a href="/tool-servers" class="{{'active' if active_page == 'tool-servers' else ''}}">Tool Servers</a>
            <a href="/tools" class="{{'active' if active_page == 'tools' else ''}}">Tools</a>
            <a href="/courses" class="{{'active' if active_page == 'courses' else ''}}">Courses</a>
            <a href="/launch-logs" class="{{'active' if active_page == 'launch-logs' else ''}}">Launch Logs</a>
            <a href="/grades" class="{{'active' if active_page == 'grades' else ''}}">Grades</a>
        </nav>
    </header>
    <div class="container">
        {{content}}
    </div>
    <script>
        function showTab(tabId) {
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            document.querySelector(`[data-tab="${tabId}"]`).classList.add('active');
            document.getElementById(tabId).classList.add('active');
        }
        
        function selectUser(userId, element) {
            document.querySelectorAll('.user-card').forEach(c => c.classList.remove('selected'));
            element.classList.add('selected');
            document.getElementById('selected_user_id').value = userId;
        }
        
        function openModal(modalId) {
            document.getElementById(modalId).classList.add('active');
        }
        
        function closeModal(modalId) {
            document.getElementById(modalId).classList.remove('active');
        }
        
        function confirmDelete(url, name) {
            if (confirm(`Are you sure you want to delete "${name}"?`)) {
                window.location.href = url;
            }
        }
    </script>
</body>
</html>
"""


def render_template(content: str, active_page: str = "dashboard") -> str:
    template = get_base_template()
    template = template.replace("{{content}}", content)
    template = template.replace("{{'active' if active_page == 'dashboard' else ''}}", 
                                "active" if active_page == "dashboard" else "")
    template = template.replace("{{'active' if active_page == 'tool-servers' else ''}}", 
                                "active" if active_page == "tool-servers" else "")
    template = template.replace("{{'active' if active_page == 'tools' else ''}}", 
                                "active" if active_page == "tools" else "")
    template = template.replace("{{'active' if active_page == 'courses' else ''}}", 
                                "active" if active_page == "courses" else "")
    template = template.replace("{{'active' if active_page == 'launch-logs' else ''}}", 
                                "active" if active_page == "launch-logs" else "")
    template = template.replace("{{'active' if active_page == 'grades' else ''}}", 
                                "active" if active_page == "grades" else "")
    return template


# ============== ROUTES ==============

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    conn = get_db()
    cursor = conn.cursor()
    
    # Get counts
    cursor.execute("SELECT COUNT(*) FROM tool_servers")
    server_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM tools")
    tool_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM courses")
    course_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM launch_logs")
    launch_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM grade_results")
    grade_count = cursor.fetchone()[0]
    
    # Recent launches
    cursor.execute("""
        SELECT ll.*, u.name as user_name, u.role as user_role,
               t.name as tool_name, c.name as course_name
        FROM launch_logs ll
        JOIN users u ON ll.user_id = u.id
        JOIN course_tools ct ON ll.course_tool_id = ct.id
        JOIN tools t ON ct.tool_id = t.id
        JOIN courses c ON ct.course_id = c.id
        ORDER BY ll.launched_at DESC
        LIMIT 5
    """)
    recent_launches = cursor.fetchall()
    
    conn.close()
    
    launches_html = ""
    for launch in recent_launches:
        role_badge = "badge-teacher" if launch['user_role'] == 'teacher' else "badge-student"
        launches_html += f"""
        <tr>
            <td>{launch['launched_at']}</td>
            <td>{launch['course_name']}</td>
            <td>{launch['tool_name']}</td>
            <td>
                {launch['user_name']}
                <span class="badge {role_badge}">{launch['user_role']}</span>
            </td>
            <td>
                <a href="/launch-logs/{launch['id']}" class="btn btn-sm btn-secondary">View</a>
            </td>
        </tr>
        """
    
    if not launches_html:
        launches_html = '<tr><td colspan="5" class="text-muted">No launches yet</td></tr>'
    
    content = f"""
    <div class="grid grid-3">
        <div class="card">
            <div class="card-title">Tool Servers</div>
            <div class="score">{server_count}</div>
            <a href="/tool-servers" class="btn btn-sm btn-secondary mt-2">Manage</a>
        </div>
        <div class="card">
            <div class="card-title">Tools Configured</div>
            <div class="score">{tool_count}</div>
            <a href="/tools" class="btn btn-sm btn-secondary mt-2">Manage</a>
        </div>
        <div class="card">
            <div class="card-title">Courses</div>
            <div class="score">{course_count}</div>
            <a href="/courses" class="btn btn-sm btn-secondary mt-2">View</a>
        </div>
    </div>
    
    <div class="grid grid-2">
        <div class="card">
            <div class="card-title">Total Launches</div>
            <div class="score">{launch_count}</div>
        </div>
        <div class="card">
            <div class="card-title">Grades Received</div>
            <div class="score">{grade_count}</div>
        </div>
    </div>
    
    <div class="card">
        <div class="card-header">
            <div class="card-title">Recent Launches</div>
            <a href="/launch-logs" class="btn btn-sm btn-secondary">View All</a>
        </div>
        <table>
            <thead>
                <tr>
                    <th>Time</th>
                    <th>Course</th>
                    <th>Tool</th>
                    <th>User</th>
                    <th>Actions</th>
                </tr>
            </thead>
            <tbody>
                {launches_html}
            </tbody>
        </table>
    </div>
    
    <div class="card">
        <div class="card-title mb-2">Quick Start Guide</div>
        <ol style="padding-left: 1.5rem; color: var(--text-secondary);">
            <li class="mb-1"><strong>Add a Tool Server</strong> - Configure the domain and port where your LTI tool is running</li>
            <li class="mb-1"><strong>Create a Tool</strong> - Set up the consumer key/secret and launch path</li>
            <li class="mb-1"><strong>Add Tool to Course</strong> - Associate the tool with one of the demo courses</li>
            <li class="mb-1"><strong>Launch!</strong> - Select a user and launch the tool to test</li>
        </ol>
    </div>
    """
    
    return HTMLResponse(render_template(content, "dashboard"))


# ============== TOOL SERVERS ==============

@app.get("/tool-servers", response_class=HTMLResponse)
async def list_tool_servers():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM tool_servers ORDER BY created_at DESC")
    servers = cursor.fetchall()
    conn.close()
    
    servers_html = ""
    for server in servers:
        servers_html += f"""
        <tr>
            <td><strong>{server['name']}</strong></td>
            <td><code>{server['domain']}:{server['port']}</code></td>
            <td>{server['description'] or '-'}</td>
            <td>
                <a href="/tool-servers/{server['id']}/edit" class="btn btn-sm btn-secondary">Edit</a>
                <button onclick="confirmDelete('/tool-servers/{server['id']}/delete', '{server['name']}')" 
                        class="btn btn-sm btn-danger">Delete</button>
            </td>
        </tr>
        """
    
    if not servers_html:
        servers_html = '<tr><td colspan="4" class="text-muted">No tool servers configured yet</td></tr>'
    
    content = f"""
    <div class="card">
        <div class="card-header">
            <div class="card-title">Tool Servers</div>
            <button onclick="openModal('addServerModal')" class="btn btn-primary">+ Add Server</button>
        </div>
        <table>
            <thead>
                <tr>
                    <th>Name</th>
                    <th>Address</th>
                    <th>Description</th>
                    <th>Actions</th>
                </tr>
            </thead>
            <tbody>
                {servers_html}
            </tbody>
        </table>
    </div>
    
    <div id="addServerModal" class="modal">
        <div class="modal-content">
            <div class="modal-header">
                <h3>Add Tool Server</h3>
                <button onclick="closeModal('addServerModal')" class="modal-close">&times;</button>
            </div>
            <form action="/tool-servers/add" method="post">
                <div class="form-group">
                    <label>Server Name</label>
                    <input type="text" name="name" required placeholder="My LTI Tool Server">
                </div>
                <div class="form-group">
                    <label>Domain</label>
                    <input type="text" name="domain" required placeholder="localhost">
                </div>
                <div class="form-group">
                    <label>Port</label>
                    <input type="number" name="port" required placeholder="8080" value="8080">
                </div>
                <div class="form-group">
                    <label>Description</label>
                    <textarea name="description" placeholder="Optional description..."></textarea>
                </div>
                <button type="submit" class="btn btn-primary">Add Server</button>
            </form>
        </div>
    </div>
    """
    
    return HTMLResponse(render_template(content, "tool-servers"))


@app.post("/tool-servers/add")
async def add_tool_server(
    name: str = Form(...),
    domain: str = Form(...),
    port: int = Form(...),
    description: str = Form(None)
):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO tool_servers (name, domain, port, description) VALUES (?, ?, ?, ?)",
        (name, domain, port, description)
    )
    conn.commit()
    conn.close()
    return RedirectResponse(url="/tool-servers", status_code=303)


@app.get("/tool-servers/{server_id}/edit", response_class=HTMLResponse)
async def edit_tool_server_form(server_id: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM tool_servers WHERE id = ?", (server_id,))
    server = cursor.fetchone()
    conn.close()
    
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    
    content = f"""
    <div class="card">
        <div class="card-header">
            <div class="card-title">Edit Tool Server</div>
        </div>
        <form action="/tool-servers/{server_id}/edit" method="post">
            <div class="form-group">
                <label>Server Name</label>
                <input type="text" name="name" required value="{server['name']}">
            </div>
            <div class="form-group">
                <label>Domain</label>
                <input type="text" name="domain" required value="{server['domain']}">
            </div>
            <div class="form-group">
                <label>Port</label>
                <input type="number" name="port" required value="{server['port']}">
            </div>
            <div class="form-group">
                <label>Description</label>
                <textarea name="description">{server['description'] or ''}</textarea>
            </div>
            <div class="flex">
                <button type="submit" class="btn btn-primary">Save Changes</button>
                <a href="/tool-servers" class="btn btn-secondary">Cancel</a>
            </div>
        </form>
    </div>
    """
    
    return HTMLResponse(render_template(content, "tool-servers"))


@app.post("/tool-servers/{server_id}/edit")
async def edit_tool_server(
    server_id: int,
    name: str = Form(...),
    domain: str = Form(...),
    port: int = Form(...),
    description: str = Form(None)
):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE tool_servers SET name = ?, domain = ?, port = ?, description = ? WHERE id = ?",
        (name, domain, port, description, server_id)
    )
    conn.commit()
    conn.close()
    return RedirectResponse(url="/tool-servers", status_code=303)


@app.get("/tool-servers/{server_id}/delete")
async def delete_tool_server(server_id: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM tool_servers WHERE id = ?", (server_id,))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/tool-servers", status_code=303)


# ============== TOOLS ==============

@app.get("/tools", response_class=HTMLResponse)
async def list_tools():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT t.*, ts.name as server_name, ts.domain, ts.port
        FROM tools t
        JOIN tool_servers ts ON t.tool_server_id = ts.id
        ORDER BY t.created_at DESC
    """)
    tools = cursor.fetchall()
    
    cursor.execute("SELECT * FROM tool_servers")
    servers = cursor.fetchall()
    conn.close()
    
    tools_html = ""
    for tool in tools:
        launch_url = f"http://{tool['domain']}:{tool['port']}{tool['launch_path']}"
        tools_html += f"""
        <tr>
            <td><strong>{tool['name']}</strong></td>
            <td>{tool['server_name']}</td>
            <td><code style="font-size: 0.75rem;">{launch_url}</code></td>
            <td><code>{tool['consumer_key']}</code></td>
            <td>
                <a href="/tools/{tool['id']}/edit" class="btn btn-sm btn-secondary">Edit</a>
                <button onclick="confirmDelete('/tools/{tool['id']}/delete', '{tool['name']}')" 
                        class="btn btn-sm btn-danger">Delete</button>
            </td>
        </tr>
        """
    
    if not tools_html:
        tools_html = '<tr><td colspan="5" class="text-muted">No tools configured yet</td></tr>'
    
    server_options = "".join(
        f'<option value="{s["id"]}">{s["name"]} ({s["domain"]}:{s["port"]})</option>'
        for s in servers
    )
    
    if not servers:
        server_options = '<option disabled>Add a tool server first</option>'
    
    content = f"""
    <div class="card">
        <div class="card-header">
            <div class="card-title">Tools</div>
            <button onclick="openModal('addToolModal')" class="btn btn-primary">+ Add Tool</button>
        </div>
        <table>
            <thead>
                <tr>
                    <th>Name</th>
                    <th>Server</th>
                    <th>Launch URL</th>
                    <th>Consumer Key</th>
                    <th>Actions</th>
                </tr>
            </thead>
            <tbody>
                {tools_html}
            </tbody>
        </table>
    </div>
    
    <div id="addToolModal" class="modal">
        <div class="modal-content">
            <div class="modal-header">
                <h3>Add Tool</h3>
                <button onclick="closeModal('addToolModal')" class="modal-close">&times;</button>
            </div>
            <form action="/tools/add" method="post">
                <div class="form-group">
                    <label>Tool Name</label>
                    <input type="text" name="name" required placeholder="My LTI Activity">
                </div>
                <div class="form-group">
                    <label>Tool Server</label>
                    <select name="tool_server_id" required>
                        {server_options}
                    </select>
                </div>
                <div class="form-group">
                    <label>Launch Path</label>
                    <input type="text" name="launch_path" required placeholder="/lti/launch" value="/lti/launch">
                </div>
                <div class="form-group">
                    <label>Consumer Key</label>
                    <input type="text" name="consumer_key" required placeholder="test_key" value="test_key">
                </div>
                <div class="form-group">
                    <label>Consumer Secret</label>
                    <input type="text" name="consumer_secret" required placeholder="test_secret" value="test_secret">
                </div>
                <div class="form-group">
                    <label>Custom Parameters (JSON, optional)</label>
                    <textarea name="custom_params" placeholder='{{"param1": "value1"}}'></textarea>
                </div>
                <div class="form-group">
                    <label>Launch URL Override (optional, for Docker networking)</label>
                    <input type="text" name="launch_url_override" placeholder="http://backend:8080/lti/launch">
                    <small style="color: var(--text-muted); display: block; margin-top: 0.25rem;">
                        If set, this exact URL will be used for OAuth signing instead of constructing from server domain/port.
                        Use this when the URL the tool sees differs from what the browser uses.
                    </small>
                </div>
                <div class="form-group">
                    <label>Description</label>
                    <textarea name="description" placeholder="Optional description..."></textarea>
                </div>
                <button type="submit" class="btn btn-primary">Add Tool</button>
            </form>
        </div>
    </div>
    """
    
    return HTMLResponse(render_template(content, "tools"))


@app.post("/tools/add")
async def add_tool(
    name: str = Form(...),
    tool_server_id: int = Form(...),
    launch_path: str = Form(...),
    consumer_key: str = Form(...),
    consumer_secret: str = Form(...),
    custom_params: str = Form(None),
    description: str = Form(None),
    launch_url_override: str = Form(None)
):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """INSERT INTO tools 
           (tool_server_id, name, launch_path, consumer_key, consumer_secret, custom_params, description, launch_url_override) 
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (tool_server_id, name, launch_path, consumer_key, consumer_secret, custom_params, description, launch_url_override or None)
    )
    conn.commit()
    conn.close()
    return RedirectResponse(url="/tools", status_code=303)


@app.get("/tools/{tool_id}/edit", response_class=HTMLResponse)
async def edit_tool_form(tool_id: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM tools WHERE id = ?", (tool_id,))
    tool = cursor.fetchone()
    
    cursor.execute("SELECT * FROM tool_servers")
    servers = cursor.fetchall()
    conn.close()
    
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")
    
    server_options = "".join(
        f'<option value="{s["id"]}" {"selected" if s["id"] == tool["tool_server_id"] else ""}>'
        f'{s["name"]} ({s["domain"]}:{s["port"]})</option>'
        for s in servers
    )
    
    content = f"""
    <div class="card">
        <div class="card-header">
            <div class="card-title">Edit Tool</div>
        </div>
        <form action="/tools/{tool_id}/edit" method="post">
            <div class="form-group">
                <label>Tool Name</label>
                <input type="text" name="name" required value="{tool['name']}">
            </div>
            <div class="form-group">
                <label>Tool Server</label>
                <select name="tool_server_id" required>
                    {server_options}
                </select>
            </div>
            <div class="form-group">
                <label>Launch Path</label>
                <input type="text" name="launch_path" required value="{tool['launch_path']}">
            </div>
            <div class="form-group">
                <label>Consumer Key</label>
                <input type="text" name="consumer_key" required value="{tool['consumer_key']}">
            </div>
            <div class="form-group">
                <label>Consumer Secret</label>
                <input type="text" name="consumer_secret" required value="{tool['consumer_secret']}">
            </div>
            <div class="form-group">
                <label>Custom Parameters (JSON, optional)</label>
                <textarea name="custom_params">{tool['custom_params'] or ''}</textarea>
            </div>
            <div class="form-group">
                <label>Launch URL Override (optional, for Docker networking)</label>
                <input type="text" name="launch_url_override" value="{tool['launch_url_override'] or ''}">
                <small style="color: var(--text-muted); display: block; margin-top: 0.25rem;">
                    If set, this exact URL will be used for OAuth signing. Use when the tool sees a different URL than the browser.
                </small>
            </div>
            <div class="form-group">
                <label>Description</label>
                <textarea name="description">{tool['description'] or ''}</textarea>
            </div>
            <div class="flex">
                <button type="submit" class="btn btn-primary">Save Changes</button>
                <a href="/tools" class="btn btn-secondary">Cancel</a>
            </div>
        </form>
    </div>
    """
    
    return HTMLResponse(render_template(content, "tools"))


@app.post("/tools/{tool_id}/edit")
async def edit_tool(
    tool_id: int,
    name: str = Form(...),
    tool_server_id: int = Form(...),
    launch_path: str = Form(...),
    consumer_key: str = Form(...),
    consumer_secret: str = Form(...),
    custom_params: str = Form(None),
    description: str = Form(None),
    launch_url_override: str = Form(None)
):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """UPDATE tools SET 
           tool_server_id = ?, name = ?, launch_path = ?, consumer_key = ?, 
           consumer_secret = ?, custom_params = ?, description = ?, launch_url_override = ?
           WHERE id = ?""",
        (tool_server_id, name, launch_path, consumer_key, consumer_secret, custom_params, description, launch_url_override or None, tool_id)
    )
    conn.commit()
    conn.close()
    return RedirectResponse(url="/tools", status_code=303)


@app.get("/tools/{tool_id}/delete")
async def delete_tool(tool_id: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM tools WHERE id = ?", (tool_id,))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/tools", status_code=303)


# ============== COURSES ==============

@app.get("/courses", response_class=HTMLResponse)
async def list_courses():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM courses ORDER BY created_at DESC")
    courses = cursor.fetchall()
    conn.close()
    
    courses_html = ""
    for course in courses:
        courses_html += f"""
        <div class="card">
            <div class="card-header">
                <div>
                    <div class="card-title">{course['name']}</div>
                    <div class="text-muted">{course['code']}</div>
                </div>
                <a href="/courses/{course['id']}" class="btn btn-primary">Open Course</a>
            </div>
            <p style="color: var(--text-secondary);">{course['description'] or 'No description'}</p>
        </div>
        """
    
    if not courses_html:
        courses_html = '<div class="empty-state"><h3>No courses</h3><p>Demo courses should have been created automatically.</p></div>'
    
    content = f"""
    <h2 class="mb-2">Courses</h2>
    <div class="grid grid-3">
        {courses_html}
    </div>
    """
    
    return HTMLResponse(render_template(content, "courses"))


@app.get("/courses/{course_id}", response_class=HTMLResponse)
async def view_course(course_id: int):
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM courses WHERE id = ?", (course_id,))
    course = cursor.fetchone()
    
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    
    # Get enrolled users
    cursor.execute("""
        SELECT u.* FROM users u
        JOIN enrollments e ON u.id = e.user_id
        WHERE e.course_id = ?
        ORDER BY u.role, u.name
    """, (course_id,))
    users = cursor.fetchall()
    
    # Get course tools
    cursor.execute("""
        SELECT ct.*, t.name as tool_name, t.launch_path, t.consumer_key,
               ts.domain, ts.port
        FROM course_tools ct
        JOIN tools t ON ct.tool_id = t.id
        JOIN tool_servers ts ON t.tool_server_id = ts.id
        WHERE ct.course_id = ?
    """, (course_id,))
    course_tools = cursor.fetchall()
    
    # Get available tools to add
    cursor.execute("""
        SELECT t.*, ts.name as server_name, ts.domain, ts.port
        FROM tools t
        JOIN tool_servers ts ON t.tool_server_id = ts.id
        WHERE t.id NOT IN (SELECT tool_id FROM course_tools WHERE course_id = ?)
    """, (course_id,))
    available_tools = cursor.fetchall()
    
    conn.close()
    
    # Build users section
    teachers = [u for u in users if u['role'] == 'teacher']
    students = [u for u in users if u['role'] == 'student']
    
    users_html = "<h4>Teachers</h4><div class='user-selector'>"
    for u in teachers:
        users_html += f"""
        <div class="user-card" onclick="selectUser({u['id']}, this)">
            <div class="name">{u['name']}</div>
            <div class="role">👨‍🏫 Teacher</div>
        </div>
        """
    users_html += "</div><h4>Students</h4><div class='user-selector'>"
    for u in students:
        users_html += f"""
        <div class="user-card" onclick="selectUser({u['id']}, this)">
            <div class="name">{u['name']}</div>
            <div class="role">👨‍🎓 Student</div>
        </div>
        """
    users_html += "</div>"
    
    # Build tools section
    tools_html = ""
    for ct in course_tools:
        launch_url = f"http://{ct['domain']}:{ct['port']}{ct['launch_path']}"
        tools_html += f"""
        <tr>
            <td><strong>{ct['tool_name']}</strong></td>
            <td><code style="font-size: 0.75rem;">{ct['resource_link_id'][:20]}...</code></td>
            <td>
                <button onclick="launchTool({ct['id']}, 'iframe')" class="btn btn-sm btn-success">Launch (iframe)</button>
                <button onclick="launchTool({ct['id']}, 'window')" class="btn btn-sm btn-primary">Launch (new tab)</button>
                <button onclick="confirmDelete('/courses/{course_id}/tools/{ct['id']}/remove', '{ct['tool_name']}')" 
                        class="btn btn-sm btn-danger">Remove</button>
            </td>
        </tr>
        """
    
    if not tools_html:
        tools_html = '<tr><td colspan="3" class="text-muted">No tools added to this course yet</td></tr>'
    
    # Available tools dropdown
    tool_options = "".join(
        f'<option value="{t["id"]}">{t["name"]} ({t["server_name"]})</option>'
        for t in available_tools
    )
    
    if not available_tools:
        tool_options = '<option disabled>No more tools available</option>'
    
    content = f"""
    <div class="flex flex-between mb-2">
        <div>
            <h2>{course['name']}</h2>
            <p class="text-muted">{course['code']} • {course['description'] or 'No description'}</p>
        </div>
        <a href="/courses" class="btn btn-secondary">← Back to Courses</a>
    </div>
    
    <div class="grid grid-2">
        <div class="card">
            <div class="card-header">
                <div class="card-title">Select User to Launch As</div>
            </div>
            <input type="hidden" id="selected_user_id" value="">
            {users_html}
        </div>
        
        <div class="card">
            <div class="card-header">
                <div class="card-title">Add Tool to Course</div>
            </div>
            <form action="/courses/{course_id}/tools/add" method="post" class="inline-form">
                <div class="form-group">
                    <select name="tool_id" required>
                        <option value="">Select a tool...</option>
                        {tool_options}
                    </select>
                </div>
                <button type="submit" class="btn btn-primary">Add Tool</button>
            </form>
        </div>
    </div>
    
    <div class="card">
        <div class="card-header">
            <div class="card-title">Course Tools</div>
        </div>
        <table>
            <thead>
                <tr>
                    <th>Tool</th>
                    <th>Resource Link ID</th>
                    <th>Actions</th>
                </tr>
            </thead>
            <tbody>
                {tools_html}
            </tbody>
        </table>
    </div>
    
    <div id="launchFrame" class="card" style="display: none;">
        <div class="card-header">
            <div class="card-title">Tool Launch</div>
            <button onclick="document.getElementById('launchFrame').style.display='none'" class="btn btn-sm btn-secondary">Close</button>
        </div>
        <iframe id="toolIframe" class="launch-frame"></iframe>
    </div>
    
    <script>
        function launchTool(courseToolId, mode) {{
            const userId = document.getElementById('selected_user_id').value;
            if (!userId) {{
                alert('Please select a user first');
                return;
            }}
            
            const launchUrl = `/launch/${{courseToolId}}?user_id=${{userId}}`;
            
            if (mode === 'iframe') {{
                document.getElementById('launchFrame').style.display = 'block';
                document.getElementById('toolIframe').src = launchUrl;
            }} else {{
                window.open(launchUrl, '_blank');
            }}
        }}
    </script>
    """
    
    return HTMLResponse(render_template(content, "courses"))


@app.post("/courses/{course_id}/tools/add")
async def add_tool_to_course(course_id: int, tool_id: int = Form(...)):
    conn = get_db()
    cursor = conn.cursor()
    
    # Generate unique resource link id
    resource_link_id = str(uuid.uuid4())
    
    # Get tool name for title
    cursor.execute("SELECT name FROM tools WHERE id = ?", (tool_id,))
    tool = cursor.fetchone()
    resource_link_title = tool['name'] if tool else "LTI Activity"
    
    cursor.execute(
        """INSERT INTO course_tools (course_id, tool_id, resource_link_id, resource_link_title)
           VALUES (?, ?, ?, ?)""",
        (course_id, tool_id, resource_link_id, resource_link_title)
    )
    conn.commit()
    conn.close()
    
    return RedirectResponse(url=f"/courses/{course_id}", status_code=303)


@app.get("/courses/{course_id}/tools/{course_tool_id}/remove")
async def remove_tool_from_course(course_id: int, course_tool_id: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM course_tools WHERE id = ? AND course_id = ?", (course_tool_id, course_id))
    conn.commit()
    conn.close()
    return RedirectResponse(url=f"/courses/{course_id}", status_code=303)


# ============== LAUNCH ==============

@app.get("/launch/{course_tool_id}", response_class=HTMLResponse)
async def launch_tool(request: Request, course_tool_id: int, user_id: int):
    conn = get_db()
    cursor = conn.cursor()
    
    # Get course tool details
    cursor.execute("""
        SELECT ct.*, t.*, ts.domain, ts.port,
               c.id as course_id, c.name as course_name, c.code as course_code
        FROM course_tools ct
        JOIN tools t ON ct.tool_id = t.id
        JOIN tool_servers ts ON t.tool_server_id = ts.id
        JOIN courses c ON ct.course_id = c.id
        WHERE ct.id = ?
    """, (course_tool_id,))
    ct = cursor.fetchone()
    
    if not ct:
        raise HTTPException(status_code=404, detail="Course tool not found")
    
    # Get user
    cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    user = cursor.fetchone()
    
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Build launch URL - use override if specified, otherwise construct from server
    if ct['launch_url_override']:
        launch_url = ct['launch_url_override']
        print(f"DEBUG: Using launch_url_override: {launch_url}")
    else:
        launch_url = f"http://{ct['domain']}:{ct['port']}{ct['launch_path']}"
        print(f"DEBUG: Constructed launch_url: {launch_url}")
    
    # Get the platform's base URL for outcomes
    base_url = str(request.base_url).rstrip('/')
    outcomes_url = f"{base_url}/outcomes"
    
    # Parse custom params if any
    custom_params = {}
    if ct['custom_params']:
        try:
            custom_params = json.loads(ct['custom_params'])
        except:
            pass
    
    # Build LTI params
    tool_dict = dict(ct)
    course_dict = {
        'id': ct['course_id'],
        'name': ct['course_name'],
        'code': ct['course_code']
    }
    user_dict = dict(user)
    
    params = build_lti_launch_params(
        tool=tool_dict,
        course=course_dict,
        user=user_dict,
        resource_link_id=ct['resource_link_id'],
        resource_link_title=ct['resource_link_title'] or ct['name'],
        launch_url=launch_url,
        outcomes_url=outcomes_url,
        custom_params=custom_params
    )
    
    # Log the launch
    params_without_sig = {k: v for k, v in params.items() if k != 'oauth_signature'}
    cursor.execute(
        """INSERT INTO launch_logs 
           (course_tool_id, user_id, launch_params, signed_params, oauth_signature)
           VALUES (?, ?, ?, ?, ?)""",
        (course_tool_id, user_id, json.dumps(params_without_sig, indent=2), 
         json.dumps(params, indent=2), params['oauth_signature'])
    )
    conn.commit()
    conn.close()
    
    # Generate auto-submit form
    form_fields = "".join(
        f'<input type="hidden" name="{k}" value="{v}">'
        for k, v in params.items()
    )
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Launching LTI Tool...</title>
        <style>
            body {{
                font-family: 'Space Grotesk', sans-serif;
                background: #0a0a0f;
                color: #f8fafc;
                display: flex;
                align-items: center;
                justify-content: center;
                min-height: 100vh;
                margin: 0;
            }}
            .loader {{
                text-align: center;
            }}
            .spinner {{
                width: 40px;
                height: 40px;
                border: 3px solid #2d2d3a;
                border-top-color: #6366f1;
                border-radius: 50%;
                animation: spin 1s linear infinite;
                margin: 0 auto 1rem;
            }}
            @keyframes spin {{
                to {{ transform: rotate(360deg); }}
            }}
        </style>
    </head>
    <body>
        <div class="loader">
            <div class="spinner"></div>
            <p>Launching LTI Tool...</p>
        </div>
        <form id="ltiForm" action="{launch_url}" method="POST" style="display:none;">
            {form_fields}
        </form>
        <script>
            document.getElementById('ltiForm').submit();
        </script>
    </body>
    </html>
    """
    
    return HTMLResponse(html)


# ============== LAUNCH LOGS ==============

@app.get("/launch-logs", response_class=HTMLResponse)
async def list_launch_logs():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT ll.*, u.name as user_name, u.role as user_role,
               t.name as tool_name, c.name as course_name
        FROM launch_logs ll
        JOIN users u ON ll.user_id = u.id
        JOIN course_tools ct ON ll.course_tool_id = ct.id
        JOIN tools t ON ct.tool_id = t.id
        JOIN courses c ON ct.course_id = c.id
        ORDER BY ll.launched_at DESC
        LIMIT 100
    """)
    logs = cursor.fetchall()
    conn.close()
    
    logs_html = ""
    for log in logs:
        role_badge = "badge-teacher" if log['user_role'] == 'teacher' else "badge-student"
        logs_html += f"""
        <tr>
            <td>{log['launched_at']}</td>
            <td>{log['course_name']}</td>
            <td>{log['tool_name']}</td>
            <td>
                {log['user_name']}
                <span class="badge {role_badge}">{log['user_role']}</span>
            </td>
            <td>
                <a href="/launch-logs/{log['id']}" class="btn btn-sm btn-secondary">Inspect</a>
            </td>
        </tr>
        """
    
    if not logs_html:
        logs_html = '<tr><td colspan="5" class="text-muted">No launches recorded yet</td></tr>'
    
    content = f"""
    <div class="card">
        <div class="card-header">
            <div class="card-title">Launch Logs</div>
        </div>
        <table>
            <thead>
                <tr>
                    <th>Time</th>
                    <th>Course</th>
                    <th>Tool</th>
                    <th>User</th>
                    <th>Actions</th>
                </tr>
            </thead>
            <tbody>
                {logs_html}
            </tbody>
        </table>
    </div>
    """
    
    return HTMLResponse(render_template(content, "launch-logs"))


@app.get("/launch-logs/{log_id}", response_class=HTMLResponse)
async def view_launch_log(log_id: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT ll.*, u.name as user_name, u.role as user_role, u.email as user_email,
               t.name as tool_name, t.launch_path, t.consumer_key, t.consumer_secret,
               ts.domain, ts.port,
               c.name as course_name, c.code as course_code
        FROM launch_logs ll
        JOIN users u ON ll.user_id = u.id
        JOIN course_tools ct ON ll.course_tool_id = ct.id
        JOIN tools t ON ct.tool_id = t.id
        JOIN tool_servers ts ON t.tool_server_id = ts.id
        JOIN courses c ON ct.course_id = c.id
        WHERE ll.id = ?
    """, (log_id,))
    log = cursor.fetchone()
    conn.close()
    
    if not log:
        raise HTTPException(status_code=404, detail="Launch log not found")
    
    launch_url = f"http://{log['domain']}:{log['port']}{log['launch_path']}"
    role_badge = "badge-teacher" if log['user_role'] == 'teacher' else "badge-student"
    
    content = f"""
    <div class="flex flex-between mb-2">
        <div>
            <h2>Launch Inspection</h2>
            <p class="text-muted">Launched at {log['launched_at']}</p>
        </div>
        <a href="/launch-logs" class="btn btn-secondary">← Back to Logs</a>
    </div>
    
    <div class="grid grid-2">
        <div class="card">
            <div class="card-title mb-2">Launch Details</div>
            <table>
                <tr><td class="text-muted">Course</td><td>{log['course_name']} ({log['course_code']})</td></tr>
                <tr><td class="text-muted">Tool</td><td>{log['tool_name']}</td></tr>
                <tr><td class="text-muted">Launch URL</td><td><code>{launch_url}</code></td></tr>
                <tr>
                    <td class="text-muted">User</td>
                    <td>{log['user_name']} <span class="badge {role_badge}">{log['user_role']}</span></td>
                </tr>
                <tr><td class="text-muted">Email</td><td>{log['user_email']}</td></tr>
            </table>
        </div>
        
        <div class="card">
            <div class="card-title mb-2">OAuth Details</div>
            <table>
                <tr><td class="text-muted">Consumer Key</td><td><code>{log['consumer_key']}</code></td></tr>
                <tr><td class="text-muted">Consumer Secret</td><td><code>{log['consumer_secret']}</code></td></tr>
                <tr><td class="text-muted">Signature</td><td><code style="word-break: break-all;">{log['oauth_signature']}</code></td></tr>
            </table>
        </div>
    </div>
    
    <div class="card">
        <div class="card-title mb-2">Launch Parameters</div>
        <div class="tabs">
            <button class="tab active" data-tab="unsigned" onclick="showTab('unsigned')">Unsigned Params</button>
            <button class="tab" data-tab="signed" onclick="showTab('signed')">Signed Params</button>
        </div>
        <div id="unsigned" class="tab-content active">
            <div class="code-block"><pre>{log['launch_params']}</pre></div>
        </div>
        <div id="signed" class="tab-content">
            <div class="code-block"><pre>{log['signed_params']}</pre></div>
        </div>
    </div>
    """
    
    return HTMLResponse(render_template(content, "launch-logs"))


# ============== OUTCOMES (GRADES) ==============

@app.post("/outcomes")
async def receive_outcome(request: Request):
    """LTI Basic Outcomes Service endpoint for receiving grades."""
    body = await request.body()
    body_text = body.decode('utf-8')
    
    # Parse the XML (simple extraction)
    import re
    
    # Extract sourcedId
    sourced_id_match = re.search(r'<sourcedId>([^<]+)</sourcedId>', body_text)
    sourced_id = sourced_id_match.group(1) if sourced_id_match else None
    
    # Extract score
    score_match = re.search(r'<textString>([^<]+)</textString>', body_text)
    score = float(score_match.group(1)) if score_match else None
    
    # Extract message identifier
    msg_id_match = re.search(r'<imsx_messageIdentifier>([^<]+)</imsx_messageIdentifier>', body_text)
    msg_id = msg_id_match.group(1) if msg_id_match else str(uuid.uuid4())
    
    if sourced_id and score is not None:
        # Decode sourced_id to get course_id, resource_link_id, user_id
        try:
            decoded = base64.b64decode(sourced_id).decode()
            parts = decoded.split(':')
            if len(parts) == 3:
                course_id, resource_link_id, user_id = parts
                
                conn = get_db()
                cursor = conn.cursor()
                
                # Find the course_tool
                cursor.execute("""
                    SELECT id FROM course_tools 
                    WHERE course_id = ? AND resource_link_id = ?
                """, (course_id, resource_link_id))
                ct = cursor.fetchone()
                
                if ct:
                    cursor.execute(
                        """INSERT INTO grade_results 
                           (course_tool_id, user_id, sourced_id, score, raw_xml)
                           VALUES (?, ?, ?, ?, ?)""",
                        (ct['id'], user_id, sourced_id, score, body_text)
                    )
                    conn.commit()
                conn.close()
        except:
            pass
    
    # Return success response
    response_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<imsx_POXEnvelopeResponse xmlns="http://www.imsglobal.org/services/ltiv1p1/xsd/imsoms_v1p0">
  <imsx_POXHeader>
    <imsx_POXResponseHeaderInfo>
      <imsx_version>V1.0</imsx_version>
      <imsx_messageIdentifier>{msg_id}</imsx_messageIdentifier>
      <imsx_statusInfo>
        <imsx_codeMajor>success</imsx_codeMajor>
        <imsx_severity>status</imsx_severity>
        <imsx_description>Score received</imsx_description>
        <imsx_messageRefIdentifier>{msg_id}</imsx_messageRefIdentifier>
        <imsx_operationRefIdentifier>replaceResult</imsx_operationRefIdentifier>
      </imsx_statusInfo>
    </imsx_POXResponseHeaderInfo>
  </imsx_POXHeader>
  <imsx_POXBody>
    <replaceResultResponse/>
  </imsx_POXBody>
</imsx_POXEnvelopeResponse>"""
    
    return HTMLResponse(content=response_xml, media_type="application/xml")


@app.get("/grades", response_class=HTMLResponse)
async def list_grades():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT gr.*, u.name as user_name, u.role as user_role,
               t.name as tool_name, c.name as course_name
        FROM grade_results gr
        JOIN users u ON gr.user_id = u.id
        JOIN course_tools ct ON gr.course_tool_id = ct.id
        JOIN tools t ON ct.tool_id = t.id
        JOIN courses c ON ct.course_id = c.id
        ORDER BY gr.received_at DESC
        LIMIT 100
    """)
    grades = cursor.fetchall()
    conn.close()
    
    grades_html = ""
    for grade in grades:
        role_badge = "badge-teacher" if grade['user_role'] == 'teacher' else "badge-student"
        score_pct = int(grade['score'] * 100) if grade['score'] else 0
        grades_html += f"""
        <tr>
            <td>{grade['received_at']}</td>
            <td>{grade['course_name']}</td>
            <td>{grade['tool_name']}</td>
            <td>
                {grade['user_name']}
                <span class="badge {role_badge}">{grade['user_role']}</span>
            </td>
            <td><span class="score text-success">{score_pct}%</span></td>
            <td>
                <a href="/grades/{grade['id']}" class="btn btn-sm btn-secondary">View XML</a>
            </td>
        </tr>
        """
    
    if not grades_html:
        grades_html = '<tr><td colspan="6" class="text-muted">No grades received yet</td></tr>'
    
    content = f"""
    <div class="card">
        <div class="card-header">
            <div class="card-title">Received Grades</div>
        </div>
        <table>
            <thead>
                <tr>
                    <th>Time</th>
                    <th>Course</th>
                    <th>Tool</th>
                    <th>User</th>
                    <th>Score</th>
                    <th>Actions</th>
                </tr>
            </thead>
            <tbody>
                {grades_html}
            </tbody>
        </table>
    </div>
    
    <div class="card">
        <div class="card-title mb-2">Outcomes Endpoint</div>
        <p class="text-muted mb-1">Tools should send grade results to:</p>
        <code style="display: block; padding: 0.75rem; background: var(--bg-tertiary); border-radius: var(--radius);">
            POST /outcomes
        </code>
    </div>
    """
    
    return HTMLResponse(render_template(content, "grades"))


@app.get("/grades/{grade_id}", response_class=HTMLResponse)
async def view_grade(grade_id: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT gr.*, u.name as user_name, u.role as user_role,
               t.name as tool_name, c.name as course_name
        FROM grade_results gr
        JOIN users u ON gr.user_id = u.id
        JOIN course_tools ct ON gr.course_tool_id = ct.id
        JOIN tools t ON ct.tool_id = t.id
        JOIN courses c ON ct.course_id = c.id
        WHERE gr.id = ?
    """, (grade_id,))
    grade = cursor.fetchone()
    conn.close()
    
    if not grade:
        raise HTTPException(status_code=404, detail="Grade not found")
    
    role_badge = "badge-teacher" if grade['user_role'] == 'teacher' else "badge-student"
    score_pct = int(grade['score'] * 100) if grade['score'] else 0
    
    # Pretty print XML
    import xml.dom.minidom
    try:
        pretty_xml = xml.dom.minidom.parseString(grade['raw_xml']).toprettyxml(indent="  ")
    except:
        pretty_xml = grade['raw_xml']
    
    content = f"""
    <div class="flex flex-between mb-2">
        <div>
            <h2>Grade Details</h2>
            <p class="text-muted">Received at {grade['received_at']}</p>
        </div>
        <a href="/grades" class="btn btn-secondary">← Back to Grades</a>
    </div>
    
    <div class="card">
        <div class="card-title mb-2">Grade Information</div>
        <table>
            <tr><td class="text-muted">Course</td><td>{grade['course_name']}</td></tr>
            <tr><td class="text-muted">Tool</td><td>{grade['tool_name']}</td></tr>
            <tr>
                <td class="text-muted">User</td>
                <td>{grade['user_name']} <span class="badge {role_badge}">{grade['user_role']}</span></td>
            </tr>
            <tr><td class="text-muted">Score</td><td><span class="score text-success">{score_pct}%</span> ({grade['score']})</td></tr>
            <tr><td class="text-muted">Sourced ID</td><td><code style="word-break: break-all;">{grade['sourced_id']}</code></td></tr>
        </table>
    </div>
    
    <div class="card">
        <div class="card-title mb-2">Raw XML Payload</div>
        <div class="code-block"><pre>{pretty_xml}</pre></div>
    </div>
    """
    
    return HTMLResponse(render_template(content, "grades"))


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
