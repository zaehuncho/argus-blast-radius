# Team Argus - Blast Radius. Built on linux/amd64; runs with no network.
# Same base image the commits were labeled in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# The packages the commits were labeled with (pytest 9, pytest-timeout, duckdb, pandas...),
# so runner.py can run candidate tests exactly as the organizers did.
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --root-user-action=ignore -r /app/requirements.lock

# sqlglot's own source at the pinned commit: the selector reads the original
# files to compare code trees before and after each patch.
ARG SQLGLOT_COMMIT=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
RUN python -c "import io, tarfile, urllib.request; \
data = urllib.request.urlopen('https://github.com/tobymao/sqlglot/archive/${SQLGLOT_COMMIT}.tar.gz', timeout=120).read(); \
tarfile.open(fileobj=io.BytesIO(data)).extractall('/app', filter='data')" \
 && mv /app/sqlglot-${SQLGLOT_COMMIT} /app/sqlglot

# Coverage and data-file maps built from the pinned commit (tools/build_coverage.sh, tools/make_map.py).
# The map ships xz-compressed to keep the repository small; unpack it at build time.
COPY map/cov.sqlite.xz map/classes.sqlite.xz /app/map/
RUN python -c "import lzma; [open(f'/app/map/{n}.sqlite','wb').write(lzma.open(f'/app/map/{n}.sqlite.xz').read()) for n in ('cov', 'classes')]" \
 && rm /app/map/*.xz
COPY classmap.py /app/classmap.py
# Bytecode for sqlglot and its tests, so a confirming run only compiles what a patch changed.
COPY runner.py /app/runner.py
RUN python /app/runner.py --prepare /app/sqlglot
COPY selector.py /app/selector.py
RUN chmod -R a+rX /app
# PROBE: drops test_execute_tpcds from every selection to measure its share of the practice score.
ENV ARGUS_DROP_TESTS=tests/test_executor.py::TestExecutor::test_execute_tpcds
CMD ["python", "/app/selector.py"]
