# =============================================================================
# El Paso Seismic Pipeline — Multi-stage Docker build
#
# Stage 1: Build Conda environment from environment.yml
# Stage 2: Runtime image with pipeline code and pre-downloaded PhaseNet weights
#
# Build:
#   docker build -t elpaso-quake .
#
# Run pipeline (continuous mode):
#   docker run --rm -v $(pwd)/output:/app/output -v $(pwd)/logs:/app/logs elpaso-quake
#
# Run dashboard:
#   docker run --rm -p 8050:8050 -v $(pwd)/output:/app/output elpaso-quake \
#       python -m dashboard --port 8050 --browser
# =============================================================================

# ── Stage 1: Conda environment ──────────────────────────────────────────────

FROM continuumio/miniconda3:24.7.1-0 AS conda-build

COPY environment.yml /tmp/environment.yml

RUN conda env create -f /tmp/environment.yml -n elpaso && \
    conda clean -afy && \
    find /opt/conda/envs/elpaso -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null; true

# Pre-download PhaseNet weights so they are baked into the image.
# SeisBench caches models under ~/.seisbench/models/
SHELL ["conda", "run", "-n", "elpaso", "/bin/bash", "-c"]
RUN python -c "from seisbench.models import PhaseNet; PhaseNet.from_pretrained('original')"

# ── Stage 2: Runtime ────────────────────────────────────────────────────────

FROM continuumio/miniconda3:24.7.1-0 AS runtime

# Copy the pre-built Conda environment
COPY --from=conda-build /opt/conda/envs/elpaso /opt/conda/envs/elpaso

# Copy pre-downloaded SeisBench model cache
COPY --from=conda-build /root/.seisbench /root/.seisbench

# Make the elpaso env the default
ENV PATH="/opt/conda/envs/elpaso/bin:$PATH"
ENV CONDA_DEFAULT_ENV=elpaso

WORKDIR /app

# Copy pipeline source code
COPY lib/ lib/
COPY 1-ingestion/ 1-ingestion/
COPY 2-processing/ 2-processing/
COPY 3-detection/ 3-detection/
COPY 4-association/ 4-association/
COPY 5-catalog/ 5-catalog/
COPY dashboard/ dashboard/
COPY run_pipeline.py .
COPY stations.json .

# Create directories for bind-mount targets
RUN mkdir -p output logs

# Default: continuous pipeline mode
ENTRYPOINT ["python", "run_pipeline.py"]
CMD ["--continuous"]
