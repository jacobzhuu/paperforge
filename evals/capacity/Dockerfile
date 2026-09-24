FROM paperforge-worker:capacity AS worker
COPY evals/capacity/worker.py /capacity/worker.py
ENV PYTHONPATH=/capacity

FROM nginx:1.27-alpine@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10 AS gateway
COPY evals/capacity/nginx.conf /etc/nginx/conf.d/default.conf
