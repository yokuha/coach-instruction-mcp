# Reverse proxy (examples)

The server only listens on 127.0.0.1:8765. TLS is handled by the reverse proxy; if one already
runs on your server, you only add another host.

## Caddy

    coach-mcp.example.com {
        reverse_proxy 127.0.0.1:8765
    }

## nginx (after `certbot --nginx -d coach-mcp.example.com`)

    server {
        server_name coach-mcp.example.com;
        location / {
            proxy_pass http://127.0.0.1:8765;
            proxy_set_header Host $host;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_read_timeout 120s;
        }
        # TLS parts added by certbot
    }

## DNS

Create an A record (and AAAA for IPv6) for `coach-mcp` at your DNS provider pointing to the
server's IP. The subdomain name is up to you; it just has to match `COACH_ALLOWED_HOSTS`.
