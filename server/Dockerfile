# Build from this directory. One image supports the API, database initialization,
# and the standalone worker. No environment files or credentials enter the image.

# --------------------------------------------------------------------------- #
# LibreDWG 0.14 command-line tools for native DWG (app/cad/dwg.py).
# Built from the pinned official GNU tarball, verified by sha256, on the same
# base image as the runtime so the binaries match its glibc. LibreDWG is
# GPL-3.0-or-later: the server only runs these tools as separate processes and
# never loads libredwg into Python. The original source archive, license and
# build instructions are retained in the runtime image alongside the tools.
# --------------------------------------------------------------------------- #
FROM python:3.12-slim-bookworm AS libredwg-build

ARG LIBREDWG_VERSION=0.14
ARG LIBREDWG_SHA256=cb6ee0b078c6d9e0f09d66f1feac33ba6342df88ae544e9f9335fab475218351
ARG LIBREDWG_BUILD_JOBS=2

RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
 && apt-get -o Acquire::Retries=5 update \
 && apt-get -o Acquire::Retries=5 install -y --no-install-recommends ca-certificates curl gcc libc6-dev make pkg-config \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN curl --retry 5 --retry-delay 2 --retry-all-errors -fsSLo libredwg.tar.gz "https://ftp.gnu.org/gnu/libredwg/libredwg-${LIBREDWG_VERSION}.tar.gz" \
 && echo "${LIBREDWG_SHA256}  libredwg.tar.gz" | sha256sum -c - \
 && tar -xzf libredwg.tar.gz \
 && cd "libredwg-${LIBREDWG_VERSION}" \
 && ./configure --prefix=/opt/libredwg --disable-bindings --disable-static --disable-docs \
        LDFLAGS="-Wl,-rpath,/opt/libredwg/lib" \
 && make -j"${LIBREDWG_BUILD_JOBS}" \
 && make install-strip \
 && out=/opt/libredwg-runtime \
 && mkdir -p "$out/bin" "$out/lib" "$out/share/licenses/libredwg" \
 && cp /opt/libredwg/bin/dwg2dxf /opt/libredwg/bin/dxf2dwg /opt/libredwg/bin/dwgread /opt/libredwg/bin/dwgwrite "$out/bin/" \
 && cp -P /opt/libredwg/lib/libredwg.so* "$out/lib/" \
 && cp COPYING "$out/share/licenses/libredwg/" \
 && cp /src/libredwg.tar.gz "$out/share/licenses/libredwg/libredwg-${LIBREDWG_VERSION}.tar.gz" \
 && printf 'LibreDWG %s (GPL-3.0-or-later)\nsource: https://ftp.gnu.org/gnu/libredwg/libredwg-%s.tar.gz\nsha256: %s\nconfigure: --prefix=/opt/libredwg --disable-bindings --disable-static --disable-docs LDFLAGS=-Wl,-rpath,/opt/libredwg/lib\nbuild: make install-strip; copied: bin/dwg2dxf bin/dxf2dwg bin/dwgread bin/dwgwrite lib/libredwg.so*\n' \
        "${LIBREDWG_VERSION}" "${LIBREDWG_VERSION}" "${LIBREDWG_SHA256}" > "$out/share/licenses/libredwg/SOURCE"

# --------------------------------------------------------------------------- #
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_HTTP_TIMEOUT=120 \
    UV_HTTP_RETRIES=5 \
    PATH="/app/.venv/bin:/opt/libredwg/bin:$PATH"

# OCCT (via cadquery-ocp) needs a few shared libraries that slim images lack.
# Fonts let ezdxf measure text (DXF/DWG extents; the slim image has none):
# fonts-dejavu-core (~3 MB) is ezdxf's fallback font and covers Latin and the
# drawing symbols (Ø × ↧ ⌴); fonts-wqy-microhei (~5 MB, one TTC) is a small
# CJK font covering GB2312/GBK for Chinese drawings, where Noto CJK would add
# about 100 MB.
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
 && apt-get -o Acquire::Retries=5 update \
 && apt-get -o Acquire::Retries=5 install -y --no-install-recommends ca-certificates libgl1 libglib2.0-0 libxrender1 libxext6 \
        fonts-dejavu-core fonts-wqy-microhei \
 && rm -rf /var/lib/apt/lists/*

# LibreDWG tools, shared library, license, build record, and original source archive.
COPY --from=libredwg-build /opt/libredwg-runtime /opt/libredwg
RUN dwg2dxf --version && dxf2dwg --version && dwgread --version && dwgwrite --version

COPY --from=ghcr.io/astral-sh/uv:0.10 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
# Local deployments use filesystem storage; the optional GCS extra is unnecessary.
RUN uv sync --frozen --no-dev --no-install-project --no-cache

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

# Named volumes inherit this ownership on first initialization. The API and
# worker use the same UID and blob directory. Cache files live in the user's home.
RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app \
 && mkdir -p /app/var/blobs \
 && chown -R app:app /app/var

USER app
# Warm geometry and font caches under the runtime user rather than root.
RUN python -c "import build123d; from ezdxf.fonts import fonts; fonts.build_system_font_cache()"

EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
