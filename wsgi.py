"""
WSGI adapter for WashTrack SimpleHTTPRequestHandler on PythonAnywhere.
"""
from io import BytesIO
from pathlib import Path
import mimetypes
import server
from server import Handler, ROOT

def application(environ, start_response):
    path = environ.get('PATH_INFO', '/')
    query = environ.get('QUERY_STRING', '')
    full_path = f"{path}?{query}" if query else path
    method = environ.get('REQUEST_METHOD', 'GET')
    
    # Check if requesting root or static asset (e.g. index.html, logo, etc.)
    clean_path = path.lstrip('/')
    if not clean_path:
        clean_path = 'index.html'

    file_path = ROOT / clean_path

    # If it's a file request that exists (HTML, PNG, CSS, JS), serve it directly
    if not path.startswith('/api/') and file_path.is_file() and file_path.exists():
        mime_type, _ = mimetypes.guess_type(str(file_path))
        mime_type = mime_type or 'application/octet-stream'
        with open(file_path, 'rb') as f:
            content = f.read()
        start_response('200 OK', [
            ('Content-Type', mime_type),
            ('Content-Length', str(len(content))),
            ('Access-Control-Allow-Origin', '*'),
        ])
        return [content]

    # Otherwise route to WashTrack API Handler
    handler = Handler.__new__(Handler)
    handler.directory = str(ROOT)
    handler.path = full_path
    handler.command = method
    handler.headers = {}
    
    # Read request body
    content_length = int(environ.get('CONTENT_LENGTH') or 0)
    body_data = environ['wsgi.input'].read(content_length) if content_length > 0 else b''
    handler.rfile = BytesIO(body_data)

    response_headers = []
    response_status = ["200 OK"]
    out_body = BytesIO()

    def send_response(code, message=None):
        status_map = {
            200: "200 OK",
            204: "204 No Content",
            400: "400 Bad Request",
            401: "401 Unauthorized",
            403: "403 Forbidden",
            404: "404 Not Found",
            500: "500 Internal Server Error"
        }
        response_status[0] = status_map.get(code, f"{code} OK")

    def send_header(k, v):
        response_headers.append((k, v))

    def end_headers():
        pass

    handler.send_response = send_response
    handler.send_header = send_header
    handler.end_headers = end_headers
    handler.wfile = out_body

    # Populate HTTP headers
    for k, v in environ.items():
        if k.startswith('HTTP_'):
            h = k[5:].replace('_', '-').title()
            handler.headers[h] = v
        elif k in ('CONTENT_TYPE', 'CONTENT_LENGTH'):
            h = k.replace('_', '-').title()
            handler.headers[h] = v

    try:
        if method == "GET":
            handler.do_GET()
        elif method == "POST":
            handler.do_POST()
        elif method == "PATCH":
            handler.do_PATCH()
        elif method == "OPTIONS":
            handler.do_OPTIONS()
    except Exception as e:
        response_status[0] = "500 Internal Server Error"
        out_body.write(f'{{"error": "{str(e)}"}}'.encode('utf-8'))

    headers = [("Access-Control-Allow-Origin", "*")] + response_headers
    start_response(response_status[0], headers)
    return [out_body.getvalue()]
