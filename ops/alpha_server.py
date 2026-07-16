from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


SITE_ROOT = "/opt/alpha-origin-placeholder/site"


class DocsHandler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if self.path.endswith((".css", ".js", ".png", ".svg")):
            self.send_header("Cache-Control", "public, max-age=3600")
        else:
            self.send_header("Cache-Control", "public, max-age=120")
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:
        print(f"alpha-docs {self.address_string()} {format % args}", flush=True)


if __name__ == "__main__":
    handler = partial(DocsHandler, directory=SITE_ROOT)
    ThreadingHTTPServer(("127.0.0.1", 8765), handler).serve_forever()
