FROM python:3.11-slim AS base

# System dependencies for alt-format generators:
#   - tesseract-ocr, ghostscript, qpdf, unpaper → ocrmypdf
#   - poppler-utils                             → pdf2image
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        ghostscript \
        poppler-utils \
        qpdf \
        unpaper \
        curl \
        default-jre-headless \
        wget \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# Install VeraPDF — the official PDF/UA-1 (WCAG-mapped) accessibility
# validator. We call it as a subprocess from
# ``connector.canvas.verapdf_audit`` to produce real per-criterion
# pass/fail data instead of the rough source-score heuristic. Installs
# under /opt/verapdf/verapdf with launcher script linked into /usr/local/bin.
# Use the verapdf.org "rel" channel — always the latest stable release.
# (Version-pinned URLs aren't published; the org doesn't ship GitHub
# releases either.) Override via build-arg if pinning is needed.
ARG VERAPDF_URL=https://software.verapdf.org/rel/verapdf-installer.zip
RUN cd /tmp \
 && wget -q "${VERAPDF_URL}" -O verapdf-installer.zip \
 && unzip -q verapdf-installer.zip \
 && cd verapdf-greenfield-* \
 && printf '%s\n' \
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>' \
        '<AutomatedInstallation langpack="eng">' \
        '  <com.izforge.izpack.panels.htmlhello.HTMLHelloPanel id="welcome"/>' \
        '  <com.izforge.izpack.panels.target.TargetPanel id="install_dir">' \
        '    <installpath>/opt/verapdf</installpath>' \
        '  </com.izforge.izpack.panels.target.TargetPanel>' \
        '  <com.izforge.izpack.panels.packs.PacksPanel id="sdk_pack_select">' \
        '    <pack index="0" name="veraPDF GUI" selected="true"/>' \
        '    <pack index="1" name="veraPDF Mac Validation Profiles" selected="false"/>' \
        '    <pack index="2" name="veraPDF Documentation" selected="false"/>' \
        '    <pack index="3" name="veraPDF Sample Plugins" selected="false"/>' \
        '  </com.izforge.izpack.panels.packs.PacksPanel>' \
        '  <com.izforge.izpack.panels.install.InstallPanel id="install"/>' \
        '  <com.izforge.izpack.panels.process.ProcessPanel id="process"/>' \
        '  <com.izforge.izpack.panels.finish.FinishPanel id="finish"/>' \
        '</AutomatedInstallation>' > auto-install.xml \
 && java -jar verapdf-izpack-installer-*.jar auto-install.xml \
 && ln -s /opt/verapdf/verapdf /usr/local/bin/verapdf \
 && cd / && rm -rf /tmp/verapdf-installer.zip /tmp/verapdf-greenfield-*

WORKDIR /app

# Copy package source first — hatch builds the wheel from connector/ so the
# directory has to exist before ``pip install .`` runs.
COPY pyproject.toml README.md ./
COPY connector/ ./connector/

RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir .

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "connector.main:app", "--host", "0.0.0.0", "--port", "8000"]
