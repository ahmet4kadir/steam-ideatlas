FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir numpy scipy "scikit-learn>=1.3" umap-learn sentence-transformers openai
COPY run.py .
ENV STEAM_DB=/data/steam.db OUT=/data/pipeline WEB=/data/web HF_HOME=/data/hf PYTHONUNBUFFERED=1 TOKENIZERS_PARALLELISM=false
CMD ["python", "run.py"]
