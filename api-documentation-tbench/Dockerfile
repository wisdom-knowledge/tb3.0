FROM node:18-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends python3 python3-requests tmux asciinema && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
CMD ["bash"]
