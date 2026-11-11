FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir numpy sentence-transformers fastapi "uvicorn[standard]" anthropic httpx
COPY main.py .
ENV PIPE=/data/pipeline IDEAS=/data/ideas HF_HOME=/data/hf PYTHONUNBUFFERED=1 TOKENIZERS_PARALLELISM=false
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
