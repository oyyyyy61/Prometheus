FROM python:3.12-slim

RUN pip install --no-cache-dir kubernetes prometheus_client
COPY recovery/k8s_controller.py /controller/k8s_controller.py

CMD ["python", "/controller/k8s_controller.py"]
