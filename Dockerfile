FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home booking && mkdir /data && chown booking:booking /data
COPY app.py accounts.py scheduling.py storage.py calendar.js app.css index.html ./
USER booking
ENV BOOKING_DATA_DIR=/data COOKIE_SECURE=true ALLOW_DEMO=false
EXPOSE 8000
CMD ["python","-m","uvicorn","app:app","--host","0.0.0.0","--port","8000"]
