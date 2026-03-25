FROM node:18-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl python3 python3-requests tmux asciinema && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
# COPY starter/ /app/
RUN find /app -maxdepth 1 -type f -exec chmod +x {} +
CMD ["bash"]
