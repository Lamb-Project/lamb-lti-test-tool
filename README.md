# LTI 1.1 Test Platform

A local development platform for testing LTI 1.1 tool integrations. Built with Python, FastAPI, and SQLite3.

## Features

- **Tool Server Management**: Add and manage LTI tool server configurations (domain + port)
- **Tool Configuration**: Configure tools with consumer key/secret, launch paths, and custom parameters
- **Demo Courses**: Pre-seeded courses with 2 teachers and 4 students each
- **User Switching**: Easily switch between users to test different roles (Instructor/Learner)
- **Flexible Launch**: Launch tools in iframe or new tab
- **Launch Inspection**: View all launch parameters, signed requests, and OAuth signatures
- **Grade Reception**: Receive grades via LTI Basic Outcomes Service (replaceResult)
- **Complete Logging**: Full audit trail of all launches and grade submissions

## Quick Start

### Option 1: Run with Python directly

```bash
# Install dependencies
pip install -r requirements.txt

# Run the platform (port 8000)
python app.py

# In another terminal, run the sample tool (port 8080)
python sample_tool.py
```

### Option 2: Run with Docker

```bash
# Build and run the platform
docker build -t lti-platform .
docker run -p 8000:8000 lti-platform

# Or use docker-compose
docker-compose up
```

### Option 3: For Docker networking with your own LTI tool

When running in Docker, use `host.docker.internal` instead of `localhost` to access services on your host machine.

## Usage Guide

### 1. Add a Tool Server

Navigate to **Tool Servers** and add your LTI tool's server:
- **Name**: A friendly name for the server
- **Domain**: `localhost` (or `host.docker.internal` in Docker)
- **Port**: The port your LTI tool runs on (e.g., `8080`)

### 2. Create a Tool

Navigate to **Tools** and create a new tool:
- **Tool Server**: Select the server you just created
- **Launch Path**: The LTI launch endpoint (e.g., `/lti/launch`)
- **Consumer Key**: Your OAuth consumer key
- **Consumer Secret**: Your OAuth consumer secret
- **Custom Parameters**: Optional JSON object for custom LTI parameters

### 3. Add Tool to a Course

Navigate to **Courses**, select a course, and add your tool to it. This creates a unique `resource_link_id` for the course-tool combination.

### 4. Launch the Tool

1. Select a user (teacher or student) from the user cards
2. Click **Launch (iframe)** or **Launch (new tab)**
3. The platform will:
   - Build all required LTI 1.1 parameters
   - Sign the request with OAuth 1.0a HMAC-SHA1
   - Submit the form to your tool
   - Log the complete launch for inspection

### 5. Inspect Launches

Navigate to **Launch Logs** to see all launches. Click **Inspect** to view:
- All unsigned parameters
- Complete signed request
- OAuth signature details
- User and context information

### 6. Receive Grades

When your tool sends grades back, they appear in **Grades**. The platform provides:
- Score display (as percentage)
- User and course context
- Raw XML payload for debugging

## LTI Parameters Sent

The platform sends all standard LTI 1.1 parameters:

### Required LTI Parameters
- `lti_message_type`: basic-lti-launch-request
- `lti_version`: LTI-1p0

### OAuth 1.0a Parameters
- `oauth_consumer_key`
- `oauth_signature_method`: HMAC-SHA1
- `oauth_timestamp`
- `oauth_nonce`
- `oauth_version`: 1.0
- `oauth_signature`

### Context (Course) Parameters
- `context_id`
- `context_label`
- `context_title`
- `context_type`: CourseSection

### User Parameters
- `user_id`
- `lis_person_name_given`
- `lis_person_name_family`
- `lis_person_name_full`
- `lis_person_contact_email_primary`
- `roles`: Instructor or Learner

### Resource Parameters
- `resource_link_id`
- `resource_link_title`

### Outcomes Service
- `lis_outcome_service_url`
- `lis_result_sourcedid`

### Tool Consumer Info
- `tool_consumer_instance_guid`
- `tool_consumer_instance_name`
- `tool_consumer_info_product_family_code`
- `tool_consumer_info_version`

## Sample Tool

The included `sample_tool.py` is a complete LTI 1.1 Tool Provider that:
- Validates LTI launch requests
- Verifies OAuth signatures
- Displays launch context and parameters
- Sends grades back to the platform

Run it on port 8080:
```bash
python sample_tool.py
```

Default credentials:
- **Consumer Key**: `test_key`
- **Consumer Secret**: `test_secret`

## Outcomes Endpoint

Your LTI tool should send grades to:
```
POST http://localhost:8000/outcomes
```

Example replaceResult XML:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<imsx_POXEnvelopeRequest xmlns="http://www.imsglobal.org/services/ltiv1p1/xsd/imsoms_v1p0">
  <imsx_POXHeader>
    <imsx_POXRequestHeaderInfo>
      <imsx_version>V1.0</imsx_version>
      <imsx_messageIdentifier>unique-id</imsx_messageIdentifier>
    </imsx_POXRequestHeaderInfo>
  </imsx_POXHeader>
  <imsx_POXBody>
    <replaceResultRequest>
      <resultRecord>
        <sourcedGUID>
          <sourcedId>{lis_result_sourcedid}</sourcedId>
        </sourcedGUID>
        <result>
          <resultScore>
            <language>en</language>
            <textString>0.85</textString>
          </resultScore>
        </result>
      </resultRecord>
    </replaceResultRequest>
  </imsx_POXBody>
</imsx_POXEnvelopeRequest>
```

## Docker Networking Tips

When your LTI tool runs in a Docker container and needs to send grades back:

1. **Platform → Tool**: Use `host.docker.internal:8080` as the tool domain
2. **Tool → Platform**: The outcomes URL will be `http://host.docker.internal:8000/outcomes`

Or use the provided `docker-compose.yml` with a shared network.

## Demo Data

The platform auto-seeds with:

### Courses
- CS101 - Introduction to Python
- WEB201 - Web Development
- DS301 - Data Science Fundamentals

### Teachers
- Dr. Alice Smith (alice.smith@example.edu)
- Prof. Bob Johnson (bob.johnson@example.edu)

### Students
- Charlie Brown (charlie.brown@example.edu)
- Diana Prince (diana.prince@example.edu)
- Edward Norton (edward.norton@example.edu)
- Fiona Green (fiona.green@example.edu)

All users are enrolled in all courses.

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Dashboard |
| `/tool-servers` | GET | List tool servers |
| `/tool-servers/add` | POST | Add tool server |
| `/tools` | GET | List tools |
| `/tools/add` | POST | Add tool |
| `/courses` | GET | List courses |
| `/courses/{id}` | GET | View course |
| `/courses/{id}/tools/add` | POST | Add tool to course |
| `/launch/{course_tool_id}` | GET | Launch tool |
| `/launch-logs` | GET | List launch logs |
| `/launch-logs/{id}` | GET | View launch log |
| `/grades` | GET | List grades |
| `/grades/{id}` | GET | View grade |
| `/outcomes` | POST | Receive grade (LTI Outcomes) |

## Tech Stack

- **Backend**: FastAPI (Python)
- **Database**: SQLite3
- **Styling**: Embedded CSS (no external dependencies)
- **OAuth**: Custom HMAC-SHA1 implementation

## License

MIT
