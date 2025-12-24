"""
Sample LTI 1.1 Tool Provider
A simple tool that receives LTI launches and can send grades back.

Run this on port 8080 to test with the LTI Platform.
"""

import hashlib
import hmac
import base64
import urllib.parse
import uuid
import time

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
import httpx

app = FastAPI(title="Sample LTI Tool")

# Mount static files directory
app.mount("/static", StaticFiles(directory="."), name="static")

# Store launch data temporarily (in production, use a proper database)
launches = {}

# Expected credentials (should match what you configure in the platform)
EXPECTED_KEY = "test_key"
EXPECTED_SECRET = "test_secret"


def verify_oauth_signature(method: str, url: str, params: dict, consumer_secret: str, received_signature: str) -> bool:
    """Verify OAuth 1.0a signature."""
    verify_params = {k: v for k, v in params.items() if k != 'oauth_signature'}
    sorted_params = sorted(verify_params.items())
    
    param_string = "&".join(
        f"{urllib.parse.quote(str(k), safe='')}"
        f"={urllib.parse.quote(str(v), safe='')}"
        for k, v in sorted_params
    )
    
    signature_base = "&".join([
        method.upper(),
        urllib.parse.quote(url, safe=''),
        urllib.parse.quote(param_string, safe='')
    ])
    
    signing_key = f"{urllib.parse.quote(consumer_secret, safe='')}&"
    
    hashed = hmac.new(
        signing_key.encode('utf-8'),
        signature_base.encode('utf-8'),
        hashlib.sha1
    )
    expected_signature = base64.b64encode(hashed.digest()).decode('utf-8')
    
    return hmac.compare_digest(expected_signature, received_signature)


@app.post("/lti/launch", response_class=HTMLResponse)
async def lti_launch(request: Request):
    """Handle LTI launch requests."""
    form_data = await request.form()
    params = dict(form_data)
    
    # Validate required LTI parameters
    if params.get('lti_message_type') != 'basic-lti-launch-request':
        raise HTTPException(status_code=400, detail="Invalid LTI message type")
    
    if params.get('lti_version') != 'LTI-1p0':
        raise HTTPException(status_code=400, detail="Invalid LTI version")
    
    # Verify OAuth
    consumer_key = params.get('oauth_consumer_key')
    if consumer_key != EXPECTED_KEY:
        raise HTTPException(status_code=401, detail="Invalid consumer key")
    
    url = str(request.url).split('?')[0]
    received_signature = params.get('oauth_signature', '')
    
    if not verify_oauth_signature('POST', url, params, EXPECTED_SECRET, received_signature):
        print(f"WARNING: OAuth signature mismatch. URL: {url}")
    
    # Store launch data
    launch_id = str(uuid.uuid4())
    launches[launch_id] = {'params': params, 'timestamp': time.time()}
    
    # Extract user info
    user_name = params.get('lis_person_name_full', 'Unknown User')
    user_role = params.get('roles', 'Unknown')
    course_name = params.get('context_title', 'Unknown Course')
    resource_title = params.get('resource_link_title', 'Activity')
    
    # Check if we can send grades
    outcomes_url = params.get('lis_outcome_service_url')
    sourced_id = params.get('lis_result_sourcedid')
    can_send_grade = bool(outcomes_url and sourced_id)
    
    grade_form = ""
    if can_send_grade:
        grade_form = f'''
        <div class="grade-section">
            <h3>📊 Send Grade</h3>
            <form action="/send-grade" method="post">
                <input type="hidden" name="launch_id" value="{launch_id}">
                <div class="form-group">
                    <label>Score (0.0 - 1.0):</label>
                    <input type="number" name="score" min="0" max="1" step="0.01" value="0.85" required>
                </div>
                <button type="submit" class="btn btn-success">Send Grade to Platform</button>
            </form>
        </div>
        '''
    
    params_display = "\n".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in sorted(params.items()))
    
    html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <title>LTI Tool - {resource_title}</title>
        <style>
            * {{ margin: 0; padding: 0; box-sizing: border-box; }}
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                padding: 2rem;
            }}
            .container {{
                max-width: 900px;
                margin: 0 auto;
                background: white;
                border-radius: 16px;
                box-shadow: 0 25px 50px -12px rgba(0,0,0,0.25);
                overflow: hidden;
            }}
            .header {{
                background: linear-gradient(135deg, #1e3a5f 0%, #2d5a87 100%);
                color: white;
                padding: 2rem;
            }}
            .header h1 {{ font-size: 1.5rem; margin-bottom: 0.5rem; }}
            .header p {{ opacity: 0.8; }}
            .content {{ padding: 2rem; }}
            .info-card {{
                background: #f8fafc;
                border-radius: 8px;
                padding: 1.5rem;
                margin-bottom: 1.5rem;
            }}
            .info-card h3 {{ color: #1e3a5f; margin-bottom: 1rem; }}
            .info-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                gap: 1rem;
            }}
            .info-item {{
                padding: 0.75rem;
                background: white;
                border-radius: 6px;
                border: 1px solid #e2e8f0;
            }}
            .info-item label {{
                font-size: 0.75rem;
                color: #64748b;
                text-transform: uppercase;
                letter-spacing: 0.05em;
            }}
            .info-item p {{
                font-weight: 600;
                color: #1e293b;
                margin-top: 0.25rem;
            }}
            .grade-section {{
                background: #ecfdf5;
                border: 1px solid #a7f3d0;
                border-radius: 8px;
                padding: 1.5rem;
                margin-bottom: 1.5rem;
            }}
            .grade-section h3 {{ color: #065f46; margin-bottom: 1rem; }}
            .form-group {{ margin-bottom: 1rem; }}
            .form-group label {{ display: block; margin-bottom: 0.5rem; font-weight: 500; }}
            .form-group input {{
                padding: 0.75rem;
                border: 1px solid #d1d5db;
                border-radius: 6px;
                font-size: 1rem;
                width: 150px;
            }}
            .btn {{
                padding: 0.75rem 1.5rem;
                border: none;
                border-radius: 6px;
                font-weight: 600;
                cursor: pointer;
                font-size: 0.9rem;
            }}
            .btn-success {{ background: #10b981; color: white; }}
            .btn-success:hover {{ background: #059669; }}
            .params-table {{
                width: 100%;
                border-collapse: collapse;
                font-size: 0.85rem;
            }}
            .params-table td {{
                padding: 0.5rem;
                border-bottom: 1px solid #e2e8f0;
                vertical-align: top;
            }}
            .params-table td:first-child {{
                font-weight: 500;
                color: #64748b;
                width: 250px;
            }}
            .params-table td:last-child {{
                word-break: break-all;
                font-family: monospace;
                font-size: 0.8rem;
            }}
            details {{ margin-top: 1rem; }}
            summary {{
                cursor: pointer;
                font-weight: 600;
                color: #1e3a5f;
                padding: 0.5rem;
                background: #f1f5f9;
                border-radius: 6px;
            }}
            .activity-content {{
                background: #fef3c7;
                border: 1px solid #fcd34d;
                border-radius: 8px;
                padding: 1.5rem;
                margin-bottom: 1.5rem;
            }}
            .activity-content h3 {{ color: #92400e; margin-bottom: 1rem; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <div style="display: flex; align-items: center; justify-content: space-between;">
                    <div>
                        <h1>🎓 Sample LTI Tool</h1>
                        <p>Successfully launched via LTI 1.1</p>
                    </div>
                    <a href="https://lamb-project.org" target="_blank" rel="noopener">
                        <img src="/static/lamb_1.png" alt="LAMB Project" style="height: 90px; width: auto;">
                    </a>
                </div>
            </div>
            <div class="content">
                <div class="info-card">
                    <h3>👤 Launch Context</h3>
                    <div class="info-grid">
                        <div class="info-item">
                            <label>User</label>
                            <p>{user_name}</p>
                        </div>
                        <div class="info-item">
                            <label>Role</label>
                            <p>{user_role}</p>
                        </div>
                        <div class="info-item">
                            <label>Course</label>
                            <p>{course_name}</p>
                        </div>
                        <div class="info-item">
                            <label>Activity</label>
                            <p>{resource_title}</p>
                        </div>
                    </div>
                </div>
                
                <div class="activity-content">
                    <h3>📝 Sample Activity</h3>
                    <p>This is where your actual LTI tool content would go. This could be:</p>
                    <ul style="margin: 1rem 0 0 1.5rem;">
                        <li>An interactive quiz or assessment</li>
                        <li>A coding exercise environment</li>
                        <li>A collaborative document editor</li>
                        <li>A video player with tracking</li>
                    </ul>
                </div>
                
                {grade_form}
                
                <details>
                    <summary>📋 View All Launch Parameters</summary>
                    <div style="margin-top: 1rem; max-height: 400px; overflow-y: auto;">
                        <table class="params-table">
                            {params_display}
                        </table>
                    </div>
                </details>
            </div>
        </div>
    </body>
    </html>
    '''
    
    return HTMLResponse(html)


@app.post("/send-grade", response_class=HTMLResponse)
async def send_grade(launch_id: str = Form(...), score: float = Form(...)):
    """Send a grade back to the LTI platform."""
    
    if launch_id not in launches:
        raise HTTPException(status_code=404, detail="Launch not found")
    
    launch_data = launches[launch_id]
    params = launch_data['params']
    
    outcomes_url = params.get('lis_outcome_service_url')
    sourced_id = params.get('lis_result_sourcedid')
    
    if not outcomes_url or not sourced_id:
        raise HTTPException(status_code=400, detail="Outcomes not supported")
    
    message_id = str(uuid.uuid4())
    xml_payload = f'''<?xml version="1.0" encoding="UTF-8"?>
<imsx_POXEnvelopeRequest xmlns="http://www.imsglobal.org/services/ltiv1p1/xsd/imsoms_v1p0">
  <imsx_POXHeader>
    <imsx_POXRequestHeaderInfo>
      <imsx_version>V1.0</imsx_version>
      <imsx_messageIdentifier>{message_id}</imsx_messageIdentifier>
    </imsx_POXRequestHeaderInfo>
  </imsx_POXHeader>
  <imsx_POXBody>
    <replaceResultRequest>
      <resultRecord>
        <sourcedGUID>
          <sourcedId>{sourced_id}</sourcedId>
        </sourcedGUID>
        <result>
          <resultScore>
            <language>en</language>
            <textString>{score}</textString>
          </resultScore>
        </result>
      </resultRecord>
    </replaceResultRequest>
  </imsx_POXBody>
</imsx_POXEnvelopeRequest>'''
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                outcomes_url,
                content=xml_payload,
                headers={'Content-Type': 'application/xml'}
            )
        
        success = response.status_code == 200 and 'success' in response.text.lower()
        
        return HTMLResponse(f'''
        <!DOCTYPE html>
        <html>
        <head>
            <title>Grade Sent</title>
            <style>
                body {{
                    font-family: -apple-system, sans-serif;
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    min-height: 100vh;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    padding: 2rem;
                }}
                .card {{
                    background: white;
                    border-radius: 16px;
                    padding: 2rem;
                    max-width: 600px;
                    box-shadow: 0 25px 50px -12px rgba(0,0,0,0.25);
                }}
                .success {{ color: #10b981; }}
                .error {{ color: #ef4444; }}
                h1 {{ margin-bottom: 1rem; }}
                pre {{
                    background: #f1f5f9;
                    padding: 1rem;
                    border-radius: 8px;
                    overflow-x: auto;
                    font-size: 0.8rem;
                    max-height: 300px;
                    overflow-y: auto;
                }}
                .back-link {{
                    display: inline-block;
                    margin-top: 1rem;
                    color: #6366f1;
                }}
            </style>
        </head>
        <body>
            <div class="card">
                <h1 class="{'success' if success else 'error'}">
                    {'✅ Grade Sent Successfully!' if success else '❌ Failed to Send Grade'}
                </h1>
                <p><strong>Score:</strong> {score} ({int(score * 100)}%)</p>
                <p><strong>Outcomes URL:</strong> {outcomes_url}</p>
                <h3 style="margin-top: 1rem;">Response:</h3>
                <pre>{response.text}</pre>
                <a href="javascript:history.back()" class="back-link">← Back to Activity</a>
            </div>
        </body>
        </html>
        ''')
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send grade: {str(e)}")


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse('''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Sample LTI Tool</title>
        <style>
            body {
                font-family: -apple-system, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                display: flex;
                align-items: center;
                justify-content: center;
            }
            .card {
                background: white;
                border-radius: 16px;
                padding: 3rem;
                text-align: center;
                box-shadow: 0 25px 50px -12px rgba(0,0,0,0.25);
            }
            h1 { color: #1e3a5f; margin-bottom: 1rem; }
            p { color: #64748b; }
            code {
                display: block;
                background: #f1f5f9;
                padding: 1rem;
                border-radius: 8px;
                margin-top: 1rem;
            }
        </style>
    </head>
    <body>
        <div class="card">
            <a href="https://lamb-project.org" target="_blank" rel="noopener" style="display: block; margin-bottom: 1rem;">
                <img src="/static/lamb_1.png" alt="LAMB Project" style="height: 150px; width: auto;">
            </a>
            <h1>🎓 Sample LTI 1.1 Tool</h1>
            <p>This tool must be launched via LTI from a platform.</p>
            <p>Configure your platform to launch:</p>
            <code>POST /lti/launch</code>
            <p style="margin-top: 1rem;">
                <strong>Consumer Key:</strong> test_key<br>
                <strong>Consumer Secret:</strong> test_secret
            </p>
        </div>
    </body>
    </html>
    ''')


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
