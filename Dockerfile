FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
COPY web ./web
COPY fonts ./fonts
ENV STL_DIR=/data PORT=8765
# Baked in by CI (docker/build-push-action build-args). server.py prints it in the
# startup marker, and the deploy job requires it to match the commit.
ARG GIT_SHA=""
ENV GIT_SHA=$GIT_SHA
VOLUME /data
EXPOSE 8765
CMD ["python", "server.py"]
