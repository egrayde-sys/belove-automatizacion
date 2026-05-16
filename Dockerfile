FROM mcr.microsoft.com/playwright/python:v1.59.0-jammy
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
RUN echo "build $(date)" > /app/build_time.txt
CMD ["python", "-u", "main.py"]
