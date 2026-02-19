FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN mkdir -p /app/dao_monitor
COPY . /app/dao_monitor/

CMD ["python", "-m", "dao_monitor.main", "continuous", "dao_monitor/config.yaml"]
