FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    texlive-latex-extra \
    texlive-fonts-recommended \
    texlive-fonts-extra \
    latexmk \
    python3 \
    && rm -rf /var/lib/apt/lists/*

# Separate layer so adding it does not invalidate the TeX Live layer above.
# pdftoppm and pdfinfo render PDF pages for the mobile shell at /m.
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# Print straight to `docker logs` instead of buffering.
ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY server.py .

EXPOSE 8080
CMD ["python3", "server.py"]
