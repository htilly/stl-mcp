FROM python:3.12-slim
RUN useradd --system --uid 10001 --user-group --home-dir /app --no-create-home app
WORKDIR /app
# OpenSCAD for create_from_openscad (sandboxed, see openscad.py) and BOSL2, pinned to a commit.
# The libraries dir is read-only for the server; OPENSCADPATH points only there.
ARG BOSL2_COMMIT=f1493a8425f51bd0585dd6d89aea962561100d32
ADD https://github.com/BelfrySCAD/BOSL2/archive/${BOSL2_COMMIT}.tar.gz /tmp/bosl2.tar.gz
RUN apt-get update \
 && apt-get install -y --no-install-recommends openscad \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /opt/openscad-libs/BOSL2 \
 && tar -xzf /tmp/bosl2.tar.gz -C /opt/openscad-libs/BOSL2 --strip-components=1 \
 && rm /tmp/bosl2.tar.gz \
 && chmod -R a-w /opt/openscad-libs
ENV OPENSCAD_LIBS=/opt/openscad-libs
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
COPY migrations ./migrations
COPY web ./web
COPY fonts ./fonts
COPY docker-entrypoint.sh /usr/local/bin/
ENV STL_DIR=/data PORT=8765
# Baked in by CI (docker/build-push-action build-args). server.py prints it in the
# startup marker, and the deploy job requires it to match the commit.
ARG GIT_SHA=""
ENV GIT_SHA=$GIT_SHA
VOLUME /data
EXPOSE 8765
# Starts as root only to fix the ownership of /data, then runs as "app" (docker-entrypoint.sh).
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "server.py"]
