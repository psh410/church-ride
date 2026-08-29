# Dockerfile for the church-rides Flask app, deployed to Cloud Run.

# Start from a slim, official Python 3.11 image - small footprint,
# fast to pull, and Debian-based underneath.
FROM python:3.11-slim

# All subsequent commands (COPY, RUN, CMD) run relative to this
# directory inside the container.
WORKDIR /app

# Copy just the dependency manifest first, before the rest of the
# code. Docker caches this layer, so as long as requirements.txt
# doesn't change, later code changes won't force a slow reinstall
# of every dependency on each build.
COPY requirements.txt .

# Install dependencies. --no-cache-dir keeps the image smaller by not
# persisting pip's download cache. --break-system-packages is needed
# because this base image's Python is managed by Debian (PEP 668
# "externally managed environment") - safe here since the container
# only ever runs this one app, so there's no system Python to protect.
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt

# Now copy the rest of the application code (everything not excluded
# by .dockerignore) into the image.
COPY . .

# Documents that the app listens on port 8080 - Cloud Run expects
# this and reads it from the PORT env var at runtime (see cloud_app.py).
EXPOSE 8080

# Start the Flask app.
CMD ["python", "cloud_app.py"]
