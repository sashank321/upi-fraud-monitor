FROM python:3.12-slim
WORKDIR /service
RUN --mount=type=cache,target=/root/.cache/pip pip install --timeout 90 --retries 5 torch==2.9.1 --index-url https://download.pytorch.org/whl/cpu
RUN --mount=type=cache,target=/root/.cache/pip pip install --timeout 90 --retries 5 laya==0.4.35 transformers==5.23.0
COPY laya_service/server.py /service/server.py
ENV HF_HOME=/models HF_HUB_DISABLE_XET=1 TOKENIZERS_PARALLELISM=false
EXPOSE 8090
CMD ["python", "-u", "/service/server.py"]
