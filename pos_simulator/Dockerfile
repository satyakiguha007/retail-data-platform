FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml ./
COPY pos_simulator/ ./pos_simulator/

RUN pip install --no-cache-dir -e .

# Default: generate 7 days for stores 1-5 (override via CMD or env)
ENV OUTPUT_DIR=/data/pos_rtlog
ENV STORES=1,2,3,4,5
ENV START_DATE=2024-01-01
ENV DAYS=7
ENV TPD=150
ENV FAULT_RATE=0.02
ENV TAX_MODE=IGTAX

VOLUME ["/data"]

CMD python -m pos_simulator.main generate \
    --stores "${STORES}" \
    --start  "${START_DATE}" \
    --days   "${DAYS}" \
    --tpd    "${TPD}" \
    --fault-rate "${FAULT_RATE}" \
    --tax-mode "${TAX_MODE}" \
    --output "${OUTPUT_DIR}"
