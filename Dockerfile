# Team Argus - Blast Radius. Built on linux/amd64; runs with no network.
# Same base image the commits were labeled in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# sqlglot's own source at the pinned commit: the selector reads the original
# files to compare code trees before and after each patch.
ARG SQLGLOT_COMMIT=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
RUN python -c "import io, tarfile, urllib.request; \
data = urllib.request.urlopen('https://github.com/tobymao/sqlglot/archive/${SQLGLOT_COMMIT}.tar.gz', timeout=120).read(); \
tarfile.open(fileobj=io.BytesIO(data)).extractall('/app', filter='data')" \
 && mv /app/sqlglot-${SQLGLOT_COMMIT} /app/sqlglot

# Coverage and data-file maps built from the pinned commit (tools/build_coverage.sh, tools/make_map.py).
# The map ships xz-compressed to keep the repository small; unpack it at build time.
COPY map/cov.sqlite.xz /app/map/cov.sqlite.xz
RUN python -c "import lzma; open('/app/map/cov.sqlite','wb').write(lzma.open('/app/map/cov.sqlite.xz').read())" \
 && rm /app/map/cov.sqlite.xz
COPY selector.py /app/selector.py
RUN chmod -R a+rX /app
CMD ["python", "/app/selector.py"]
