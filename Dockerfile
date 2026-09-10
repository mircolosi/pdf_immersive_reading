FROM python:3.12-slim

WORKDIR /app

# Layer-cache dependencies separately from source so code edits don't
# invalidate the (slow) pip install layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 0.0.0.0 inside the container so `docker run -p` port-mapping can reach the
# server — README's "127.0.0.1, local-only" stance is about the host's own
# network interface, not this container-internal bind; docker still won't
# expose the port unless you publish it with -p/-e PORT.
ENV HOST=0.0.0.0
ENV PORT=7860
EXPOSE 7860

ENTRYPOINT ["python", "app.py"]
