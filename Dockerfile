FROM python:3.12-slim
RUN useradd --system --uid 10001 --user-group --home-dir /app --no-create-home app
WORKDIR /app
# OpenSCAD for create_from_openscad (sandboxed, see openscad.py) and its libraries, each pinned to
# a commit. Every directory in /opt/openscad-libs is a library the code can include (openscad.py);
# docs, tests and examples are left out (they only define names the source check would block).
# The libraries dir is read-only for the server; OPENSCADPATH points only there.
ARG BOSL2_COMMIT=f1493a8425f51bd0585dd6d89aea962561100d32
ARG NOPSCADLIB_COMMIT=00ec2898c0a2cdebd446f0175671b633d5ae7d5f
ARG ROUND_ANYTHING_COMMIT=061fef7c429628808e847696bb345a9b0ec6e279
ARG GRIDFINITY_COMMIT=910e22d8607fd7f5f51ad5e5cbc5287a76810bfd
ARG MCAD_COMMIT=bd0a7ba3f042bfbced5ca1894b236cea08904e26
ADD https://github.com/BelfrySCAD/BOSL2/archive/${BOSL2_COMMIT}.tar.gz /tmp/scad/BOSL2.tar.gz
ADD https://github.com/nophead/NopSCADlib/archive/${NOPSCADLIB_COMMIT}.tar.gz /tmp/scad/NopSCADlib.tar.gz
ADD https://github.com/Irev-Dev/Round-Anything/archive/${ROUND_ANYTHING_COMMIT}.tar.gz /tmp/scad/Round-Anything.tar.gz
ADD https://github.com/kennetek/gridfinity-rebuilt-openscad/archive/${GRIDFINITY_COMMIT}.tar.gz /tmp/scad/gridfinity-rebuilt-openscad.tar.gz
ADD https://github.com/openscad/MCAD/archive/${MCAD_COMMIT}.tar.gz /tmp/scad/MCAD.tar.gz
RUN apt-get update \
 && apt-get install -y --no-install-recommends openscad libgl1 libexpat1 \
 && rm -rf /var/lib/apt/lists/* \
 && cd /tmp/scad \
 && for f in *.tar.gz; do \
      lib="/opt/openscad-libs/${f%.tar.gz}"; \
      mkdir -p "$lib" && tar -xzf "$f" -C "$lib" --strip-components=1; \
    done \
 && cd /opt/openscad-libs \
 && rm -rf NopSCADlib/docs NopSCADlib/examples NopSCADlib/gallery NopSCADlib/scripts \
           NopSCADlib/tests NopSCADlib/libtest.* \
           gridfinity-rebuilt-openscad/docs gridfinity-rebuilt-openscad/images \
           gridfinity-rebuilt-openscad/tests \
           Round-Anything/examples Round-Anything/images Round-Anything/roundAnythingExamples.scad \
           MCAD/*.py \
 && rm -rf /tmp/scad \
 && chmod -R a-w /opt/openscad-libs
ENV OPENSCAD_LIBS=/opt/openscad-libs
# build123d (OpenCascade, needs libgl1 and libexpat1 above) is only imported by the sandbox service, which runs
# this same image with `python sandboxd.py` (see docker-compose.yml and sandboxd.py).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
COPY migrations ./migrations
COPY web ./web
COPY fonts ./fonts
COPY reference ./reference
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
