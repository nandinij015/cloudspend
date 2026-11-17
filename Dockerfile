FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN python -m cloudspend.data
ENV PORT=8080
CMD exec uvicorn cloudspend.server:app --host 0.0.0.0 --port $PORT
