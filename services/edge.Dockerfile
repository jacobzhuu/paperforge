FROM nginx:1.27-alpine@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10 AS gateway
COPY infra/capacity-nginx.conf /etc/nginx/conf.d/default.conf

FROM prom/prometheus:v3.2.1@sha256:6927e0919a144aa7616fd0137d4816816d42f6b816de3af269ab065250859a62 AS monitoring
COPY infra/monitoring/prometheus.yml /etc/prometheus/prometheus.yml
COPY infra/monitoring/alerts.yml /etc/prometheus/alerts.yml
