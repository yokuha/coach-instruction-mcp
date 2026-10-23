# Reverse Proxy (Beispiele)

Der Server lauscht nur auf 127.0.0.1:8765. TLS macht der Reverse Proxy, der auf dem VPS
vermutlich schon fuer iicu-mcp.domain.tld laeuft. Es kommt nur ein weiterer Host dazu.

## Caddy

    coach-mcp.domain.tld {
        reverse_proxy 127.0.0.1:8765
    }

## nginx (nach certbot --nginx -d coach-mcp.domain.tld)

    server {
        server_name coach-mcp.domain.tld;
        location / {
            proxy_pass http://127.0.0.1:8765;
            proxy_set_header Host $host;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_read_timeout 120s;
        }
        # TLS-Teile von certbot
    }

DNS: A-Record (und AAAA falls IPv6) fuer `coach-mcp` bei Strato auf die VPS-IP.
Den Namen der Subdomain kannst du frei waehlen; er muss nur zu COACH_ALLOWED_HOSTS passen.
