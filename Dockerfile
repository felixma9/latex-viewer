FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    texlive-latex-extra \
    texlive-fonts-recommended \
    texlive-fonts-extra \
    latexmk \
    python3 \
    && rm -rf /var/lib/apt/lists/*

# Print straight to `docker logs` instead of buffering.
ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY server.py .

EXPOSE 8080
CMD ["python3", "server.py"]
