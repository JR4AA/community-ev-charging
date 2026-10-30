FROM python:3.12-alpine
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY evcharge ./evcharge
RUN adduser -D -H -u 10001 app && mkdir /data && chown app /data
USER app
ENV DB_PATH=/data/ev.sqlite3 PYTHONUNBUFFERED=1
EXPOSE 8080 9000
CMD ["uvicorn", "--factory", "evcharge.web:create_app", "--host", "0.0.0.0", "--port", "8080", "--no-server-header"]
