"""Run locally: python server.py (no pip install needed)."""
from http.server import ThreadingHTTPServer
from api.index import handler
import os
if __name__ == '__main__':
    port = int(os.environ.get('PORT', '8000'))
    host = '0.0.0.0' if 'PORT' in os.environ else '127.0.0.1'
    print(f'Paupahan Manager listening on port {port}')
    ThreadingHTTPServer((host, port), handler).serve_forever()
