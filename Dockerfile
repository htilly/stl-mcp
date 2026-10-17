FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY geometry.py server.py ./
ENV STL_DIR=/data PORT=8765
VOLUME /data
EXPOSE 8765
CMD ["python", "server.py"]
