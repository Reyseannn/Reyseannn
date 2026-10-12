"""Python standard-library API, usable locally or as a Vercel Python Function.
Supabase stores records and private files; no data is written to serverless disk.
"""
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from http.cookies import SimpleCookie
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.ledger import LedgerError, apply_action, fresh_state, get_record

URL = os.environ.get('SUPABASE_URL', '').rstrip('/')
PUBLIC_KEY = os.environ.get('SUPABASE_PUBLISHABLE_KEY', '')
COOKIE_SECRET = os.environ.get('COOKIE_SECRET', '')
BUCKET = 'paupahan-documents'
MAX_BODY = 3 * 1024 * 1024 + 100000
CONFIGURED = bool(URL.startswith('https://') and PUBLIC_KEY and len(COOKIE_SECRET) >= 32)

class ApiError(Exception):
    def __init__(self, message, status=400):
        self.status = status
        super().__init__(message)

def supabase(path, method='GET', payload=None, token=None, raw=None, content_type='application/json'):
    key = PUBLIC_KEY
    headers = {'apikey': key, 'Content-Type': content_type}
    # Publishable API keys are not bearer JWTs. Service-role legacy key is.
    if token:
        headers['Authorization'] = 'Bearer ' + token
    data = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
    request = urllib.request.Request(URL + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = response.read()
            return json.loads(result) if result else None
    except urllib.error.HTTPError as error:
        # Provider payloads may contain private details. Return controlled messages.
        if error.code in (401, 403):
            raise ApiError('Sign-in failed or your session has expired.', 401)
        if error.code == 409:
            raise ApiError('Another record changed. Refresh and try again.', 409)
        raise ApiError('The database request failed. Check the project connection.', 502)
    except (urllib.error.URLError, TimeoutError):
        raise ApiError('The database is unavailable. Please try again later.', 503)

def encode_cookie(payload):
    data = base64.urlsafe_b64encode(json.dumps(payload, separators=(',', ':')).encode()).decode().rstrip('=')
    signature = hmac.new(COOKIE_SECRET.encode(), data.encode(), hashlib.sha256).hexdigest()
    return data + '.' + signature

def decode_cookie(value):
    try:
        data, signature = value.rsplit('.', 1)
        expected = hmac.new(COOKIE_SECRET.encode(), data.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        payload = json.loads(base64.urlsafe_b64decode(data + '=' * (-len(data) % 4)))
        if payload['exp'] < time.time():
            raise ValueError()
        return payload
    except Exception:
        raise ApiError('Please sign in again.', 401)

def load_state(token):
    rows = supabase('/rest/v1/rental_workspace?id=eq.1&select=version,data', token=token)
    if not rows:
        raise ApiError('Apply the database schema before using live records.', 503)
    s = rows[0]['data']
    s['version'] = rows[0]['version']
    return s

def commit_state(s, expected, token):
    result = supabase('/rest/v1/rpc/rental_save_workspace', 'POST', {'expected_version': expected, 'new_data': s}, token=token)
    if not result:
        raise ApiError('Someone else saved a change. Refresh before saving again.', 409)

def family_user(token):
    verified = supabase('/auth/v1/user', token=token)
    member = supabase('/rest/v1/rental_members?user_id=eq.' + urllib.parse.quote(verified['id']) + '&active=eq.true&select=name,role', token=token)
    if not member:
        raise ApiError('This account has not been approved for family access.', 403)
    return {'id': verified['id'], 'name': member[0]['name'], 'role': member[0]['role'], 'email': verified.get('email', '')}

class handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Do not write credentials, document paths, or payment request bodies to logs.
        pass

    def reply(self, payload, status=200, cookies=None):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        for cookie in cookies or []:
            self.send_header('Set-Cookie', cookie)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            raise ApiError('Invalid request size.')
        if length < 0 or length > MAX_BODY:
            raise ApiError('File exceeds the 3 MB limit.', 413)
        return self.rfile.read(length)

    def cookie(self, name, value, age=3600):
        secure = self.headers.get('X-Forwarded-Proto') == 'https' or os.environ.get('VERCEL') == '1' or os.environ.get('RENDER') == 'true'
        return f'{name}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={age}' + ('; Secure' if secure else '')

    def require_user(self):
        cookies = SimpleCookie(self.headers.get('Cookie', ''))
        if 'rental_session' not in cookies:
            raise ApiError('Please sign in.', 401)
        session = decode_cookie(cookies['rental_session'].value)
        return family_user(session['access']), session['access']

    def do_GET(self):
        self.dispatch('GET')

    def do_POST(self):
        self.dispatch('POST')

    def do_DELETE(self):
        self.dispatch('DELETE')

    def dispatch(self, method):
        try:
            route = urllib.parse.urlparse(self.path).path
            if method == 'GET' and not route.startswith('/api/'):
                return self.static(route)
            if route == '/api/config' and method == 'GET':
                return self.reply({'configured': CONFIGURED})
            if not CONFIGURED:
                raise ApiError('Live workspace is not configured. Use the demo for now.', 503)
            if method != 'GET':
                origin = self.headers.get('Origin')
                host = self.headers.get('X-Forwarded-Host') or self.headers.get('Host')
                if origin and urllib.parse.urlparse(origin).netloc != host:
                    raise ApiError('This request came from another website.', 403)
                if self.headers.get('Sec-Fetch-Site') == 'cross-site':
                    raise ApiError('Cross-site requests are not allowed.', 403)
            if route == '/api/signup' and method == 'POST':
                p = json.loads(self.body())
                email, password = p.get('email', ''), p.get('password', '')
                if not isinstance(email, str) or '@' not in email or not isinstance(password, str) or len(password) < 10 or len(password) > 200:
                    raise ApiError('Use a valid email and a password of at least 10 characters.')
                supabase('/auth/v1/signup', 'POST', {'email': email.lower().strip(), 'password': password})
                return self.reply({'message': 'Check your email to confirm your account, then return here and sign in. Only preapproved family accounts can open the ledger.'})
            if route == '/api/password-reset' and method == 'POST':
                p = json.loads(self.body())
                if not isinstance(p.get('email'), str) or '@' not in p['email']:
                    raise ApiError('Enter a valid email address.')
                supabase('/auth/v1/recover', 'POST', {'email': p['email'].strip()})
                return self.reply({'message': 'If an account matches, you will receive a recovery email.'})
            if route == '/api/login' and method == 'POST':
                p = json.loads(self.body())
                if not isinstance(p.get('email'), str) or not isinstance(p.get('password'), str):
                    raise ApiError('Enter an email and password.')
                auth = supabase('/auth/v1/token?grant_type=password', 'POST', {'email': p['email'], 'password': p['password']})
                user = family_user(auth['access_token'])
                expiration = min(auth.get('expires_in', 3600), 3600)
                cookie = self.cookie('rental_session', encode_cookie({'access': auth['access_token'], 'exp': time.time() + expiration}), expiration)
                return self.reply({'user': user, 'state': load_state(auth['access_token'])}, cookies=[cookie])
            if route == '/api/logout' and method == 'POST':
                return self.reply({'ok': True}, cookies=[self.cookie('rental_session', '', 0)])
            user, token = self.require_user()
            if route == '/api/state' and method == 'GET':
                return self.reply({'state': load_state(token), 'user': user})
            if route == '/api/members':
                if user['role'] != 'owner':
                    raise PermissionError('Only the owner can manage family access.')
                if method == 'GET':
                    rows = supabase('/rest/v1/rental_access_emails?select=email,name,role,active&order=name', token=token)
                    return self.reply({'members': rows})
                if method == 'POST':
                    p = json.loads(self.body())
                    email = p.get('email', '').strip().lower()
                    if '@' not in email or len(email) > 200 or p.get('role') not in ('cashier', 'viewer') or not isinstance(p.get('name'), str) or not 1 <= len(p['name'].strip()) <= 100:
                        raise ApiError('Enter a valid name, email, and family role.')
                    if email == user['email']:
                        raise ApiError('Your Owner account cannot be changed here.')
                    existing = supabase('/rest/v1/rental_access_emails?email=eq.' + urllib.parse.quote(email) + '&select=email', token=token)
                    record = {'email': email, 'name': p['name'].strip(), 'role': p['role'], 'active': p.get('active', True) is True}
                    if existing:
                        supabase('/rest/v1/rental_access_emails?email=eq.' + urllib.parse.quote(email), 'PATCH', record, token=token)
                    else:
                        supabase('/rest/v1/rental_access_emails', 'POST', record, token=token)
                    return self.reply({'ok': True})
            if route == '/api/action' and method == 'POST':
                p = json.loads(self.body())
                current = load_state(token)
                # Retried payments return the same receipt, even after a successful write.
                existing = next((x for x in current['payments'] if x['id'] == p.get('payload', {}).get('requestId')), None) if p.get('action') == 'payment' else None
                if existing:
                    return self.reply({'state': current, 'payment': existing})
                if p.get('version') != current['version']:
                    raise ApiError('Someone else updated the ledger. Refresh and try again.', 409)
                if p.get('action') in ('document_add', 'document_delete'):
                    raise ApiError('Use the document endpoint for file changes.')
                new_state, payment = apply_action(current, p.get('action'), p.get('payload'), user)
                if p.get('action') == 'payment':
                    result = supabase('/rest/v1/rpc/rental_record_payment', 'POST', {'expected_version': current['version'], 'payload': p['payload']}, token=token)
                    return self.reply(result)
                commit_state(new_state, current['version'], token)
                return self.reply({'state': new_state, 'payment': payment})
            if route == '/api/documents' and method == 'POST':
                return self.upload(user, token)
            if route.startswith('/api/documents/'):
                document_id = route.rsplit('/', 1)[1]
                current = load_state(token)
                match = next(((t, d) for t in current['tenants'] for d in t['documents'] if d['id'] == document_id), None)
                if not match:
                    raise ApiError('Document not found.', 404)
                tenant, document = match
                if method == 'GET':
                    result = supabase('/storage/v1/object/sign/' + BUCKET + '/' + document['path'], 'POST', {'expiresIn': 60}, token=token)
                    signed = result.get('signedURL') or result.get('signedUrl')
                    return self.reply({'url': URL + '/storage/v1' + signed})
                if method == 'DELETE':
                    if user['role'] != 'owner':
                        raise PermissionError('Only the owner can remove documents.')
                    new_state, _ = apply_action(current, 'document_delete', {'tenantId': tenant['id'], 'documentId': document['id']}, user)
                    commit_state(new_state, current['version'], token)
                    # Remove storage only after removing the authorized ledger reference.
                    supabase('/storage/v1/object/' + BUCKET, 'DELETE', {'prefixes': [document['path']]}, token=token)
                    return self.reply({'ok': True})
            raise ApiError('Endpoint not found.', 404)
        except ApiError as e:
            self.reply({'error': str(e)}, e.status)
        except PermissionError as e:
            self.reply({'error': str(e)}, 403)
        except (LedgerError, ValueError, KeyError, TypeError) as e:
            self.reply({'error': str(e) if isinstance(e, LedgerError) else 'Invalid request. Check the fields and try again.'}, 400)
        except Exception:
            self.reply({'error': 'The request could not be completed. Please try again.'}, 500)

    def upload(self, user, token):
        if user['role'] != 'owner':
            raise PermissionError('Only the owner can upload documents.')
        # email parser handles multipart safely without deprecated cgi module.
        from email.parser import BytesParser
        from email.policy import default
        content_type = self.headers.get('Content-Type', '')
        if not content_type.startswith('multipart/form-data;'):
            raise ApiError('Choose a PDF, JPG, or PNG file.')
        message = BytesParser(policy=default).parsebytes(('Content-Type: ' + content_type + '\r\nMIME-Version: 1.0\r\n\r\n').encode() + self.body())
        fields, file_data, filename = {}, None, ''
        for part in message.iter_parts():
            name = part.get_param('name', header='content-disposition')
            if name == 'file':
                file_data = part.get_payload(decode=True)
                filename = Path(part.get_filename() or '').name
            elif name == 'tenantId':
                fields[name] = part.get_payload(decode=True).decode()
        if not file_data or len(file_data) > 3 * 1024 * 1024:
            raise ApiError('Choose a file no larger than 3 MB.')
        if file_data.startswith(b'%PDF-'):
            extension, mime = '.pdf', 'application/pdf'
        elif file_data.startswith(b'\x89PNG\r\n\x1a\n'):
            extension, mime = '.png', 'image/png'
        elif file_data.startswith(b'\xff\xd8\xff'):
            extension, mime = '.jpg', 'image/jpeg'
        else:
            raise ApiError('Only PDF, JPG, and PNG documents are accepted.')
        current = load_state(token)
        tenant = get_record(current['tenants'], fields.get('tenantId'))
        doc_id = secrets.token_hex(16)
        path = tenant['id'] + '/' + doc_id + extension
        supabase('/storage/v1/object/' + BUCKET + '/' + path, 'POST', token=token, raw=file_data, content_type=mime)
        try:
            new_state, _ = apply_action(current, 'document_add', {'tenantId': tenant['id'], 'id': doc_id, 'name': filename[:150], 'path': path}, user)
            commit_state(new_state, current['version'], token)
        except Exception:
            supabase('/storage/v1/object/' + BUCKET, 'DELETE', {'prefixes': [path]}, token=token)
            raise
        return self.reply({'ok': True})

    def static(self, route):
        public = Path(__file__).resolve().parents[1] / 'dist'
        name = route.lstrip('/') or 'index.html'
        file = (public / name).resolve()
        if file.parent != public or not file.is_file() or file.suffix not in ('.html', '.js', '.css'):
            return self.reply({'error': 'Not found.'}, 404)
        data = file.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', {'.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8', '.js': 'text/javascript; charset=utf-8'}[file.suffix])
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)
